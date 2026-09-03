"""Account self-service + per-account visibility (18.0.41.0.0)."""
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import OwaTestCase


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestAccountVisibility(OwaTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Account = cls.env['owa.account']
        cls.admin_user = cls.env.ref('base.user_admin')
        group_user = cls.env.ref('base.group_user')
        Team = cls.env['crm.team']

        def _mk_user(login):
            return cls.env['res.users'].create({
                'name': login, 'login': login, 'email': '%s@ex.com' % login,
                'group_ids': [(6, 0, [group_user.id])],
            })
        cls.leader = _mk_user('wa_leader')
        cls.sales_a = _mk_user('wa_sales_a')
        cls.sales_b = _mk_user('wa_sales_b')
        cls.team = Team.create({
            'name': 'WA Sales Team', 'user_id': cls.leader.id,
            'member_ids': [(6, 0, [cls.sales_a.id, cls.sales_b.id])],
        })
        cls.other_team = Team.create({'name': 'WA Other', 'user_id': cls.sales_b.id})

    def setUp(self):
        super().setUp()
        self._set_mode('shared')

    def _set_mode(self, mode):
        self.env['ir.config_parameter'].sudo().set_param(
            'open_whatsapp_connector.account_visibility', mode)
        acl = self.env.ref(
            'open_whatsapp_connector.access_owa_account_user_manage',
            raise_if_not_found=False)
        if acl:
            acl.sudo().active = (mode == 'own_team')
        self.env.registry.clear_cache()

    def _mk_account(self, name, owner, **kw):
        vals = {'name': name, 'session_state': 'connected',
                'phone_number': '+1555000%s' % owner.id,
                'webhook_secret': 's-%s' % owner.id,
                'user_id': owner.id,
                'notify_user_ids': [(6, 0, [owner.id])]}
        vals.update(kw)
        return self.Account.sudo().create(vals)

    def test_admin_create_auto_approves(self):
        acc = self.Account.create({
            'name': 'admin-acct', 'webhook_secret': 'x',
            'notify_user_ids': [(6, 0, [self.admin_user.id])]})
        self.assertEqual(acc.approval_state, 'approved')
        self.assertTrue(acc.user_id)

    def test_fields_exist(self):
        self.assertIn('approval_state', self.Account._fields)
        self.assertIn('user_id', self.Account._fields)
        self.assertIn('team_id', self.Account._fields)

    def _can_see(self, user, account):
        return bool(self.Account.with_user(user).search([('id', '=', account.id)]))

    def test_shared_everyone_sees(self):
        acc = self._mk_account('a-own', self.sales_a)
        self.assertTrue(self._can_see(self.sales_b, acc))   # peer sees in shared
        self.assertTrue(self._can_see(self.leader, acc))

    def test_own_team_owner_leader_admin_only(self):
        acc = self._mk_account('a-own', self.sales_a, team_id=self.team.id)
        self._set_mode('own_team')
        self.assertTrue(self._can_see(self.sales_a, acc))    # owner
        self.assertTrue(self._can_see(self.leader, acc))     # team leader
        self.assertTrue(self._can_see(self.admin_user, acc)) # admin
        self.assertFalse(self._can_see(self.sales_b, acc))   # peer hidden

    def test_setting_writes_param_and_toggles_acl(self):
        acl = self.env.ref('open_whatsapp_connector.access_owa_account_user_manage')
        self.env['res.config.settings'].create(
            {'owa_account_visibility': 'own_team'}).set_values()
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.account_visibility'), 'own_team')
        self.assertTrue(acl.active)
        self.env['res.config.settings'].create(
            {'owa_account_visibility': 'shared'}).set_values()
        self.assertFalse(acl.active)

    def test_self_service_create_only_in_own_team(self):
        # Exercises the ACL directly: create AS the non-admin user (NOT the
        # sudo _mk_account helper).
        def _create_as(user):
            return self.Account.with_user(user).create({
                'name': 'mine', 'webhook_secret': 'ss',
                'phone_number': '+15550001', 'session_state': 'connected',
                'notify_user_ids': [(6, 0, [user.id])]})
        # shared mode: non-admin cannot create (manage ACL inactive)
        with self.assertRaises(AccessError):
            _create_as(self.sales_a)
        # own_team: non-admin creates, forced owner=self + pending
        self._set_mode('own_team')
        acc = _create_as(self.sales_a)
        self.assertEqual(acc.user_id, self.sales_a)
        self.assertEqual(acc.approval_state, 'pending')

    def test_leader_can_create_for_team_member(self):
        self._set_mode('own_team')
        acc = self.Account.with_user(self.leader).create({
            'name': 'for-a', 'webhook_secret': 'y',
            'user_id': self.sales_a.id, 'team_id': self.team.id,
            'notify_user_ids': [(6, 0, [self.sales_a.id])]})
        # leader owns the team => create() keeps the chosen owner
        self.assertEqual(acc.user_id, self.sales_a)

    def test_manage_guard(self):
        acc = self._mk_account('a-own', self.sales_a, team_id=self.team.id)
        # shared mode: non-admin cannot manage (lockdown intact)
        with self.mock_sidecar(), self.assertRaises(AccessError):
            acc.with_user(self.sales_a).button_refresh_status()
        # own_team: owner manages own
        self._set_mode('own_team')
        with self.mock_sidecar():
            acc.with_user(self.sales_a).button_refresh_status()
        # own_team: a peer cannot manage someone else's
        with self.mock_sidecar(), self.assertRaises(AccessError):
            acc.with_user(self.sales_b).button_refresh_status()
        # team leader can manage team account
        with self.mock_sidecar():
            acc.with_user(self.leader).button_refresh_status()
        # sidecar start stays admin-only even in own_team
        with self.assertRaises(AccessError):
            acc.with_user(self.sales_a).button_stop_sidecar()

    def test_approve_reject_admin_only(self):
        self._set_mode('own_team')
        acc = self._mk_account('pend', self.sales_a, team_id=self.team.id,
                               approval_state='pending')
        # non-admin cannot approve
        with self.assertRaises(AccessError):
            acc.with_user(self.sales_a).action_approve()
        # admin approves
        acc.with_user(self.admin_user).action_approve()
        self.assertEqual(acc.approval_state, 'approved')
        self.assertEqual(acc.approved_user_id, self.admin_user)
        self.assertTrue(acc.approved_date)
        # admin rejects
        acc.with_user(self.admin_user).action_reject()
        self.assertEqual(acc.approval_state, 'rejected')

    def test_approval_gate_blocks_then_allows(self):
        self._set_mode('own_team')
        acc = self._mk_account('pend', self.sales_a, team_id=self.team.id,
                               approval_state='pending')
        self.assertEqual(acc.approval_state, 'pending')
        # outbound blocked while pending
        msg = self.env['owa.message'].sudo().create({
            'mobile_number': '+15551230000', 'message_type': 'outbound',
            'body': '<p>hi</p>', 'wa_account_id': acc.id, 'state': 'outgoing'})
        with self.mock_sidecar():
            msg._send_single_message()
        self.assertNotEqual(msg.state, 'sent')
        self.assertTrue(acc._account_messaging_blocked())
        # inbound dropped while pending
        before = self.env['discuss.channel'].sudo().search_count(
            [('channel_type', '=', 'whatsapp')])
        acc._handle_inbound({'from_number': '15551230000', 'msg_uid': 'm1',
                             'type': 'text', 'body': 'hello'})
        after = self.env['discuss.channel'].sudo().search_count(
            [('channel_type', '=', 'whatsapp')])
        self.assertEqual(before, after)  # no channel created
        # approve -> gate lifts
        acc.action_approve()
        self.assertFalse(acc._account_messaging_blocked())

    def test_no_gate_in_shared_mode(self):
        acc = self.Account.create({'name': 's', 'webhook_secret': 'z',
            'notify_user_ids': [(6, 0, [self.admin_user.id])],
            'approval_state': 'pending'})
        self.assertFalse(acc._account_messaging_blocked())

    def test_status_broadcast_blocked_when_pending(self):
        """Status broadcasts must be refused for a pending (unapproved) account."""
        from odoo.exceptions import UserError
        self._set_mode('own_team')
        acc = self._mk_account('pend-sb', self.sales_a, team_id=self.team.id,
                               approval_state='pending')
        self.assertTrue(acc._account_messaging_blocked())
        sb = self.env['owa.status.broadcast'].sudo().create({
            'wa_account_id': acc.id,
            'media_type': 'text',
            'body_text': 'Hello from pending account',
        })
        with self.mock_sidecar(), self.assertRaises(UserError):
            sb.action_post()

    def test_admin_form_create_auto_approves(self):
        """Regression (form-submit path): the web client merges default_get()
        into the create payload. The dynamic default must resolve to 'approved'
        for an admin so a form-create lands Approved. The shipped bug used a
        static 'pending' default + create() setdefault no-op (the form always
        submits the default), leaving admin accounts stuck Pending — under that
        old code default_get() returns 'pending' and the asserts below fail."""
        defaults = self.Account.default_get(['approval_state'])
        self.assertEqual(defaults.get('approval_state'), 'approved')
        acc = self.Account.create({
            **defaults,
            'name': 'admin-form-acct', 'webhook_secret': 'formx',
            'notify_user_ids': [(6, 0, [self.admin_user.id])]})
        self.assertEqual(acc.approval_state, 'approved')

    def test_self_service_create_cannot_self_approve(self):
        """A non-admin self-service create is forced Pending + owned by the
        creator, even if vals injects 'approved' / another owner."""
        self._set_mode('own_team')
        acc = self.Account.with_user(self.sales_a).create({
            'name': 'sneaky', 'webhook_secret': 'sn',
            'approval_state': 'approved',   # injection attempt
            'user_id': self.sales_b.id,     # mint-for-someone-else attempt
            'team_id': self.team.id,
            'notify_user_ids': [(6, 0, [self.sales_a.id])]})
        self.assertEqual(acc.approval_state, 'pending')
        self.assertEqual(acc.user_id, self.sales_a)
        self.assertTrue(acc._account_messaging_blocked())

    def test_non_admin_cannot_write_approval_state(self):
        """own_team owner can edit their own account but cannot self-approve
        via a raw write() — only an admin's action_approve may flip it."""
        self._set_mode('own_team')
        acc = self._mk_account('pend-w', self.sales_a, team_id=self.team.id,
                               approval_state='pending')
        with self.assertRaises(AccessError):
            acc.with_user(self.sales_a).write({'approval_state': 'approved'})
        self.assertEqual(acc.approval_state, 'pending')
        # admin still flips it via the gated action
        acc.with_user(self.admin_user).action_approve()
        self.assertEqual(acc.approval_state, 'approved')
