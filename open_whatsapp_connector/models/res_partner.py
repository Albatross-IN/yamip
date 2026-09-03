import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    owa_channel_count = fields.Integer(
        string="WhatsApp Channels", compute='_compute_owa_channel_count')

    # Phase 21: incoming call awareness
    owa_call_count = fields.Integer(
        string="WhatsApp Calls", compute='_compute_owa_call_count')
    owa_call_log_ids = fields.One2many(
        'owa.call.log', 'partner_id', string="WhatsApp Calls")
    owa_wa_link = fields.Char(
        string="WhatsApp link", compute='_compute_owa_wa_link',
        help="https://wa.me/<intl> link to start a chat with this contact "
             "in WhatsApp.")
    owa_wa_call_link = fields.Char(
        string="WhatsApp voice-call link", compute='_compute_owa_wa_link')
    owa_wa_video_link = fields.Char(
        string="WhatsApp video-call link", compute='_compute_owa_wa_link')

    # ── Drip campaigns: per-contact "already dispatched" marker ────────
    # Transient scheduling state, NOT history: the drip cron flips it to True
    # the moment a queue row exists for this contact, and the campaign's final
    # tick clears it for everyone. The durable record of who was messaged is
    # owa.message.campaign_id plus wa_campaign_last_id below. (#drip)
    wa_campaign_done = fields.Boolean(
        string="WhatsApp campaign sent", default=False, index=True, copy=False,
        help="Set while a drip campaign is running, once a message has been "
             "queued for this contact. Reset by the campaign's last cron tick.")
    wa_campaign_last_id = fields.Many2one(
        'owa.campaign', string="Last WhatsApp campaign",
        readonly=True, copy=False, ondelete='set null',
        help="The last drip campaign that queued a message for this contact. "
             "Survives the end-of-run reset of the flag above.")

    def _compute_owa_channel_count(self):
        for partner in self:
            partner.owa_channel_count = self.env['discuss.channel'].search_count([
                ('channel_type', '=', 'whatsapp'),
                ('whatsapp_partner_id', '=', partner.id),
            ])

    def _owa_get_or_create_channel(self):
        """Return the WhatsApp discuss.channel for this contact, reusing an
        existing conversation or starting one on the first connected QR account.
        Shared by the 'Open WhatsApp' smart button (opens it in Discuss) and the
        chatter 'WhatsApp' button (renders it inline). (#wa-open-chat)"""
        self.ensure_one()
        Channel = self.env['discuss.channel'].sudo()
        channel = Channel.search([
            ('channel_type', '=', 'whatsapp'),
            ('whatsapp_partner_id', '=', self.id),
        ], order='id desc', limit=1)
        if channel:
            return channel
        # No conversation yet — start one. (Odoo 19 has no res.partner.mobile.)
        number = self.phone
        if not number:
            raise UserError(_(
                "Add a phone number to this contact to start a WhatsApp chat."))
        formatted = wa_phone_format(self.env, number)
        if not formatted:
            raise UserError(_(
                "Couldn't read '%s' as a valid phone number — use the country "
                "code, e.g. +919999999999.") % number)
        account = (self.env['owa.account'].search(
                       [('connection_type', '=', 'qr'),
                        ('session_state', '=', 'connected')], limit=1)
                   or self.env['owa.account'].search(
                       [('connection_type', '=', 'qr')], limit=1)
                   or self.env['owa.account'].search([], limit=1))
        if not account:
            raise UserError(_("No WhatsApp account is configured yet."))
        channel = account._get_or_create_channel(
            formatted.lstrip('+'), sender_name=self.name)
        if not channel:
            raise UserError(_(
                "Couldn't open a WhatsApp conversation for %s.") % self.display_name)
        return channel

    def action_open_wa_discuss(self):
        """Open the live WhatsApp conversation with this contact in the Discuss
        app — used by the 'Open WhatsApp' smart button. (#wa-open-chat)"""
        self.ensure_one()
        return self._owa_get_or_create_channel().action_open_in_discuss()

    def _compute_owa_call_count(self):
        for partner in self:
            partner.owa_call_count = self.env['owa.call.log'].search_count([
                ('partner_id', '=', partner.id),
            ])

    @api.depends('phone')
    def _compute_owa_wa_link(self):
        """Compute click-to-WhatsApp URLs. wa.me only accepts a digits-only
        phone number with no +/spaces. ?call= is a non-standard param the
        WhatsApp app interprets as 'open the call screen' on Android/desktop;
        falls back to opening a chat on iOS.
        NB: Odoo 19 has NO res.partner.mobile field (removed from base) — the
        v18 tree falls back to mobile, but v19 must use phone only or the
        registry fails to load. (#calls / gotcha #1)"""
        for partner in self:
            digits = ''.join(c for c in (partner.phone or '') if c.isdigit())
            if digits:
                base = f"https://wa.me/{digits}"
                partner.owa_wa_link = base
                partner.owa_wa_call_link = f"{base}?call=1"
                partner.owa_wa_video_link = f"{base}?call=video"
            else:
                partner.owa_wa_link = False
                partner.owa_wa_call_link = False
                partner.owa_wa_video_link = False

    def action_open_wa_voice_call(self):
        """Phase 21: open the device's WhatsApp app on a voice-call URL.
        Returns an act_url action that opens in a new browser tab; the OS
        handles the wa.me deep link."""
        self.ensure_one()
        if not self.owa_wa_call_link:
            return False
        return {'type': 'ir.actions.act_url', 'url': self.owa_wa_call_link, 'target': 'new'}

    def action_open_wa_video_call(self):
        """Phase 21: open the device's WhatsApp app on a video-call URL."""
        self.ensure_one()
        if not self.owa_wa_video_link:
            return False
        return {'type': 'ir.actions.act_url', 'url': self.owa_wa_video_link, 'target': 'new'}

    # ── Phase C2: block / unblock this contact on WhatsApp ────────────
    def _default_owa_account(self):
        return self.env['owa.account'].search(
            [('session_state', '=', 'connected')], limit=1)

    def action_block_on_whatsapp(self):
        """Block this partner's WhatsApp number on the active account.
        Mirrors into ``owa.blacklist`` so we don't send to them again either."""
        from odoo.exceptions import UserError
        from odoo import _
        self.ensure_one()
        if not self.phone:
            raise UserError(_("This partner has no phone number set."))
        account = self._default_owa_account()
        if not account:
            raise UserError(_("No connected WhatsApp account found."))
        digits = (wa_phone_format(self.env, self.phone) or '').lstrip('+')
        if account.connection_type == 'cloud':
            account._get_cloud_api().block_users([digits])
        else:
            account._get_baileys_api().block_contact(
                digits + '@s.whatsapp.net')
        # Mirror into owa.blacklist via the canonical helper (it normalizes the
        # number, honours the unique constraint, and reactivates archived rows).
        self.env['owa.blacklist'].sudo().add_to_blacklist(
            self.phone, reason=_("Blocked from partner form"))
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("Blocked on WhatsApp"),
                'message': _("%s won't be able to message you on WhatsApp.") % self.display_name,
                'sticky': False,
            },
        }

    def action_unblock_on_whatsapp(self):
        """Reverse of action_block_on_whatsapp."""
        from odoo.exceptions import UserError
        from odoo import _
        self.ensure_one()
        if not self.phone:
            raise UserError(_("This partner has no phone number set."))
        account = self._default_owa_account()
        if not account:
            raise UserError(_("No connected WhatsApp account found."))
        digits = (wa_phone_format(self.env, self.phone) or '').lstrip('+')
        if account.connection_type == 'cloud':
            account._get_cloud_api().unblock_users([digits])
        else:
            account._get_baileys_api().unblock_contact(
                digits + '@s.whatsapp.net')
        # Mirror the un-block into owa.blacklist so outbound is re-enabled.
        self.env['owa.blacklist'].sudo().remove_from_blacklist(self.phone)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("Unblocked on WhatsApp"),
                'message': _("%s can message you on WhatsApp again.") % self.display_name,
                'sticky': False,
            },
        }

    # ── Phase C6: WhatsApp call-link generator ────────────────────────
    def action_create_wa_call_link(self, kind='audio'):
        """Generate a shareable WhatsApp call link (audio or video) and
        return it as an act_url so the agent can paste it into a chat or
        email. Compensates for the fact that we can't carry call audio."""
        from odoo.exceptions import UserError
        from odoo import _
        from odoo.addons.open_whatsapp_connector.tools.baileys_exception import (
            BaileysError,
        )
        self.ensure_one()
        account = self._default_owa_account()
        if not account:
            raise UserError(_("No connected WhatsApp account found."))
        api = account._get_baileys_api()
        # createCallLink is flaky on the gateway rc10 — WhatsApp doesn't always
        # answer the IQ. Convert the sidecar timeout/error into a friendly
        # notification instead of letting it surface as an RPC_ERROR.
        try:
            result = api.create_call_link(kind=kind) or {}
        except BaileysError as e:
            raise UserError(_(
                "Couldn't generate the WhatsApp %(kind)s call link: %(err)s",
                kind=kind, err=e.error_message or str(e),
            ))
        url = result.get('url') or ''
        if not url:
            raise UserError(_("WhatsApp did not return a call link. Try again later."))
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp call link"),
                'message': url,
                'sticky': True,
            },
        }

    def action_create_wa_audio_call_link(self):
        return self.action_create_wa_call_link(kind='audio')

    def action_create_wa_video_call_link(self):
        return self.action_create_wa_call_link(kind='video')

    @api.model
    def _owa_upgrade_partner_name(self, partner, name):
        """When partner.name is still the raw '+digits' placeholder (auto-created
        before a pushName was available), upgrade it from the inbound pushName so
        chatter shows a real human name. Never overwrite a curated name."""
        if name and partner.name:
            # `mobile` was dropped from res.partner in Odoo 19 — guard it.
            stored = partner.phone or (
                partner.mobile if 'mobile' in partner._fields else False) or ''
            if partner.name.lstrip('+').replace(' ', '') == stored.lstrip('+').replace(' ', ''):
                partner.name = name
        return partner

    @api.model
    def _find_or_create_from_wa_number(self, number, name=None):
        """Find an existing partner by WhatsApp number or create a new one.

        Matches on ``phone`` (and ``mobile`` where that field exists — it was
        dropped from res.partner in Odoo 19) first by exact E.164 variants, then
        on the normalized ``phone_sanitized`` so differently-formatted stored
        numbers (spaces, no country code, leading zero) still match instead of
        spawning a duplicate.

        :param number: WhatsApp number (digits only, no +)
        :param name: optional name for the partner
        :return: res.partner record
        """
        if not number:
            return self.env['res.partner']

        # Format for search
        formatted = wa_phone_format(self.env, '+' + number)

        # (1) Exact matches on phone (and mobile where present) across the E.164
        # variants first — fast and unambiguous. Including `mobile` fixes the
        # common case where the number is in the Mobile field rather than Phone.
        has_mobile = 'mobile' in self._fields
        candidates = []
        if formatted:
            candidates.append(formatted)
        candidates.append('+' + number)
        candidates.append(number)
        unique = list(dict.fromkeys(candidates))
        subexprs = []
        for candidate in unique:
            if has_mobile:
                subexprs.append(['|', ('phone', '=', candidate), ('mobile', '=', candidate)])
            else:
                subexprs.append([('phone', '=', candidate)])
        exact_domain = ['|'] * (len(subexprs) - 1)
        for sub in subexprs:
            exact_domain += sub
        partner = self.search(exact_domain, limit=1)
        if partner:
            return self._owa_upgrade_partner_name(partner, name)

        # (2) Format-insensitive match on the normalized number. phone_sanitized
        # is maintained by phone_validation on res.partner (E.164), so a contact
        # whose number was typed with spaces, in national form, or with a leading
        # zero — i.e. not equal to any exact variant above — is still reused
        # instead of duplicated. It is stored + indexed, so this stays cheap and
        # precise (exact equality, no substring false positives). (#dedupe)
        if formatted and 'phone_sanitized' in self._fields:
            partner = self.search([('phone_sanitized', '=', '+' + formatted)], limit=1)
            if partner:
                return self._owa_upgrade_partner_name(partner, name)

        # (3) Auto-create if configured. An EXPLICIT import ("Import contacts"
        # wizard/job) is direct user intent and must not be vetoed by the
        # passive auto-create-on-inbound toggle — those callers set
        # ``owa_force_contact_create`` in context.
        auto_create = self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.auto_create_contacts', 'true'
        )
        if (auto_create.lower() in ('true', '1', 'yes')
                or self.env.context.get('owa_force_contact_create')):
            partner = self.create({
                'name': name or f'+{number}',
                'phone': f'+{number}',
            })
            _logger.info("Auto-created partner %s for WhatsApp number %s", partner.id, number)
            return partner

        return self.env['res.partner']
