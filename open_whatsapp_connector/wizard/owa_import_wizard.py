import logging

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OwaImportWizard(models.TransientModel):
    _name = 'owa.import.wizard'
    _description = 'Import WhatsApp Data'

    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account", required=True,
        default=lambda self: self.env['owa.account'].search(
            [('session_state', '=', 'connected'), ('connection_type', '=', 'qr')], limit=1),
        domain=[('session_state', '=', 'connected'), ('connection_type', '=', 'qr')],
        help="The connected WhatsApp account to import from.")
    import_groups = fields.Boolean(
        string="Groups", default=True,
        help="Fetch the WhatsApp groups this number belongs to and create their conversations in Odoo.")
    import_contacts = fields.Boolean(
        string="Contacts", default=True,
        help="Create contact records for the people in those groups (the ones whose number is visible).")
    import_history = fields.Boolean(
        string="Chat history", default=False,
        help="Import your existing conversations and recent messages. If you "
             "just connected, they were already captured during that QR scan and "
             "import now in the background with a progress bar — no extra step. "
             "(For an account you linked earlier, importing history needs one "
             "more quick QR scan, because WhatsApp only resends history when a "
             "device is freshly linked.) Groups & Contacts always import "
             "instantly.")
    import_communities = fields.Boolean(
        string="Communities", default=True,
        help="Create WhatsApp Community records and link each one's groups, "
             "members and announcements conversation. Imports instantly from "
             "the groups feed (no extra scan).")
    import_channels = fields.Boolean(
        string="Channels", default=True,
        help="Import recent posts from the WhatsApp Channels this number "
             "follows. Best-effort: channels are added one at a time via "
             "Subscribe, and WhatsApp's channel history is more limited than "
             "groups.")
    import_avatars = fields.Boolean(
        string="Contact photos", default=True,
        help="Download each imported contact's WhatsApp profile picture. Runs "
             "in the background (one lookup per contact), so it never slows the "
             "import. Group, community and channel photos always import.")

    @staticmethod
    def _digits(value):
        return ''.join(c for c in (value or '') if c.isdigit())

    @classmethod
    def _participant_digits(cls, part):
        """Return the visible phone digits for a group participant, or ''.

        WhatsApp Web (rc10) returns participants as
        ``{"id": "<lid>@lid", "phoneNumber": "<digits>@s.whatsapp.net", ...}`` —
        the resolvable number lives in ``phoneNumber`` (or ``jid``/``pn`` on
        some events), NOT ``id`` (now a privacy LID). When a member keeps their
        number private, only an ``@lid`` is present and we return '' so the
        caller skips them (they fill in as they message)."""
        if not isinstance(part, dict):
            return ''
        for key in ('phoneNumber', 'jid', 'pn', 'id'):
            val = part.get(key) or ''
            if '@s.whatsapp.net' in val:
                return cls._digits(val.split('@')[0])
        return ''

    def action_import(self):
        """Pull groups + the contacts inside them from the connected account
        using ONLY the sidecar endpoints that already exist (no sidecar change).
        Past chat messages and a bulk contact/avatar fetch are not exposed by the
        WhatsApp Web protocol via the current sidecar, so they are out of scope —
        conversations sync from now on."""
        self.ensure_one()
        account = self.wa_account_id
        if not account or account.session_state != 'connected':
            raise UserError(_("Connect a WhatsApp account first, then try again."))
        # Respect the per-account access model (shared = admin; own_team = owner/leader).
        if hasattr(account, '_ensure_can_manage'):
            account._ensure_can_manage()
        api = account._get_baileys_api()  # raises a clean error for Cloud-API accounts

        n_groups = n_contacts = n_members = 0
        groups = []
        if self.import_groups or self.import_contacts:
            try:
                groups = api.fetch_all_groups() or []
            except Exception:
                _logger.exception("Import: fetch_all_groups failed for %s", account.name)
                groups = []

        GS = self.env['owa.group.session'].sudo()
        Partner = self.env['res.partner'].sudo()
        seen = set()
        for meta in groups:
            row = None
            if self.import_groups:
                try:
                    row = GS._upsert_from_metadata(account, meta)
                    n_groups += 1
                except Exception:
                    _logger.exception("Import: group upsert failed (%s)", meta.get('id'))

            # Resolve the group's participants to partners — used both for the
            # Contacts import and for populating the group conversation's
            # members. Members whose WhatsApp number is hidden (LID only, no
            # phoneNumber) can't be resolved and are skipped.
            group_partners = Partner.browse()
            for part in (meta.get('participants') or []):
                digits = self._participant_digits(part)
                if not digits:
                    continue
                # Explicit import: bypass the passive auto-create toggle.
                partner = Partner.with_context(
                    owa_force_contact_create=True,
                )._find_or_create_from_wa_number(digits)
                if not partner:
                    continue
                group_partners |= partner
                if digits not in seen:
                    seen.add(digits)
                    n_contacts += 1

            # Mirror the WhatsApp roster onto the Discuss channel so imported
            # groups aren't empty (fixes "group name created, no members").
            if self.import_groups and row and row.channel_id and group_partners:
                channel = row.channel_id.sudo()
                missing = group_partners - channel.channel_member_ids.partner_id
                if missing:
                    channel.channel_member_ids = [
                        (0, 0, {'partner_id': p.id}) for p in missing]
                    n_members += len(missing)

        # Communities: fast, synchronous discovery from the groups feed (reuse
        # the list already fetched above when present, else fetch in the helper).
        n_communities = 0
        if self.import_communities:
            try:
                n_communities = self.env['owa.community']._sync_account_communities(
                    account, api, groups or None)
            except Exception:
                _logger.exception("Import: community sync failed for %s", account.name)

        # Chat history and channel posts run as a background job (the WhatsApp
        # history sync is async/large; channel fetch can be slow). Communities
        # already imported synchronously above, so the job skips them.
        if self.import_history or self.import_channels:
            job = self.env['owa.import.job'].create({
                'account_id': account.id,
                'import_contacts': self.import_contacts and self.import_history,
                'import_chats': self.import_history,
                'import_messages': self.import_history,
                'import_communities': False,
                'import_channels': self.import_channels,
                # The history job fetches each contact's photo as its page lands.
                'import_avatars': self.import_avatars,
            })
            return job.action_start()

        # No history/channels job, but if photos are wanted, run a background
        # photo pass over the contacts we just imported. A per-contact network
        # fetch would block this request (and risk a timeout on big address
        # books), so it runs on the import-job cron — the user keeps the
        # summary below and photos fill in over the next minute. (#wa-fetch-pic)
        if self.import_avatars:
            self.env['owa.import.job'].sudo().create({
                'account_id': account.id,
                'import_contacts': False, 'import_chats': False,
                'import_messages': False, 'import_communities': False,
                'import_channels': False, 'import_avatars': True,
                'state': 'queued',
            })
            cron = self.env.ref(
                'open_whatsapp_connector.ir_cron_owa_import_jobs',
                raise_if_not_found=False)
            if cron:
                cron = cron.sudo()
                heal = {}
                if not cron.active:
                    heal['active'] = True
                # numbercall was removed from ir.cron in Odoo 18+; only heal
                # it on runtimes that still have it. (#cron-numbercall)
                if 'numbercall' in cron._fields and cron.numbercall == 0:
                    heal['numbercall'] = -1
                if heal:
                    cron.write(heal)
                cron._trigger()

        msg = _(
            "Imported %(g)s group(s), %(c)s contact(s) and %(comm)s "
            "community(ies); added %(m)s member(s) to conversations. Members "
            "who keep their WhatsApp number private can't be imported — they "
            "appear automatically as they message you.",
            g=n_groups, c=n_contacts, comm=n_communities, m=n_members)
        if self.import_avatars:
            msg += _(" Contact photos are downloading in the background.")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp data imported"),
                'message': msg,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
