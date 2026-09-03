import ast
import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import plaintext2html
from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format

_PLACEHOLDER_RE = re.compile(r'\{\{\s*\w+\s*\}\}')
SYNC_SEND_LIMIT = 25

# Drip mode: `{{field.path}}` tokens in a campaign's free-text body, resolved
# against the recipient partner. Dotted paths are allowed so `{{parent_id.name}}`
# or `{{country_id.name}}` work without defining a quick-reply variable.
_DRIP_TOKEN_RE = re.compile(r'\{\{\s*([\w.]+)\s*\}\}')
# Friendly aliases matching the ones owa.quick.reply.render_body understands,
# so the same token spelling works whether or not a template is used.
_DRIP_ALIASES = {
    'partner_name': 'name',
    'contact_name': 'name',
    'customer_name': 'name',
    'record_name': 'display_name',
}

_logger = logging.getLogger(__name__)


class OwaCampaign(models.Model):
    _name = 'owa.campaign'
    _description = 'WhatsApp Campaign'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(string="Campaign Name", required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('cancelled', 'Cancelled'),
    ], string="Status", default='draft', required=True, tracking=True)

    # Phase 11: optional per-campaign override for account-level reply quoting.
    reply_to_mode_override = fields.Selection([
        ('off', 'Off (never quote)'),
        ('first', 'Quote first chunk only'),
        ('all', 'Quote every chunk'),
    ], string="Reply quoting (override)",
       help="If set, overrides the account's reply_to_mode for messages "
            "produced by this campaign.")
    # No connected-only domain: a lagging status cron would empty the picker
    # and block campaign creation. action_launch() re-checks the live state.
    wa_account_id = fields.Many2one('owa.account', string="WhatsApp Account",
        required=True)
    quick_reply_id = fields.Many2one('owa.quick.reply', string="Message Template")
    body = fields.Text(string="Message Body",
        help="Used if no quick reply is selected")
    attachment_ids = fields.Many2many('ir.attachment', string="Attachments")

    # `required` moved to a mode-aware constraint: a drip campaign targets
    # res.partner directly and has no contact list. (#drip)
    contact_list_id = fields.Many2one('owa.contact.list', string="Contact List")
    scheduled_date = fields.Datetime(string="Scheduled Date",
        help="Leave empty to send immediately on launch")

    # ── Drip mode ─────────────────────────────────────────────────────
    send_mode = fields.Selection([
        ('bulk', 'Contact list, all at once'),
        ('drip', 'Contacts, a few per cron tick'),
    ], string="Send Mode", default='bulk', required=True, tracking=True,
       help="Bulk queues every recipient the moment you launch. Drip walks "
            "res.partner one batch per cron tick, so a large audience is "
            "spread over hours or days instead of hitting WhatsApp at once.")
    partner_domain = fields.Char(
        string="Recipient Filter", default="[('phone', '!=', False)]",
        help="Which contacts this drip targets. A contact with no phone number "
             "is always excluded, and each contact is messaged at most once "
             "per run.")
    drip_batch_size = fields.Integer(
        string="Contacts per tick", default=1,
        help="How many contacts each cron tick messages. At the default 5-minute "
             "interval, 1 per tick is 12 an hour / 288 a day.")
    drip_finished_at = fields.Datetime(
        string="Drip finished at", readonly=True, copy=False,
        help="Set when the drip ran out of matching contacts. The campaign stays "
             "in Sending until the queued messages themselves drain.")
    drip_target_count = fields.Integer(
        string="Contacts matched", compute='_compute_drip_counts')
    drip_remaining_count = fields.Integer(
        string="Contacts remaining", compute='_compute_drip_counts')
    drip_progress = fields.Float(
        string="Progress", compute='_compute_drip_counts')

    # Per-user / per-team ownership (visibility controlled by the
    # "Campaign & Contact Visibility" setting — see res.config.settings).
    # Always present; only the toggle-able ir.rules act on them.
    user_id = fields.Many2one(
        'res.users', string="Responsible", index=True,
        default=lambda self: self.env.user, tracking=True)
    team_id = fields.Many2one(
        'crm.team', string="Sales Team", index=True,
        default=lambda self: self.env['crm.team']._get_default_team_id(
            user_id=self.env.uid))

    # Stats
    message_ids = fields.One2many('owa.message', 'campaign_id', string="Messages")
    total_count = fields.Integer(string="Total", compute='_compute_stats', store=True)
    sent_count = fields.Integer(string="Sent", compute='_compute_stats', store=True)
    delivered_count = fields.Integer(string="Delivered", compute='_compute_stats', store=True)
    read_count = fields.Integer(string="Read", compute='_compute_stats', store=True)
    failed_count = fields.Integer(string="Failed", compute='_compute_stats', store=True)

    @api.depends('message_ids.state')
    def _compute_stats(self):
        for campaign in self:
            messages = campaign.message_ids
            campaign.total_count = len(messages)
            campaign.sent_count = len(messages.filtered(lambda m: m.state in ('sent', 'delivered', 'read')))
            campaign.delivered_count = len(messages.filtered(lambda m: m.state in ('delivered', 'read')))
            campaign.read_count = len(messages.filtered(lambda m: m.state == 'read'))
            campaign.failed_count = len(messages.filtered(lambda m: m.state in ('error', 'bounced')))

    @api.constrains('send_mode', 'contact_list_id')
    def _check_contact_list_required(self):
        """A contact list is mandatory for bulk campaigns only — drip campaigns
        target res.partner through `partner_domain` instead."""
        for campaign in self:
            if campaign.send_mode == 'bulk' and not campaign.contact_list_id:
                raise ValidationError(_(
                    "Campaign '%s' sends to a contact list, so a contact list "
                    "is required.", campaign.name or ''))

    # message_ids is the dependency that actually moves: these counts come from
    # a search on res.partner, which the ORM cannot track, so without a trigger
    # that changes as the drip advances the cached value is served forever and
    # the progress bar freezes at its first reading.
    @api.depends('send_mode', 'state', 'partner_domain', 'drip_finished_at',
                 'message_ids')
    def _compute_drip_counts(self):
        Partner = self.env['res.partner'].sudo()
        for campaign in self:
            if campaign.send_mode != 'drip':
                campaign.drip_target_count = 0
                campaign.drip_remaining_count = 0
                campaign.drip_progress = 0.0
                continue
            # A half-typed domain must not make the form unopenable, so a bad
            # filter reads as "0 matched" here; action_launch raises properly.
            try:
                base = campaign._drip_domain(include_flag=False)
            except UserError:
                campaign.drip_target_count = 0
                campaign.drip_remaining_count = 0
                campaign.drip_progress = 0.0
                continue
            target = Partner.search_count(base)
            remaining = Partner.search_count(
                base + [('wa_campaign_done', '=', False)])
            campaign.drip_target_count = target
            campaign.drip_remaining_count = remaining
            campaign.drip_progress = (
                100.0 * (target - remaining) / target) if target else 0.0

    def action_launch(self):
        """Launch the campaign — create messages for all contacts."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Campaign can only be launched from draft state."))
        if self.send_mode == 'drip':
            return self._launch_drip()

        if not self.wa_account_id or self.wa_account_id.session_state != 'connected':
            raise UserError(_("WhatsApp account is not connected."))

        contacts = self.contact_list_id.member_ids.filtered('active')
        if not contacts:
            raise UserError(_("Contact list is empty."))

        Blacklist = self.env['owa.blacklist'].sudo()
        owa_message_vals = []

        for member in contacts:
            phone = member.phone_formatted or member.phone
            if not phone:
                continue
            if Blacklist.is_blacklisted(phone):
                continue

            partner_record = member.partner_id or None
            if self.quick_reply_id:
                free_text = self._campaign_free_text_values(member, partner_record)
                body = self.quick_reply_id.render_body(
                    record=partner_record, free_text_values=free_text,
                )
                body = _PLACEHOLDER_RE.sub('', body)
            else:
                body = self.body or ''

            mail_message = self.env['mail.message'].create({
                'body': plaintext2html(body or ''),
                'message_type': 'whatsapp_message',
                'attachment_ids': [(6, 0, self.attachment_ids.ids)],
            })

            msg_vals = {
                'mobile_number': phone,
                'message_type': 'outbound',
                'state': 'outgoing',
                'wa_account_id': self.wa_account_id.id,
                'mail_message_id': mail_message.id,
                'campaign_id': self.id,
                'scheduled_date': self.scheduled_date,
            }
            if self.quick_reply_id:
                msg_vals['quick_reply_id'] = self.quick_reply_id.id
            if self.reply_to_mode_override:
                msg_vals['reply_to_mode_override'] = self.reply_to_mode_override
            owa_message_vals.append(msg_vals)

        self.state = 'sending'
        if owa_message_vals:
            queued = self.env['owa.message'].create(owa_message_vals)
            if not self.scheduled_date:
                if len(queued) <= SYNC_SEND_LIMIT:
                    queued._send_message()
                    if not self.message_ids.filtered(lambda m: m.state == 'outgoing'):
                        self.state = 'sent'
                else:
                    cron = self.env.ref(
                        'open_whatsapp_connector.ir_cron_send_owa_queue',
                        raise_if_not_found=False,
                    )
                    if cron:
                        cron.sudo()._trigger()

        _logger.info("Campaign '%s' launched: %d messages queued", self.name, len(owa_message_vals))
        self.message_post(body=_("Campaign launched: %d messages queued for sending.", len(owa_message_vals)))

    def _campaign_free_text_values(self, member, partner):
        """Build placeholder values from the contact list member + linked partner.

        Campaigns address partner contacts; SO/invoice-style placeholders like
        amount_total/currency have no source, so we leave those keys absent and
        let the post-render regex strip the unresolved tokens.
        """
        partner_name = (partner.name if partner else None) or member.name or ''
        return {
            'partner_name': partner_name,
            'record_name': partner_name,
            'phone': member.phone_formatted or member.phone or '',
        }

    # ------------------------------------------------------------------
    # DRIP MODE
    #
    # One campaign at a time walks res.partner, queueing `drip_batch_size`
    # messages per cron tick. Progress is held on res.partner.wa_campaign_done
    # — a single shared boolean, which is exactly why only one drip may run at
    # a time (two would reset each other's contacts on finish).
    # ------------------------------------------------------------------

    # Over-fetch beyond the batch size so a stretch of blacklisted or
    # unreachable contacts is burned through in one tick instead of costing
    # five minutes each.
    _DRIP_SKIP_HEADROOM = 50

    @api.model
    def _drip_cron(self):
        return self.env.ref(
            'open_whatsapp_connector.ir_cron_owa_campaign_drip',
            raise_if_not_found=False)

    @api.model
    def _drip_running_domain(self):
        return [
            ('send_mode', '=', 'drip'),
            ('state', '=', 'sending'),
            ('drip_finished_at', '=', False),
        ]

    def _drip_domain(self, include_flag=True):
        """Recipient domain for this drip: the user's filter, plus the two
        invariants the engine depends on — a phone number to send to, and the
        not-yet-messaged flag."""
        self.ensure_one()
        try:
            domain = ast.literal_eval(self.partner_domain or '[]')
        except (ValueError, SyntaxError):
            raise UserError(_(
                "The recipient filter is not a valid domain: %s",
                self.partner_domain))
        if not isinstance(domain, list):
            raise UserError(_("The recipient filter must be a list of criteria."))
        domain = list(domain) + [('phone', '!=', False)]
        if include_flag:
            domain.append(('wa_campaign_done', '=', False))
        return domain

    def _launch_drip(self):
        """Start a drip run: validate, clear stale flags, wake the dispatcher."""
        self.ensure_one()
        if not self.wa_account_id or self.wa_account_id.session_state != 'connected':
            raise UserError(_("WhatsApp account is not connected."))
        if not self.quick_reply_id and not (self.body or '').strip():
            raise UserError(_(
                "Pick a message template or type a message body — a drip "
                "campaign with nothing to say would flag every contact and "
                "send nothing."))
        running = self.search(
            self._drip_running_domain() + [('id', '!=', self.id)], limit=1)
        if running:
            raise UserError(_(
                "Drip campaign '%s' is still running. Only one drip can run at "
                "a time — they share the same per-contact flag on Contacts. "
                "Let it finish, or cancel it first.", running.name))
        if not self.env['res.partner'].sudo().search_count(
                self._drip_domain(include_flag=False)):
            raise UserError(_("No contacts match the recipient filter."))

        # Clear flags left behind by an aborted run so the whole pool is
        # eligible again.
        self._drip_release_flags()
        self.write({'state': 'sending', 'drip_finished_at': False})
        cron = self._drip_cron()
        if cron:
            cron.sudo().write({
                'active': True,
                'nextcall': fields.Datetime.now(),
            })
        _logger.info("Drip campaign '%s' started: %d contacts matched",
                     self.name, self.drip_target_count)
        self.message_post(body=_(
            "Drip campaign started: %(count)d contacts matched, "
            "%(batch)d per tick.",
            count=self.drip_target_count, batch=max(1, self.drip_batch_size)))

    def action_stop_drip(self):
        """Stop dispatching new contacts but let already-queued messages go.

        The nuclear option is Cancel, which also voids the queue."""
        self.ensure_one()
        if self.send_mode != 'drip' or self.state != 'sending' or self.drip_finished_at:
            raise UserError(_("This campaign is not currently dripping."))
        self._finish_drip(reason=_("stopped manually"))

    def _drip_release_flags(self):
        """Clear the shared per-contact flag. Safe to call at any point: the
        flag is scheduling state, and who-was-messaged lives on owa.message
        (campaign_id) and res.partner.wa_campaign_last_id."""
        # active_test=False: a contact archived mid-run would otherwise keep a
        # stale flag forever, since the default search hides it.
        flagged = self.env['res.partner'].sudo().with_context(
            active_test=False).search([('wa_campaign_done', '=', True)])
        if flagged:
            flagged.write({'wa_campaign_done': False})
        return len(flagged)

    def _drip_stop_cron_if_idle(self):
        """Deactivate the 5-minute dispatcher once nothing is left to drip, so
        an idle database isn't waking a cron forever."""
        if self.search_count(self._drip_running_domain()):
            return False
        cron = self._drip_cron()
        if cron and cron.active:
            cron.sudo().active = False
            _logger.info("owa drip cron deactivated: no drip campaign running")
            return True
        return False

    def _render_drip_body(self, partner):
        """Render this campaign's message for one contact.

        With a template, the existing quick-reply machinery resolves
        `{{variables}}` (including per-variable field paths) against the
        partner. Without one, the free-text body gets the same treatment — it
        used to be delivered verbatim, so `{{partner_name}}` typed into Message
        Body reached the contact as literal braces. (#drip)"""
        self.ensure_one()
        if self.quick_reply_id:
            return self.quick_reply_id.render_plain(record=partner)
        return self._render_drip_free_text(self.body or '', partner)

    def _render_drip_free_text(self, body, partner):
        """Substitute `{{field.path}}` tokens from `partner`; drop what doesn't
        resolve rather than shipping raw braces to a customer.

        Only real fields are traversed — never methods or attributes — so a
        token can't reach into the environment or call anything."""
        def _resolve(match):
            path = match.group(1).strip()
            path = _DRIP_ALIASES.get(path, path)
            value = partner
            for part in path.split('.'):
                if not hasattr(value, '_fields') or part not in value._fields:
                    return ''
                value = value[part]
                if not value:
                    return ''
            if hasattr(value, '_fields'):
                value = value.display_name
            return str(value)
        return _DRIP_TOKEN_RE.sub(_resolve, body).strip()

    def _drip_queue_one(self, partner, phone):
        """Queue one outbound message and flag the contact in the SAME
        transaction, so a rollback can never lose a contact or double-send
        one."""
        self.ensure_one()
        body = self._render_drip_body(partner)
        mail_message = self.env['mail.message'].sudo().create({
            'body': plaintext2html(body or ''),
            'message_type': 'whatsapp_message',
            'attachment_ids': [(6, 0, self.attachment_ids.ids)],
        })
        msg_vals = {
            'mobile_number': phone,
            'message_type': 'outbound',
            'state': 'outgoing',
            'wa_account_id': self.wa_account_id.id,
            'mail_message_id': mail_message.id,
            'campaign_id': self.id,
            'whatsapp_partner_id': partner.id,
        }
        if self.quick_reply_id:
            msg_vals['quick_reply_id'] = self.quick_reply_id.id
        if self.reply_to_mode_override:
            msg_vals['reply_to_mode_override'] = self.reply_to_mode_override
        message = self.env['owa.message'].sudo().create(msg_vals)
        partner.write({
            'wa_campaign_done': True,
            'wa_campaign_last_id': self.id,
        })
        return message

    def _drip_send_next(self, limit):
        """Queue up to `limit` contacts. Returns (queued, skipped).

        Blacklisted and unformattable contacts are flagged as processed without
        being messaged — they have to be, or the same contact would be picked
        on every tick forever — but they don't count against the tick's budget.
        """
        self.ensure_one()
        Partner = self.env['res.partner'].sudo()
        Blacklist = self.env['owa.blacklist'].sudo()
        queued = skipped = 0
        candidates = Partner.search(
            self._drip_domain(), order='id',
            limit=max(1, limit) + self._DRIP_SKIP_HEADROOM)
        for partner in candidates:
            if queued >= limit:
                break
            phone = wa_phone_format(self.env, partner.phone) or partner.phone
            if not phone:
                partner.wa_campaign_done = True
                skipped += 1
                continue
            if Blacklist.is_blacklisted(phone):
                partner.wa_campaign_done = True
                skipped += 1
                continue
            self._drip_queue_one(partner, phone)
            queued += 1
        if skipped:
            # A skipped contact advances the run without creating a message, so
            # nothing in the counts' depends fired — drop them by hand.
            self.invalidate_recordset(
                ['drip_target_count', 'drip_remaining_count', 'drip_progress'])
        return queued, skipped

    def _drip_tick(self):
        """Advance this campaign by one batch. Returns the number queued."""
        self.ensure_one()
        account = self.wa_account_id
        # Skipping a tick costs five minutes; burning contacts we can't
        # actually transmit to costs the message. So bail without consuming
        # anyone whenever the account isn't in a position to send.
        if not account or account.session_state != 'connected':
            _logger.info("drip '%s': account not connected, tick skipped", self.name)
            return 0
        if account._account_messaging_blocked():
            _logger.info("drip '%s': account pending approval, tick skipped", self.name)
            return 0
        if account._owa_is_throttled():
            _logger.info("drip '%s': account throttled, tick skipped", self.name)
            return 0

        queued, skipped = self._drip_send_next(max(1, self.drip_batch_size))
        if queued:
            # Hand off immediately instead of waiting up to a minute for the
            # send queue's own tick.
            send_cron = self.env.ref(
                'open_whatsapp_connector.ir_cron_send_owa_queue',
                raise_if_not_found=False)
            if send_cron:
                send_cron.sudo()._trigger()
        if queued or skipped:
            return queued
        # Nothing matched: every contact in the filter has been through.
        self._finish_drip()
        return 0

    def _finish_drip(self, reason=None):
        """End of the run: release the shared flag and stop the dispatcher.

        The campaign stays in Sending until its queued messages drain — the
        existing _cron_update_campaign_state moves it to Sent once
        drip_finished_at is stamped."""
        self.ensure_one()
        released = self._drip_release_flags()
        self.drip_finished_at = fields.Datetime.now()
        self._drip_stop_cron_if_idle()
        _logger.info("Drip campaign '%s' finished (%s): %d contacts messaged, "
                     "%d flags released", self.name, reason or 'completed',
                     self.total_count, released)
        self.message_post(body=_(
            "Drip campaign finished (%(reason)s): %(count)d messages queued in "
            "total. Contact flags have been reset.",
            reason=reason or _("all contacts processed"), count=self.total_count))

    @api.model
    def _cron_drip_send(self):
        """Cron (5 min): advance each running drip campaign by one batch."""
        campaigns = self.search(self._drip_running_domain(), order='id')
        for campaign in campaigns:
            try:
                with self.env.cr.savepoint():
                    campaign._drip_tick()
            except Exception:
                _logger.exception("drip campaign %s: tick failed", campaign.name)
        # _drip_tick may have finished the last one — re-check before idling.
        self._drip_stop_cron_if_idle()

    def action_cancel(self):
        """Cancel the campaign and queued messages."""
        self.ensure_one()
        if self.state not in ('draft', 'sending'):
            raise UserError(_("Cannot cancel a completed campaign."))
        # Cancel queued messages
        self.message_ids.filtered(lambda m: m.state == 'outgoing').write({'state': 'cancel'})
        self.state = 'cancelled'
        if self.send_mode == 'drip':
            # Release the per-contact flag and stop the dispatcher, otherwise a
            # cancelled drip leaves every contact it touched marked as done and
            # blocks the next campaign.
            self._drip_release_flags()
            self.drip_finished_at = fields.Datetime.now()
            self._drip_stop_cron_if_idle()

    def action_reset_to_draft(self):
        """Reset a cancelled campaign to draft."""
        self.ensure_one()
        if self.state != 'cancelled':
            raise UserError(_("Only cancelled campaigns can be reset."))
        self.state = 'draft'
        self.drip_finished_at = False

    def _cron_update_campaign_state(self):
        """Cron: Update campaign state when all messages are processed."""
        # A drip campaign is legitimately "sending" with an empty queue between
        # ticks — the rest of its recipients don't exist as messages yet. Only
        # consider it once _finish_drip has stamped drip_finished_at, or this
        # cron would flip it to Sent after the very first batch drained and the
        # drip dispatcher would drop it. (#drip)
        campaigns = self.search([
            ('state', '=', 'sending'),
            '|',
            ('send_mode', '!=', 'drip'),
            ('drip_finished_at', '!=', False),
        ])
        Bus = self.env['bus.bus'].sudo()
        for campaign in campaigns:
            pending = campaign.message_ids.filtered(lambda m: m.state == 'outgoing')
            if not pending:
                campaign.state = 'sent'
                partner = campaign.create_uid.partner_id
                if partner:
                    Bus._sendone(partner, "owa.campaign/refresh", {"id": campaign.id})


class OwaContactList(models.Model):
    _name = 'owa.contact.list'
    _description = 'WhatsApp Contact List'
    _order = 'name'

    name = fields.Char(string="List Name", required=True)
    active = fields.Boolean(default=True)
    member_ids = fields.One2many('owa.contact.list.member', 'list_id', string="Contacts")
    member_count = fields.Integer(string="Contacts", compute='_compute_member_count')

    # Per-user / per-team ownership. No mail.thread on this model -> no
    # tracking attribute. Visibility gated by the toggle-able ir.rules.
    user_id = fields.Many2one(
        'res.users', string="Responsible", index=True,
        default=lambda self: self.env.user)
    team_id = fields.Many2one(
        'crm.team', string="Sales Team", index=True,
        default=lambda self: self.env['crm.team']._get_default_team_id(
            user_id=self.env.uid))

    @api.depends('member_ids')
    def _compute_member_count(self):
        for rec in self:
            rec.member_count = len(rec.member_ids.filtered('active'))

    def action_import_partners(self):
        """Import contacts from res.partner records with phone numbers."""
        self.ensure_one()
        partners = self.env['res.partner'].search([
            ('phone', '!=', False),
        ])
        existing_phones = set(
            (p for p in self.member_ids.mapped('phone_formatted') if p)
        )
        # Also include raw phones so a member saved before a phone-format
        # round-trip is still recognised.
        existing_phones |= set(
            (p for p in self.member_ids.mapped('phone') if p)
        )
        new_members = []
        for partner in partners:
            phone = partner.phone
            formatted = wa_phone_format(self.env, phone) or phone
            if formatted and formatted not in existing_phones:
                new_members.append({
                    'list_id': self.id,
                    'partner_id': partner.id,
                    'name': partner.name,
                    'phone': formatted,
                })
                existing_phones.add(formatted)
        if new_members:
            self.env['owa.contact.list.member'].create(new_members)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Import Complete"),
                'message': _("%d contacts imported.", len(new_members)),
                'type': 'success',
                'sticky': False,
            }
        }


class OwaContactListMember(models.Model):
    _name = 'owa.contact.list.member'
    _description = 'WhatsApp Contact List Member'
    _order = 'name'

    list_id = fields.Many2one('owa.contact.list', string="Contact List",
        required=True, ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string="Contact")
    name = fields.Char(string="Name")
    phone = fields.Char(string="Phone Number", required=True)
    phone_formatted = fields.Char(string="Formatted Phone",
        compute='_compute_phone_formatted', store=True)
    active = fields.Boolean(default=True)

    @api.depends('phone')
    def _compute_phone_formatted(self):
        for member in self:
            if member.phone:
                member.phone_formatted = wa_phone_format(self.env, member.phone) or member.phone
            else:
                member.phone_formatted = False

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        if self.partner_id:
            self.name = self.partner_id.name
            self.phone = self.partner_id.phone or ''
