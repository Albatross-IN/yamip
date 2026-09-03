"""Access-control feature tests (18.0.36.0.0):

  * Part 1 — account connection management (connect/disconnect/logout) is
    restricted to WhatsApp Administrators: a plain agent gets AccessError.
  * Part 2 — the optional "WhatsApp Conversation Visibility = own_agent"
    toggle isolates WhatsApp channels per assigned agent WITHOUT touching
    non-WhatsApp Discuss, admins, or unassigned conversations.
"""
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import OwaTestCase

RULE_XMLID = 'open_whatsapp_connector.owa_chat_own_agent_rule'
MEMBER_RULE_XMLID = 'open_whatsapp_connector.owa_chat_member_own_agent_rule'
ADMIN_RULE_XMLID = 'open_whatsapp_connector.owa_chat_admin_whatsapp_rule'


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestChatVisibility(OwaTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Channel = cls.env['discuss.channel']
        cls.admin_user = cls.env.ref('base.user_admin')
        group_user = cls.env.ref('base.group_user')

        def _mk_agent(login):
            return cls.env['res.users'].create({
                'name': login,
                'login': login,
                'email': '%s@example.com' % login,
                'group_ids': [(6, 0, [group_user.id])],  # v19: group_ids, not groups_id
            })

        cls.agent_a = _mk_agent('wa_agent_a')
        cls.agent_b = _mk_agent('wa_agent_b')
        # Sanity: plain agents must NOT be WhatsApp Administrators.
        assert not cls.agent_a.has_group('open_whatsapp_connector.group_owa_admin')
        assert not cls.agent_b.has_group('open_whatsapp_connector.group_owa_admin')

        members = [
            (0, 0, {'partner_id': cls.agent_a.partner_id.id}),
            (0, 0, {'partner_id': cls.agent_b.partner_id.id}),
        ]
        cls.ch_a = cls.Channel.create({
            'name': 'WA A', 'channel_type': 'whatsapp',
            'whatsapp_number': '+15551110001', 'owa_account_id': cls.account.id,
            'assignee_id': cls.agent_a.id, 'channel_member_ids': members,
        })
        cls.ch_b = cls.Channel.create({
            'name': 'WA B', 'channel_type': 'whatsapp',
            'whatsapp_number': '+15551110002', 'owa_account_id': cls.account.id,
            'assignee_id': cls.agent_b.id, 'channel_member_ids': list(members),
        })
        cls.ch_unassigned = cls.Channel.create({
            'name': 'WA U', 'channel_type': 'whatsapp',
            'whatsapp_number': '+15551110003', 'owa_account_id': cls.account.id,
            'channel_member_ids': list(members),  # assignee_id left empty
        })
        # A non-WhatsApp group chat both agents belong to (regression guard).
        cls.ch_group = cls.Channel.create({
            'name': 'Team Group', 'channel_type': 'group',
            'channel_member_ids': list(members),
        })

    def setUp(self):
        super().setUp()
        # Baseline every test at 'shared' (the shipped default). The parameter
        # is global DB state, so we must not assume what a previous run / manual
        # configuration left it at. Restricted tests call _set_mode('own_agent').
        self._set_mode('shared')

    # -- helpers ------------------------------------------------------------
    def _visible(self, user, channel):
        return bool(self.Channel.with_user(user).search([('id', '=', channel.id)]))

    def _set_mode(self, mode):
        """The rules are ALWAYS active; behaviour is driven by the
        chat_visibility parameter their domain reads. Set it and clear the
        ir.rule domain cache so it takes effect immediately (as set_values does)."""
        self.env['ir.config_parameter'].sudo().set_param(
            'open_whatsapp_connector.chat_visibility', mode)
        self.env.registry.clear_cache()

    def _member_rows(self, user, channel):
        return self.env['discuss.channel.member'].with_user(user).search(
            [('channel_id', '=', channel.id)])

    # == Part 1 — connection management gate ===============================
    def test_nonadmin_cannot_disconnect(self):
        with self.mock_sidecar(), self.assertRaises(AccessError):
            self.account.with_user(self.agent_a).button_disconnect()

    def test_nonadmin_cannot_logout(self):
        with self.mock_sidecar(), self.assertRaises(AccessError):
            self.account.with_user(self.agent_a).button_logout()

    def test_admin_can_disconnect(self):
        with self.mock_sidecar():
            self.account.with_user(self.admin_user).button_disconnect()
        self.assertEqual(self.account.session_state, 'disconnected')

    def test_admin_can_logout(self):
        with self.mock_sidecar():
            self.account.with_user(self.admin_user).button_logout()
        self.assertEqual(self.account.session_state, 'logged_out')

    def test_nonadmin_cannot_stop_sidecar(self):
        # button_stop_sidecar does NO owa.account write, so only the explicit
        # _ensure_owa_admin guard protects the shared sidecar from being killed.
        with self.assertRaises(AccessError):
            self.account.with_user(self.agent_a).button_stop_sidecar()

    def test_nonadmin_cannot_run_diagnostics(self):
        # action_run_diagnostics never writes owa.account either — guard-only.
        with self.assertRaises(AccessError):
            self.account.with_user(self.agent_a).action_run_diagnostics()

    def test_nonadmin_cannot_start_dashboard_sidecars(self):
        # owa.dashboard is an AbstractModel (no ACL) that sudo()s internally.
        with self.assertRaises(AccessError):
            self.env['owa.dashboard'].with_user(
                self.agent_a).action_start_sidecars()

    def test_nonadmin_cannot_view_dashboard(self):
        # The dashboard client action is reachable by URL, so the data loader
        # is guarded too (aggregates every account's traffic + health).
        with self.assertRaises(AccessError):
            self.env['owa.dashboard'].with_user(
                self.agent_a).get_dashboard_data()

    # == Part 2 — shared mode (default, no regression) =====================
    def test_rules_ship_active_default_shared(self):
        # The rules are always active; the DEFAULT parameter is 'shared', so
        # their domain imposes no restriction (verified by the shared tests).
        for xmlid in (RULE_XMLID, MEMBER_RULE_XMLID, ADMIN_RULE_XMLID):
            self.assertTrue(self.env.ref(xmlid).active, xmlid)
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param(
                'open_whatsapp_connector.chat_visibility', 'shared'),
            'shared')

    def test_shared_both_agents_see_channel(self):
        # Default behaviour: every member sees every WhatsApp conversation.
        self.assertTrue(self._visible(self.agent_a, self.ch_b))
        self.assertTrue(self._visible(self.agent_b, self.ch_a))

    # == Part 2 — restricted mode ==========================================
    def test_restricted_agent_sees_own(self):
        self._set_mode('own_agent')
        self.assertTrue(self._visible(self.agent_a, self.ch_a))

    def test_restricted_agent_not_see_other(self):
        # THE core assertion: agent A no longer sees agent B's conversation.
        self._set_mode('own_agent')
        self.assertFalse(self._visible(self.agent_a, self.ch_b))
        self.assertFalse(self._visible(self.agent_b, self.ch_a))

    def test_restricted_admin_sees_all(self):
        self._set_mode('own_agent')
        self.assertTrue(self._visible(self.admin_user, self.ch_a))
        self.assertTrue(self._visible(self.admin_user, self.ch_b))

    def test_restricted_whatsapp_manager_sees_all(self):
        # A WhatsApp Administrator who is NOT a Settings (group_system) admin and
        # is NOT a member of the channels must still see every WhatsApp
        # conversation — provided by the dedicated admin group rule (rule 3).
        manager = self.env['res.users'].create({
            'name': 'wa_manager', 'login': 'wa_manager',
            'email': 'wa_manager@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('open_whatsapp_connector.group_owa_admin').id,
            ])],
        })
        self.assertFalse(manager.has_group('base.group_system'))
        self._set_mode('own_agent')
        self.assertTrue(self._visible(manager, self.ch_a))
        self.assertTrue(self._visible(manager, self.ch_b))

    def test_restricted_unassigned_fail_open(self):
        self._set_mode('own_agent')
        self.assertTrue(self._visible(self.agent_a, self.ch_unassigned))
        self.assertTrue(self._visible(self.agent_b, self.ch_unassigned))

    def test_restricted_non_whatsapp_unaffected(self):
        # REGRESSION GUARD: a group chat both agents belong to stays visible.
        self._set_mode('own_agent')
        self.assertTrue(self._visible(self.agent_a, self.ch_group))
        self.assertTrue(self._visible(self.agent_b, self.ch_group))

    def test_round_trip_restores_visibility(self):
        self._set_mode('own_agent')
        self.assertFalse(self._visible(self.agent_a, self.ch_b))
        self._set_mode('shared')
        self.assertTrue(self._visible(self.agent_a, self.ch_b))

    def test_restricted_member_rows_isolated(self):
        # Metadata leak guard: agent B cannot enumerate the members of agent A's
        # hidden conversation, but still sees members of their own + the group.
        self._set_mode('own_agent')
        self.assertFalse(self._member_rows(self.agent_b, self.ch_a))
        self.assertTrue(self._member_rows(self.agent_b, self.ch_b))
        self.assertTrue(self._member_rows(self.agent_b, self.ch_group))

    # == Part 2 — settings wiring =========================================
    def test_set_values_drives_visibility(self):
        # Exercise the real set_values() wiring: it writes the parameter and
        # clears the rule cache, and the always-active rules read that param.
        self.env['res.config.settings'].create(
            {'owa_chat_visibility': 'own_agent'}).set_values()
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param(
                'open_whatsapp_connector.chat_visibility'), 'own_agent')
        self.assertFalse(self._visible(self.agent_a, self.ch_b))
        self.env['res.config.settings'].create(
            {'owa_chat_visibility': 'shared'}).set_values()
        self.assertTrue(self._visible(self.agent_a, self.ch_b))

    # == Part 2 — assignee integrity ======================================
    def test_restricted_agent_can_claim_but_not_reassign(self):
        self._set_mode('own_agent')
        # An agent may CLAIM the unassigned conversation for themselves...
        self.ch_unassigned.with_user(self.agent_a).write(
            {'assignee_id': self.agent_a.id})
        self.assertEqual(self.ch_unassigned.assignee_id, self.agent_a)
        # ...but may not reassign it to someone else or clear it.
        with self.assertRaises(AccessError):
            self.ch_a.with_user(self.agent_a).write(
                {'assignee_id': self.agent_b.id})
        with self.assertRaises(AccessError):
            self.ch_a.with_user(self.agent_a).write({'assignee_id': False})
        # Admins can reassign freely.
        self.ch_a.with_user(self.admin_user).write(
            {'assignee_id': self.agent_b.id})
        self.assertEqual(self.ch_a.assignee_id, self.agent_b)

    # == Part 2 — auto-stamp ==============================================
    def test_autostamp_only_in_restricted_mode(self):
        with self.mock_sidecar():
            # Shared (default): _get_whatsapp_channel leaves assignee empty.
            self.env['ir.config_parameter'].sudo().set_param(
                'open_whatsapp_connector.chat_visibility', 'shared')
            ch_shared = self.Channel._get_owa_whatsapp_channel(
                '+15551119001', self.account, create_if_not_found=True)
            self.assertFalse(ch_shared.assignee_id)
            # Restricted: a new inbound conversation is auto-assigned an owner.
            self.env['ir.config_parameter'].sudo().set_param(
                'open_whatsapp_connector.chat_visibility', 'own_agent')
            ch_owned = self.Channel._get_owa_whatsapp_channel(
                '+15551119002', self.account, create_if_not_found=True)
        self.assertTrue(ch_owned.assignee_id)
        self.assertIn(ch_owned.assignee_id, self.account.notify_user_ids)
