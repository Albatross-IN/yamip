"""Phase 25C: WhatsApp Status / Stories — text/image/video posted to all
contacts for 24h via the sidecar's STATUS_BROADCAST_JID route."""
import base64
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OwaStatusBroadcast(models.Model):
    _name = 'owa.status.broadcast'
    _description = 'WhatsApp Status Broadcast'
    _inherit = ['mail.thread']
    _order = 'create_date desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account",
        required=True, ondelete='cascade', tracking=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, index=True)
    # Per-user / per-team ownership (visibility gated by toggle-able ir.rules).
    user_id = fields.Many2one(
        'res.users', string="Responsible", index=True,
        default=lambda self: self.env.user, tracking=True)
    team_id = fields.Many2one(
        'crm.team', string="Sales Team", index=True,
        default=lambda self: self.env['crm.team']._get_default_team_id(
            user_id=self.env.uid))
    body_text = fields.Text(string="Caption / text body")
    attachment_id = fields.Many2one('ir.attachment', string="Image/Video")
    # Thumbnail rendered on the My Status kanban/form — mirrors the image the
    # attachment carries so image Stories preview inline (parity with the
    # Recent Updates view). Computed, not stored: the bytes already live on the
    # attachment.
    media_image = fields.Binary(string="Preview", compute='_compute_media_image')
    media_type = fields.Selection([
        ('text',  'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
    ], default='text', required=True, tracking=True)
    state = fields.Selection([
        ('draft',     'Draft'),
        ('queued',    'Queued'),
        ('posted',    'Posted'),
        ('expired',   'Expired'),
        ('revoked',   'Deleted'),
        ('error',     'Error'),
    ], default='draft', tracking=True, index=True)
    posted_at = fields.Datetime(readonly=True)
    expires_at = fields.Datetime(readonly=True)
    error_message = fields.Char(readonly=True)
    # WhatsApp message id of the posted status — needed to revoke (delete) it
    # from every contact's phone, not just remove the Odoo row. (#status-revoke)
    wa_message_id = fields.Char(string="WhatsApp Message ID", readonly=True, copy=False)

    @api.depends('body_text', 'media_type', 'create_date')
    def _compute_name(self):
        for rec in self:
            preview = (rec.body_text or '')[:40]
            rec.name = f"[{rec.media_type}] {preview or _('(no text)')}"

    @api.depends('attachment_id', 'attachment_id.datas', 'media_type')
    def _compute_media_image(self):
        for rec in self:
            if rec.media_type == 'image' and rec.attachment_id and rec.attachment_id.datas:
                rec.media_image = rec.attachment_id.datas
            else:
                rec.media_image = False

    def action_post(self):
        from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
        for rec in self:
            if rec.wa_account_id._account_messaging_blocked():
                raise UserError(_(
                    "This WhatsApp account is pending administrator approval and "
                    "cannot send messages yet."))
            try:
                api = BaileysApi(rec.wa_account_id)
                # statusJidList must be non-empty for the broadcast to be
                # visible to anyone other than the sender — gather the JIDs of
                # every DM channel we have on this account so the status shows
                # up for the user's existing WhatsApp contacts.
                channels = self.env['discuss.channel'].sudo().search([
                    ('owa_account_id', '=', rec.wa_account_id.id),
                    ('whatsapp_number', '!=', False),
                ])
                recipients = sorted({
                    (ch.whatsapp_number or '').lstrip('+').strip()
                    for ch in channels if ch.whatsapp_number
                })
                payload = {
                    'account_id': api._account_key(),
                    'type': rec.media_type,
                    'text': rec.body_text or '',
                    'recipients': recipients,
                }
                if rec.attachment_id:
                    payload['attachment_b64'] = (rec.attachment_id.datas or b'').decode('ascii')
                    payload['mimetype'] = rec.attachment_id.mimetype or 'application/octet-stream'
                resp = api._request('POST', '/status/broadcast', json=payload)
                rec.write({
                    'state': 'posted',
                    'wa_message_id': (resp or {}).get('message_id') or False,
                    'posted_at': fields.Datetime.now(),
                    'expires_at': fields.Datetime.now() + timedelta(hours=24),
                    'error_message': False,
                })
            except Exception as e:
                _logger.exception("status broadcast post failed")
                rec.write({'state': 'error', 'error_message': str(e)[:200]})

    def action_revoke(self):
        """Delete a posted status from WhatsApp (revoke it from contacts'
        phones), then mark the row 'revoked'. WhatsApp has no 'edit status';
        deleting + re-posting is the only way to change one."""
        from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
        for rec in self:
            if rec.state == 'posted' and rec.wa_message_id:
                try:
                    BaileysApi(rec.wa_account_id).revoke_status(rec.wa_message_id)
                except Exception as e:
                    _logger.warning("status revoke failed for %s: %s", rec.id, e)
            rec.state = 'revoked'
        return True

    def action_repost(self):
        """WhatsApp can't edit a live status — clone this one to a fresh draft
        and post it again (optionally revoking the old one first)."""
        self.ensure_one()
        clone = self.copy({'state': 'draft', 'wa_message_id': False})
        clone.action_post()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'owa.status.broadcast',
            'res_id': clone.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ── Ingest own phone-posted statuses (from /webhook/story, from_me) ──
    @api.model
    def _ingest_own_status(self, account, payload):
        """Log a Status (Story) the user posted directly from their phone / another
        linked device so it shows under My Status. Idempotent on
        (account, message_id) — this also de-dupes the echo of statuses posted
        THROUGH Odoo, whose row + wa_message_id already exist from action_post."""
        msg_id = payload.get('message_id')
        if not msg_id:
            return self.browse()
        existing = self.sudo().search([
            ('wa_account_id', '=', account.id),
            ('wa_message_id', '=', msg_id),
        ], limit=1)
        if existing:
            return existing
        media_type = payload.get('media_type') or 'text'
        if media_type not in ('text', 'image', 'video'):
            media_type = 'text'
        ts = payload.get('timestamp')
        try:
            posted_at = (datetime.utcfromtimestamp(int(ts))
                         if ts else fields.Datetime.now())
        except (ValueError, TypeError, OSError):
            posted_at = fields.Datetime.now()
        rec = self.sudo().create({
            'wa_account_id': account.id,
            'company_id': account.company_id.id,
            'user_id': account.user_id.id or False,
            'team_id': account.team_id.id or False,
            'media_type': media_type,
            'body_text': payload.get('text') or '',
            'state': 'posted',
            'wa_message_id': msg_id,
            'posted_at': posted_at,
            'expires_at': posted_at + timedelta(hours=24),
        })
        b64 = payload.get('attachment_b64')
        if b64 and media_type in ('image', 'video'):
            attachment = self.env['ir.attachment'].sudo().create({
                'name': 'status.%s' % media_type,
                'datas': b64,
                'mimetype': payload.get('mimetype') or (
                    'image/jpeg' if media_type == 'image' else 'video/mp4'),
                'res_model': 'owa.status.broadcast',
                'res_id': rec.id,
            })
            rec.attachment_id = attachment.id
        return rec

    @api.model
    def _cron_expire(self):
        now = fields.Datetime.now()
        self.search([
            ('state', '=', 'posted'),
            ('expires_at', '<', now),
        ]).write({'state': 'expired'})
