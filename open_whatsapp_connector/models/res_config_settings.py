import os
import shutil
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError


def _bundled_sidecar_path():
    """Absolute path to this addon's bundled ``sidecar/`` directory, used as
    the default for the Sidecar Path setting so the field is pre-filled even
    before the install hook persists it (and after any manual clear)."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sidecar')


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    owa_default_sidecar_url = fields.Char(
        string="Default Sidecar URL",
        config_parameter='open_whatsapp_connector.default_sidecar_url',
        default='http://localhost:3100')
    owa_default_api_key = fields.Char(
        string="Default API Key",
        config_parameter='open_whatsapp_connector.default_api_key')
    owa_sidecar_path = fields.Char(
        string="Sidecar Path",
        config_parameter='open_whatsapp_connector.sidecar_path',
        default=lambda self: _bundled_sidecar_path(),
        help="Absolute path to the sidecar directory (containing package.json)")
    owa_sidecar_state = fields.Selection(
        [('ok', 'Ready'),
         ('no_path', 'Path not set'),
         ('no_node', 'Node.js not found'),
         ('not_built', 'Not built')],
        string="Sidecar Status", compute='_compute_owa_sidecar_state',
        help="Live check of whether the QR sidecar can run with the current "
             "Sidecar Path: is Node.js on the server PATH and is the sidecar built.")
    owa_sidecar_auto_start = fields.Boolean(
        string="Auto-start Sidecar",
        config_parameter='open_whatsapp_connector.sidecar_auto_start',
        default=False,
        help="Automatically start sidecar when connecting an account")

    # NOTE: intentionally NOT a `config_parameter` Boolean. Odoo stores a
    # boolean *False* config parameter as the ABSENCE of the row
    # (ir.config_parameter.set_param(key, False) unlinks it), so a
    # default-True config_parameter boolean can never be turned off — on
    # reload get_values falls back to the True default. We persist an explicit
    # 'true'/'false' string in get_values()/set_values() instead. (#F-autocreate)
    owa_auto_create_contacts = fields.Boolean(
        string="Auto-create Contacts",
        help="Automatically create contact records for unknown WhatsApp numbers")

    owa_default_account_id = fields.Many2one(
        'owa.account', string="Default WhatsApp Account",
        config_parameter='open_whatsapp_connector.default_account_id',
        help="Used as the reply account when a conversation's account is "
             "missing/disconnected, and as the account its conversations are "
             "reassigned to when a WhatsApp account is deleted. Also pre-selects "
             "the composer wizard account.")

    # ------------------------------------------------------------------
    # Marketing record visibility (per-user / per-team isolation toggle)
    # ------------------------------------------------------------------
    owa_marketing_visibility = fields.Selection(
        [
            ('shared', 'Shared across company (everyone sees all)'),
            ('own_team', 'Restricted to owner + their Sales Team'),
        ],
        string="Campaign & Contact Visibility",
        default='shared',
        config_parameter='open_whatsapp_connector.marketing_visibility',
        help="Shared: every WhatsApp user in the company sees all campaigns, "
             "contact lists, broadcast groups, standing orders and status "
             "broadcasts (default).\n"
             "Restricted: each user sees only the records they own or that "
             "belong to a Sales Team they are a member of / lead; WhatsApp "
             "users can create and manage their own records. WhatsApp "
             "Administrators always see everything.")

    # ------------------------------------------------------------------
    # Per-agent WhatsApp conversation visibility (Discuss channel isolation)
    # ------------------------------------------------------------------
    owa_chat_visibility = fields.Selection(
        [
            ('shared', 'All agents see every WhatsApp conversation (default)'),
            ('own_agent', 'Each agent sees only conversations assigned to them'),
        ],
        string="WhatsApp Conversation Visibility",
        default='shared',
        config_parameter='open_whatsapp_connector.chat_visibility',
        help="Shared (default): every agent sees all WhatsApp conversations "
             "they are a member of — no change to existing behaviour.\n"
             "Own agent: a WhatsApp conversation is visible only to its assigned "
             "agent (the channel 'Assignee'); unassigned conversations stay "
             "visible to everyone, and non-WhatsApp Discuss (DMs, group chats, "
             "livechat, regular channels) is never affected. WhatsApp "
             "Administrators always see every conversation.")

    # Record-rule + ACL XML ids toggled by the visibility setting. Kept in
    # one place so set_values() and any future maintenance stay in sync.
    _OWA_VISIBILITY_RULE_XMLIDS = (
        'open_whatsapp_connector.owa_campaign_user_rule',
        'open_whatsapp_connector.owa_campaign_admin_all_rule',
        'open_whatsapp_connector.owa_contact_list_user_rule',
        'open_whatsapp_connector.owa_contact_list_admin_all_rule',
        'open_whatsapp_connector.owa_contact_list_member_user_rule',
        'open_whatsapp_connector.owa_contact_list_member_admin_all_rule',
        'open_whatsapp_connector.owa_broadcast_group_user_rule',
        'open_whatsapp_connector.owa_broadcast_group_admin_all_rule',
        'open_whatsapp_connector.owa_standing_order_user_rule',
        'open_whatsapp_connector.owa_standing_order_admin_all_rule',
        'open_whatsapp_connector.owa_status_broadcast_user_rule',
        'open_whatsapp_connector.owa_status_broadcast_admin_all_rule',
    )
    _OWA_VISIBILITY_ACL_XMLIDS = (
        'open_whatsapp_connector.access_owa_campaign_user_manage',
        'open_whatsapp_connector.access_owa_contact_list_user_manage',
        'open_whatsapp_connector.access_owa_contact_list_member_user_manage',
        'open_whatsapp_connector.access_owa_broadcast_group_user_manage',
        'open_whatsapp_connector.access_owa_standing_order_user_manage',
        'open_whatsapp_connector.access_owa_status_broadcast_user_manage',
    )

    # Per-agent WhatsApp conversation isolation is enforced by ALWAYS-active
    # discuss.channel / discuss.channel.member rules whose domain reads the
    # `open_whatsapp_connector.chat_visibility` parameter (see
    # security/ir_rules.xml). set_values() only writes the parameter + clears
    # the ir.rule cache — there is no `active` flag to toggle, which keeps the
    # feature immune to module-upgrade (-u) rule reloads.

    # ------------------------------------------------------------------
    # Per-account self-service / visibility toggle
    # ------------------------------------------------------------------
    owa_account_visibility = fields.Selection(
        [
            ('shared', 'Shared (administrators manage all WhatsApp accounts)'),
            ('own_team', 'Self-service: each user scans their own; team leaders '
                         'see their team; admins approve & see all'),
        ],
        string="WhatsApp Account Visibility",
        default='shared',
        config_parameter='open_whatsapp_connector.account_visibility',
        help="Shared (default): only WhatsApp Administrators add/scan/manage "
             "accounts, and everyone who can see accounts sees all of them.\n"
             "Self-service: an internal user can add & scan their OWN WhatsApp "
             "(it stays pending until an admin approves it before it can "
             "send/receive); a user sees only their own account, a Sales Team "
             "leader sees their team's, and WhatsApp Administrators see all.")

    @api.depends('owa_sidecar_path')
    def _compute_owa_sidecar_state(self):
        """Live sidecar readiness for the Settings hint (case-D fallback): is
        the path set, is Node.js on the server PATH, is dist/index.js built."""
        for rec in self:
            path = rec.owa_sidecar_path
            if not path:
                rec.owa_sidecar_state = 'no_path'
            elif not shutil.which('node'):
                rec.owa_sidecar_state = 'no_node'
            elif not os.path.isfile(os.path.join(path, 'dist', 'index.js')):
                rec.owa_sidecar_state = 'not_built'
            else:
                rec.owa_sidecar_state = 'ok'

    def get_values(self):
        res = super().get_values()
        # Read the explicit 'true'/'false' string (see the field's NOTE) with the
        # SAME case-insensitive test the runtime consumer uses (res_partner.py).
        # A DB upgraded from the old default-True config_parameter Boolean stored
        # this as the capitalized 'True'/'False' (Python str(bool)); a bare
        # `== 'true'` would misread that as OFF and the UI would silently disagree
        # with the still-active behaviour. A missing parameter defaults to ON so
        # fresh installs keep auto-create. (#F-autocreate)
        res['owa_auto_create_contacts'] = self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.auto_create_contacts', 'true'
        ).strip().lower() in ('true', '1', 'yes')
        return res

    def set_values(self):
        super().set_values()
        # Persist auto-create as an explicit string so OFF survives a reload
        # (a boolean-False config param would be deleted -> reverts to default).
        self.env['ir.config_parameter'].sudo().set_param(
            'open_whatsapp_connector.auto_create_contacts',
            'true' if self.owa_auto_create_contacts else 'false')
        # Reflect the chosen visibility mode by activating/deactivating the
        # shipped-inactive record rules + user-CRUD ACLs.
        restrict = self.owa_marketing_visibility == 'own_team'
        for xmlid in (self._OWA_VISIBILITY_RULE_XMLIDS
                      + self._OWA_VISIBILITY_ACL_XMLIDS):
            rec = self.env.ref(xmlid, raise_if_not_found=False)
            if rec and rec.active != restrict:
                rec.sudo().active = restrict

        # Per-agent WhatsApp conversation isolation is enforced by ALWAYS-active
        # record rules whose domain reads the `chat_visibility` parameter (set
        # above by super()). Nothing to toggle — just clear the ir.rule domain
        # cache so the new parameter value is picked up on the next request.

        # Account self-service/visibility: toggle the user CRUD ACL so a
        # non-admin can create/scan their own account ONLY in own_team mode.
        acct_manage = self.env.ref(
            'open_whatsapp_connector.access_owa_account_user_manage',
            raise_if_not_found=False)
        if acct_manage:
            want = self.owa_account_visibility == 'own_team'
            if acct_manage.active != want:
                acct_manage.sudo().active = want

        self.env.registry.clear_cache()

    def action_owa_enable_recommended(self):
        """One-click: (re)seed the starter rules for the apps you have
        installed, then activate the 'recommended' core ones.

        The seeding step is idempotent — it dedupes on model + trigger, so
        clicking again never creates duplicates — and only creates rules for
        models that are actually installed. Running it here makes the
        recommended rules appear regardless of whether the business apps
        (Sales, Invoicing, …) were installed before or after this connector.
        The wider library and the stage-based rules (whose trigger stage name
        varies per database) still ship inactive — enable those individually
        from the Notification Rules list.
        """
        self.ensure_one()
        # Make sure the starter rules exist for whatever apps are installed
        # right now. Idempotent + install-order-proof; never duplicates. We must
        # NOT raise after this point, or the seeding would be rolled back — so
        # the branches below return a notification instead of a UserError.
        from odoo.addons.open_whatsapp_connector import (
            _create_default_notification_rules)
        _create_default_notification_rules(self.env)

        Rule = self.env['owa.notification.rule'].with_context(active_test=False)
        total_recommended = Rule.search_count([('recommended', '=', True)])
        recommended_inactive = Rule.search([
            ('active', '=', False), ('recommended', '=', True)])

        def _notify(message, ntype):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("WhatsApp automations"),
                    'message': message,
                    'type': ntype,
                    'sticky': False,
                },
            }

        if not total_recommended:
            return _notify(_(
                "No recommended automation rules apply to your installed apps "
                "yet. Install the apps you want to notify on (Sales, Invoicing, "
                "…), then click “Enable recommended automations” again."),
                'warning')

        account = self.env['owa.account'].search(
            [('session_state', '=', 'connected')], limit=1)
        if not account:
            return _notify(_(
                "%(n)s recommended starter rule(s) are ready under "
                "Configuration > Notification Rules. Connect a WhatsApp "
                "account, then click “Enable recommended automations” again to "
                "activate them.", n=total_recommended), 'warning')

        if not recommended_inactive:
            return _notify(_(
                "All recommended automation rules are already active on account "
                "“%(name)s”.", name=account.name), 'info')

        recommended_inactive.write({'wa_account_id': account.id, 'active': True})
        return _notify(_(
            "%(count)s recommended rule(s) activated on account “%(name)s”.",
            count=len(recommended_inactive), name=account.name), 'success')

    # Website WhatsApp Widget
    owa_website_widget_enabled = fields.Boolean(
        string="Website WhatsApp Widget",
        config_parameter='open_whatsapp_connector.website_widget_enabled')
    owa_website_widget_phone = fields.Char(
        string="Widget Phone Number",
        config_parameter='open_whatsapp_connector.website_widget_phone')
    owa_website_widget_message = fields.Char(
        string="Default Message",
        config_parameter='open_whatsapp_connector.website_widget_message',
        default="Hello! I'd like to know more about your products.")
    owa_website_widget_code = fields.Text(
        string="Widget Embed Code",
        compute='_compute_website_widget_code')

    @api.depends('owa_website_widget_enabled', 'owa_website_widget_phone', 'owa_website_widget_message')
    def _compute_website_widget_code(self):
        for rec in self:
            if rec.owa_website_widget_enabled and rec.owa_website_widget_phone:
                phone = (rec.owa_website_widget_phone or '').replace('+', '').replace(' ', '').replace('-', '')
                # URL-encode the prefilled message — spaces/&/? would otherwise
                # break the wa.me link. (matches the glue addon's QWeb button)
                message = quote(rec.owa_website_widget_message or '', safe='')
                rec.owa_website_widget_code = (
                    '<!-- WhatsApp Chat Widget -->\n'
                    '<a href="https://wa.me/%s?text=%s" target="_blank" rel="noopener"\n'
                    '   style="position:fixed;bottom:24px;right:24px;z-index:9999;width:60px;height:60px;\n'
                    '   border-radius:50%%;background:#25D366;display:flex;align-items:center;\n'
                    '   justify-content:center;box-shadow:0 4px 12px rgba(0,0,0,.15);text-decoration:none">\n'
                    '   <svg viewBox="0 0 24 24" width="32" height="32" fill="white">\n'
                    '      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15'
                    '-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475'
                    '-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52'
                    '.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207'
                    '-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297'
                    '-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487'
                    '.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413'
                    '.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z"/>\n'
                    '      <path d="M12 0C5.373 0 0 5.373 0 12c0 2.625.846 5.059 2.284 7.034L.789 23.492'
                    'l4.632-1.467A11.95 11.95 0 0012 24c6.627 0 12-5.373 12-12S18.627 0 12 0zm0 21.818'
                    'c-2.168 0-4.18-.587-5.918-1.608l-.424-.252-4.393 1.152 1.172-4.283-.276-.439A9.77 9.77'
                    ' 0 012.182 12c0-5.423 4.395-9.818 9.818-9.818S21.818 6.577 21.818 12 17.423 21.818 12'
                    ' 21.818z"/>\n'
                    '   </svg>\n'
                    '</a>'
                ) % (phone, message)
            else:
                rec.owa_website_widget_code = False

    # ------------------------------------------------------------------
    # Meta App (Cloud API — Embedded Signup)
    # ------------------------------------------------------------------
    # Global Meta App credentials used ONLY by the Facebook "Embedded Signup"
    # one-click onboarding (static/src/cloud). Per-number tokens live on each
    # owa.account. The App Secret is stored here for the server-side code→token
    # exchange and is never exposed to the browser (see
    # owa.account.get_embedded_signup_config, which returns only app_id /
    # config_id / version).
    owa_meta_app_id = fields.Char(
        string="Meta App ID",
        config_parameter='open_whatsapp_connector.meta_app_id',
        help="Your Meta App's ID — used by the Embedded Signup Facebook login.")
    owa_meta_app_secret = fields.Char(
        string="Meta App Secret",
        config_parameter='open_whatsapp_connector.meta_app_secret',
        help="Your Meta App's secret — used server-side to exchange the "
             "Embedded Signup code for an access token. Never sent to the "
             "browser.")
    owa_meta_config_id = fields.Char(
        string="Embedded Signup Config ID",
        config_parameter='open_whatsapp_connector.meta_config_id',
        help="The WhatsApp Embedded Signup configuration id from your Meta "
             "App's Facebook Login for Business settings.")
    owa_meta_api_version = fields.Char(
        string="Graph API Version",
        config_parameter='open_whatsapp_connector.meta_api_version',
        default='v23.0',
        help="Meta Graph API version used for the onboarding token exchange, "
             "e.g. v23.0.")
