"""Regression tests for the 2026-07 bug-fix batch:

  * FIX1 — the "Auto-create Contacts" setting can be turned OFF and stays off.
  * FIX4 — inbound contact de-duplication matches phone AND mobile, and matches
    a differently-formatted existing contact (via phone_sanitized).
  * FIX2 — an account's Allowed Group members become channel members (so
    "shared" visibility genuinely shows chats to everyone in the pool).
  * FIX3 — deleting an account reassigns its conversations to the Default
    account (else the first connected one), and refuses when none exists.
"""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import OwaTestCase

AUTO = 'open_whatsapp_connector.auto_create_contacts'
CHATVIS = 'open_whatsapp_connector.chat_visibility'
DEFACC = 'open_whatsapp_connector.default_account_id'


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestOwaBugfixBatch(OwaTestCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.Partner = self.env['res.partner']
        self.Settings = self.env['res.config.settings']
        self.Channel = self.env['discuss.channel'].sudo()

    def _channel_on(self, account, number):
        return self.Channel.create({
            'name': number, 'channel_type': 'whatsapp',
            'whatsapp_number': number.lstrip('+'),
            'owa_account_id': account.id,
        })

    def _internal_user(self, login):
        # v19: res.groups members are `user_ids` (v18/v17 use `users`).
        user = self.env['res.users'].create({'name': login, 'login': login})
        self.env.ref('base.group_user').sudo().write({'user_ids': [(4, user.id)]})
        return user

    def _connected_account(self, name):
        return self.Account.create({
            'name': name, 'sidecar_url': 'http://localhost:9999',
            'session_state': 'connected', 'phone_number': '+15559999999',
            'webhook_secret': 'fixture-secret-2',
            'notify_user_ids': [(6, 0, [self.env.ref('base.user_admin').id])],
        })

    # == FIX1 — auto-create toggle persistence ============================
    def test_autocreate_off_persists_and_reads_back(self):
        self.Settings.create({'owa_auto_create_contacts': False}).set_values()
        # OFF is stored as an explicit string (not deleted) ...
        self.assertEqual(self.ICP.get_param(AUTO), 'false')
        # ... and a freshly-opened settings screen reflects OFF.
        self.assertFalse(self.Settings.create({}).owa_auto_create_contacts)

    def test_autocreate_back_on_persists(self):
        self.Settings.create({'owa_auto_create_contacts': False}).set_values()
        self.Settings.create({'owa_auto_create_contacts': True}).set_values()
        self.assertEqual(self.ICP.get_param(AUTO), 'true')
        self.assertTrue(self.Settings.create({}).owa_auto_create_contacts)

    def test_autocreate_off_blocks_creation(self):
        self.ICP.set_param(AUTO, 'false')
        before = self.Partner.search_count([])
        got = self.Partner._find_or_create_from_wa_number('919812390001', 'Ghost')
        self.assertFalse(got)
        self.assertEqual(self.Partner.search_count([]), before)

    # == FIX4 — contact de-duplication ====================================
    def test_dedupe_matches_phone_field(self):
        p = self.Partner.create({'name': 'Phone Guy', 'phone': '+919812345671'})
        self.assertEqual(
            self.Partner._find_or_create_from_wa_number('919812345671'), p)

    def test_dedupe_matches_mobile_field(self):
        # The number lives in Mobile, not Phone — the classic duplicate cause.
        # (res.partner.mobile was dropped in Odoo 19; skip there.)
        if 'mobile' not in self.Partner._fields:
            self.skipTest("res.partner has no 'mobile' field on this version")
        p = self.Partner.create({'name': 'Mobile Gal', 'mobile': '+919812345672'})
        self.assertEqual(
            self.Partner._find_or_create_from_wa_number('919812345672'), p)

    def test_dedupe_matches_differently_formatted(self):
        # Stored with spaces; phone_sanitized normalizes it to E.164.
        p = self.Partner.create({'name': 'Spacey', 'phone': '+91 98123 45673'})
        self.assertEqual(
            self.Partner._find_or_create_from_wa_number('919812345673'), p)

    def test_dedupe_no_false_positive(self):
        p = self.Partner.create({'name': 'Other', 'phone': '+919812340000'})
        got = self.Partner._find_or_create_from_wa_number('919899999999')
        self.assertNotEqual(got, p)

    # == FIX2 — Allowed Group access pool =================================
    def test_allowed_group_member_added_on_new_inbound(self):
        self.ICP.set_param(CHATVIS, 'shared')
        group = self.env['res.groups'].create({'name': 'WA Agents T1'})
        agent = self._internal_user('wa_grp_agent1')
        group.sudo().write({'user_ids': [(4, agent.id)]})
        self.account.allowed_group_id = group.id
        ch = self.Channel._get_owa_whatsapp_channel(
            '919812300001', self.account, create_if_not_found=True)
        self.assertIn(agent.partner_id, ch.channel_member_ids.partner_id)

    def test_allowed_group_backfills_existing_on_write(self):
        self.ICP.set_param(CHATVIS, 'shared')
        ch = self._channel_on(self.account, '+919812300002')
        agent = self._internal_user('wa_grp_agent2')
        group = self.env['res.groups'].create({'name': 'WA Agents T2'})
        group.sudo().write({'user_ids': [(4, agent.id)]})
        self.assertNotIn(agent.partner_id, ch.channel_member_ids.partner_id)
        # Writing allowed_group_id triggers the member re-sync.
        self.account.write({'allowed_group_id': group.id})
        self.assertIn(agent.partner_id, ch.channel_member_ids.partner_id)

    def test_resync_button_applies_group(self):
        self.ICP.set_param(CHATVIS, 'shared')
        group = self.env['res.groups'].create({'name': 'WA Agents T3'})
        agent = self._internal_user('wa_grp_agent3')
        group.sudo().write({'user_ids': [(4, agent.id)]})
        # Set the group first, then create a channel with only the account, then
        # resync — proves the button (not just the write hook) backfills.
        self.account.allowed_group_id = group.id
        ch = self._channel_on(self.account, '+919812300003')
        ch.channel_member_ids = [(5, 0, 0)]
        self.account.action_owa_resync_members()
        self.assertIn(agent.partner_id, ch.channel_member_ids.partner_id)

    # == FIX3 — delete reassignment =======================================
    def test_delete_reassigns_to_default_account(self):
        acct2 = self._connected_account('reassign-target')
        ch = self._channel_on(self.account, '+919812300010')
        self.ICP.set_param(DEFACC, str(acct2.id))
        with self.mock_sidecar():
            self.account.unlink()
        self.assertEqual(ch.owa_account_id, acct2)

    def test_delete_reassigns_to_first_connected_when_no_default(self):
        acct2 = self._connected_account('other-connected')
        ch = self._channel_on(self.account, '+919812300011')
        self.ICP.set_param(DEFACC, '')
        with self.mock_sidecar():
            self.account.unlink()
        self.assertEqual(ch.owa_account_id, acct2)

    def test_delete_last_account_with_convos_raises(self):
        ch = self._channel_on(self.account, '+919812300012')
        self.assertTrue(ch)
        with self.mock_sidecar(), self.assertRaises(UserError):
            self.account.unlink()

    def test_delete_reassigns_pending_messages_without_channels(self):
        # Broadcast-only account: queued message, no discuss channel.
        acct2 = self._connected_account('pending-target')
        msg = self.Message.create({
            'mobile_number': '+919812300030', 'message_type': 'outbound',
            'state': 'outgoing', 'wa_account_id': self.account.id})
        self.ICP.set_param(DEFACC, str(acct2.id))
        with self.mock_sidecar():
            self.account.unlink()
        self.assertEqual(msg.wa_account_id, acct2)

    def test_delete_archives_cross_account_duplicate(self):
        # Same customer number has a conversation on BOTH accounts. Deleting one
        # must NOT crash on the (account, number) unique index — the redundant
        # channel is archived; the survivor's conversation stays live.
        acct2 = self._connected_account('dup-target')
        num = '+919812300031'
        ch_a = self._channel_on(self.account, num)
        ch_c = self._channel_on(acct2, num)
        self.ICP.set_param(DEFACC, str(acct2.id))
        with self.mock_sidecar():
            self.account.unlink()
        self.assertFalse(ch_a.active)          # redundant one archived
        self.assertTrue(ch_c.exists() and ch_c.active)
        self.assertEqual(ch_c.owa_account_id, acct2)

    def test_switch_to_colliding_account_raises_clean_error(self):
        # Switching a conversation to an account that already has one with the
        # same number surfaces a clean UserError, not a raw DB IntegrityError.
        acct2 = self._connected_account('switch-target')
        num = '+919812300032'
        ch_a = self._channel_on(self.account, num)
        self._channel_on(acct2, num)
        with self.assertRaises(UserError):
            ch_a.owa_account_id = acct2.id

    def test_autocreate_legacy_capital_true_reads_on(self):
        # A DB upgraded from the old default-True config_parameter Boolean stored
        # 'True' (capital) — the settings screen must still read it as ON.
        self.ICP.set_param(AUTO, 'True')
        self.assertTrue(self.Settings.create({}).owa_auto_create_contacts)

    def test_is_default_checkbox_syncs_param(self):
        # The checkbox is the UI for the default_account_id parameter that
        # delete-reassign and the reply fallback read.
        acct2 = self._connected_account('second-default')
        self.account.is_default = True
        self.assertEqual(self.ICP.get_param(DEFACC), str(self.account.id))
        self.assertTrue(self.account.is_default)
        self.assertFalse(acct2.is_default)
        # Moving the default to another account updates the param...
        acct2.is_default = True
        self.assertEqual(self.ICP.get_param(DEFACC), str(acct2.id))
        self.assertFalse(self.account.is_default)
        # ...and unchecking clears it.
        acct2.is_default = False
        self.assertFalse(int(self.ICP.get_param(DEFACC) or 0))

    # == 2026-07-02 v19 bug-hunt round =====================================
    def test_wa_phone_format_rejects_jids(self):
        # JIDs are not phone numbers: phonenumbers would parse the leading
        # digits and silently drop the @-suffix, re-routing group traffic.
        from odoo.addons.open_whatsapp_connector.tools.phone_validation import (
            wa_phone_format,
        )
        self.assertFalse(wa_phone_format(self.env, '120363426701982141@g.us'))
        self.assertFalse(wa_phone_format(self.env, '917888433643@s.whatsapp.net'))
        self.assertTrue(wa_phone_format(self.env, '+919812345678'))

    def test_import_wizard_avatars_only_no_crash(self):
        # ir.cron.numbercall was removed in Odoo 18+ — the wizard's post-import
        # cron heal must not read it (AttributeError before the fix).
        wiz = self.env['owa.import.wizard'].create({
            'wa_account_id': self.account.id,
            'import_groups': False, 'import_contacts': False,
            'import_avatars': True,
        })
        with self.mock_sidecar():
            wiz.action_import()

    def test_import_avatars_step_without_mobile_field(self):
        # The avatar-fetch step must not read res.partner.mobile (gone in v19).
        partner = self.Partner.create({'name': 'Avatarless',
                                       'phone': '+919812300050'})
        ch = self._channel_on(self.account, '+919812300050')
        ch.whatsapp_partner_id = partner.id
        job = self.env['owa.import.job'].create({'account_id': self.account.id})
        with self.mock_sidecar():
            job._import_avatars(self.account)  # AttributeError before the fix

    def test_compose_channel_without_partner_autocreate_off(self):
        # With Auto-create Contacts OFF, composing to an unknown number must
        # still create the channel — an empty {} member row used to violate
        # mail's partner-or-guest CHECK constraint and crash the compose.
        self.ICP.set_param(AUTO, 'false')
        ch = self.account._get_or_create_channel('919812300060')
        self.assertTrue(ch)
        self.assertFalse(ch.whatsapp_partner_id)
        # every member row is a real partner (no empty member created)
        self.assertTrue(all(ch.channel_member_ids.mapped('partner_id')))

    def test_explicit_import_bypasses_autocreate_off(self):
        # "Import contacts" is explicit user intent — the passive auto-create
        # toggle being OFF must not silently import zero contacts.
        self.ICP.set_param(AUTO, 'false')
        Partner = self.Partner.with_context(owa_force_contact_create=True)
        p = Partner._find_or_create_from_wa_number('919812300070', 'Imported Guy')
        self.assertTrue(p)
        self.assertEqual(p.name, 'Imported Guy')

    def test_dedupe_claim_rolls_back_with_savepoint(self):
        # The webhook wraps each inbound in a savepoint: when processing
        # fails AFTER the dedupe claim, the claim must roll back with it so
        # the sidecar's retry is NOT rejected as a duplicate (lost message).
        Dedupe = self.env['owa.inbound.dedupe']
        try:
            with self.env.cr.savepoint():
                self.assertTrue(Dedupe.claim('edge-uid-1', self.account.id))
                raise UserError('simulated processing failure')
        except UserError:
            pass
        # Retry after rollback must be able to claim (and thus reprocess).
        self.assertTrue(Dedupe.claim('edge-uid-1', self.account.id))
        # And a genuine duplicate is still rejected.
        self.assertFalse(Dedupe.claim('edge-uid-1', self.account.id))

    def test_stop_in_group_does_not_blacklist(self):
        # One member typing STOP in a group must not opt out the whole group.
        Blacklist = self.env['owa.blacklist'].sudo()
        before = Blacklist.search_count([])
        with self.mock_sidecar():
            self.account._handle_inbound({
                'from_number': '120363000000000001@g.us',
                'sender_name': 'Some Group', 'msg_uid': 'stopmsg1',
                'type': 'text', 'body': 'STOP',
            })
        self.assertEqual(Blacklist.search_count([]), before)
