import datetime
import logging

import odoo
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format

_logger = logging.getLogger(__name__)

PAGE_LIMIT = 200            # history messages drained per page
MAX_PAGES_PER_RUN = 50      # bound a single cron run (incremental commit per page)
MAX_SYNC_POLLS = 60         # ~ how many cron ticks to wait for WhatsApp to stream
AVATAR_BATCH = 300          # contact photos fetched per cron tick (rest resume next tick)


class OwaImportJob(models.Model):
    """Background job that imports a connected WhatsApp account's contacts,
    conversations and recent messages into Odoo, draining the sidecar history
    buffer page-by-page with a live progress bar. See
    docs/superpowers/specs/2026-06-18-whatsapp-history-import-design.md.
    """
    _name = 'owa.import.job'
    _description = 'WhatsApp History Import Job'
    _order = 'id desc'

    account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account", required=True, ondelete='cascade')
    state = fields.Selection([
        ('queued', 'Queued'),
        ('syncing', 'Syncing from WhatsApp'),
        ('importing', 'Importing'),
        ('done', 'Done'),
        ('error', 'Failed'),
    ], string="Status", default='queued', required=True)
    progress = fields.Integer(string="Progress", default=0, help="0-100%")
    count_contacts = fields.Integer(string="Contacts", default=0)
    count_chats = fields.Integer(string="Conversations", default=0)
    count_messages = fields.Integer(string="Messages", default=0)
    count_communities = fields.Integer(string="Communities", default=0)
    count_channel_posts = fields.Integer(string="Channel posts", default=0)
    count_avatars = fields.Integer(string="Photos", default=0)
    import_contacts = fields.Boolean(default=True)
    import_chats = fields.Boolean(default=True)
    import_messages = fields.Boolean(default=True)
    import_communities = fields.Boolean(default=True)
    import_channels = fields.Boolean(default=True)
    import_avatars = fields.Boolean(default=False)
    # Communities + channel history come from live feeds (not the history
    # buffer), so we run that discovery exactly once per job. (#wa-import)
    structure_synced = fields.Boolean(default=False)
    # The contact-photo pass is batched + resumable across cron ticks; this
    # flips True once every photo-less contact has been processed. (#wa-fetch-pic)
    avatars_synced = fields.Boolean(default=False)
    cursor = fields.Integer(default=0)
    sync_polls = fields.Integer(default=0)
    log = fields.Text(string="Details", readonly=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def action_start(self):
        """Kick off the history import.

        Single-scan path (the common case): history was already captured when
        the user scanned the QR on Connect (armed beforehand), so we just drain
        the sidecar buffer and send the user to the live progress view — NO
        second scan. Re-link path: an account linked earlier has nothing
        buffered, so the first run re-links it (drops the session, shows a fresh
        QR with full-history sync on) and we send the user to the account form
        to scan that QR. WhatsApp only streams the bulk history at the initial
        device-link, so an account linked earlier genuinely needs the re-scan.
        Either way, live progress shows under Chats > Import Jobs."""
        self.ensure_one()
        if self.account_id.session_state != 'connected':
            raise UserError(_(
                "Connect the WhatsApp account before importing — it is currently "
                "“%s”.") % (self.account_id.session_state or 'not connected'))
        self.write({
            'state': 'queued', 'progress': 0, 'cursor': 0, 'sync_polls': 0,
            'count_contacts': 0, 'count_chats': 0, 'count_messages': 0, 'log': False,
        })
        # Decide the destination up front. If the connect scan already captured
        # the history (or it's still streaming in), there is NO second scan —
        # send the user to the live progress view. Only an account linked
        # earlier, with nothing buffered, needs a fresh QR scan. A structure-only
        # import (communities/channels, no chat history) never re-scans.
        wants_history = self.import_chats or self.import_messages
        needs_relink = False
        if wants_history:
            try:
                status = self.account_id._get_baileys_api().history_status() or {}
            except Exception:
                status = {}
            needs_relink = not (status.get('available') or status.get('syncing'))
        # Run the first iteration inline so the work starts on click (drain the
        # buffer, or fire the re-link) instead of waiting for the first cron
        # tick. Connection / cloud-account problems surface right away as a
        # clear error rather than a job that silently sits at 0%.
        try:
            self._run_once()
        except UserError:
            raise
        except Exception as e:
            _logger.exception("WhatsApp import job %s failed to start", self.id)
            raise UserError(_("Couldn't start the import: %s") % e)
        # Keep draining the streamed history in the background. Self-heal the
        # scheduled action in case a previous run left it disabled — Odoo turns
        # a cron off once its numbercall reaches 0.
        cron = self.env.ref(
            'open_whatsapp_connector.ir_cron_owa_import_jobs', raise_if_not_found=False)
        if cron:
            cron = cron.sudo()
            heal = {}
            if not cron.active:
                heal['active'] = True
            # numbercall/doall were removed from ir.cron in Odoo 18; only heal
            # the call-count where the field still exists (Odoo 17). On 18/19 a
            # cron with active=True simply runs on schedule. (#cron-numbercall)
            if 'numbercall' in cron._fields and cron.numbercall == 0:
                heal['numbercall'] = -1
            if heal:
                cron.write(heal)
            cron._trigger()
        if needs_relink:
            # The re-link just dropped a fresh QR on the account form to scan,
            # which is what starts the history stream for an account linked
            # earlier. Send the user there.
            return {
                'type': 'ir.actions.act_window',
                'name': _("Scan to import chat history"),
                'res_model': 'owa.account',
                'res_id': self.account_id.id,
                'view_mode': 'form',
                'target': 'current',
            }
        # Single-scan: history was captured during the connect scan (or is still
        # streaming in). No second scan — show the live import progress.
        return {
            'type': 'ir.actions.act_window',
            'name': _("Importing chat history"),
            'res_model': 'owa.import.job',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _commit(self):
        """Persist progress so a worker time-limit kill can't roll back the
        whole import (mirrors owa.message._send_cron). No-op under tests."""
        if not odoo.modules.module.current_test:
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Cron / runner
    # ------------------------------------------------------------------
    @api.model
    def _cron_run(self):
        for job in self.search([('state', 'in', ('queued', 'syncing', 'importing'))]):
            try:
                job._run_once()
            except Exception as e:
                _logger.exception("WhatsApp import job %s failed", job.id)
                job.write({'state': 'error', 'log': str(e)})
                self._commit()

    def _run_once(self):
        """Advance one job: queue→sync (resync the session)→import (drain pages)."""
        self.ensure_one()
        account = self.account_id
        api_client = account._get_baileys_api()  # clean UserError for cloud accounts

        # Once per job: communities + channel-post history. Both derive from
        # live feeds (groups feed / newsletterFetchMessages), independent of the
        # chat-history buffer drain below, so do them on the first iteration.
        if not self.structure_synced and (self.import_communities or self.import_channels):
            self._sync_structure(api_client)

        wants_history = self.import_chats or self.import_messages

        # Contact-photo pass for NON-history imports (the wizard already created
        # those contacts synchronously). For a history import, _import_page
        # fetches each contact's photo as its page lands, so we don't double up
        # here. Batched + resumable so a large address book never blocks. (#wa-fetch-pic)
        if self.import_avatars and not self.avatars_synced and not wants_history:
            if self.state == 'queued':
                self.state = 'importing'
            self._import_avatars(account)

        if not wants_history:
            # Structure / photos-only import: nothing to drain from the history
            # buffer and no QR re-scan needed.
            if self.import_avatars and not self.avatars_synced:
                self._commit()
                return  # more photos to fetch on the next cron tick
            if self.state != 'done':
                self.write({'state': 'done', 'progress': 100})
                self._commit()
            return

        if self.state in ('queued', 'syncing'):
            status = api_client.history_status() or {}
            if status.get('available'):
                # Give the import phase its own idle budget (don't carry over
                # polls spent waiting for the sync to start).
                self.write({'state': 'importing', 'sync_polls': 0})
            elif self.state == 'queued':
                if status.get('syncing'):
                    # History is ALREADY streaming in — it was armed during the
                    # connect scan (the single-scan path). Just wait for it on
                    # the next ticks; never re-link (a second QR scan).
                    self.write({'state': 'syncing'})
                else:
                    # Nothing buffered and no sync running → an account linked
                    # earlier needs a fresh device-link to get its history.
                    # Re-link (this shows a new QR to scan).
                    api_client.history_resync()
                    self.write({'state': 'syncing', 'progress': 5})
                self._commit()
                return
            else:  # syncing, still nothing available yet
                self.sync_polls += 1
                if self.sync_polls > MAX_SYNC_POLLS:
                    self.write({
                        'state': 'error',
                        'log': _("WhatsApp returned no history to import. Make sure "
                                 "the account is connected, then try again."),
                    })
                self._commit()
                return

        if self.state == 'importing':
            for _i in range(MAX_PAGES_PER_RUN):
                page = api_client.fetch_history_page(
                    cursor=self.cursor, limit=PAGE_LIMIT) or {}
                self._import_page(page)
                new_cursor = page.get('nextCursor', self.cursor)
                advanced = new_cursor != self.cursor
                self.cursor = new_cursor
                if advanced or page.get('messages'):
                    self.sync_polls = 0   # productive tick clears accumulated stalls
                status = api_client.history_status() or {}
                self.progress = max(self.progress, min(99, int(status.get('progress') or 0)))
                if page.get('done'):
                    vals = {'state': 'done', 'progress': 100}
                    if status.get('truncated'):
                        vals['log'] = _(
                            "Imported recent history only — WhatsApp capped the "
                            "sync (large or slow history). Older messages aren't "
                            "available over the QR connection.")
                    self.write(vals)
                    self._commit()
                    break
                # Drained everything buffered so far but the sidecar hasn't
                # signalled completion yet — stop spinning on the empty tail
                # (resume next tick). Never hang: after MAX_SYNC_POLLS idle ticks
                # accept what we imported. Bounds an interrupted sync or a
                # sidecar restart that lost its in-memory 'finished' state.
                if not advanced and not page.get('messages'):
                    self.sync_polls += 1
                    if self.sync_polls > MAX_SYNC_POLLS:
                        self.write({'state': 'done', 'progress': 100})
                    self._commit()
                    break
                self._commit()

    # ------------------------------------------------------------------
    # Import one drained page
    # ------------------------------------------------------------------
    def _import_page(self, page):
        """Write one page of buffered history. Returns (contacts, chats, msgs)
        newly created. Idempotent: re-importing the same page creates nothing."""
        self.ensure_one()
        account = self.account_id
        Partner = self.env['res.partner'].sudo()
        n_contacts = n_chats = n_msgs = 0

        # Conversations first so the channels exist before their messages land.
        # Handles 1:1 phone, private LID and group JIDs uniformly (see
        # _resolve_channel). Groups keep their name; LID chats are anonymous.
        if self.import_chats:
            for chat in page.get('chats') or []:
                _ch, created = self._resolve_channel(
                    chat.get('id') or chat.get('jid'), sender_name=chat.get('name') or None)
                if created:
                    n_chats += 1

        if self.import_messages:
            for m in page.get('messages') or []:
                if self._import_message(m):
                    n_msgs += 1

        # Contacts: only the ones WhatsApp still exposes by phone number get a
        # standalone partner here. LID-only / group contacts carry no phone
        # (WhatsApp privacy) — their partner is created with the conversation.
        if self.import_contacts:
            for contact in page.get('contacts') or []:
                digits = self._jid_digits(contact.get('id') or contact.get('jid'))
                if not digits or set(digits) == {'0'}:
                    continue
                name = (contact.get('name') or contact.get('notify')
                        or contact.get('verifiedName') or None)
                if not self._partner_exists(digits):
                    n_contacts += 1
                # Explicit import: bypass the passive auto-create toggle.
                partner = Partner.with_context(
                    owa_force_contact_create=True,
                )._find_or_create_from_wa_number(digits, name)
                # Contact-photo fetch as the page lands — backfill only (skip
                # contacts that already have a photo). Gated on import_avatars.
                # Best-effort; never blocks the import. (#wa-fetch-pic)
                if self.import_avatars and partner and not partner.image_1920:
                    b64 = account._fetch_picture_b64('%s@s.whatsapp.net' % digits)
                    if b64:
                        partner.image_1920 = b64

        self.write({
            'count_contacts': self.count_contacts + n_contacts,
            'count_chats': self.count_chats + n_chats,
            'count_messages': self.count_messages + n_msgs,
        })
        return n_contacts, n_chats, n_msgs

    def _resolve_channel(self, jid, sender_name=None):
        """Find or create the discuss channel for a history chat JID, the SAME
        way the live inbound path does (discuss.channel._get_whatsapp_channel).
        Handles 1:1 phone (@s.whatsapp.net), private LID (@lid), groups
        (@g.us) and channels/newsletters (@newsletter). Returns (channel,
        created). (empty, False) for broadcasts, the 0@s.whatsapp.net system
        jid, and unknown jids."""
        jid = jid or ''
        Channel = self.env['discuss.channel'].sudo()
        if jid.endswith('@newsletter'):
            # Channel/newsletter surfaced in the history feed → register it so
            # it becomes "known" and its posts import. Naming comes from the
            # newsletter record / _ensure_channel, never the raw JID.
            Newsletter = self.env['owa.newsletter'].sudo()
            nl = Newsletter.search([
                ('wa_account_id', '=', self.account_id.id),
                ('newsletter_jid', '=', jid),
            ], limit=1)
            if not nl:
                nl = Newsletter.create({
                    'wa_account_id': self.account_id.id,
                    'newsletter_jid': jid,
                    'name': sender_name or jid,
                })
            return nl._ensure_channel(), False
        if jid.endswith('@g.us') or jid.endswith('@lid'):
            key = jid  # _get_whatsapp_channel keeps @-JIDs verbatim (group/LID)
            if jid.endswith('@g.us'):
                # The history chat 'name' is the LAST SENDER, not the group
                # subject — never use it to name a group channel. The real
                # subject is applied by the group/community metadata sync. (#wa-name)
                sender_name = None
        elif '@s.whatsapp.net' in jid:
            key = ''.join(c for c in jid.split('@')[0].split(':')[0] if c.isdigit())
            if not key or set(key) == {'0'}:
                return Channel, False
        else:
            return Channel, False
        existing = Channel._get_owa_whatsapp_channel(
            key, self.account_id, create_if_not_found=False)
        if existing:
            return existing, False
        ch = Channel._get_owa_whatsapp_channel(
            key, self.account_id, sender_name=sender_name, create_if_not_found=True)
        return ch, bool(ch)

    def _import_message(self, m):
        """Persist one historical message into its channel WITHOUT sending or
        firing inbound side-effects (acks/auto-replies/rules). Returns True if a
        new message was created. Deduped by provider message id. Routes 1:1
        phone, LID and group messages alike via _resolve_channel."""
        account = self.account_id
        key = (m or {}).get('key') or {}
        remote_jid = key.get('remoteJid') or ''
        mid = key.get('id') or ''
        if not mid:
            return False
        # Dedupe — skip if we already imported/saw this provider message id.
        if self.env['owa.message'].sudo().search_count([
            ('msg_uid', '=', mid), ('wa_account_id', '=', account.id),
        ]):
            return False
        body = self._history_body(m)
        if not body:
            return False
        from_me = bool(key.get('fromMe'))
        channel, _created = self._resolve_channel(remote_jid)
        if not channel:
            return False
        author = (
            (account.user_id.partner_id or self.env.company.partner_id)
            if from_me else channel.whatsapp_partner_id
        )
        # owa_inbound_msg_uid routes message_post through the NON-sending path
        # (_notify_thread creates an owa.message, no _send_message). For fromMe
        # we relabel that record to outbound/sent afterward.
        posted = channel.sudo().with_context(
            mail_create_nosubscribe=True, owa_inbound=True,
        ).message_post(
            body=body,
            message_type='whatsapp_message',
            subtype_xmlid='mail.mt_comment',
            author_id=author.id if author else False,
            owa_inbound_msg_uid=mid,
        )
        # Stamp the real WhatsApp timestamp on the chatter message (epoch seconds).
        secs = int(self._ts_seconds(m.get('messageTimestamp')))
        if posted and secs:
            try:
                dt = datetime.datetime.utcfromtimestamp(secs)
                posted.sudo().write({'date': fields.Datetime.to_string(dt)})
            except Exception:
                pass
        if from_me and posted:
            posted.owa_message_ids.sudo().write({
                'message_type': 'outbound', 'state': 'sent',
            })
        return True

    # ------------------------------------------------------------------
    # Communities + Channels (live feeds, not the history buffer)
    # ------------------------------------------------------------------
    def _sync_structure(self, api_client):
        """Run once per job: create community umbrellas + linked groups, and
        best-effort import channel (newsletter) post history. Both come from
        live feeds, so they're independent of the chat-history page drain. Any
        failure is logged and never aborts the chat import. (#wa-import)"""
        self.ensure_one()
        account = self.account_id
        if self.import_communities:
            try:
                # Reuse the same discovery the manual Communities ▸ Refresh uses.
                self.count_communities = self.env['owa.community']._sync_account_communities(
                    account, api_client)
            except Exception:
                _logger.exception("Community import failed for job %s", self.id)
        if self.import_channels:
            try:
                self._import_channels(account, api_client)
            except Exception:
                _logger.exception("Channel import failed for job %s", self.id)
        self.structure_synced = True
        self._commit()

    def _import_avatars(self, account):
        """Download WhatsApp profile pictures for this account's contacts that
        have none yet. Batched (AVATAR_BATCH per call) + idempotent (only
        touches photo-less partners), so it resumes safely across cron ticks
        and never blocks the wizard request. JIDs are rebuilt from the partner
        phone the same way the rest of the addon does. (#wa-fetch-pic)"""
        Channel = self.env['discuss.channel'].sudo()
        channels = Channel.search([
            ('owa_account_id', '=', account.id),
            ('channel_type', '=', 'whatsapp'),
        ])
        # res.partner.mobile does not exist on Odoo 19 — guard the access.
        partners = (channels.channel_member_ids.partner_id
                    | channels.whatsapp_partner_id).filtered(
                        lambda p: not p.image_1920
                        and (p.phone or getattr(p, 'mobile', False)))
        fetched = 0
        for idx, partner in enumerate(partners[:AVATAR_BATCH], 1):
            digits = (wa_phone_format(
                self.env, partner.phone or getattr(partner, 'mobile', False))
                or '').lstrip('+')
            if digits:
                b64 = account._fetch_picture_b64('%s@s.whatsapp.net' % digits)
                if b64:
                    partner.image_1920 = b64
                    fetched += 1
            if idx % 25 == 0:
                self._commit()
        if fetched:
            self.count_avatars += fetched
        # Done once this pass has covered everything still missing a photo.
        if len(partners) <= AVATAR_BATCH:
            self.avatars_synced = True
        self._commit()

    def _import_channels(self, account, api_client):
        """Best-effort per-known-channel post import. WhatsApp has no
        'list all channels' API, so we only import channels already known in
        Odoo (subscribed via link / created). Per-channel errors are isolated
        and `newsletterFetchMessages` may be unsupported on some builds —
        either way the job continues."""
        Newsletter = self.env['owa.newsletter'].sudo()
        total = 0
        for nl in Newsletter.search([('wa_account_id', '=', account.id)]):
            try:
                channel = nl._ensure_channel()
                if not channel:
                    continue
                data = api_client.newsletter_fetch_messages(nl.newsletter_jid) or {}
                for post in data.get('messages') or []:
                    if self._import_newsletter_post(channel, post):
                        total += 1
            except Exception:
                _logger.exception(
                    "Channel post import failed for %s", nl.newsletter_jid)
        if total:
            self.count_channel_posts = self.count_channel_posts + total

    def _import_newsletter_post(self, channel, post):
        """Persist one channel post into its conversation, non-sending + deduped
        by provider id. The post shape from newsletterFetchMessages varies on
        the pinned build, so read defensively."""
        account = self.account_id
        post = post or {}
        key = post.get('key') or {}
        mid = key.get('id') or post.get('id') or str(post.get('server_id') or '')
        if not mid:
            return False
        if self.env['owa.message'].sudo().search_count([
            ('msg_uid', '=', mid), ('wa_account_id', '=', account.id),
        ]):
            return False
        body = (self._history_body(post) if post.get('message')
                else (post.get('text') or post.get('caption') or ''))
        if not body:
            return False
        posted = channel.sudo().with_context(
            mail_create_nosubscribe=True, owa_inbound=True,
        ).message_post(
            body=body,
            message_type='whatsapp_message',
            subtype_xmlid='mail.mt_comment',
            author_id=channel.whatsapp_partner_id.id or self.env.company.partner_id.id,
            owa_inbound_msg_uid=mid,
        )
        secs = int(self._ts_seconds(
            post.get('messageTimestamp') or post.get('timestamp') or 0))
        if posted and secs:
            try:
                dt = datetime.datetime.utcfromtimestamp(secs)
                posted.sudo().write({'date': fields.Datetime.to_string(dt)})
            except Exception:
                pass
        return bool(posted)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _jid_digits(jid):
        """Return the bare phone digits for a 1:1 WhatsApp JID, else ''.
        Groups (@g.us), private LIDs (@lid) and broadcasts are skipped."""
        jid = jid or ''
        if '@s.whatsapp.net' not in jid:
            return ''
        local = jid.split('@')[0].split(':')[0]
        return ''.join(c for c in local if c.isdigit())

    @staticmethod
    def _ts_seconds(ts):
        """WhatsApp messageTimestamp may be a plain number or a 64-bit Long
        ``{low, high}``; reconstruct the full unsigned value (``low`` is a
        signed 32-bit word, so mask it and add the high word)."""
        if isinstance(ts, dict):
            low = ts.get('low') or 0
            high = ts.get('high') or 0
            return (high << 32) | (low & 0xFFFFFFFF)
        return ts or 0

    def _partner_exists(self, digits):
        formatted = wa_phone_format(self.env, '+' + digits)
        candidates = [c for c in (formatted, '+' + digits, digits) if c]
        return bool(self.env['res.partner'].sudo().search(
            [('phone', 'in', candidates)], limit=1))

    @staticmethod
    def _history_body(m):
        """Extract a displayable body from a WhatsApp history message."""
        msg = (m or {}).get('message') or {}
        for wrap in ('ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2',
                     'deviceSentMessage'):
            inner = msg.get(wrap)
            if isinstance(inner, dict) and inner.get('message'):
                msg = inner['message']
        if msg.get('conversation'):
            return msg['conversation']
        ext = msg.get('extendedTextMessage') or {}
        if ext.get('text'):
            return ext['text']
        for mtype, label in (
            ('imageMessage', '📷 Image'), ('videoMessage', '🎥 Video'),
            ('audioMessage', '🎵 Audio'), ('documentMessage', '📄 Document'),
            ('stickerMessage', '🏷️ Sticker'),
        ):
            block = msg.get(mtype)
            if isinstance(block, dict):
                return block.get('caption') or label
        return ''
