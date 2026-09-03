"""WhatsApp Status updates RECEIVED from contacts (their Stories).

Captured by the sidecar only when the account's ``capture_contact_statuses``
toggle is on (opt-in — capturing every contact's story is high-volume and
storage-heavy). Each row is one contact's status; media is downloaded into an
attachment and auto-purged with the row after WhatsApp's 24-hour expiry.
"""
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format

_logger = logging.getLogger(__name__)


class OwaStatusUpdate(models.Model):
    _name = 'owa.status.update'
    _description = "Contact's WhatsApp Status"
    _order = 'posted_at desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account",
        required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, index=True)
    # Ownership mirrors the account so the marketing-visibility ir.rules apply.
    user_id = fields.Many2one('res.users', string="Responsible", index=True)
    team_id = fields.Many2one('crm.team', string="Sales Team", index=True)

    partner_id = fields.Many2one('res.partner', string="Contact", index=True)
    contact_number = fields.Char(string="Number", index=True)
    sender_name = fields.Char(string="Name")

    media_type = fields.Selection([
        ('text',  'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
    ], default='text', required=True)
    body_text = fields.Text(string="Caption / text")
    attachment_id = fields.Many2one('ir.attachment', string="Media", ondelete='set null')
    media_image = fields.Image(string="Preview", max_width=512, max_height=512)

    # WhatsApp status message key + poster JID — needed to view (mark seen),
    # reply (DM quoting it) and react.
    wa_message_id = fields.Char(string="WhatsApp Message ID", index=True, copy=False)
    wa_participant = fields.Char(string="Poster JID")

    posted_at = fields.Datetime(index=True)
    expires_at = fields.Datetime(index=True)
    viewed = fields.Boolean(default=False)

    _sql_constraints = [
        ('wa_msg_uniq', 'unique(wa_account_id, wa_message_id)',
         'This status update was already captured.'),
    ]

    @api.depends('sender_name', 'contact_number', 'media_type', 'body_text')
    def _compute_name(self):
        for rec in self:
            who = rec.sender_name or rec.contact_number or _('Unknown')
            preview = (rec.body_text or '')[:30]
            rec.name = f"{who} · [{rec.media_type}] {preview}".strip()

    # ── Interactions ──────────────────────────────────────────────────
    def _status_key(self):
        """The WhatsApp status message key needed by the reply/react/read APIs."""
        self.ensure_one()
        return {
            'remoteJid': 'status@broadcast',
            'id': self.wa_message_id,
            'participant': self.wa_participant,
        }

    def action_mark_seen(self):
        """Mark this contact's status as viewed. Only sends a WhatsApp view
        receipt (so the contact sees you watched it) when the account opted in
        via ``status_mark_seen`` — otherwise it's a local flag only."""
        from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
        for rec in self:
            if (rec.wa_account_id.status_mark_seen and rec.wa_message_id
                    and rec.wa_account_id.session_state == 'connected'):
                try:
                    BaileysApi(rec.wa_account_id).status_mark_seen(rec._status_key())
                except Exception as e:
                    _logger.warning("status mark-seen failed for %s: %s", rec.id, e)
            rec.viewed = True
        return True

    def action_reply(self):
        self.ensure_one()
        self.action_mark_seen()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Reply to status"),
            'res_model': 'owa.status.interact.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_status_id': self.id, 'default_mode': 'reply'},
        }

    def action_react(self):
        self.ensure_one()
        self.action_mark_seen()
        return {
            'type': 'ir.actions.act_window',
            'name': _("React to status"),
            'res_model': 'owa.status.interact.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_status_id': self.id, 'default_mode': 'react'},
        }

    # ── Ingest (from the /webhook/story sidecar push) ─────────────────
    @api.model
    def _ingest_status_update(self, account, payload):
        """Create one captured-status row from a sidecar payload. Idempotent
        on (account, message_id). Best-effort partner lookup — never
        auto-creates a contact just because they posted a status."""
        msg_id = payload.get('message_id')
        if not msg_id:
            return self.browse()
        existing = self.search([
            ('wa_account_id', '=', account.id),
            ('wa_message_id', '=', msg_id),
        ], limit=1)
        if existing:
            return existing
        number = (payload.get('from') or '').lstrip('+').strip()
        partner = self.env['res.partner'].browse()
        if number:
            formatted = wa_phone_format(self.env, number) or number
            partner = self.env['res.partner'].sudo().search(
                ['|', ('phone', '=', formatted), ('phone', 'like', number)],
                limit=1)
        media_type = payload.get('media_type') or 'text'
        ts = payload.get('timestamp')
        try:
            posted_at = (datetime.utcfromtimestamp(int(ts))
                         if ts else fields.Datetime.now())
        except (ValueError, TypeError, OSError):
            posted_at = fields.Datetime.now()
        vals = {
            'wa_account_id': account.id,
            'user_id': account.user_id.id or False,
            'team_id': account.team_id.id or False,
            'partner_id': partner.id or False,
            'contact_number': number,
            'sender_name': payload.get('sender_name') or (partner.name or False),
            'media_type': media_type,
            'body_text': payload.get('text') or '',
            'wa_message_id': msg_id,
            'wa_participant': payload.get('participant') or '',
            'posted_at': posted_at,
            'expires_at': posted_at + timedelta(hours=24),
        }
        b64 = payload.get('attachment_b64')
        if b64 and media_type in ('image', 'video'):
            attachment = self.env['ir.attachment'].sudo().create({
                'name': 'status.%s' % media_type,
                'datas': b64,
                'mimetype': payload.get('mimetype') or (
                    'image/jpeg' if media_type == 'image' else 'video/mp4'),
                'res_model': 'owa.status.update',
            })
            vals['attachment_id'] = attachment.id
            if media_type == 'image':
                vals['media_image'] = b64
        rec = self.create(vals)
        if rec.attachment_id:
            rec.attachment_id.res_id = rec.id
        return rec

    @api.model
    def _cron_expire(self):
        """Drop statuses past their 24h window and purge their media."""
        now = fields.Datetime.now()
        stale = self.search([('expires_at', '<', now)])
        stale.mapped('attachment_id').unlink()
        stale.unlink()
