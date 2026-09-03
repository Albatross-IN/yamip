"""Phase 25D: per-group session state record. Replaces ad-hoc per-channel
state with one consolidated row keyed on the discuss.channel."""
import logging
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi

_logger = logging.getLogger(__name__)

_VALID_SETTINGS = ('announcement', 'not_announcement', 'locked', 'unlocked')


class OwaGroupSession(models.Model):
    _name = 'owa.group.session'
    _description = 'WhatsApp Group Session State'
    _order = 'last_message_at desc, id desc'
    _rec_name = 'channel_id'

    channel_id = fields.Many2one(
        'discuss.channel', ondelete='cascade', index=True,
        help='The Discuss channel that mirrors this WhatsApp group. Optional '
             'so we can persist group metadata received via groups.upsert '
             'before any message has actually arrived in the group.')
    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account",
        required=True, ondelete='cascade', index=True,
        help='WhatsApp account this group belongs to. Set explicitly so '
             'metadata rows can exist before any channel is linked.')
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, index=True)
    last_message_at = fields.Datetime()
    last_inbound_at = fields.Datetime()
    last_outbound_at = fields.Datetime()
    participant_count = fields.Integer(default=0)
    member_partner_ids = fields.Many2many(
        'res.partner', string="Members",
        compute='_compute_member_partner_ids',
        help="People in this group's Odoo conversation — imported group "
             "members plus anyone who has messaged. Members who keep their "
             "WhatsApp number private appear here once they message you.")
    # Phase B1: WhatsApp group metadata, kept fresh by the groups.upsert /
    # groups.update events the sidecar forwards.
    group_jid = fields.Char(string='WhatsApp Group JID', index=True)
    group_subject = fields.Char(string='WhatsApp Group Name')
    group_description = fields.Text(string='WhatsApp Group Description')
    group_owner_jid = fields.Char(string='WhatsApp Group Owner JID')
    group_announce_only = fields.Boolean(
        string='WhatsApp Announce-only',
        help='When True, only admins can post messages in this WhatsApp group.')
    group_creation_ts = fields.Integer(string='WhatsApp Group Creation (epoch)')
    group_created_at = fields.Datetime(
        string="Created", compute='_compute_group_created_at',
        help="When this group was created on WhatsApp (from the raw "
             "creation timestamp).")
    invite_url = fields.Char(
        string="Invite Link", readonly=True, copy=False,
        help="WhatsApp invite link. Click 'Get Invite Link' to fetch it, then "
             "use the copy button to share it.")
    bot_state = fields.Selection([
        ('active',   'Active'),
        ('idle',     'Idle'),
        ('disabled', 'Disabled'),
    ], default='active', index=True)
    state_json = fields.Text(default='{}')
    # Phase G — local-only buffer for the WhatsApp group profile picture.
    # We don't read the live picture down (the gateway returns a URL we'd need
    # to fetch); we only push uploads up via action_push_picture.
    image_1920 = fields.Image(string="Group Picture")
    # Phase G — Communities support: when set, this group is part of a
    # parent community. Maintained by the Community refresh job; users
    # don't typically edit it directly.
    parent_community_jid = fields.Char(string='Parent Community JID', index=True)

    # `unique(channel_id)` would forbid multiple rows where channel_id
    # is NULL; in Postgres multiple NULLs are accepted by a UNIQUE
    # constraint so this still enforces "one row per linked channel".
    _uniq_channel_session = models.Constraint(
        'unique(channel_id)',
        'Only one session record per channel.',
    )
    _uniq_account_group_jid = models.Constraint(
        'unique(wa_account_id, group_jid)',
        'Only one session record per (account, WhatsApp group JID).',
    )

    @api.depends('channel_id', 'channel_id.channel_member_ids.partner_id')
    def _compute_member_partner_ids(self):
        for rec in self:
            rec.member_partner_ids = (
                rec.channel_id.channel_member_ids.partner_id
                if rec.channel_id else False)

    @api.depends('channel_id', 'channel_id.channel_member_ids.partner_id')
    def _compute_member_partner_ids(self):
        for rec in self:
            rec.member_partner_ids = (
                rec.channel_id.channel_member_ids.partner_id
                if rec.channel_id else False)

    @api.depends('group_creation_ts')
    def _compute_group_created_at(self):
        """Render the raw WhatsApp creation epoch (Unix seconds) as a normal
        datetime for the form. Stored as naive UTC, the way Odoo expects."""
        for rec in self:
            ts = rec.group_creation_ts
            rec.group_created_at = (
                datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                if ts and ts > 0 else False)

    @api.model
    def _record_group_meta(self, account, kind, group):
        """Phase B1 — upsert a row mirroring the sidecar's groups.upsert /
        groups.update payload. ``group`` is the dict the sidecar forwards.

        We look up the matching discuss.channel by (account, group_jid) and
        keep our row in sync. If no channel exists yet for this group (the
        bot was added but no one has messaged since), we still persist the
        metadata indexed by group_jid so a later inbound can attach.
        """
        jid = (group or {}).get('id')
        if not jid:
            return False
        Channel = self.env['discuss.channel'].sudo()
        channel = Channel.search([
            ('owa_account_id', '=', account.id),
            ('whatsapp_number', '=', jid),
            ('channel_type', '=', 'whatsapp'),
        ], limit=1)
        vals = {
            'group_jid': jid,
            'group_subject': group.get('subject'),
            'group_description': group.get('desc'),
            'group_owner_jid': group.get('subjectOwner') or group.get('owner'),
            'group_announce_only': bool(group.get('announce')),
            'group_creation_ts': group.get('creation') or 0,
            'participant_count': group.get('size') or 0,
        }
        # Drop None values so partial 'update' events don't blank existing data.
        vals = {k: v for k, v in vals.items() if v is not None}
        # Account-scoped lookup so two accounts in the same group don't
        # overwrite each other's metadata row.
        existing = self.search([
            ('wa_account_id', '=', account.id),
            ('group_jid', '=', jid),
        ], limit=1)
        if existing:
            if channel and not existing.channel_id:
                vals['channel_id'] = channel.id
            # Idempotency: only write fields whose value actually changed.
            # groups.update fires repeatedly with identical metadata; an
            # unconditional write bumps write_date on every event, producing a
            # redundant-UPDATE storm that collides with concurrent community/
            # group refreshes under serializable isolation (SerializationFailure).
            changed = {}
            for fname, newval in vals.items():
                curval = existing.channel_id.id if fname == 'channel_id' else existing[fname]
                if curval != newval:
                    changed[fname] = newval
            if changed:
                existing.write(changed)
            row = existing
        else:
            create_vals = dict(vals)
            create_vals['wa_account_id'] = account.id
            if channel:
                create_vals['channel_id'] = channel.id
            else:
                _logger.info(
                    "owa.group.session: %s for jid=%s; persisting metadata "
                    "without channel until the first message arrives",
                    kind, jid,
                )
            row = self.create(create_vals)
        # Keep the channel name in sync with the live WhatsApp group subject.
        # Never leave the raw JID as the visible name: rename to the real
        # subject when known, else to a friendly placeholder. (#wa-name)
        if channel:
            if row.group_subject and channel.name != row.group_subject:
                channel.write({'name': row.group_subject})
            elif not row.group_subject and '@' in (channel.name or ''):
                channel.write(
                    {'name': channel._owa_placeholder_name(channel.whatsapp_number)})
        return row

    # ── Phase F — group management actions (create / leave / settings / invite) ─

    def _ensure_owa_admin(self):
        """Server-side guard for group-management operations that change the
        SHARED WhatsApp account's groups (leave, revoke invite, rename, change
        description / announce-only, push picture, refresh from WhatsApp). View
        ``groups=`` only hides buttons; these methods talk to the live sidecar
        without writing owa.account, so a non-admin agent could otherwise invoke
        them via RPC. Agent-level chat actions (pin/mute/archive) are NOT gated.
        (base.group_system implies the group, so Settings admins pass.)"""
        if not self.env.su and not self.env.user.has_group(
                'open_whatsapp_connector.group_owa_admin'):
            raise AccessError(_(
                "Only WhatsApp Administrators can manage WhatsApp groups."))

    def _api(self):
        self.ensure_one()
        if not self.wa_account_id:
            raise UserError(_("Group has no linked WhatsApp account."))
        if self.wa_account_id.session_state != 'connected':
            raise UserError(_("WhatsApp account %s is not connected.") % self.wa_account_id.name)
        return self.wa_account_id._get_baileys_api()

    @api.model
    def _upsert_from_metadata(self, account, metadata, link_create_channel=True,
                              force_picture=False):
        """Upsert a session row from a ``GroupMetadata`` dict, also
        creating the linked ``discuss.channel`` when missing — used by the
        explicit Create / Join / Refresh flows where there's no inbound
        message to seed the channel through ``_record_group_meta``.

        ``force_picture`` re-downloads the group photo even when one is already
        stored (per-row form Refresh); the default only backfills a missing
        picture (bulk Refresh / import) so we don't re-download every group on
        every list refresh."""
        if not metadata:
            return self.browse()
        jid = metadata.get('id') or metadata.get('jid')
        if not jid:
            return self.browse()
        row = self._record_group_meta(account, 'manual', {
            'id': jid,
            'subject': metadata.get('subject'),
            'desc': metadata.get('desc') or metadata.get('description'),
            'subjectOwner': metadata.get('subjectOwner') or metadata.get('owner'),
            'announce': bool(metadata.get('announce')),
            'creation': metadata.get('creation'),
            'size': metadata.get('size') or len(metadata.get('participants') or []),
        })
        if link_create_channel and row and not row.channel_id:
            Channel = self.env['discuss.channel'].sudo()
            channel = Channel.create({
                # Never seed the channel name with the raw JID — use the group
                # subject when present, else a friendly placeholder. (#wa-name)
                'name': metadata.get('subject') or Channel._owa_placeholder_name(jid),
                'channel_type': 'whatsapp',
                'whatsapp_number': jid,
                'owa_account_id': account.id,
            })
            row.write({'channel_id': channel.id})
        # Pull the group picture into image_1920 AND the linked Discuss channel
        # avatar (image_128) so it shows in the sidebar too. Force a re-fetch on
        # the per-row form Refresh; otherwise only backfill a missing one so we
        # don't re-download every group on every list refresh. (#wa-fetch-pic)
        if row and (force_picture or not row.image_1920):
            b64 = account._fetch_picture_b64(jid)
            if b64:
                row.image_1920 = b64
                if row.channel_id:
                    row.channel_id.sudo().image_128 = b64
        return row

    # NOT @api.model: this is a list-header <button type="object">, and Odoo
    # passes the selected resIds positionally. @api.model's _call_kw_model would
    # forward that as an extra arg ("takes 1 positional argument but 2 were
    # given"); as a plain recordset method _call_kw_multi strips the ids and the
    # body operates account-wide via self.env regardless of selection. (#wa-list-btn)
    def action_refresh_all(self):
        """Pull the live group list from the sidecar for every connected
        account, upserting rows. Returns a notification action describing the
        outcome."""
        self._ensure_owa_admin()
        Account = self.env['owa.account'].sudo()
        accounts = Account.search([('session_state', '=', 'connected')])
        if not accounts:
            raise UserError(_("No connected WhatsApp accounts."))
        total = 0
        errors = []
        for account in accounts:
            try:
                api = account._get_baileys_api()
                groups = api.fetch_all_groups()
                for meta in groups or []:
                    self._upsert_from_metadata(account, meta)
                total += len(groups or [])
            except Exception as exc:
                _logger.exception("Group refresh failed for %s", account.name)
                errors.append("%s: %s" % (account.name, exc))
        msg = _("Refreshed %s group(s) across %s account(s).") % (total, len(accounts))
        if errors:
            msg += "\n" + "\n".join(errors)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp Groups"),
                'message': msg,
                'type': 'warning' if errors else 'success',
                'sticky': bool(errors),
            },
        }

    def action_refresh_metadata(self):
        """Per-row refresh — fetches fresh metadata for one group only."""
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            groups = api.fetch_all_groups()
            target = next(
                (g for g in groups or [] if (g.get('id') or g.get('jid')) == rec.group_jid),
                None,
            )
            if target:
                self._upsert_from_metadata(rec.wa_account_id, target, force_picture=True)
        return True

    def action_leave_group(self):
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.leave_group(rec.group_jid)
            rec.bot_state = 'disabled'
            if rec.channel_id:
                rec.channel_id.sudo().write({'active': False})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp Group"),
                'message': _("Left %s group(s).") % len(self),
                'type': 'success',
            },
        }

    def _set_invite_url(self, code):
        """Store the fetched WhatsApp invite link on ``invite_url`` so the form
        shows it with a copy button (a toast can't be copied / is truncated).
        Returning nothing lets the form reload and render the field. (#wa-invite)"""
        self.ensure_one()
        if not code:
            raise UserError(_(
                "WhatsApp returned no invite code. The bot usually needs to be "
                "an admin of this group to fetch its invite link."))
        self.invite_url = BaileysApi.INVITE_URL_PREFIX + code

    def action_get_invite_link(self):
        self.ensure_one()
        api = self._api()
        self._set_invite_url(api.get_invite_code(self.group_jid))

    def action_revoke_invite_link(self):
        self.ensure_one()
        self._ensure_owa_admin()
        api = self._api()
        self._set_invite_url(api.revoke_invite_code(self.group_jid))

    def action_set_subject(self):
        """Pushes the current ``group_subject`` value to WhatsApp. The user
        edits the field inline in the form view, then clicks this button to
        commit. Avoids a tiny one-field wizard."""
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.update_group_subject(rec.group_jid, rec.group_subject or '')
            if rec.channel_id and rec.group_subject:
                rec.channel_id.sudo().write({'name': rec.group_subject})
        return True

    def action_set_description(self):
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.update_group_description(rec.group_jid, rec.group_description or '')
        return True

    def action_toggle_announce_only(self):
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            new = not rec.group_announce_only
            api.update_group_setting(
                rec.group_jid,
                'announcement' if new else 'not_announcement',
            )
            rec.group_announce_only = new
        return True

    def action_open_channel(self):
        self.ensure_one()
        if not self.channel_id:
            raise UserError(_("This group has no linked Discuss channel."))
        # Open the live conversation in the Discuss app — NOT the discuss.channel
        # backend form (that shows channel settings, not the chat). Reuse the
        # version-aware helper the Conversations view uses. (#wa-open-chat)
        return self.channel_id.action_open_in_discuss()

    def action_manage_participants(self):
        self.ensure_one()
        if not self.channel_id:
            raise UserError(_("This group has no linked Discuss channel — "
                              "managing participants is only possible after "
                              "the channel exists."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Manage WhatsApp Group Participants"),
            'res_model': 'owa.group.participant.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_channel_id': self.channel_id.id},
        }

    def action_push_picture(self):
        """Push the local ``image_1920`` field to WhatsApp as the group's
        profile picture. If the field is empty, removes the picture."""
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.update_group_picture(rec.group_jid, rec.image_1920 or None)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("Group picture"),
                       'message': _("Pushed to WhatsApp."),
                       'type': 'success'},
        }

    def action_chat_modify(self, action):
        for rec in self:
            api = rec._api()
            api.chat_modify(rec.group_jid, action)
        return True

    def action_chat_pin(self):
        return self.action_chat_modify('pin')

    def action_chat_unpin(self):
        return self.action_chat_modify('unpin')

    def action_chat_archive(self):
        return self.action_chat_modify('archive')

    def action_chat_unarchive(self):
        return self.action_chat_modify('unarchive')

    def action_chat_mute(self):
        return self.action_chat_modify('mute')

    def action_chat_unmute(self):
        return self.action_chat_modify('unmute')

    @api.model
    def action_open_create_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create WhatsApp Group"),
            'res_model': 'owa.group.create.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
        }

    # NOT @api.model — list-header button; see action_refresh_all. (#wa-list-btn)
    def action_open_join_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Join WhatsApp Group"),
            'res_model': 'owa.group.join.wizard',
            'view_mode': 'form',
            'target': 'new',
        }
