"""Drip campaigns: one batch of res.partner per cron tick, gated by the
shared wa_campaign_done flag, self-terminating when the audience runs out."""
from odoo.exceptions import UserError, ValidationError

from .common import OwaTestCase


class TestCampaignDrip(OwaTestCase):

    DRIP_REF = 'DRIPTEST'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cron = cls.env.ref(
            'open_whatsapp_connector.ir_cron_owa_campaign_drip')
        cls.partners = cls.env['res.partner'].create([
            {'name': 'Drip One', 'ref': cls.DRIP_REF, 'phone': '+12025550101'},
            {'name': 'Drip Two', 'ref': cls.DRIP_REF, 'phone': '+12025550102'},
            {'name': 'Drip Three', 'ref': cls.DRIP_REF, 'phone': '+12025550103'},
        ])
        # No phone -> must never be picked, and must not stall the run.
        cls.no_phone = cls.env['res.partner'].create(
            {'name': 'Drip NoPhone', 'ref': cls.DRIP_REF})
        cls.template = cls.env['owa.quick.reply'].create({
            'name': 'Drip template',
            'body': 'Hello {{partner_name}}, this is a drip.',
        })

    def _campaign(self, **overrides):
        vals = {
            'name': 'Drip campaign',
            'send_mode': 'drip',
            'wa_account_id': self.account.id,
            'quick_reply_id': self.template.id,
            'partner_domain': "[('ref', '=', '%s')]" % self.DRIP_REF,
            'drip_batch_size': 1,
        }
        vals.update(overrides)
        return self.env['owa.campaign'].create(vals)

    def _drip_messages(self, campaign):
        return self.env['owa.message'].search([('campaign_id', '=', campaign.id)])

    # ── launch ────────────────────────────────────────────────────────

    def test_launch_activates_cron_and_clears_stale_flags(self):
        self.partners[0].wa_campaign_done = True  # left over from an aborted run
        self.cron.active = False
        campaign = self._campaign()
        campaign.action_launch()
        self.assertEqual(campaign.state, 'sending')
        self.assertFalse(campaign.drip_finished_at)
        self.assertTrue(self.cron.active)
        self.assertFalse(any(self.partners.mapped('wa_campaign_done')))
        # The phone-less contact is excluded from the audience.
        self.assertEqual(campaign.drip_target_count, 3)

    def test_launch_requires_a_message(self):
        campaign = self._campaign(quick_reply_id=False, body='   ')
        with self.assertRaises(UserError):
            campaign.action_launch()

    def test_launch_requires_matching_contacts(self):
        campaign = self._campaign(partner_domain="[('ref', '=', 'NOBODY')]")
        with self.assertRaises(UserError):
            campaign.action_launch()

    def test_only_one_drip_at_a_time(self):
        first = self._campaign()
        first.action_launch()
        second = self._campaign(name='Second drip')
        with self.assertRaises(UserError):
            second.action_launch()

    def test_bulk_campaign_still_requires_a_contact_list(self):
        with self.assertRaises(ValidationError):
            self.env['owa.campaign'].create({
                'name': 'Bulk without list',
                'send_mode': 'bulk',
                'wa_account_id': self.account.id,
                'body': 'hi',
            })

    # ── pacing ────────────────────────────────────────────────────────

    def test_one_contact_per_tick_and_flag_set_on_dispatch(self):
        campaign = self._campaign()
        campaign.action_launch()

        self.env['owa.campaign']._cron_drip_send()
        messages = self._drip_messages(campaign)
        self.assertEqual(len(messages), 1)
        recipient = messages.whatsapp_partner_id
        self.assertTrue(recipient.wa_campaign_done)
        self.assertEqual(recipient.wa_campaign_last_id, campaign)
        self.assertEqual(messages.state, 'outgoing')
        # Body rendered per contact, no raw placeholders left.
        self.assertIn(recipient.name, messages.body)
        self.assertNotIn('{{', messages.body)

        self.env['owa.campaign']._cron_drip_send()
        self.assertEqual(len(self._drip_messages(campaign)), 2)
        self.assertEqual(campaign.drip_remaining_count, 1)

    def test_batch_size_is_respected(self):
        campaign = self._campaign(drip_batch_size=2)
        campaign.action_launch()
        self.env['owa.campaign']._cron_drip_send()
        self.assertEqual(len(self._drip_messages(campaign)), 2)

    def test_disconnected_account_consumes_nothing(self):
        campaign = self._campaign()
        campaign.action_launch()
        self.account.session_state = 'disconnected'
        self.env['owa.campaign']._cron_drip_send()
        self.assertFalse(self._drip_messages(campaign))
        self.assertFalse(any(self.partners.mapped('wa_campaign_done')))
        # Still running — a skipped tick must not end the campaign.
        self.assertFalse(campaign.drip_finished_at)
        self.assertTrue(self.cron.active)

    def test_blacklisted_contact_is_flagged_but_not_messaged(self):
        self.env['owa.blacklist'].create({
            'phone': self.partners[0].phone, 'reason': 'test'})
        campaign = self._campaign()
        campaign.action_launch()
        self.env['owa.campaign']._cron_drip_send()
        # The blacklisted contact is consumed (or it would be re-picked
        # forever) but doesn't eat the tick's budget: one real send still went.
        self.assertEqual(len(self._drip_messages(campaign)), 1)
        self.assertNotEqual(
            self._drip_messages(campaign).whatsapp_partner_id, self.partners[0])
        self.assertTrue(self.partners[0].wa_campaign_done)

    # ── completion ────────────────────────────────────────────────────

    def _run_to_completion(self, campaign, max_ticks=10):
        for _i in range(max_ticks):
            self.env['owa.campaign']._cron_drip_send()
            if campaign.drip_finished_at:
                return
        self.fail("drip campaign did not finish within %d ticks" % max_ticks)

    def test_last_tick_resets_flags_and_deactivates_cron(self):
        campaign = self._campaign()
        campaign.action_launch()
        self._run_to_completion(campaign)

        self.assertEqual(len(self._drip_messages(campaign)), 3)
        self.assertFalse(any(self.partners.mapped('wa_campaign_done')))
        self.assertFalse(self.no_phone.wa_campaign_done)
        self.assertFalse(self.cron.active)
        # The audit trail survives the flag reset.
        self.assertEqual(
            self.partners.mapped('wa_campaign_last_id'), campaign)

    def test_state_cron_does_not_finish_a_drip_mid_run(self):
        campaign = self._campaign()
        campaign.action_launch()
        self.env['owa.campaign']._cron_drip_send()
        # Drain the queue the way a real send would, leaving no pending message
        # while contacts remain unprocessed.
        self._drip_messages(campaign).write({'state': 'sent'})
        self.env['owa.campaign']._cron_update_campaign_state()
        self.assertEqual(campaign.state, 'sending')

        self._run_to_completion(campaign)
        self._drip_messages(campaign).write({'state': 'sent'})
        self.env['owa.campaign']._cron_update_campaign_state()
        self.assertEqual(campaign.state, 'sent')

    def test_stop_drip_releases_flags_and_keeps_queue(self):
        campaign = self._campaign()
        campaign.action_launch()
        self.env['owa.campaign']._cron_drip_send()
        campaign.action_stop_drip()
        self.assertTrue(campaign.drip_finished_at)
        self.assertFalse(self.cron.active)
        self.assertFalse(any(self.partners.mapped('wa_campaign_done')))
        self.assertEqual(len(self._drip_messages(campaign)), 1)
        self.assertEqual(self._drip_messages(campaign).state, 'outgoing')

    def test_cancel_releases_flags_and_voids_queue(self):
        campaign = self._campaign()
        campaign.action_launch()
        self.env['owa.campaign']._cron_drip_send()
        campaign.action_cancel()
        self.assertEqual(campaign.state, 'cancelled')
        self.assertFalse(any(self.partners.mapped('wa_campaign_done')))
        self.assertFalse(self.cron.active)
        self.assertEqual(self._drip_messages(campaign).state, 'cancel')

    # ── free-text placeholders ────────────────────────────────────────

    def test_free_text_body_resolves_contact_fields(self):
        campaign = self._campaign(
            quick_reply_id=False,
            body="Hi {{name}} from {{country_id.name}}{{nope}}!")
        self.partners.write({'country_id': self.env.ref('base.in').id})
        rendered = campaign._render_drip_body(self.partners[0])
        self.assertEqual(rendered, "Hi Drip One from India!")

    def test_free_text_body_cannot_reach_non_fields(self):
        campaign = self._campaign(quick_reply_id=False, body="[{{env.cr}}]")
        self.assertEqual(campaign._render_drip_body(self.partners[0]), "[]")
