"""Phase G: WhatsApp Communities — umbrella linking related groups.

A Community is a parent JID (also ending in @g.us) that aggregates child
groups. Membership / admin events are still per-group, but communities
let you push announcements that fan out to every linked group.

This model mirrors :class:`OwaGroupSession` but at the community level.
We don't materialise child groups here — instead the existing
:class:`OwaGroupSession` already represents each member group, and the
``parent_community_jid`` linkage is maintained on the community side.
"""
import logging
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


class OwaCommunity(models.Model):
    _name = 'owa.community'
    _description = 'WhatsApp Community'
    _order = 'subject, id'
    _rec_name = 'subject'

    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account",
        required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, index=True)
    community_jid = fields.Char(string='WhatsApp Community JID', index=True)
    subject = fields.Char(required=True)
    description = fields.Text()
    owner_jid = fields.Char(string='Owner JID')
    creation_ts = fields.Integer(string='Created (epoch)')
    created_at = fields.Datetime(
        string="Created", compute='_compute_created_at',
        help="When this community was created on WhatsApp.")
    invite_url = fields.Char(
        string="Invite Link", readonly=True, copy=False,
        help="WhatsApp community invite link. Click 'Get Invite Link' to fetch "
             "it, then use the copy button to share it.")
    bot_state = fields.Selection([
        ('active',   'Active'),
        ('disabled', 'Disabled'),
    ], default='active', index=True)
    image_1920 = fields.Image(string='Picture')
    linked_group_count = fields.Integer(
        compute='_compute_linked_group_count',
        string='Linked groups')
    # Communities are messaged through their "Announcements" group — a normal
    # @g.us group. We mirror that group's Discuss channel onto the community so
    # it works like a group: Open Chat, members, invite link, picture, etc. all
    # operate on this JID via the existing group endpoints (no sidecar change).
    announce_group_jid = fields.Char(
        string='Announcements Group JID', index=True,
        help="The community's Announcements group — where announcements are "
             "sent and members live. Resolved on Refresh.")
    channel_id = fields.Many2one(
        'discuss.channel', string='Announcements Channel', ondelete='set null',
        help="Discuss channel mirroring the community's Announcements group. "
             "Messages sent here go to the whole community.")
    participant_count = fields.Integer(string='Members', default=0)
    member_partner_ids = fields.Many2many(
        'res.partner', string='Members',
        compute='_compute_member_partner_ids',
        help="People in the community's Announcements conversation — resolved "
             "on Refresh (members whose WhatsApp number is private are skipped).")

    _uniq_account_community_jid = models.Constraint(
        'unique(wa_account_id, community_jid)',
        'Only one community record per (account, community JID).',
    )

    def _compute_linked_group_count(self):
        Session = self.env['owa.group.session'].sudo()
        for rec in self:
            rec.linked_group_count = Session.search_count([
                ('wa_account_id', '=', rec.wa_account_id.id),
                ('parent_community_jid', '=', rec.community_jid or '__none__'),
            ])

    @api.depends('channel_id', 'channel_id.channel_member_ids.partner_id')
    def _compute_member_partner_ids(self):
        for rec in self:
            rec.member_partner_ids = (
                rec.channel_id.channel_member_ids.partner_id
                if rec.channel_id else False)

    @api.depends('creation_ts')
    def _compute_created_at(self):
        """Render the raw WhatsApp creation epoch (Unix seconds) as a normal
        datetime for the form (naive UTC, the way Odoo expects)."""
        for rec in self:
            ts = rec.creation_ts
            rec.created_at = (
                datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                if ts and ts > 0 else False)

    @staticmethod
    def _participant_digits(part):
        """Visible phone digits for a community participant, or '' when the
        number is hidden behind a privacy LID. Mirrors the import wizard:
        rc10 returns participants as {"id":"<lid>@lid",
        "phoneNumber":"<digits>@s.whatsapp.net", ...}."""
        if not isinstance(part, dict):
            return ''
        for key in ('phoneNumber', 'jid', 'pn', 'id'):
            val = part.get(key) or ''
            if '@s.whatsapp.net' in val:
                return ''.join(c for c in val.split('@')[0] if c.isdigit())
        return ''

    def _announce_jid(self):
        """The @g.us JID every community action operates on — the Announcements
        group when resolved, else the community JID itself (some WhatsApp
        representations make the community parent the announcement target)."""
        self.ensure_one()
        jid = self.announce_group_jid or self.community_jid
        if not jid:
            raise UserError(_(
                "Refresh the community first so its Announcements group is known."))
        return jid

    def _community_target(self):
        """The JID for community-LEVEL actions (invite link, picture): the
        community node itself, which carries the community's own invite and
        icon (distinct from the Announcements group's). Falls back to the
        announcements JID if the community node is somehow unknown."""
        self.ensure_one()
        return self.community_jid or self._announce_jid()

    def _apply_community_metadata(self, api):
        """Enrich this record from the community node's OWN metadata — subject,
        description and owner come straight from the community rather than being
        inferred from its Announcements group. The community metadata lookup is
        reliable on the upgraded transport; guarded so any build where it is
        unavailable degrades to the groups-feed values. (participant_count is
        intentionally left to the Announcements-group sync below, which owns it.)"""
        self.ensure_one()
        if not self.community_jid:
            return
        try:
            meta = api.get_community_metadata(self.community_jid)
        except Exception:
            _logger.debug(
                "communityMetadata unavailable for %s", self.community_jid)
            return
        if not isinstance(meta, dict):
            return
        vals = {}
        if meta.get('subject'):
            vals['subject'] = meta['subject']
        desc = meta.get('desc')
        if desc is None:
            desc = meta.get('description')
        if desc is not None:
            vals['description'] = desc or ''
        owner = meta.get('subjectOwner') or meta.get('owner')
        if owner:
            vals['owner_jid'] = owner
        if vals:
            self.write(vals)

    def _set_invite_url(self, code):
        """Store the fetched invite link on ``invite_url`` so the form shows it
        with a copy button (toasts can't be copied / are truncated). (#wa-invite)"""
        from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
        self.ensure_one()
        if not code:
            raise UserError(_(
                "WhatsApp returned no invite code. The bot usually needs to be "
                "an admin of this community to fetch its invite link."))
        self.invite_url = BaileysApi.INVITE_URL_PREFIX + code

    def _refresh_links_and_members(self, api, groups=None):
        """Resolve the community's Announcements group + linked groups + members,
        creating/linking the Discuss channel so the community behaves like a
        group. Derives links from the GROUPS feed (community endpoints are
        unreliable on the pinned build) — pass the cached groups list to avoid
        re-fetching per community."""
        self.ensure_one()
        account = self.wa_account_id
        GroupSession = self.env['owa.group.session'].sudo()
        Partner = self.env['res.partner'].sudo()
        Channel = self.env['discuss.channel'].sudo()
        cjid = self.community_jid
        # Prefer the community node's own metadata for subject/description/owner.
        self._apply_community_metadata(api)
        if groups is None:
            groups = api.fetch_all_groups() or []
        announce_meta = None
        for g in [grp for grp in groups if grp.get('linkedParent') == cjid]:
            gjid = g.get('id') or g.get('jid')
            if not gjid:
                continue
            is_ann = bool(g.get('isCommunityAnnounce'))
            grow = GroupSession._upsert_from_metadata(
                account, g, link_create_channel=is_ann)
            if grow:
                grow.parent_community_jid = cjid
            if is_ann:
                announce_meta = g
                self.announce_group_jid = gjid
                if grow and grow.channel_id:
                    self.channel_id = grow.channel_id.id
        # Ensure a Discuss channel exists for the ANNOUNCEMENTS group (never the
        # community node JID itself — that is not a chat target, and creating a
        # channel for it produces a confusing duplicate with the same name) so
        # Open Chat / messaging works even before the first inbound message.
        if not self.channel_id and self.announce_group_jid:
            ajid = self.announce_group_jid
            ch = Channel.search([
                ('owa_account_id', '=', account.id),
                ('whatsapp_number', '=', ajid),
                ('channel_type', '=', 'whatsapp'),
            ], limit=1)
            if not ch:
                ch = Channel.create({
                    # Never seed the channel name with the raw JID. (#wa-name)
                    'name': self.subject or _("WhatsApp Community"),
                    'channel_type': 'whatsapp',
                    'whatsapp_number': ajid,
                    'owa_account_id': account.id,
                })
            self.channel_id = ch.id
        # Members = the Announcements group's participants (everyone in the
        # community). Resolve the visible ones and add them to the channel.
        parts = (announce_meta or {}).get('participants') or []
        if parts:
            self.participant_count = (announce_meta or {}).get('size') or len(parts)
            channel = self.channel_id.sudo()
            add = Partner.browse()
            for p in parts:
                digits = self._participant_digits(p)
                if not digits:
                    continue
                partner = Partner._find_or_create_from_wa_number(digits)
                if partner:
                    add |= partner
            missing = add - channel.channel_member_ids.partner_id
            if missing:
                channel.channel_member_ids = [
                    (0, 0, {'partner_id': p.id}) for p in missing]

    def _ensure_owa_admin(self):
        """Server-side guard for community-management operations that change
        the SHARED WhatsApp account's communities (leave, rename, refresh).
        View ``groups=`` only hides buttons; these methods hit the live sidecar
        without writing owa.account, so a non-admin could otherwise invoke them
        via RPC. Mirrors owa.group.session._ensure_owa_admin. (#F042)"""
        if not self.env.su and not self.env.user.has_group(
                'open_whatsapp_connector.group_owa_admin'):
            raise AccessError(_(
                "Only WhatsApp Administrators can manage WhatsApp communities."))

    def _api(self):
        self.ensure_one()
        if not self.wa_account_id:
            raise UserError(_("Community has no linked WhatsApp account."))
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
        vals = {
            'community_jid': jid,
            'subject': metadata.get('subject') or jid,
            'description': metadata.get('desc') or metadata.get('description') or '',
            'owner_jid': metadata.get('subjectOwner') or metadata.get('owner') or '',
            'creation_ts': metadata.get('creation') or 0,
        }
        existing = self.search([
            ('wa_account_id', '=', account.id),
            ('community_jid', '=', jid),
        ], limit=1)
        if existing:
            existing.write(vals)
            row = existing
        else:
            vals['wa_account_id'] = account.id
            row = self.create(vals)
        # Pull the community picture into image_1920 (and the linked Announcements
        # channel avatar when already linked) — force a re-fetch on the per-record
        # Refresh, else only backfill when missing. (#wa-fetch-pic)
        if row and (force_picture or not row.image_1920):
            b64 = account._fetch_picture_b64(jid)
            if b64:
                row.image_1920 = b64
                if row.channel_id:
                    row.channel_id.sudo().image_128 = b64
        return row

    @api.model
    def _sync_account_communities(self, account, api=None, groups=None):
        """Discover + sync one account's communities from the GROUPS feed and
        return the number synced. Communities surface there as the node flagged
        isCommunity=True (its Announcements group is isCommunityAnnounce and
        member groups carry linkedParent=<communityJid>). The dedicated
        community endpoints are unreliable on the pinned WhatsApp-web build
        (return empty / 500), so derive everything from the working groups feed.
        Reused by the manual Refresh button AND the history-import job. Pass a
        cached ``groups`` list to avoid re-fetching. (#community)"""
        if api is None:
            api = account._get_baileys_api()
        if groups is None:
            groups = api.fetch_all_groups() or []
        comm_metas = [g for g in groups if g.get('isCommunity')]
        for meta in comm_metas:
            row = self._upsert_from_metadata(account, meta)
            try:
                row._refresh_links_and_members(api, groups)
            except Exception:
                _logger.exception(
                    "community link/member sync failed for %s",
                    meta.get('id') or meta.get('jid'))
        return len(comm_metas)

    # NOT @api.model: list-header <button type="object"> passes the selected
    # resIds positionally; @api.model would forward it as an extra arg and crash
    # ("takes 1 positional argument but 2 were given"). _call_kw_multi strips the
    # ids and the body operates account-wide via self.env regardless. (#wa-list-btn)
    def action_refresh_all(self):
        self._ensure_owa_admin()
        Account = self.env['owa.account'].sudo()
        accounts = Account.search([('session_state', '=', 'connected')])
        if not accounts:
            raise UserError(_("No connected WhatsApp accounts."))
        total = 0
        errors = []
        for account in accounts:
            try:
                total += self._sync_account_communities(account)
            except Exception as exc:
                _logger.exception("Community refresh failed for %s", account.name)
                errors.append("%s: %s" % (account.name, exc))
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp Communities"),
                'message': _("Refreshed %s community(ies).") % total,
                'type': 'warning' if errors else 'success',
                'sticky': bool(errors),
            },
        }

    def action_leave(self):
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.leave_community(rec.community_jid)
            rec.bot_state = 'disabled'
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("Community"),
                       'message': _("Left %s community(ies).") % len(self),
                       'type': 'success'},
        }

    def action_set_subject(self):
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.update_community_subject(rec.community_jid, rec.subject or '')
        return True

    def action_refresh_metadata(self):
        """Per-record refresh: re-pull this community's metadata + relink its
        Announcements group, channel and members."""
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            groups = api.fetch_all_groups() or []
            meta = next(
                (g for g in groups if g.get('isCommunity')
                 and (g.get('id') or g.get('jid')) == rec.community_jid), None)
            if meta:
                rec._upsert_from_metadata(rec.wa_account_id, meta, force_picture=True)
            rec._refresh_links_and_members(api, groups)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("WhatsApp Community"),
                       'message': _("Refreshed."), 'type': 'success'},
        }

    def action_open_chat(self):
        """Open the community's Announcements conversation in Discuss — messages
        sent there reach the whole community."""
        self.ensure_one()
        if not self.channel_id:
            raise UserError(_(
                "No conversation yet — click Refresh to link the community's "
                "Announcements group."))
        # Open the live conversation in the Discuss app — NOT the discuss.channel
        # backend form (that shows channel settings, not the chat). Reuse the
        # version-aware helper the Conversations view already uses. (#wa-open-chat)
        return self.channel_id.action_open_in_discuss()

    def action_get_invite_link(self):
        self.ensure_one()
        api = self._api()
        self._set_invite_url(api.get_invite_code(self._community_target()))

    def action_revoke_invite_link(self):
        self.ensure_one()
        self._ensure_owa_admin()
        api = self._api()
        self._set_invite_url(api.revoke_invite_code(self._community_target()))

    def action_push_picture(self):
        """Push the local ``image_1920`` to WhatsApp as the community picture
        (operates on the community node itself). Empty field removes it."""
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.update_group_picture(rec._community_target(), rec.image_1920 or None)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("Community picture"),
                       'message': _("Pushed to WhatsApp."), 'type': 'success'},
        }

    def action_toggle_announce(self):
        """Toggle whether only admins can post in the Announcements group."""
        self._ensure_owa_admin()
        for rec in self:
            api = rec._api()
            api.update_group_setting(rec._announce_jid(), 'announcement')
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("WhatsApp Community"),
                       'message': _("Announcements set to admins-only."),
                       'type': 'success'},
        }

    def action_open_linked_groups(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Linked Groups"),
            'res_model': 'owa.group.session',
            'view_mode': 'list,form',
            'domain': [
                ('wa_account_id', '=', self.wa_account_id.id),
                ('parent_community_jid', '=', self.community_jid),
            ],
            'context': {
                'default_wa_account_id': self.wa_account_id.id,
                'default_parent_community_jid': self.community_jid,
            },
        }

    @api.model
    def action_open_create_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create WhatsApp Community"),
            'res_model': 'owa.community.create.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
        }
