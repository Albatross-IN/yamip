from odoo.tests import tagged
from odoo.addons.open_whatsapp_connector.tests.common import OwaTestCase


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestNotifyBackfill(OwaTestCase):

    def _make_channel(self, number):
        return self.env['discuss.channel'].sudo().create({
            'name': number, 'channel_type': 'whatsapp',
            'whatsapp_number': number.lstrip('+'),
            'owa_account_id': self.account.id,
        })

    def test_add_notify_user_backfills_existing_channels(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'open_whatsapp_connector.chat_visibility', 'shared')
        ch = self._make_channel('+15551230000')
        agent = self.env['res.users'].create({
            'name': 'Agent Z', 'login': 'agentz@test.com'})
        self.assertNotIn(agent.partner_id, ch.channel_member_ids.partner_id)
        self.account.write({'notify_user_ids': [(4, agent.id)]})
        self.assertIn(agent.partner_id, ch.channel_member_ids.partner_id)

    def test_backfill_skipped_in_own_agent_mode(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'open_whatsapp_connector.chat_visibility', 'own_agent')
        ch = self._make_channel('+15551230001')
        agent = self.env['res.users'].create({
            'name': 'Agent Y', 'login': 'agenty@test.com'})
        self.account.write({'notify_user_ids': [(4, agent.id)]})
        self.assertNotIn(agent.partner_id, ch.channel_member_ids.partner_id)

    def test_inbound_on_old_channel_tops_up_members(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'open_whatsapp_connector.chat_visibility', 'shared')
        ch = self._make_channel('+15551239999')
        ch.channel_member_ids = [(5, 0, 0)]  # clear members
        notify_partners = self.account.notify_user_ids.partner_id
        # Drive the REAL inbound get-or-create entry point: this is the exact
        # call owa.account._handle_inbound makes for every inbound message.
        channel = self.env['discuss.channel'].sudo()._get_owa_whatsapp_channel(
            '15551239999', self.account, create_if_not_found=True)
        # The pre-existing channel must be the one resolved (no new create).
        self.assertEqual(channel, ch)
        self.assertTrue(set(notify_partners.ids) <=
                        set(ch.channel_member_ids.partner_id.ids))
