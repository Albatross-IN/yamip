"""Phase G: WhatsApp Newsletters / Channels.

A Newsletter is a one-to-many broadcast (writer → followers). The
connected account can either own one (and post to it) or follow others'
newsletters to ingest broadcasts as a special discuss.channel.

JIDs end with ``@newsletter`` and behave differently from group / DM
JIDs in the gateway — handled separately to avoid muddying the group code.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OwaNewsletter(models.Model):
    _name = 'owa.newsletter'
    _description = 'WhatsApp Newsletter / Channel'
    _order = 'name, id'

    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account",
        required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, index=True)
    newsletter_jid = fields.Char(string='Newsletter JID', index=True)
    name = fields.Char(required=True)
    description = fields.Text()
    role = fields.Selection([
        ('owner',     'Owner'),
        ('admin',     'Admin'),
        ('subscriber','Subscriber'),
        ('unknown',   'Unknown'),
    ], default='unknown')
    subscriber_count = fields.Integer(default=0)
    muted = fields.Boolean(default=False)
    image_1920 = fields.Image(string='Picture')
    # The Discuss conversation that mirrors this channel's posts. Mirrors
    # owa.community.channel_id; lets imported channel history land somewhere
    # and drives the discuss.channel.whatsapp_kind = 'channel' classification.
    channel_id = fields.Many2one(
        'discuss.channel', string='Channel Conversation', ondelete='set null',
        help="Discuss conversation mirroring this WhatsApp channel's posts.")

    _uniq_account_newsletter_jid = models.Constraint(
        'unique(wa_account_id, newsletter_jid)',
        'Only one newsletter record per (account, JID).',
    )

    def _api(self):
        self.ensure_one()
        if not self.wa_account_id:
            raise UserError(_("Newsletter has no linked WhatsApp account."))
        if self.wa_account_id.session_state != 'connected':
            raise UserError(_("WhatsApp account is not connected."))
        return self.wa_account_id._get_baileys_api()

    @api.model
    def _upsert_from_metadata(self, account, metadata, force_picture=False):
        if not metadata:
            return self.browse()
        jid = metadata.get('id') or metadata.get('jid')
        if not jid:
            return self.browse()
        # The gateway returns the newsletter shape inconsistently — sometimes
        # everything is on the outer dict, sometimes a nested ``metadata`` key
        # holds richer fields. Merge so siblings of the nested dict don't get
        # silently dropped, then resolve thread_metadata identically.
        flat = dict(metadata)
        nested = metadata.get('metadata') or {}
        if isinstance(nested, dict):
            flat.update(nested)
        thread = flat.get('thread_metadata') if isinstance(flat.get('thread_metadata'), dict) else flat
        vals = {
            'newsletter_jid': jid,
            'name': thread.get('name') or flat.get('name') or jid,
            'description': thread.get('description') or flat.get('description') or '',
            'subscriber_count': thread.get('subscribers_count')
                or flat.get('subscriber_count') or 0,
            'role': (flat.get('role') or 'unknown').lower(),
        }
        existing = self.search([
            ('wa_account_id', '=', account.id),
            ('newsletter_jid', '=', jid),
        ], limit=1)
        if existing:
            existing.write(vals)
            row = existing
        else:
            vals['wa_account_id'] = account.id
            row = self.create(vals)
        # Pull the channel picture into image_1920 (best-effort — profile-picture
        # lookup may not be supported for @newsletter JIDs on every build). Force
        # a re-fetch when asked, else only backfill when missing. (#wa-fetch-pic)
        if row and (force_picture or not row.image_1920):
            b64 = account._fetch_picture_b64(jid)
            if b64:
                row.image_1920 = b64
                if row.channel_id:
                    row.channel_id.sudo().image_128 = b64
        return row

    def _ensure_channel(self):
        """Find or create the Discuss conversation that mirrors this channel's
        posts and link it, so imported history has somewhere to land. Mirrors
        owa.community's bare-channel creation; never names the channel after the
        raw @newsletter JID. Returns the channel. (#wa-channel-import)"""
        self.ensure_one()
        account = self.wa_account_id
        Channel = self.env['discuss.channel'].sudo()
        if self.channel_id:
            return self.channel_id
        name = self.name if (self.name and '@' not in self.name) else _("WhatsApp Channel")
        ch = Channel.search([
            ('owa_account_id', '=', account.id),
            ('whatsapp_number', '=', self.newsletter_jid),
            ('channel_type', '=', 'whatsapp'),
        ], limit=1)
        if not ch:
            ch = Channel.create({
                'name': name,
                'channel_type': 'whatsapp',
                'whatsapp_number': self.newsletter_jid,
                'owa_account_id': account.id,
            })
        elif '@' in (ch.name or ''):
            ch.write({'name': name})
        self.channel_id = ch.id
        return ch

    def action_follow(self):
        for rec in self:
            api = rec._api()
            api.follow_newsletter(rec.newsletter_jid)
            rec.role = 'subscriber'
        return True

    def action_unfollow(self):
        for rec in self:
            api = rec._api()
            api.unfollow_newsletter(rec.newsletter_jid)
        return True

    def action_mute(self):
        for rec in self:
            api = rec._api()
            api.mute_newsletter(rec.newsletter_jid, True)
            rec.muted = True
        return True

    def action_unmute(self):
        for rec in self:
            api = rec._api()
            api.mute_newsletter(rec.newsletter_jid, False)
            rec.muted = False
        return True

    def action_push_picture(self):
        for rec in self:
            api = rec._api()
            api.update_newsletter(rec.newsletter_jid, picture_data=rec.image_1920 or '')
        return True

    def action_push_metadata(self):
        for rec in self:
            api = rec._api()
            api.update_newsletter(
                rec.newsletter_jid,
                name=rec.name,
                description=rec.description or '',
            )
        return True

    @api.model
    def action_open_create_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create WhatsApp Newsletter"),
            'res_model': 'owa.newsletter.create.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
        }

    # NOT @api.model — list-header button passes resIds positionally; a recordset
    # method lets _call_kw_multi strip them. See group action_refresh_all. (#wa-list-btn)
    def action_open_subscribe_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Subscribe to WhatsApp Newsletter"),
            'res_model': 'owa.newsletter.subscribe.wizard',
            'view_mode': 'form',
            'target': 'new',
        }
