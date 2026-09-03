import logging
import secrets
import string
from datetime import timedelta
from urllib.parse import quote

from markupsafe import Markup

import base64

from odoo import api, fields, models, SUPERUSER_ID, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
from odoo.addons.open_whatsapp_connector.tools.baileys_exception import BaileysError

_logger = logging.getLogger(__name__)


class OwaAccount(models.Model):
    _name = 'owa.account'
    _inherit = ['mail.thread']
    _description = 'Open WhatsApp Connector Account'
    _rec_name = 'display_name'
    _order = 'display_name, id'

    name = fields.Char(string="Name", required=True, tracking=1)
    display_name = fields.Char(
        string="Display Name", compute='_compute_display_name', store=True,
        help="Human-friendly name shown in lists and menus. Falls back to "
             "the technical 'name' if unset.")
    label = fields.Char(
        string="Label", help="Optional human-friendly label; if set, this "
                              "overrides the internal name in lists.")
    active = fields.Boolean(default=True, tracking=6)
    enabled = fields.Boolean(
        string="Enabled", default=True,
        help="If unchecked, the sidecar listener is not started for this "
             "account on Connect — useful for keeping a configured account "
             "around without it consuming a sidecar slot.")
    auth_dir = fields.Char(
        string="Auth directory override",
        groups='open_whatsapp_connector.group_owa_admin',
        help="Optional absolute path; sidecar reads the gateway's multi-file auth "
             "state from here instead of the default per-account dir. Only "
             "set this if you need to share auth state with another tool.")

    @api.depends('label', 'name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.label or rec.name or ''

    @api.onchange('throttle_preset')
    def _onchange_throttle_preset(self):
        # The preset is a convenience that fills the per-hour cap; the numeric
        # caps remain editable for fine-tuning. Without this the preset field
        # was inert (selecting it changed nothing).
        presets = {
            'unlimited': (0, 0, 0),
            'safe_low_volume': (0, 60, 0),
            'medium': (0, 300, 0),
            'high': (0, 1000, 0),
        }
        for rec in self:
            if rec.throttle_preset in presets:
                m, h, d = presets[rec.throttle_preset]
                rec.messages_per_minute = m
                rec.messages_per_hour = h
                rec.messages_per_day = d

    # Sidecar connection
    sidecar_url = fields.Char(
        string="Sidecar URL",
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.default_sidecar_url') or 'http://localhost:3100',
        required=True,
        help="URL of the Node.js sidecar service. New accounts inherit the "
             "Default Sidecar URL configured in Settings.")
    sidecar_api_key = fields.Char(
        string="API Key", groups='open_whatsapp_connector.group_owa_admin',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.default_api_key') or False,
        help="Shared API key for authenticating with the sidecar service. New "
             "accounts inherit the Default API Key configured in Settings.")
    webhook_secret = fields.Char(
        string="Webhook Secret", compute='_compute_webhook_secret', store=True,
        copy=False,
        groups='open_whatsapp_connector.group_owa_admin')

    # Session state (synced from sidecar)
    session_state = fields.Selection([
        ('disconnected', 'Disconnected'),
        ('qr_pending', 'Awaiting QR Scan'),
        ('connecting', 'Connecting'),
        ('connected', 'Connected'),
        ('logged_out', 'Logged Out'),
    ], string="Status", default='disconnected', readonly=True, tracking=5)
    qr_code_base64 = fields.Text(string="QR Code Data", readonly=True)
    phone_number = fields.Char(string="Phone Number", readonly=True, copy=False)
    # Set True by write() the moment a QR account FRESHLY connects, so the form's
    # always-mounted wa_import_prompt widget can auto-open the "Import WhatsApp
    # Data" dialog exactly once per scan and then clear it. A server flag (not a
    # JS latch) is required because the QR widget unmounts the instant the
    # account becomes connected. (#import_prompt_flag)
    import_prompt_pending = fields.Boolean(
        string="Import prompt pending", default=False, copy=False, readonly=True)

    # Access control
    allowed_company_ids = fields.Many2many(
        comodel_name='res.company', string="Allowed Companies",
        default=lambda self: self.env.company)
    notify_user_ids = fields.Many2many(
        comodel_name='res.users', string="Users to Notify",
        default=lambda self: self.env.user,
        domain=[('share', '=', False)], required=True, tracking=3,
        help="Users notified when a new message is received")
    # Access pool: everyone in this group PLUS the notify users can see this
    # account's WhatsApp conversations (they are added as channel members, which
    # is what Discuss visibility requires). Optional — leave empty to keep chats
    # to the Users to Notify only. (#allowed-group)
    allowed_group_id = fields.Many2one(
        'res.groups', string="Allowed Group", tracking=3,
        help="Members of this user group can see this account's WhatsApp "
             "conversations (in addition to the Users to Notify). Leave empty "
             "to restrict chats to the Users to Notify.")

    # ── Per-account ownership + approval (18.0.41.0.0) ────────────────
    user_id = fields.Many2one(
        'res.users', string="Owner", index=True, tracking=True,
        default=lambda self: self.env.user,
        help="User who owns this WhatsApp account. In 'own team' account "
             "visibility mode, only this user, their Sales Team leader and "
             "WhatsApp Administrators can see it.")
    team_id = fields.Many2one(
        'crm.team', string="Sales Team", index=True,
        default=lambda self: self.env['crm.team']._get_default_team_id(
            user_id=self.env.uid))
    approval_state = fields.Selection([
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string="Approval", default=lambda self: self._default_approval_state(),
        tracking=True, copy=False,
        help="A self-added account stays Pending (cannot send/receive) until a "
             "WhatsApp Administrator approves it. Ignored when account "
             "visibility is 'shared'.")
    approved_user_id = fields.Many2one(
        'res.users', string="Approved by", readonly=True, copy=False)
    approved_date = fields.Datetime(string="Approved on", readonly=True, copy=False)

    # Config
    debug_logging = fields.Boolean(
        string="Debug Logging",
        help="Log sidecar API requests for debugging")
    callback_url = fields.Char(
        string="Callback URL", compute='_compute_callback_url', readonly=True)

    # Sidecar process status
    sidecar_running = fields.Boolean(
        string="Sidecar Running", compute='_compute_sidecar_running')

    # Self-service: may the CURRENT user connect/scan/manage THIS account from
    # the UI? Mirrors `_ensure_can_manage` (admin always; in own_team mode also
    # the owner / their Sales-Team leader) so the connection buttons can show
    # for a self-service owner without exposing them to every agent.
    user_can_manage = fields.Boolean(
        string="Can Manage Connection", compute='_compute_user_can_manage')

    # Phase C3: business profile picture (uploaded to WhatsApp via the sidecar)
    image_1920 = fields.Image(string="Profile Picture", max_width=1920, max_height=1920)
    image_128 = fields.Image(
        string="Avatar", related='image_1920', max_width=128, max_height=128,
        store=True)

    # Default account: conversations of a deleted account are reassigned here,
    # and it is the reply fallback for orphaned chats. Backed by the
    # `default_account_id` system parameter so the existing reassign/fallback
    # logic keeps working unchanged. (#default-account-checkbox)
    is_default = fields.Boolean(
        string="Default Account", compute='_compute_is_default',
        inverse='_inverse_is_default',
        help="When a WhatsApp account is deleted, its conversation history is "
             "reassigned to the default account. It is also the fallback "
             "reply account for conversations that lost their account. "
             "Only one account can be the default.")

    def _compute_is_default(self):
        default_id = int(self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.default_account_id') or 0)
        for rec in self:
            rec.is_default = rec.id == default_id

    def _inverse_is_default(self):
        ICP = self.env['ir.config_parameter'].sudo()
        key = 'open_whatsapp_connector.default_account_id'
        for rec in self:
            if rec.is_default:
                ICP.set_param(key, str(rec.id))
            elif int(ICP.get_param(key) or 0) == rec.id:
                ICP.set_param(key, '')
        # The flag is a non-stored compute off a global parameter: moving the
        # default to another account must drop every record's cached value,
        # or the previous default keeps showing as checked.
        self.env['owa.account'].invalidate_model(['is_default'])

    # Phase D: pairing method — explicit choice between QR scan and pairing code
    pairing_method = fields.Selection([
        ('qr', 'Scan QR Code'),
        ('code', 'Link with Code'),
    ], string="Pairing Method", default='qr', required=True, tracking=4,
       help="Choose how this account pairs to WhatsApp the first time. "
            "Scan QR is the default and works on every phone. "
            "Link with Code is a fallback if camera scanning isn't available.")
    pairing_phone = fields.Char(
        string="WhatsApp Pairing Phone",
        help="Phone number of the WhatsApp account to pair (digits only, country code included). "
             "Used by the 'Request pairing code' action as an alternative to scanning the QR.")
    pairing_code = fields.Char(
        string="WhatsApp Pairing Code", readonly=True,
        help="Most recent 8-character pairing code returned by WhatsApp. Type this on the phone "
             "in Settings → Linked Devices → Link with phone number.")

    # ─── Transport selection: QR/device-link vs Official Cloud API ──────
    connection_type = fields.Selection([
        ('qr', 'QR / Device Link'),
        ('cloud', 'Official Cloud API'),
    ], string="Connection Type", default='qr', required=True, tracking=True,
       help="How this account talks to WhatsApp. 'QR / Device Link' uses the "
            "Node.js gateway (scan a QR / pairing code). 'Official Cloud API' "
            "talks directly to the Meta Graph API for a WhatsApp Business "
            "number — no gateway, supports multiple business numbers.")

    # ─── Official Cloud API (Meta Graph API) credentials ────────────────
    cloud_phone_number_id = fields.Char(
        string="Phone Number ID", copy=False,
        help="Meta Phone Number ID of the sending WhatsApp Business number.")
    cloud_waba_id = fields.Char(
        string="WhatsApp Business Account ID", copy=False,
        help="Meta WhatsApp Business Account (WABA) ID — matches the inbound "
             "webhook entry.id used to route messages to this account.")
    cloud_app_id = fields.Char(
        string="Meta App ID", copy=False,
        help="Meta App ID — used for resumable media upload sessions.")
    cloud_app_secret = fields.Char(
        string="App Secret", copy=False,
        groups='open_whatsapp_connector.group_owa_admin',
        help="Meta App Secret — used to verify the X-Hub-Signature-256 on "
             "inbound webhooks. Never logged.")
    cloud_access_token = fields.Char(
        string="Access Token", copy=False,
        groups='open_whatsapp_connector.group_owa_admin',
        help="Bearer token for the Graph API. Sandbox tokens are short-lived; "
             "use a system-user permanent token in production.")
    cloud_verify_token = fields.Char(
        string="Webhook Verify Token", copy=False, readonly=True,
        groups='open_whatsapp_connector.group_owa_admin',
        help="Random token auto-generated per cloud account. Enter this in the "
             "Meta console webhook setup so the GET verification handshake "
             "succeeds.")
    cloud_api_version = fields.Char(
        string="Graph API Version", default='v23.0',
        help="Meta Graph API version, e.g. v23.0.")
    cloud_callback_url = fields.Char(
        string="Callback URL", compute='_compute_cloud_callback_url',
        help="Webhook URL to register in the Meta console for this account.")

    @api.depends('connection_type')
    def _compute_cloud_callback_url(self):
        # Carry ?db=<dbname> so Meta's webhook resolves the right database on a
        # multi-database host — the controllers/ir_http.py patch reads this query
        # arg (and the X-Odoo-Database header) before dispatch. Mirrors the QR
        # callback_url pattern so both transports behave identically on multi-DB
        # deployments. web.base.url already carries the https scheme Meta requires.
        db_name = quote(self.env.cr.dbname or '')
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for acc in self:
            acc.cloud_callback_url = (
                (base or '')
                + f'/open_whatsapp_connector/cloud/webhook?db={db_name}')

    @api.model
    def _default_approval_state(self):
        """New accounts created by an admin/su are auto-approved; everyone
        else (self-service salespeople) start Pending. Used as the field
        default so the form reflects the right state before save, and mirrored
        in create() as the authoritative server-side guard."""
        if self.env.su or self.env.user.has_group(
                'open_whatsapp_connector.group_owa_admin'):
            return 'approved'
        return 'pending'

    @api.model_create_multi
    def create(self, vals_list):
        is_admin = self.env.su or self.env.user.has_group(
            'open_whatsapp_connector.group_owa_admin')
        for vals in vals_list:
            if (vals.get('connection_type') == 'cloud'
                    and not vals.get('cloud_verify_token')):
                vals['cloud_verify_token'] = ''.join(
                    secrets.choice(string.ascii_letters + string.digits)
                    for _ in range(20))
            if is_admin:
                # Auto-approve admin/su-created accounts. The dynamic field
                # default (_default_approval_state) already resolves to
                # 'approved' for admins, so the web form submits 'approved' and
                # this setdefault is a no-op there; it only fills the value for
                # programmatic creates that omit it. setdefault (not a hard
                # assignment) lets sudo() callers — migrations and tests —
                # deliberately create a Pending account by passing it explicitly.
                vals.setdefault('approval_state', 'approved')
            else:
                team = self.env['crm.team'].browse(vals.get('team_id')) \
                    if vals.get('team_id') else self.env['crm.team']
                if team.user_id.id != self.env.uid:
                    vals['user_id'] = self.env.uid
                vals['approval_state'] = 'pending'
        own_team = self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.account_visibility', 'shared') == 'own_team'
        if not is_admin and own_team:
            # v19 idiom: finish a self-service (non-admin) create with elevated
            # rights so the admin-only, auto-generated webhook_secret (a stored
            # computed field) can be written — v19's stricter create-time
            # field-access check blocks that system write for a non-admin, which
            # v18 allowed. Gated to own_team mode: in 'shared' mode a non-admin
            # has no create ACL, so we must fall through to the normal create
            # below and let that raise the expected AccessError. To keep v18's
            # guarantee that a non-admin cannot set any admin-only field via raw
            # RPC, strip every group-restricted field from vals first, preserving
            # only the system-generated cloud verify token set above. Ownership
            # is pinned via user_id (not create_uid); rebind to the caller's env
            # for downstream code.
            restricted = [fn for fn, f in self._fields.items() if f.groups]
            for vals in vals_list:
                cvt = vals.get('cloud_verify_token')
                for fn in restricted:
                    vals.pop(fn, None)
                if cvt:
                    vals['cloud_verify_token'] = cvt
            return super(OwaAccount, self.sudo()).create(vals_list).with_env(self.env)
        return super().create(vals_list)

    # ─── Phase 1: outbound payload shaping ──────────────────────────────
    media_max_mb = fields.Integer(
        string="Media size cap (MB)", default=50,
        help="Outbound + inbound media files larger than this are rejected "
             "(images are auto-resized first).")
    text_chunk_limit = fields.Integer(
        string="Text chunk limit", default=4000,
        help="Outbound text messages longer than this are split into chunks "
             "and sent sequentially. WhatsApp's hard limit is ~65000 chars; "
             "4000 keeps each chunk readable on a phone screen.")
    chunk_mode = fields.Selection([
        ('length', 'Length (split at character limit)'),
        ('newline', 'Newline (prefer paragraph boundaries)'),
    ], string="Chunk mode", default='newline')

    # ─── Phase 3: reactions ─────────────────────────────────────────────
    reaction_level = fields.Selection([
        ('off', 'Off (no reactions)'),
        ('ack', 'Ack only (👀 on receipt)'),
        ('manual', 'Ack + manual agent reactions'),
    ], string="Reaction level", default='off',
        help="Whether to auto-react to inbound messages. 'Off' by default so the "
             "connector never sends an unsolicited 👀 acknowledgement reaction; "
             "switch to 'Ack only' to enable it.")
    ack_reaction_emoji = fields.Char(
        string="Ack emoji", default='👀',
        help="Emoji sent immediately when an inbound message is received, "
             "before any agent or bot replies. Empty disables ack reactions.")
    ack_reaction_dm = fields.Boolean(
        string="Ack on DMs", default=True,
        help="Send ack reaction on direct messages.")
    ack_reaction_group = fields.Selection([
        ('always', 'Always'),
        ('mentions', 'Only when mentioned'),
        ('never', 'Never'),
    ], string="Ack on groups", default='mentions')
    remove_ack_after_reply = fields.Boolean(
        string="Clear ack after reply", default=True,
        help="When the bot or an agent replies to a message we ack'd, "
             "remove the original ack reaction so the recipient sees only "
             "the reply.")

    # ─── Phase 22A: triage SLA ─────────────────────────────────────────
    sla_minutes = fields.Integer(
        string="Triage SLA (minutes)", default=60,
        help="If a conversation goes unanswered or unassigned for this many "
             "minutes, an SLA-breach activity is scheduled for the assignee. "
             "Set 0 to disable.")

    # ─── Phase 23B: customer-satisfaction survey ───────────────────────
    auto_csat = fields.Boolean(
        string="Auto-send CSAT after resolve", default=False,
        help="When a WhatsApp Discuss channel flips to 'resolved', send the "
             "customer a survey link (requires the survey module and a "
             "configured CSAT survey).")
    # Plain int so the base module installs without the survey module. (#F131)
    csat_survey_id_int = fields.Integer(
        string="CSAT survey id",
        help="Numeric id of the survey.survey to send when a conversation "
             "resolves. Leave empty to disable the CSAT invite — the invite is "
             "skipped rather than sending an arbitrary survey.")
    csat_minimum_score = fields.Integer(
        string="CSAT alert threshold", default=3,
        help="Scores at or below this trigger an activity for the manager.")

    # ─── Phase 24D: project-task auto-create default ───────────────────
    # Plain int so it installs without project module.
    default_project_id_int = fields.Integer(
        string="Default project id (for inbound rules)",
        help="Numeric id of a project.project record, when the project "
             "module is installed.")

    # ─── Phase 26A: outbound throttling ────────────────────────────────
    messages_per_minute = fields.Integer(
        string="Max msgs / minute", default=0,
        help="0 = unlimited. Cron defers dispatch when the rolling-window "
             "count for this account exceeds the cap.")
    messages_per_hour = fields.Integer(string="Max msgs / hour", default=0)
    messages_per_day = fields.Integer(string="Max msgs / day", default=0)
    throttle_preset = fields.Selection([
        ('unlimited', 'Unlimited'),
        ('safe_low_volume', 'Safe — low volume (60/h)'),
        ('medium', 'Medium (300/h)'),
        ('high', 'High (1000/h)'),
    ], string="Throttle preset", default='unlimited')
    throttle_backoff_until = fields.Datetime(string="Backoff until", readonly=True)
    # Live rolling-window usage (non-stored) so you can SEE how many outbound
    # messages this account has actually sent against its caps. Recomputed on
    # each read — reload/Refresh Status to update.
    owa_sent_last_minute = fields.Integer(
        string="Sent · last minute", compute='_compute_owa_sent_usage')
    owa_sent_last_hour = fields.Integer(
        string="Sent · last hour", compute='_compute_owa_sent_usage')
    owa_sent_last_day = fields.Integer(
        string="Sent · last 24h", compute='_compute_owa_sent_usage')

    def _compute_owa_sent_usage(self):
        now = fields.Datetime.now()
        Msg = self.env['owa.message'].sudo()
        for acct in self:
            base = [('wa_account_id', '=', acct.id),
                    ('message_type', '=', 'outbound'),
                    ('state', 'in', ('sent', 'delivered', 'read'))]
            acct.owa_sent_last_minute = Msg.search_count(
                base + [('write_date', '>=', now - timedelta(minutes=1))])
            acct.owa_sent_last_hour = Msg.search_count(
                base + [('write_date', '>=', now - timedelta(hours=1))])
            acct.owa_sent_last_day = Msg.search_count(
                base + [('write_date', '>=', now - timedelta(days=1))])

    def _owa_is_throttled(self):
        """True when this account must NOT transmit right now — either inside a
        rate-limit backoff window, or already at/over a per-minute / hour / day
        outbound cap. Called by BOTH the send-queue cron and every immediate
        send path (`owa.message._send_single_message`) so throttling applies
        uniformly, not only to queued/scheduled messages. (#throttle-all-paths)"""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.throttle_backoff_until and self.throttle_backoff_until > now:
            return True
        if not (self.messages_per_minute or self.messages_per_hour
                or self.messages_per_day):
            return False
        Msg = self.env['owa.message'].sudo()
        base = [('wa_account_id', '=', self.id),
                ('message_type', '=', 'outbound'),
                ('state', 'in', ('sent', 'delivered', 'read'))]
        for cap, start in (
            (self.messages_per_minute, now - timedelta(minutes=1)),
            (self.messages_per_hour, now - timedelta(hours=1)),
            (self.messages_per_day, now - timedelta(days=1)),
        ):
            if cap and Msg.search_count(base + [('write_date', '>=', start)]) >= cap:
                return True
        return False

    # ─── Phase 26J: avatar sync ────────────────────────────────────────
    auto_sync_avatars = fields.Boolean(
        string="Auto-sync partner avatars", default=True,
        help="On first inbound from a partner with no image, fetch their "
             "WhatsApp profile picture and store it on res.partner.")

    # ─── Phase 21: missed-call auto-reply ──────────────────────────────
    auto_reply_on_missed_call = fields.Boolean(
        string="Auto-reply on missed call", default=False,
        help="When an inbound voice or video call is missed, rejected or "
             "times out, automatically queue the template below as a text "
             "reply to the caller. Requires the partner to be resolved by "
             "phone match.")
    missed_call_reply_template = fields.Text(
        string="Missed-call reply template",
        default="Sorry I missed your call! Please send a message and I'll "
                "respond shortly.",
        help="Text sent on missed/rejected/timed-out incoming calls. "
             "Supports {{partner_name}} placeholder.")

    # ─── Phase 4: read receipts / self-chat / reply quoting ────────────
    send_read_receipts = fields.Boolean(
        string="Send read receipts", default=True,
        help="If unchecked, no blue ticks are sent — recipients see only "
             "two grey ticks when their messages are received. The sidecar "
             "honours this flag at message-receive time.")
    stealth_mode = fields.Boolean(
        string="Stealth mode", default=False, tracking=True,
        help="One-click privacy mode. When on, this account never sends blue "
             "read-receipt ticks (overrides 'Send read receipts'). The sidecar "
             "already connects without broadcasting an online/last-seen status "
             "(markOnlineOnConnect=false), so with stealth on the account is "
             "effectively invisible: no online dot, no last-seen, no blue ticks.")
    share_online_presence = fields.Boolean(
        string="Show my online status", default=False,
        help="Off by default. When on, this number is marked 'online' to "
             "WhatsApp while the connector is connected — which is what lets "
             "WhatsApp stream your contacts' online/last-seen back, so the "
             "green presence dot on conversations works. TRADE-OFF: your "
             "contacts will then see your number as 'online' whenever the "
             "connector is running. Ignored while Stealth mode is on. "
             "(WhatsApp still only reveals a contact's status while they are "
             "active in the app and if their own privacy allows it.)")
    capture_contact_statuses = fields.Boolean(
        string="Capture contacts' statuses", default=False, tracking=True,
        help="Off by default. When on, the connector captures the WhatsApp "
             "Status updates (Stories) your contacts post, into the Status "
             "screen's 'Recent updates' — including downloading their photo/"
             "video media for 24h. High-volume + storage-heavy, so opt-in. "
             "Note: WhatsApp doesn't guarantee every contact's story reaches a "
             "linked device, so this captures most, not necessarily all.")
    status_mark_seen = fields.Boolean(
        string="Mark statuses as seen", default=False,
        help="Off by default (privacy). When on, opening a contact's captured "
             "status in Odoo sends WhatsApp a view receipt, so the contact can "
             "see that you viewed their status. Leave off to view invisibly.")
    sync_own_statuses = fields.Boolean(
        string="Sync statuses I post from my phone", default=False, tracking=True,
        help="Off by default. When on, WhatsApp Statuses (Stories) you post "
             "directly from your phone (or another linked device) are logged "
             "under Status → My Status, alongside the ones you post through "
             "Odoo. Statuses posted through Odoo are never double-counted.")
    # ── Office hours (drive after-hours automation: chatbot + AI agent) ──
    office_hours_start = fields.Float(
        string="Office Hours Start", default=9.0,
        help="Start of your working hours (24h, e.g. 9.0 = 09:00). Used to gate "
             "after-hours automation (schedule-based chatbot / AI agent).")
    office_hours_end = fields.Float(
        string="Office Hours End", default=18.0,
        help="End of your working hours (24h, e.g. 18.0 = 18:00).")
    office_days = fields.Char(
        string="Working Days", default='0,1,2,3,4',
        help="Comma-separated weekday numbers that count as working days "
             "(0=Monday … 6=Sunday). Default 0,1,2,3,4 = Mon–Fri.")
    office_tz = fields.Selection(
        lambda self: [(tz, tz) for tz in __import__('pytz').all_timezones],
        string="Office Timezone",
        default=lambda self: self.env.user.tz or 'UTC',
        help="Timezone used to evaluate the office-hours window above.")
    self_chat_mode = fields.Boolean(
        string="Self-chat mode", default=False,
        help="Treat this account as if WhatsApp messages may come from the "
             "same number as the account itself. Outbound replies get a "
             "[bot-name] prefix so you can tell them apart from your own "
             "manual messages, and read receipts are skipped on self-chat.")
    reply_to_mode = fields.Selection([
        ('off', 'Off (never quote)'),
        ('first', 'Quote first chunk only'),
        ('all', 'Quote every chunk'),
    ], string="Reply quoting", default='first',
       help="Whether outbound replies natively quote the inbound message "
            "they're replying to.")

    # ─── Phase 5: DM access control ────────────────────────────────────
    dm_policy = fields.Selection([
        ('open', 'Open (anyone can DM)'),
        ('allowlist', 'Allowlist (only listed numbers can DM)'),
        ('pairing', 'Pairing (unknown senders need admin approval)'),
        ('disabled', 'Disabled (drop all DMs)'),
    ], string="DM policy", default='open', tracking=True,
       help="Controls who can send direct messages to this WhatsApp account. "
            "Default 'open' keeps existing behavior; tighten to 'allowlist' "
            "or 'pairing' for restricted use.")
    allowlist_entry_ids = fields.One2many(
        'owa.allowlist.entry', 'account_id', string="Allowlist entries")

    # ─── Phase 6: group support ────────────────────────────────────────
    group_policy = fields.Selection([
        ('open', 'Open (any group)'),
        ('allowlist', 'Allowlist (only listed group JIDs)'),
        ('disabled', 'Disabled (drop all group messages)'),
    ], string="Group policy", default='open', tracking=True,
       help="Controls whether group messages are processed. Default 'open' "
            "keeps existing behavior.")
    group_allow_jids = fields.Text(
        string="Allowed group JIDs",
        help="One group JID per line. Format: <number>-<timestamp>@g.us "
             "(e.g. 120363025246125678@g.us). Get the JID from the channel's "
             "WhatsApp Number field. Only used when Group policy = 'allowlist'.")
    group_intro_message = fields.Text(
        string="Group intro message",
        help="Sent automatically the first time the bot is added to (or sees "
             "an inbound from) a new WhatsApp group.")

    # ─── Phase 8: session reliability + diagnostics ────────────────────
    last_seen_dt = fields.Datetime(
        string="Last seen", readonly=True,
        help="Last time the heartbeat cron successfully reached the sidecar "
             "for this account.")
    is_listening = fields.Boolean(
        string="Listening", readonly=True,
        help="True when the sidecar is actively delivering events to Odoo "
             "for this account.")
    health_status = fields.Selection([
        ('healthy', 'Healthy'),
        ('degraded', 'Degraded'),
        ('down', 'Down'),
        ('unknown', 'Unknown'),
    ], string="Health", compute='_compute_health_status', store=True)

    @api.depends('session_state', 'last_seen_dt', 'sidecar_running',
                 'connection_type')
    def _compute_health_status(self):
        from datetime import timedelta as _td
        now = fields.Datetime.now()
        for rec in self:
            # Cloud API accounts have no sidecar/heartbeat — health is simply
            # whether the access token last verified as connected.
            if rec.connection_type == 'cloud':
                rec.health_status = (
                    'healthy' if rec.session_state == 'connected' else 'down')
                continue
            if rec.session_state == 'connected' and rec.sidecar_running:
                if rec.last_seen_dt and (now - rec.last_seen_dt) > _td(minutes=15):
                    rec.health_status = 'degraded'
                else:
                    rec.health_status = 'healthy'
            elif rec.session_state in ('disconnected', 'logged_out') or not rec.sidecar_running:
                rec.health_status = 'down'
            else:
                rec.health_status = 'unknown'

    @api.model
    def _cron_refresh_avatars(self):
        """Phase 26J: partner-avatar refresh — currently a no-op.

        The sidecar exposes no profile-picture lookup route (`GET /profile-pic/:jid`
        was never implemented), so the old per-contact fetch always failed silently
        and wasted one request per channel on every run. Re-enabling avatar sync
        requires adding that route to the sidecar — a sidecar change we avoid so
        existing installs don't need a rebuild on upgrade. Kept as a stub so the
        scheduled action keeps referencing a valid method.

        Per-entity, on-demand picture fetch is now available via
        ``_fetch_picture_b64`` (used by the group/community/newsletter Refresh
        buttons and the contact import) — this background cron stays disabled to
        avoid per-contact load on every install."""
        return

    def _fetch_picture_b64(self, jid, hi_res=True):
        """Best-effort: download a JID's WhatsApp profile picture and return it
        as a base64 string ready to assign to a fields.Image (image_1920), or
        None when there is no picture / privacy hides it / the fetch fails.
        Never raises. QR-only: ``_get_baileys_api`` raises a clean UserError for
        cloud accounts, which we swallow here. (#wa-fetch-pic)"""
        self.ensure_one()
        if not jid:
            return None
        try:
            res = self._get_baileys_api().fetch_profile_picture(jid, hi_res=hi_res)
        except Exception:
            _logger.exception(
                "WhatsApp profile-picture fetch failed for %s on account %s",
                jid, self.name)
            return None
        return (res or {}).get('image_base64') or None

    @api.model
    def _cron_heartbeat(self):
        """Phase 8: ping the sidecar for each connected account; updates
        last_seen_dt + is_listening. Graceful on sidecar-down (just leaves
        last_seen_dt stale and recomputes health_status)."""
        # Sidecar heartbeat is QR-only; Cloud API accounts have no sidecar and
        # their session_state is owned by action_verify_connection + the
        # Meta webhook, so they must be excluded or this cron would keep
        # flipping a connected Cloud account back to 'disconnected'.
        for account in self.search([('active', '=', True),
                                    ('connection_type', '=', 'qr')]):
            try:
                api = account._get_baileys_api()
                status = api.get_session_status()
                state = (status or {}).get('status') or 'unknown'
                vals = {
                    'last_seen_dt': fields.Datetime.now(),
                    'is_listening': state == 'connected',
                }
                if state and state != account.session_state:
                    vals['session_state'] = state
                account.write(vals)
            except Exception:
                # Sidecar unreachable — flip is_listening off but don't bury
                # last_seen_dt so we know how long it's been.
                if account.is_listening:
                    account.is_listening = False

    @api.model
    def _handle_presence_update(self, account, chat_jid, presences):
        """Phase B3: surface inbound presence (online / typing / paused) on
        the matching Discuss WhatsApp channel via a bus notification.

        Cheap to call frequently — it never writes the DB, only pushes a
        transient bus message that any open Discuss tab can ignore.
        """
        if not chat_jid or not presences:
            return False
        Channel = self.env['discuss.channel'].sudo()
        # DM JIDs arrive as '917012345678@s.whatsapp.net' but whatsapp_number
        # stores bare digits for 1:1 channels; group/newsletter JIDs (@g.us,
        # @newsletter, @broadcast) are stored verbatim. Without this strip the
        # equality match always failed for DMs, so the green dot / typing
        # indicator never lit up for any 1:1 conversation. (#presence)
        lookup_jid = (chat_jid.split('@')[0]
                      if '@s.whatsapp.net' in chat_jid else chat_jid)
        channel = Channel.search([
            ('owa_account_id', '=', account.id),
            ('whatsapp_number', '=', lookup_jid),
            ('channel_type', '=', 'whatsapp'),
        ], limit=1)
        if not channel:
            return False
        # Compact the payload: only forward the chat-level "is anyone typing?".
        any_typing = any(
            (p.get('lastKnownPresence') in ('composing', 'recording'))
            for p in presences
        )
        any_online = any(
            (p.get('lastKnownPresence') == 'available')
            for p in presences
        )
        try:
            channel._bus_send('owa_presence', {
                'channel_id': channel.id,
                'typing': any_typing,
                'online': any_online,
            })
        except Exception:  # pragma: no cover -- bus push is best-effort
            _logger.exception(
                "owa.account._handle_presence_update: bus push failed for jid=%s",
                chat_jid,
            )
        return True

    def _ensure_owa_admin(self):
        """Server-side guard for account-management actions.

        The ``groups=`` attribute on a view button only HIDES it in the UI — it
        does not stop a direct JSON-RPC ``call_kw``. Several of these methods
        act on the live sidecar / Cloud API BEFORE any ORM write, so the
        read-only ACL on owa.account is not enough on its own. Every connect /
        disconnect / logout / sidecar / diagnostics / cloud action funnels
        through here so the WhatsApp-Administrator check is enforced on the
        server regardless of entry point. (base.group_system implies the
        group, so Settings admins pass.)"""
        if not self.env.su and not self.env.user.has_group(
                'open_whatsapp_connector.group_owa_admin'):
            raise AccessError(_(
                "Only WhatsApp Administrators can manage account connections, "
                "the sidecar, or run diagnostics."))

    def _ensure_can_manage(self):
        """Self-service guard. In 'shared' account-visibility mode this is
        admin-only (today's lockdown). In 'own_team' mode the account's owner
        and that account's Sales-Team leader may also manage it; everyone else
        is refused. (su/crons bypass.)"""
        if self.env.su:
            return
        user = self.env.user
        if user.has_group('open_whatsapp_connector.group_owa_admin'):
            return
        own_team = self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.account_visibility', 'shared') == 'own_team'
        if not own_team:
            raise AccessError(_(
                "Only WhatsApp Administrators can manage account connections, "
                "the sidecar, or run diagnostics."))
        for acc in self:
            if acc.user_id == user or acc.team_id.user_id == user:
                continue
            raise AccessError(_(
                "You can only add, scan or manage your own WhatsApp account."))

    @api.depends_context('uid')
    def _compute_user_can_manage(self):
        """UI mirror of `_ensure_can_manage`: True when the current user may
        connect/scan/manage this account, so the connection buttons surface for
        a self-service owner (own_team mode) without exposing them to every
        agent. Never raises — purely a visibility flag."""
        is_admin = self.env.su or self.env.user.has_group(
            'open_whatsapp_connector.group_owa_admin')
        own_team = self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.account_visibility', 'shared') == 'own_team'
        user = self.env.user
        for acc in self:
            acc.user_can_manage = bool(is_admin or (
                own_team and (acc.user_id == user or acc.team_id.user_id == user)))

    def action_run_diagnostics(self):
        """Phase 8: chatter-posted diagnostic report. Replaces the
        ``openclaw channels doctor`` CLI with a one-click admin button."""
        self.ensure_one()
        self._ensure_owa_admin()
        from odoo.addons.open_whatsapp_connector.tools.sidecar_manager import is_sidecar_running
        from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
        sidecar_url = self.sidecar_url or 'http://localhost:3100'
        sidecar_up = is_sidecar_running(sidecar_url)
        outgoing = self.env['owa.message'].sudo().search_count([
            ('wa_account_id', '=', self.id), ('state', '=', 'outgoing'),
        ])
        last_msg = self.env['owa.message'].sudo().search([
            ('wa_account_id', '=', self.id),
        ], order='create_date desc', limit=1)
        try:
            status = BaileysApi(self).get_session_status()
            sidecar_state = (status or {}).get('status', 'unknown')
        except Exception as e:
            sidecar_state = f'unreachable: {e}'

        rows = [
            ('Sidecar URL', sidecar_url),
            ('Sidecar process up', '✓' if sidecar_up else '✗'),
            ('Sidecar session state', sidecar_state),
            ('Odoo session_state', self.session_state),
            ('health_status', self.health_status),
            ('is_listening', self.is_listening),
            ('Outgoing queue depth', outgoing),
            ('Last message at', fields.Datetime.to_string(last_msg.create_date) if last_msg else '—'),
            ('last_seen_dt', fields.Datetime.to_string(self.last_seen_dt) if self.last_seen_dt else '—'),
        ]
        # Append the registered setup checks (sidecar path / build / Node.js)
        # so the report tells the admin exactly what is missing to connect.
        from odoo.addons.open_whatsapp_connector.tools import diagnostics_registry
        _diag_icon = {'ok': '✓', 'warn': '⚠', 'error': '✗'}
        for _name, label, status, detail in diagnostics_registry.run_all(self):
            mark = _diag_icon.get(status, status)
            rows.append((label, ('%s %s' % (mark, detail)) if detail else mark))
        body = Markup(
            '<table class="table table-sm" style="margin: 0;"><tbody>'
            + ''.join(
                Markup('<tr><td style="font-weight:600; padding-right: 14px;">{}</td>'
                       '<td>{}</td></tr>').format(k, v)
                for k, v in rows
            )
            + '</tbody></table>'
        )
        self.message_post(body=body, subject="WhatsApp Diagnostics")
        return True

    def _effective_send_read_receipts(self):
        """Read-receipts actually sent to the sidecar: suppressed when stealth
        mode is on (stealth overrides the granular toggle). (#diag)"""
        self.ensure_one()
        return bool(self.send_read_receipts) and not self.stealth_mode

    def _effective_share_online_presence(self):
        """Whether the sidecar should mark this number 'online' (so WhatsApp
        streams contacts' presence back). Suppressed while stealth mode is on —
        stealth means 'be invisible', which is incompatible with broadcasting
        an online status."""
        self.ensure_one()
        return bool(self.share_online_presence) and not self.stealth_mode

    def write(self, vals):
        """Phase 4: when send_read_receipts (or stealth_mode) toggles on a
        connected session, push the change to the sidecar without requiring a
        reconnect."""
        # Approval is admin-only. A self-service owner / team leader can edit
        # their own account (own_team manage ACL), but must not be able to
        # self-approve via a raw write() — only action_approve / action_reject
        # (both admin-gated) may move it. View readonly/groups= is
        # RPC-bypassable, so enforce on the model.
        if ({'approval_state', 'approved_user_id', 'approved_date'} & set(vals)
                and not (self.env.su or self.env.user.has_group(
                    'open_whatsapp_connector.group_owa_admin'))):
            raise AccessError(_(
                "Only a WhatsApp Administrator can change an account's "
                "approval status."))
        # Arm the one-shot "Import WhatsApp Data" prompt the instant a QR is
        # DISPLAYED (any path that sets qr_code_base64 truthy on a QR account),
        # not at the moment of 'connected'. A connect-time "is a QR still
        # showing?" check misses real scans: the WhatsApp client emits a transient
        # 'disconnected' (restartRequired) right after the scan that clears
        # qr_code_base64 before the 'connected' write arrives. Showing a QR
        # only ever happens on a genuine fresh pairing — a reconnect from saved
        # creds never shows one — so this re-pops on every fresh scan but never
        # on a plain reconnect. The always-mounted wa_import_prompt widget
        # consumes the flag once connected; a clean logout clears it below.
        # (#import_prompt_flag)
        if (vals.get('qr_code_base64')
                and 'import_prompt_pending' not in vals
                and self and all(a.connection_type == 'qr' for a in self)):
            vals = dict(vals, import_prompt_pending=True)
        if vals.get('session_state') == 'logged_out':
            vals = dict(vals, import_prompt_pending=False)
        # Detect the QR transition into 'connected' so we can re-arm presence
        # subscriptions once the socket is live (captured before the write).
        newly_connected = self.browse()
        if vals.get('session_state') == 'connected':
            newly_connected = self.filtered(
                lambda a: a.connection_type == 'qr'
                and a.session_state != 'connected')
        result = super().write(vals)
        if ({'send_read_receipts', 'stealth_mode', 'share_online_presence',
             'capture_contact_statuses', 'status_mark_seen',
             'sync_own_statuses'} & set(vals)):
            for account in self:
                if account.session_state == 'connected':
                    try:
                        api = account._get_baileys_api()
                        api.update_session_config(
                            send_read_receipts=account._effective_send_read_receipts(),
                            share_online_presence=account._effective_share_online_presence(),
                            capture_contact_statuses=account.capture_contact_statuses,
                            status_mark_seen=account.status_mark_seen,
                            sync_own_statuses=account.sync_own_statuses)
                    except Exception:
                        _logger.exception("Failed to push read-receipt config to sidecar for %s", account.id)
        for account in newly_connected:
            try:
                account._subscribe_presence_for_channels()
            except Exception:
                _logger.exception(
                    "presence bulk-subscribe failed for %s", account.id)
        if {'notify_user_ids', 'allowed_group_id'} & set(vals):
            try:
                self._sync_notify_members_to_channels()
            except Exception:
                _logger.exception(
                    "Failed to sync access members to channels for %s", self.ids)
        return result

    def _owa_access_partners(self):
        """The internal (non-share) partners allowed to see this account's
        WhatsApp conversations: the Users to Notify PLUS every member of the
        Allowed Group. Single source of truth for channel membership, so
        "shared" visibility genuinely means "everyone in the pool sees it".
        (#allowed-group)"""
        self.ensure_one()
        # v19: res.groups members are `user_ids` (v18/v17 use `users`).
        users = self.notify_user_ids | self.allowed_group_id.user_ids
        return users.filtered(lambda u: not u.share).partner_id

    @api.model
    def _owa_fallback_account(self):
        """A connected account to fall back to when a conversation has no account
        or a disconnected one: the configured Default WhatsApp Account if it is
        connected, else the first connected account. (#delete-reassign)"""
        ICP = self.env['ir.config_parameter'].sudo()
        default_id = int(ICP.get_param(
            'open_whatsapp_connector.default_account_id') or 0)
        default_acct = self.browse(default_id).exists() if default_id else self.browse()
        if default_acct and default_acct.session_state == 'connected':
            return default_acct
        return self.search([('session_state', '=', 'connected')], limit=1)

    def _sync_notify_members_to_channels(self):
        """Add each account's access pool (Users to Notify + Allowed Group
        members) as members of ALL its existing WhatsApp channels. Add-only
        (never removes). No-op unless visibility is 'shared' — in 'own_agent'
        mode channels are intentionally isolated to their assignee, so broadly
        adding the pool would de-isolate them. (#S4-1)(#allowed-group)"""
        if self.env['ir.config_parameter'].sudo().get_param(
                'open_whatsapp_connector.chat_visibility', 'shared') != 'shared':
            return
        Channel = self.env['discuss.channel'].sudo()
        for account in self:
            partners = account._owa_access_partners()
            if not partners:
                continue
            channels = Channel.search([
                ('channel_type', '=', 'whatsapp'),
                ('owa_account_id', '=', account.id),
            ])
            for channel in channels:
                missing = partners - channel.channel_member_ids.partner_id
                if missing:
                    channel.channel_member_ids = [
                        Command.create({'partner_id': p.id}) for p in missing]
                    channel._broadcast(missing.ids)

    def action_owa_resync_members(self):
        """Manual "Resync Members" button: push the current access pool (Users
        to Notify + Allowed Group members) into every existing conversation of
        this account, so a newly-chosen/expanded Allowed Group applies to chats
        that already exist. Add-only. Shared-visibility mode only."""
        self._sync_notify_members_to_channels()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp access"),
                'message': _("Conversation access resynced for the selected "
                             "account(s)."),
                'type': 'success',
                'sticky': False,
            },
        }

    def unlink(self):
        """Reassign an account's conversations before it is deleted.

        The channel→account and message→account FKs are ``ondelete='set null'``,
        so a plain delete would NULL every conversation's account and leave those
        chats permanently un-repliable (the send path rejects a message with no
        connected account). Instead we re-point each deleted account's
        conversations (and its still-pending outbound messages) to the configured
        Default WhatsApp Account, else to another connected account. If neither
        exists we refuse the delete with a clear message rather than orphaning the
        chats. (#delete-reassign)"""
        Channel = self.env['discuss.channel'].sudo()
        Message = self.env['owa.message'].sudo()
        ICP = self.env['ir.config_parameter'].sudo()
        default_id = int(ICP.get_param(
            'open_whatsapp_connector.default_account_id') or 0)
        # A Default account can't be deleted while its "Default Account" checkbox
        # is ticked: it is the reassign/fallback target for orphaned chats, and
        # deleting it would leave `default_account_id` pointing at a dead row.
        # Deletion is otherwise unrestricted — the admin just has to untick
        # Default (or move it to another account) first. (#default-account-checkbox)
        if default_id and default_id in self.ids:
            blocked = self.browse(default_id)
            raise UserError(_(
                "“%(name)s” is the Default WhatsApp Account, so it can't be "
                "deleted yet. Untick its “Default Account” checkbox — or set "
                "another account as Default — then delete it.",
                name=blocked.name))
        default_acct = self.browse(default_id).exists() if default_id else self.browse()
        for account in self:
            channels = Channel.search([
                ('channel_type', '=', 'whatsapp'),
                ('owa_account_id', '=', account.id),
            ])
            # Pending outbound is keyed on the message's account, not on a
            # channel — reassign it even for a channel-less (e.g. broadcast-only)
            # account so queued/retryable sends survive the delete.
            pending = Message.search([
                ('wa_account_id', '=', account.id),
                ('state', 'in', ('outgoing', 'error')),
            ])
            if not channels and not pending:
                continue
            # Prefer the configured Default Account (if it is connected and not
            # itself being deleted); otherwise the first OTHER connected account.
            if (default_acct and default_acct not in self
                    and default_acct.session_state == 'connected'):
                target = default_acct
            else:
                target = self.search([
                    ('id', 'not in', self.ids),
                    ('session_state', '=', 'connected'),
                ], limit=1)
            if not target:
                raise UserError(_(
                    "The WhatsApp account “%(name)s” still has %(n)s "
                    "conversation(s)/queued message(s). Reassign or archive them "
                    "first, or set a connected Default WhatsApp Account in "
                    "Settings, so they stay repliable after this account is "
                    "removed.",
                    name=account.name, n=len(channels) + len(pending)))
            # Re-point each conversation. A channel whose number ALREADY exists
            # on the target account can't be re-pointed (it would violate the
            # per-(account, number) unique index) — those are genuinely separate
            # threads (same customer, two of the business's numbers), so archive
            # the redundant one rather than interleave histories; the live
            # conversation continues on the target's existing channel.
            for ch in channels:
                if Channel.search_count([
                        ('channel_type', '=', 'whatsapp'),
                        ('owa_account_id', '=', target.id),
                        ('whatsapp_number', '=', ch.whatsapp_number),
                        ('id', '!=', ch.id)]):
                    ch.active = False
                else:
                    ch.owa_account_id = target.id
            if pending:
                pending.write({'wa_account_id': target.id})
        return super().unlink()

    # Counts
    message_count = fields.Integer(compute='_compute_message_count')

    @api.constrains('notify_user_ids')
    def _check_notify_user_ids(self):
        for account in self:
            if len(account.notify_user_ids) < 1:
                raise ValidationError(_("At least one user to notify is required"))

    @api.constrains('name')
    def _check_name_normalized(self):
        """Phase 11: account name is used as part of the sidecar account_id
        (`<dbname>_<id>` plus `name` is the human reference). Reject leading/
        trailing whitespace and inner double-spaces so account_ids are
        consistent across the codebase."""
        for account in self:
            if not account.name:
                continue
            stripped = account.name.strip()
            if stripped != account.name or '  ' in stripped:
                raise ValidationError(_(
                    "Account name '%s' has leading/trailing whitespace or "
                    "double spaces. Please use the cleaned form '%s'."
                ) % (account.name, ' '.join(stripped.split())))

    @api.depends('name')
    def _compute_webhook_secret(self):
        for rec in self:
            if rec.id and not rec.webhook_secret:
                rec.webhook_secret = ''.join(
                    secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
                )

    def _compute_callback_url(self):
        for account in self:
            db_name = quote(self.env.cr.dbname or '')
            account.callback_url = (
                account.get_base_url()
                + f'/open_whatsapp_connector/webhook/incoming?db={db_name}'
            )

    def _compute_message_count(self):
        for account in self:
            account.message_count = self.env['owa.message'].search_count(
                [('wa_account_id', '=', account.id)]
            )

    @api.depends('sidecar_url', 'connection_type')
    def _compute_sidecar_running(self):
        from odoo.addons.open_whatsapp_connector.tools.sidecar_manager import is_sidecar_running
        cache = {}
        for account in self:
            # The sidecar is a QR-transport concept; Cloud API accounts use
            # Meta's Graph API and have no sidecar. Skip the ping for them so
            # the QR-only buttons stay hidden and we don't waste a request.
            if account.connection_type and account.connection_type != 'qr':
                account.sidecar_running = False
                continue
            url = account.sidecar_url or 'http://localhost:3100'
            if url not in cache:
                cache[url] = is_sidecar_running(url)
            account.sidecar_running = cache[url]

    def _get_baileys_api(self):
        """Get a BaileysApi instance for this account.

        Backstop for the whole family of QR-only actions (connect, logout,
        profile picture, pairing code, blocklist sync, test connection, …):
        a cloud account has no Baileys sidecar session, so calling any of
        them would hit the sidecar and surface a raw 500 traceback. Raise a
        clean, explanatory UserError instead. The views also hide these
        controls for cloud accounts; this guards the code path itself.
        """
        self.ensure_one()
        if self.connection_type and self.connection_type != 'qr':
            raise UserError(_(
                "This action is only available for QR-connected WhatsApp "
                "accounts. '%s' uses the Official Cloud API, which manages "
                "the profile, pairing and connection through the Meta "
                "console instead.") % (self.name or ''))
        return BaileysApi(self)

    # ── Official Cloud API transport seam ─────────────────────────────
    def _get_cloud_api(self):
        """Return a CloudApi client bound to this account."""
        self.ensure_one()
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import CloudApi
        return CloudApi(self)

    def _account_messaging_blocked(self):
        """True when self-service approval is in force and this account is not
        yet approved — its messages must not be sent or received. No effect in
        'shared' mode (approval_state ignored)."""
        self.ensure_one()
        own_team = self.env['ir.config_parameter'].sudo().get_param(
            'open_whatsapp_connector.account_visibility', 'shared') == 'own_team'
        return bool(own_team and self.approval_state != 'approved')

    def _dispatch_send(self, kind, **payload):
        """Transport-neutral outbound dispatch.

        ``kind`` in {'text', 'media', 'reaction', 'template', 'mark_read'}.
        Routes by ``connection_type`` and returns the provider message id
        (a ``wamid...`` for cloud accounts). Keyword names in ``payload``
        must match the target client's method signature; the send-queue
        builds the right kwargs per transport (see owa.message).
        """
        self.ensure_one()
        if self._account_messaging_blocked():
            raise UserError(_(
                "This WhatsApp account is pending administrator approval and "
                "cannot send messages yet."))
        method = kind if kind == 'mark_read' else 'send_' + kind
        if self.connection_type == 'cloud':
            api = self._get_cloud_api()
        else:
            # QR / Baileys path — preserve existing behaviour.
            api = self._get_baileys_api()
        return getattr(api, method)(**payload)

    # ── Cloud 24h customer-service window ──────────────────────────────
    def _cloud_within_window(self, mobile):
        """True when an INBOUND owa.message exists for ``mobile`` on this
        account within the last 24 hours.

        Meta's Cloud API only allows free-form (non-template) messages inside
        a 24h customer-service window opened by the customer's last inbound
        message. Outside that window only an approved template may be sent.
        """
        self.ensure_one()
        if not mobile:
            return False
        number = (mobile or '').lstrip('+')
        cutoff = fields.Datetime.now() - timedelta(hours=24)
        # Match either the raw or the formatted form of the number; inbound
        # rows store mobile_number / mobile_number_formatted as bare digits.
        return bool(self.env['owa.message'].sudo().search_count([
            ('wa_account_id', '=', self.id),
            ('message_type', '=', 'inbound'),
            ('create_date', '>=', cutoff),
            '|',
                ('mobile_number', 'in', (number, '+' + number, mobile)),
                ('mobile_number_formatted', 'in', (number, '+' + number, mobile)),
        ]))

    # ── Cloud delivery/read/failed status receipts ────────────────────
    _CLOUD_STATUS_MAP = {
        'sent': 'sent',
        'delivered': 'delivered',
        'read': 'read',
        'failed': 'error',
    }

    def _apply_cloud_status(self, status):
        """Apply a Meta status receipt (sent/delivered/read/failed) to the
        matching outbound owa.message, correlated by ``wa_message_uid``."""
        wamid = status.get('id')
        new_state = self._CLOUD_STATUS_MAP.get(status.get('status'))
        if not wamid or not new_state:
            return
        msg = self.env['owa.message'].sudo().search([
            ('wa_message_uid', '=', wamid),
            ('wa_account_id', '=', self.id),
        ], limit=1)
        if not msg:
            return
        vals = {'state': new_state}
        if new_state == 'error':
            errs = status.get('errors') or [{}]
            vals['error_message'] = (
                errs[0].get('title') or errs[0].get('message') or 'failed')
        msg.write(vals)

    # ── Cloud: inbound call events ─────────────────────────────────────
    def _handle_cloud_call(self, call):
        """Transport-neutral intake for one Meta Cloud API call event.

        Maps the Meta ``calls[]`` payload onto the shared owa.call.log
        pipeline so an inbound WhatsApp Business call shows up as a call
        log / missed-call entry with the same chatter line, ringing toast
        and missed-call auto-reply the QR transport already produces.

        Only inbound (``USER_INITIATED``) calls are handled; outbound
        (``BUSINESS_INITIATED``) call legs we placed are ignored. The
        Calling API is voice-only, so the resulting log is always voice.
        """
        self.ensure_one()
        from odoo.addons.open_whatsapp_connector.tools.meta_inbound import (
            normalize_meta_call)
        payload = normalize_meta_call(call)
        if not payload:
            return False
        if payload.get('direction') and payload['direction'] != 'USER_INITIATED':
            return False
        # A `terminate` maps to 'missed'; don't let it regress a call we
        # already settled locally (e.g. the agent clicked Reject, then Meta
        # sends the matching terminate webhook).
        if payload.get('status') == 'timeout':
            existing = self.env['owa.call.log'].sudo().search([
                ('wa_account_id', '=', self.id),
                ('call_id', '=', payload['id']),
            ], limit=1)
            if existing and existing.state in ('rejected', 'accepted'):
                return existing
        return self.env['owa.call.log'].sudo()._record_call_event(self, payload)

    # ── Cloud connection health check ─────────────────────────────────
    def action_verify_connection(self):
        """Ping the Graph API for each cloud account; flip session_state to
        'connected' on success, 'disconnected' on any failure."""
        self._ensure_can_manage()
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApi, CloudApiError)
        for acc in self.filtered(lambda a: a.connection_type == 'cloud'):
            try:
                CloudApi(acc).health()
                acc.session_state = 'connected'
            except (CloudApiError, Exception) as e:
                acc.session_state = 'disconnected'
                _logger.warning(
                    "Cloud verify failed for %s: %s", acc.name, e)
        return True

    def action_sync_templates(self):
        """Pull message templates + statuses from Meta into owa.cloud.template
        (upsert by wa_template_uid, else by name+language)."""
        self._ensure_owa_admin()
        Template = self.env['owa.cloud.template'].sudo()
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApiError)
        for acc in self.filtered(lambda a: a.connection_type == 'cloud'):
            try:
                rows = acc._get_cloud_api().sync_templates()
            except (CloudApiError, Exception) as e:
                _logger.warning("Template sync failed for %s: %s", acc.name, e)
                continue
            for data in rows:
                uid = data.get('id')
                rec = Template.browse()
                if uid:
                    rec = Template.search(
                        [('wa_template_uid', '=', str(uid))], limit=1)
                if not rec:
                    rec = Template.search([
                        ('wa_account_id', '=', acc.id),
                        ('name', '=', data.get('name')),
                        ('lang_code', '=', data.get('language') or 'en'),
                    ], limit=1)
                if not rec:
                    rec = Template.create({
                        'name': data.get('name') or 'template',
                        'lang_code': data.get('language') or 'en',
                        'wa_account_id': acc.id,
                        'wa_template_uid': uid,
                    })
                rec._apply_meta_template_dict(data)
        return True

    # ── Cloud phone-number / WABA management ───────────────────────────
    def _cloud_notify(self, title, message, kind='info'):
        """Return a display_notification client action (sticky on danger)."""
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': kind,
                       'sticky': kind == 'danger'}}

    def action_cloud_list_numbers(self):
        """List the WABA's phone numbers (id / verified name / status)."""
        self.ensure_one()
        self._ensure_owa_admin()
        nums = self._get_cloud_api().list_phone_numbers()
        lines = [
            "%s — %s (%s)" % (n.get('display_phone_number'),
                              n.get('verified_name') or '?',
                              n.get('code_verification_status') or '?')
            for n in nums] or [_("No phone numbers found.")]
        return self._cloud_notify(_("Phone numbers"), "\n".join(lines))

    def action_cloud_waba_info(self):
        """Show the WhatsApp Business Account name / currency / review state."""
        self.ensure_one()
        self._ensure_owa_admin()
        info = self._get_cloud_api().get_waba_info()
        msg = _("Name: %(name)s\nCurrency: %(cur)s\nReview: %(rev)s") % {
            'name': info.get('name') or '?', 'cur': info.get('currency') or '?',
            'rev': info.get('account_review_status') or '?'}
        return self._cloud_notify(_("Business Account"), msg)

    def action_cloud_subscribe_app(self):
        """Subscribe this Meta app to the WABA's webhooks."""
        self.ensure_one()
        self._ensure_owa_admin()
        self._get_cloud_api().subscribe_app()
        return self._cloud_notify(
            _("App subscribed"),
            _("This app is now subscribed to the WABA's webhooks."), 'success')

    def action_cloud_request_code(self):
        """Ask Meta to send an SMS verification code to the number."""
        self.ensure_one()
        self._ensure_owa_admin()
        self._get_cloud_api().request_code()
        return self._cloud_notify(
            _("Verification code requested"),
            _("Meta is sending a verification code by SMS. Enter it via "
              "'Verify Code'."), 'success')

    def action_cloud_deregister(self):
        """Deregister the phone number from the Cloud API."""
        self.ensure_one()
        self._ensure_owa_admin()
        self._get_cloud_api().deregister_phone()
        return self._cloud_notify(_("Deregistered"),
                                  _("The phone number was deregistered."),
                                  'success')

    def _action_cloud_phone_wizard(self, mode):
        self.ensure_one()
        self._ensure_owa_admin()
        return {
            'type': 'ir.actions.act_window',
            'name': _("WhatsApp Cloud — Phone"),
            'res_model': 'owa.cloud.phone.wizard',
            'view_mode': 'form', 'target': 'new',
            'context': {'default_account_id': self.id, 'default_mode': mode}}

    def action_cloud_register(self):
        return self._action_cloud_phone_wizard('register')

    def action_cloud_verify_code(self):
        return self._action_cloud_phone_wizard('verify_code')

    def action_cloud_set_pin(self):
        return self._action_cloud_phone_wizard('set_pin')

    @api.model
    def get_embedded_signup_config(self):
        """Public (non-secret) Meta App config for the Embedded Signup JS —
        App ID, Embedded Signup configuration id, and Graph version. The App
        Secret is never exposed to the client."""
        ICP = self.env['ir.config_parameter'].sudo()
        return {
            'app_id': ICP.get_param(
                'open_whatsapp_connector.meta_app_id', ''),
            'config_id': ICP.get_param(
                'open_whatsapp_connector.meta_config_id', ''),
            'version': ICP.get_param(
                'open_whatsapp_connector.meta_api_version', 'v23.0'),
        }

    def button_connect(self):
        """Connect to WhatsApp via the sidecar (triggers QR code)."""
        self.ensure_one()
        self._ensure_can_manage()
        if not self.enabled:
            from odoo.exceptions import UserError as _UserError
            raise _UserError(_(
                "Account is disabled. Tick 'Enabled' before connecting."
            ))
        if self.pairing_method == 'code' and not self.pairing_phone:
            from odoo.exceptions import UserError as _UserError
            raise _UserError(_(
                "Set the WhatsApp phone number in 'Pairing Phone' below before "
                "clicking Connect, or switch Pairing Method to 'Scan QR Code'."
            ))
        self._ensure_sidecar_running()
        api = self._get_baileys_api()
        # Single-scan history import: arm history capture BEFORE creating the
        # session so a fresh QR/pairing-code link streams the existing
        # chats/contacts/messages into the sidecar buffer during that one scan.
        # The post-connect "Import WhatsApp Data" dialog then drains the buffer
        # with NO second scan. Only on a genuinely fresh link — never on a live
        # reconnect of an already-paired number (that path keeps the existing
        # re-link behaviour so it isn't blocked by an empty buffer) — and
        # best-effort: a failure here must never block connecting. (#single_scan)
        is_fresh_link = (not self.phone_number) or self.session_state == 'logged_out'
        if self.session_state != 'connected' and is_fresh_link:
            try:
                api.history_arm()
            except Exception:
                _logger.exception(
                    "Arm history-on-connect failed for account %s", self.id)
        try:
            result = api.create_session(
                odoo_base_url=self.get_base_url(),
                callback_url=self.callback_url,
                webhook_secret=self.webhook_secret,
                send_read_receipts=self._effective_send_read_receipts(),
                share_online_presence=self._effective_share_online_presence(),
                capture_contact_statuses=self.capture_contact_statuses,
                status_mark_seen=self.status_mark_seen,
                sync_own_statuses=self.sync_own_statuses,
            )
            # Do NOT write session_state / qr_code_base64 / phone_number on the
            # account here. create_session() makes the sidecar start the socket,
            # which immediately streams connection.update events to the
            # /webhook/connection endpoint — and THAT handler writes exactly
            # these fields (and bus-pushes them live to the form). Writing them
            # again from this RPC raced with that webhook burst on the SAME row:
            # both transactions flush "UPDATE owa_account" -> PostgreSQL
            # "could not serialize access due to concurrent update", and because
            # each retry re-ran create_session() (re-triggering the burst) every
            # retry failed and the user saw "Oops! Something went wrong". The
            # connection webhook is the single source of truth for connect state.
            state = result.get('status', 'connecting')
            # Already connected (existing creds, no QR to scan): warm presence
            # for existing channels. The 'connected' webhook sets state + phone.
            if state == 'connected':
                # Phase B3 — bulk-subscribe presence for every existing
                # WhatsApp channel so the discuss UI can show online/typing
                # without waiting for the next inbound message.
                try:
                    self._subscribe_presence_for_channels(api=api)
                except Exception:
                    _logger.exception(
                        "subscribe presence for channels failed for account %s", self.id)
        except BaileysError as e:
            raise UserError(_("Failed to connect: %s", e.error_message))

    @api.model
    def cleanup_duplicate_whatsapp_channels(self):
        """Merge duplicate WhatsApp channels having the same
        (owa_account_id, whatsapp_number) into the oldest one. Moves
        every mail.message and member to the keeper, then unlinks the
        duplicates. Idempotent — safe to call repeatedly."""
        self.env.cr.execute("""
            SELECT owa_account_id, whatsapp_number, array_agg(id ORDER BY id) AS ids
            FROM discuss_channel
            WHERE channel_type = 'whatsapp'
              AND owa_account_id IS NOT NULL
              AND whatsapp_number IS NOT NULL
            GROUP BY owa_account_id, whatsapp_number
            HAVING COUNT(*) > 1
        """)
        groups_merged = 0
        channels_deleted = 0
        Channel = self.env['discuss.channel'].sudo()
        for _acc_id, _number, ids in self.env.cr.fetchall():
            keeper_id = ids[0]
            dupe_ids = list(ids[1:])
            if not dupe_ids:
                continue
            self.env.cr.execute("""
                UPDATE mail_message
                SET res_id = %s
                WHERE model = 'discuss.channel'
                  AND res_id = ANY(%s)
            """, (keeper_id, dupe_ids))
            # Move members from dupes to keeper, skipping any whose
            # (channel, partner) pair already exists on the keeper to
            # avoid violating discuss_channel_member's unique index.
            self.env.cr.execute("""
                DELETE FROM discuss_channel_member
                WHERE channel_id = ANY(%s)
                  AND partner_id IN (
                    SELECT partner_id FROM discuss_channel_member
                    WHERE channel_id = %s
                  )
            """, (dupe_ids, keeper_id))
            self.env.cr.execute("""
                UPDATE discuss_channel_member
                SET channel_id = %s
                WHERE channel_id = ANY(%s)
            """, (keeper_id, dupe_ids))
            Channel.browse(dupe_ids).unlink()
            groups_merged += 1
            channels_deleted += len(dupe_ids)
        return {'groups_merged': groups_merged, 'channels_deleted': channels_deleted}

    def _subscribe_presence_for_channels(self, api=None):
        """Push every existing WhatsApp channel JID for this account to
        the sidecar so it subscribes to their presence updates."""
        self.ensure_one()
        Channel = self.env['discuss.channel'].sudo()
        channels = Channel.search([
            ('owa_account_id', '=', self.id),
            ('channel_type', '=', 'whatsapp'),
            ('whatsapp_number', '!=', False),
        ])
        jids = sorted({ch.whatsapp_number for ch in channels if ch.whatsapp_number})
        if not jids:
            return {'subscribed': 0, 'total': 0}
        api = api or self._get_baileys_api()
        return api.subscribe_presence(jids)

    def _ensure_sidecar_running(self):
        """Try to auto-start sidecar if configured and not reachable."""
        from odoo.addons.open_whatsapp_connector.tools.sidecar_manager import (
            is_sidecar_running, start_sidecar,
        )
        if is_sidecar_running(self.sidecar_url):
            return
        ICP = self.env['ir.config_parameter'].sudo()
        auto_start = ICP.get_param('open_whatsapp_connector.sidecar_auto_start', 'False')
        sidecar_path = ICP.get_param('open_whatsapp_connector.sidecar_path', '')
        # Case-insensitive: the UI stores 'True' but a hand-edited system
        # parameter ('true'/'1') must behave identically.
        if str(auto_start).strip().lower() in ('true', '1', 'yes') and sidecar_path:
            port = 3100
            try:
                from urllib.parse import urlparse
                port = urlparse(self.sidecar_url).port or 3100
            except Exception:
                pass
            api_key = self._sidecar_launch_api_key()
            if start_sidecar(sidecar_path, port=port, api_key=api_key):
                import time
                time.sleep(2)

    def button_refresh_status(self):
        """Refresh session status from the sidecar."""
        self.ensure_one()
        self._ensure_can_manage()
        api = self._get_baileys_api()
        prev = self.session_state
        try:
            result = api.get_session_status()
            # If the sidecar returns no explicit status, keep the current state
            # rather than downgrading to 'disconnected' — an incomplete poll
            # response mid-pairing must not collapse the QR view.
            new_state = result.get('status') or prev or 'disconnected'
            vals = {'session_state': new_state}
            # Only touch the QR when the sidecar actually sends one; never blank
            # an existing QR mid-pairing (the sidecar does not re-send it on
            # every status poll, and blanking it makes the QR vanish so the user
            # clicks Connect again). Clear it only on terminal states.
            if result.get('qr_base64'):
                vals['qr_code_base64'] = result['qr_base64']
            elif new_state in ('connected', 'logged_out', 'disconnected'):
                vals['qr_code_base64'] = False
            if result.get('phone_number'):
                vals['phone_number'] = result['phone_number']
            self.write(vals)
        except BaileysError:
            # Sidecar momentarily unreachable: don't collapse an in-progress
            # pairing (qr_pending / connecting) to 'disconnected' — that hides
            # the QR mid-scan and forces a second Connect click. Only drop from
            # a non-pairing state. (#connect_flow)
            if prev not in ('qr_pending', 'connecting'):
                self.write({'session_state': 'disconnected'})

    def button_disconnect(self):
        """Disconnect the WhatsApp session."""
        self.ensure_one()
        self._ensure_can_manage()
        api = self._get_baileys_api()
        try:
            api.disconnect()
        except BaileysError:
            pass
        self.write({
            'session_state': 'disconnected',
            'qr_code_base64': False,
        })

    def button_logout(self):
        """Logout and delete auth state on the sidecar. Self-service: the
        account owner / team-leader may log out their OWN number (matches the
        ``user_can_manage`` view gate and the connect/disconnect guards)."""
        self.ensure_one()
        self._ensure_can_manage()
        api = self._get_baileys_api()
        try:
            api.logout()
        except BaileysError:
            pass
        self.write({
            'session_state': 'logged_out',
            'qr_code_base64': False,
            'phone_number': False,
        })

    # ── Approval actions ──────────────────────────────────────────────

    def action_approve(self):
        """Admin approves a self-added account so it can send/receive."""
        self._ensure_owa_admin()
        self.write({
            'approval_state': 'approved',
            'approved_user_id': self.env.user.id,
            'approved_date': fields.Datetime.now(),
        })
        for acc in self:
            acc.message_post(body=_("WhatsApp account approved by %s.")
                             % self.env.user.display_name)
        return True

    def action_reject(self):
        """Admin rejects a self-added account; messaging stays blocked."""
        self._ensure_owa_admin()
        self.write({'approval_state': 'rejected'})
        return True

    # ── Phase C3: WhatsApp profile picture ────────────────────────────
    def action_push_profile_picture(self):
        """Upload this account's avatar to WhatsApp as the business
        profile picture. Pulls from ``image_1920`` if set."""
        self.ensure_one()
        if not self.image_1920:
            raise UserError(_(
                "Set an image on this WhatsApp account first."))
        raw = base64.b64decode(self.image_1920)
        if self.connection_type == 'cloud':
            self._get_cloud_api().update_profile_picture(raw, 'image/jpeg')
        else:
            self._get_baileys_api().update_profile_picture(raw)
        self.message_post(body=_("WhatsApp profile picture updated."))
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp profile updated"),
                'message': _("The new picture is now visible to your contacts on WhatsApp."),
                'sticky': False,
            },
        }

    def action_clear_profile_picture(self):
        """Remove the WhatsApp business profile picture for this account."""
        self.ensure_one()
        api = self._get_baileys_api()
        api.remove_profile_picture()
        self.message_post(body=_("WhatsApp profile picture cleared."))
        return True

    def action_fetch_profile_picture(self):
        """Pull this account's CURRENT WhatsApp profile picture into
        ``image_1920`` so the form shows the photo contacts actually see."""
        self.ensure_one()
        b64 = self._fetch_own_profile_picture_b64()
        if not b64:
            raise UserError(_(
                "Couldn't fetch the profile picture from WhatsApp. Make sure "
                "the account is connected and has a photo set."))
        self.image_1920 = b64
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("Profile picture fetched"),
                'message': _("Loaded the current WhatsApp profile picture."),
                'type': 'success', 'sticky': False,
            },
        }

    def _fetch_own_profile_picture_b64(self):
        """Best-effort: the account's own current WhatsApp photo (or None)."""
        self.ensure_one()
        if self.connection_type == 'cloud':
            try:
                return self._get_cloud_api().fetch_business_profile_picture_b64()
            except Exception:
                _logger.exception(
                    "Cloud business-profile photo fetch failed for %s",
                    self.name)
                return None
        digits = (self.phone_number or '').lstrip('+').strip()
        if not digits:
            return None
        return self._fetch_picture_b64('%s@s.whatsapp.net' % digits)

    def _autofill_profile_picture(self):
        """Fill an EMPTY Business Profile Picture with the account's current
        WhatsApp photo right after a successful connect. Never overwrites an
        image the user set, never raises. (#self-pic-autofill)"""
        for account in self:
            if account.image_1920:
                continue
            try:
                b64 = account._fetch_own_profile_picture_b64()
                if b64:
                    account.image_1920 = b64
            except Exception:
                _logger.exception(
                    "Auto-fill of the profile picture failed for %s",
                    account.name)

    # ── Phase C2: WhatsApp blocklist sync ─────────────────────────────
    def action_sync_whatsapp_blocklist(self):
        """Pull the current WhatsApp blocklist and reconcile into ``owa.blacklist``.
        Adds rows that exist on WhatsApp but not in Odoo; does NOT remove
        Odoo-only rows (those usually exist for a reason)."""
        self.ensure_one()
        api = self._get_baileys_api()
        jids = api.fetch_whatsapp_blocklist() or []
        Blacklist = self.env['owa.blacklist'].sudo()
        added = 0
        for jid in jids:
            if not jid or '@' not in jid:
                continue
            phone = jid.split('@', 1)[0].split(':', 1)[0]
            if not phone:
                continue
            # owa.blacklist has no phone_number / wa_account_id fields — the
            # real fields are phone / phone_formatted / reason / active. The
            # previous code raised ValueError(Invalid field) on every click of
            # the "Sync from WhatsApp" button. (#multiacct)
            if Blacklist.search_count([('phone', '=', phone)]):
                continue
            Blacklist.create({
                'phone': phone,
                'reason': _("Synced from WhatsApp blocklist"),
            })
            added += 1
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp blocklist synced"),
                'message': _("%(added)d new entries added; %(total)d total on WhatsApp.") % {
                    'added': added, 'total': len(jids),
                },
                'sticky': False,
            },
        }

    # ── Phase D: WhatsApp pairing-code login ──────────────────────────
    def action_request_pairing_code(self):
        """Request an 8-character WhatsApp pairing code as an alternative
        to scanning the QR. Phone number for pairing comes from
        ``self.pairing_phone`` (set by the user on the connect wizard)."""
        self.ensure_one()
        self._ensure_can_manage()
        phone = (self.pairing_phone or '').strip()
        if not phone:
            raise UserError(_("Enter the WhatsApp account phone number first (digits only, country code included)."))
        # Pairing-code requires a FRESH socket where the gateway hasn't yet
        # registered. Statuses 'qr_pending' / 'connecting' are fine to reuse;
        # 'connected' / 'logged_out' / 'disconnected' / missing all mean the
        # in-memory socket is either already paired or torn down — recreate
        # so requestPairingCode runs against a live, unregistered socket.
        api = self._get_baileys_api()
        try:
            status_resp = api.get_session_status() or {}
        except BaileysError:
            status_resp = {}
        current = status_resp.get('status') or ''
        if current not in ('qr_pending', 'connecting'):
            self._ensure_sidecar_running()
            base_url = self.env['ir.config_parameter'].sudo().get_param(
                'web.base.url', 'http://localhost:8069')
            api.create_session(
                odoo_base_url=base_url,
                webhook_secret=self.webhook_secret,
                callback_url=self.callback_url or None,
                send_read_receipts=self._effective_send_read_receipts(),
                share_online_presence=self._effective_share_online_presence(),
                capture_contact_statuses=self.capture_contact_statuses,
                status_mark_seen=self.status_mark_seen,
                sync_own_statuses=self.sync_own_statuses,
            )
        # Now ask for the pairing code.
        result = api.request_pairing_code(phone)
        code = (result or {}).get('code') or ''
        if not code:
            raise UserError(_(
                "Sidecar did not return a pairing code. Make sure the sidecar is running, "
                "the account is freshly disconnected (logout first if it was paired before), "
                "and the phone number is correct."
            ))
        self.write({'pairing_code': code})
        # Log the code to chatter so the user always has a permanent record
        # even if the form reload races the toast — this also covers the case
        # where the toast is dismissed before the user types it on the phone.
        from markupsafe import Markup
        self.message_post(
            body=Markup(_(
                "WhatsApp pairing code requested for %(phone)s: <b>%(code)s</b>"
            )) % {'phone': phone, 'code': code},
        )
        # Return a notification chained to a soft reload so the form picks up
        # the freshly-written pairing_code and shows it instead of the stale
        # value from a previous attempt.
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp pairing code"),
                'message': _("Enter %(code)s on the phone in Settings → Linked Devices → Link with phone number.") % {
                    'code': code,
                },
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def button_test_connection(self):
        """Test connection to the sidecar service."""
        self.ensure_one()
        self._ensure_can_manage()
        api = self._get_baileys_api()
        try:
            result = api.health_check()
            if result.get('status') == 'ok':
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _("Connection Successful"),
                        'message': _("Sidecar service is running."),
                        'type': 'success',
                        'sticky': False,
                    }
                }
        except BaileysError as e:
            raise UserError(_("Connection failed: %s", e.error_message))

    def _sidecar_launch_api_key(self):
        """API key(s) to launch the shared sidecar with.

        The sidecar is a single process serving every account, so its auth
        accepts ANY account's key (comma-separated allow-list). We keep the
        prior on/off decision — auth stays disabled when THIS account has no
        key — and only widen the accepted set, so this can never turn auth on
        where it was off (no new 401s) but fixes 401s for sibling accounts
        whose key differed from the one that happened to launch the sidecar.
        """
        self.ensure_one()
        own = self.sudo().sidecar_api_key or ''
        if not own:
            return ''
        keys = self.env['owa.account'].sudo().search([]).mapped('sidecar_api_key')
        return ','.join(sorted({k for k in keys if k}))

    def button_start_sidecar(self):
        """Start the sidecar process."""
        from odoo.addons.open_whatsapp_connector.tools.sidecar_manager import (
            is_sidecar_running, start_sidecar,
        )
        self.ensure_one()
        self._ensure_owa_admin()
        if is_sidecar_running(self.sidecar_url):
            return self._notify_and_reload(_("Sidecar is already running."), 'warning')
        ICP = self.env['ir.config_parameter'].sudo()
        sidecar_path = ICP.get_param('open_whatsapp_connector.sidecar_path', '')
        if not sidecar_path:
            raise UserError(_(
                "Sidecar path not configured. Go to Settings > Open WhatsApp Connector."))
        port = 3100
        try:
            from urllib.parse import urlparse
            port = urlparse(self.sidecar_url).port or 3100
        except Exception:
            pass
        api_key = self._sidecar_launch_api_key()
        if start_sidecar(sidecar_path, port=port, api_key=api_key, sidecar_url=self.sidecar_url):
            return self._notify_and_reload(_("Sidecar started successfully."))
        raise UserError(_("Failed to start sidecar. Check the logs for details."))

    def button_stop_sidecar(self):
        """Stop the sidecar process."""
        from odoo.addons.open_whatsapp_connector.tools.sidecar_manager import stop_sidecar
        self.ensure_one()
        self._ensure_owa_admin()
        if stop_sidecar():
            return self._notify_and_reload(_("Sidecar stopped."))
        return self._notify_and_reload(_("Sidecar was not running."), 'warning')

    def _notify_and_reload(self, message, msg_type='success'):
        """Return a notification + reload action."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp"),
                'message': message,
                'type': msg_type,
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def _notify_success(self, message):
        """Return a success notification action."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("WhatsApp"),
                'message': message,
                'type': 'success',
                'sticky': False,
            },
        }

    def _cron_sync_session_status(self):
        """Cron: Sync session status for all active QR accounts. Cloud API
        accounts have no sidecar session to reconcile (their state is set by
        action_verify_connection + the Meta webhook), so they are excluded."""
        accounts = self.search([
            ('active', '=', True),
            ('connection_type', '=', 'qr'),
            ('session_state', 'in', ('connected', 'connecting', 'qr_pending')),
        ])
        for account in accounts:
            try:
                account.button_refresh_status()
            except Exception:
                _logger.exception("Failed to sync session status for account %s", account.name)

    @classmethod
    def _reset_stale_session_state_after_restart(cls, env):
        """One-shot startup correction.

        After an OS reboot or unexpected sidecar crash, ``session_state`` rows
        keep saying ``connected`` even though the Node sidecar process is no
        longer running. The web UI then shows the wrong badges until either
        the user clicks Refresh or the periodic cron fires (up to 5 min later).

        This method runs once per worker on registry load (see
        :meth:`_register_hook`) and cheaply resets any
        ``connected|connecting|qr_pending`` row to ``disconnected`` when its
        sidecar URL fails the live ``/health`` probe. Sidecars that ARE up
        are left untouched; the row will be corrected by the next cron pass.
        """
        from odoo.addons.open_whatsapp_connector.tools.sidecar_manager import (
            is_sidecar_running,
        )
        try:
            Account = env['owa.account'].sudo()
            # QR-only: a Cloud API account is "connected" without any sidecar,
            # so it must not be reset by the sidecar /health probe below.
            stale = Account.search([
                ('active', '=', True),
                ('connection_type', '=', 'qr'),
                ('session_state', 'in', ('connected', 'connecting', 'qr_pending')),
            ])
            if not stale:
                return
            url_cache = {}
            to_reset = Account.browse()
            for acc in stale:
                url = acc.sidecar_url or 'http://localhost:3100'
                if url not in url_cache:
                    url_cache[url] = is_sidecar_running(url)
                if not url_cache[url]:
                    to_reset |= acc
            if to_reset:
                to_reset.write({
                    'session_state': 'disconnected',
                    'qr_code_base64': False,
                })
                _logger.info(
                    "owa.account: reset %d stale session_state row(s) on startup",
                    len(to_reset),
                )
        except Exception:
            _logger.exception("startup session_state reset failed")

    def _register_hook(self):
        """Hook fired once per worker after the registry is ready. Used to
        run :meth:`_reset_stale_session_state_after_restart` so the UI shows
        accurate state immediately on Odoo (re)start instead of waiting for
        the Sync-Session-Status cron."""
        super()._register_hook()
        # CRITICAL: skip during install/upgrade. This hook opens a SECOND
        # cursor and WRITES owa_account; during `-u`/`-i` the module-load
        # transaction still holds locks on owa_account, so that write blocks
        # on a lock the load transaction won't release until the hook returns
        # — a cursor self-deadlock that hangs the upgrade indefinitely (PG
        # can't see it: the load cursor is "idle in transaction", not waiting).
        # The correction is only needed on a plain (re)start; the `-u` process
        # exits right after load anyway, and the next normal start runs it.
        from odoo.tools import config
        if config.get('update') or config.get('init'):
            return
        try:
            with self.pool.cursor() as cr:
                # Never block the registry load. If owa_account rows are locked
                # by a concurrent module install / upgrade / UNINSTALL (which
                # drops this model's columns under an exclusive lock), bail out
                # instead of deadlocking — the Sync-Session-Status cron corrects
                # the state within minutes anyway. Belt-and-braces alongside the
                # update/init guard above (that flag isn't set for an in-process
                # uninstall's Registry.new(update_module=True)).
                cr.execute("SET LOCAL lock_timeout = '3s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                self._reset_stale_session_state_after_restart(env)
        except Exception:
            # Never let a startup hook fail the registry load — the cron
            # will catch up within minutes.
            _logger.exception("owa.account._register_hook startup correction failed")

    def _find_active_channel(self, number, partner=None):
        """Find an existing active discuss.channel for a WhatsApp number."""
        self.ensure_one()
        domain = [
            ('channel_type', '=', 'whatsapp'),
            ('whatsapp_number', '=', number),
            ('owa_account_id', '=', self.id),
        ]
        channel = self.env['discuss.channel'].search(domain, limit=1, order='id desc')
        return channel

    def _get_or_create_channel(self, number, sender_name=None):
        """Get or create a discuss.channel for the given WhatsApp number."""
        self.ensure_one()
        channel = self._find_active_channel(number)
        if channel:
            return channel

        # Find or create partner
        partner = self.env['res.partner']._find_or_create_from_wa_number(number, sender_name)

        recipient_name = (
            partner.name
            if partner and partner.name and partner.name != partner.phone
            else False
        )
        channel_name = f"{recipient_name} ({number})" if recipient_name else number

        # Create channel. No member row when there is no partner (auto-create
        # contacts may be OFF): an empty {} member violates mail's
        # partner-or-guest CHECK constraint and crashes the compose.
        members = [(0, 0, {'partner_id': partner.id})] if partner else []
        seen = {partner.id} if partner else set()
        for user in self.notify_user_ids:
            if user.partner_id.id not in seen:
                seen.add(user.partner_id.id)
                members.append((0, 0, {'partner_id': user.partner_id.id}))
        channel = self.env['discuss.channel'].with_context(
            mail_create_nosubscribe=True
        ).create({
            'name': channel_name,
            'channel_type': 'whatsapp',
            'whatsapp_number': number,
            'owa_account_id': self.id,
            'whatsapp_partner_id': partner.id if partner else False,
            'channel_member_ids': members,
        })
        return channel

    # ══════════════════════════════════════════════════════════════════
    # Transport-neutral inbound processing
    # ══════════════════════════════════════════════════════════════════
    def _handle_inbound(self, normalized, claim_dedupe=True):
        """Shared inbound pipeline for every transport (QR + Cloud).

        ``normalized`` keys (see tools.meta_inbound.normalize_meta_message and
        the QR normalizer in controllers/main.py):
          from_number, sender_name, msg_uid, type, body, media,
          reply_to_wamid, timestamp, phone_number_id, raw
        plus optional QR-specific keys:
          chat_type, participant, attachment_ids (pre-decoded), is_group

        ``claim_dedupe`` — when True (Cloud webhook / direct callers) this
        method claims the msg_uid in owa.inbound.dedupe and short-circuits on
        a duplicate. The QR controller already claims before its access-control
        step (to keep the legacy ordering byte-identical), so it passes
        ``claim_dedupe=False``.

        Behaviour is byte-identical to the legacy
        controllers/main.py::_process_inbound_message body for the QR path.
        Side-effects (channel + message_post + rules + chatbot + auto-reply)
        are the same; the method returns nothing.
        """
        self.ensure_one()
        if self._account_messaging_blocked():
            _logger.info(
                "Inbound on account %s dropped: pending admin approval", self.id)
            return
        account = self
        from_number = normalized.get('from_number') or ''
        sender_name = normalized.get('sender_name') or ''
        msg_id = normalized.get('msg_uid') or ''
        msg_type = normalized.get('type') or 'text'
        chat_type = normalized.get('chat_type') or 'direct'
        participant_jid = normalized.get('participant') or ''
        reply_to_wamid = normalized.get('reply_to_wamid')
        from_me = bool(normalized.get('from_me'))

        if not from_number:
            return

        # Phase 2: explicit dedupe via owa.inbound.dedupe (covers worker races
        # the gateway's in-memory dedupe can't catch). The QR controller claims
        # earlier (before access-control) and passes claim_dedupe=False.
        if claim_dedupe and msg_id and not self.env['owa.inbound.dedupe'].sudo().claim(msg_id, account.id):
            _logger.info("Skipping duplicate inbound msg_uid=%s (account=%s)", msg_id, account.id)
            return

        # Use the central WhatsApp channel helper so inbound and outbound
        # messages resolve to the same normalized thread and new channels are broadcast.
        channel = self.env['discuss.channel'].sudo()._get_owa_whatsapp_channel(
            from_number,
            account,
            sender_name=sender_name,
            create_if_not_found=True,
        )
        if not channel:
            return
        # Welcome auto-replies fire only when this is the first contact —
        # i.e. the channel was created by the call above (no inbound history yet).
        is_new_channel = (
            bool(channel.create_date)
            and (fields.Datetime.now() - channel.create_date) < timedelta(seconds=30)
            and not channel.message_ids
        )

        # Own-device outbound sync: this message was sent from YOUR phone (or
        # another linked device), or is the echo of a message Odoo itself sent.
        # Skip the echo of an Odoo-sent message — it is already in the channel —
        # by matching its WhatsApp id against an existing outbound record.
        # (#own-device-sync)
        if from_me and msg_id and self.env['owa.message'].sudo().search_count([
                ('msg_uid', '=', msg_id),
                ('message_type', '=', 'outbound'),
                ('wa_account_id', '=', account.id),
        ]):
            return

        parent_wa_message = self.env['owa.message'].sudo()
        if reply_to_wamid:
            parent_wa_message = self.env['owa.message'].sudo().search([
                ('msg_uid', '=', reply_to_wamid),
                ('message_type', '=', 'outbound'),
                ('wa_account_id', '=', account.id),
            ], limit=1)

        # Body + attachments: the transport already prepared these. The QR
        # normalizer decodes inline base64 media into ir.attachment ids and a
        # finished body; the Cloud normalizer hands us a media dict (download
        # is a Phase-2 item) so we surface a placeholder body for now.
        body = normalized.get('body') or ''
        attachment_ids = list(normalized.get('attachment_ids') or [])
        media = normalized.get('media')
        labels = {
            'image': '📷 Image',
            'video': '🎥 Video',
            'audio': '🎵 Audio',
            'document': '📄 Document',
            'sticker': '🏷️ Sticker',
        }
        # Cloud inbound media (Phase 2): the normalizer hands us a media dict
        # with the Meta media id (no bytes). Download via the Graph API and
        # attach. The QR transport already decodes inline bytes into
        # attachment_ids upstream, so this branch only fires for cloud.
        if (media and media.get('id') and not attachment_ids
                and account.connection_type == 'cloud'):
            try:
                content, mime = account._get_cloud_api().download_media(
                    media['id'])
                if content:
                    fname = (media.get('filename')
                             or f"{msg_type or 'file'}_{media['id']}")
                    attachment = self.env['ir.attachment'].sudo().create({
                        'name': fname,
                        'datas': base64.b64encode(content),
                        'mimetype': mime or media.get('mime')
                        or 'application/octet-stream',
                        'res_model': 'discuss.channel',
                        'res_id': channel.id,
                    })
                    # Voice notes → Odoo voice metadata so Discuss renders the
                    # inline VoicePlayer (see controllers/main.py for the QR path).
                    if ((attachment.mimetype or '').startswith('audio/')
                            and hasattr(attachment, '_set_voice_metadata')):
                        try:
                            attachment._set_voice_metadata()
                        except Exception:
                            _logger.exception(
                                "Failed to set voice metadata on attachment %s",
                                attachment.id)
                    attachment_ids.append(attachment.id)
            except Exception:
                _logger.exception(
                    "Cloud inbound media download failed for media id=%s",
                    media.get('id'))
        if media and not attachment_ids and not body:
            # Media we couldn't fetch (download failed / no id) — placeholder.
            # Markup so the <em> renders instead of showing as literal text.
            label = labels.get(msg_type, f'[{msg_type}]')
            body = Markup("{label} <em>(media download pending)</em>").format(label=label)

        # Reply-context envelope (Phase 2): when this is a reply to one of our
        # outbound messages, prepend a quoted snippet so the agent sees what
        # the contact is replying to. The decoration is applied ONLY to the
        # body posted into Discuss (``display_body``) — ``body`` stays the RAW
        # inbound text so the stop-keyword / slash-command / inbound-rule /
        # chatbot / auto-reply / tag matchers below still see exactly what the
        # customer typed. Otherwise a quoted reply to the bot menu (very common)
        # never matches /menu, STOP, etc. (#F116)
        display_body = body
        if parent_wa_message and parent_wa_message.mail_message_id:
            quoted = parent_wa_message.mail_message_id.body or ''
            quoted_plain = (quoted or '').strip()
            if quoted_plain:
                # Build as Markup so message_post renders the envelope as HTML.
                # ``quoted`` is already-stored HTML (wrap as Markup so it isn't
                # double-escaped); ``body`` may be plain customer text (escaped
                # by .format) or our own Markup (location/contact card, kept
                # safe) — either way no literal markup is shown and customer
                # text can't inject HTML. (#F033)
                display_body = Markup(
                    "<blockquote class=\"owa-reply-quote\" "
                    "style=\"border-left: 3px solid #25D366; padding-left: 8px; "
                    "color: #6c757d; margin: 0 0 6px;\">"
                    "<small>↩️ Replying to:</small><br/>{quoted}</blockquote>{body}"
                ).format(quoted=Markup(quoted), body=body)

        # Author attribution: for groups every member's reply must be
        # attributed to the *participant* (actual sender), not to the group's
        # ``whatsapp_partner_id`` — otherwise the chat shows every message
        # under the same anonymous group avatar with no way to tell who said
        # what. For DMs the sender == the channel partner, so reuse it.
        author_partner = channel.whatsapp_partner_id
        if chat_type == 'group':
            participant_digits = participant_jid.split('@')[0].lstrip('+').strip()
            if participant_digits:
                author_partner = self.env['res.partner'].sudo()\
                    ._find_or_create_from_wa_number(
                        participant_digits, name=sender_name or None,
                    )
        if from_me:
            # Own outgoing message — attribute to a NON-customer partner so
            # Discuss renders it on the outbound ("me") side and the inbound-only
            # blocks in message_post (reopen / helpdesk relay, keyed on author ==
            # whatsapp_partner_id) never misfire — even when the account has no
            # owner set. Never fall back to the customer partner. (#own-device-sync)
            author_partner = (account.user_id.partner_id
                              or account.create_uid.partner_id
                              or self.env.company.partner_id)

        # Type-specific message_post keys: a fresh inbound carries
        # owa_inbound_msg_uid (+ the sender JID for group reactions); an
        # own-device message is RECORDED as an already-sent outbound (never
        # re-sent) via owa_recorded_outbound_uid. (#own-device-sync)
        post_kwargs = {
            'body': display_body,
            'message_type': 'whatsapp_message',
            'subtype_xmlid': 'mail.mt_comment',
            'author_id': author_partner.id if author_partner else False,
            'attachment_ids': attachment_ids,
            'parent_id': parent_wa_message.mail_message_id.id if parent_wa_message else False,
            'parent_msg_id': parent_wa_message.id if parent_wa_message else False,
        }
        if from_me:
            post_kwargs['owa_recorded_outbound_uid'] = msg_id
        else:
            post_kwargs['owa_inbound_msg_uid'] = msg_id
            # Store the original sender's participant JID for GROUP inbound so
            # agents can react to another member's message. (#reactions)
            post_kwargs['owa_sender_jid'] = (
                participant_jid if chat_type == 'group' else '')

        # Post message to discuss channel
        post_ctx = {'mail_create_nosubscribe': True}
        if not from_me:
            post_ctx['owa_inbound'] = True
        mail_message = channel.sudo().with_context(**post_ctx).message_post(
            **post_kwargs)

        # Safety net: if attachments we passed didn't end up linked to the
        # message (e.g. message_post's pending-attachment filter stripped
        # them), force-link them now. Otherwise the chat renders empty as
        # "This message has been removed" because isEmpty=true.
        if attachment_ids and mail_message and not mail_message.attachment_ids:
            atts = self.env['ir.attachment'].sudo().browse(attachment_ids).exists()
            if atts:
                atts.write({'res_model': 'mail.message', 'res_id': mail_message.id})
                mail_message.sudo().write({
                    'attachment_ids': [(6, 0, atts.ids)],
                })

        # Own outgoing messages are now recorded in the channel — stop here.
        # Inbound-only side-effects (group intro, notification rules, chatbot,
        # auto-replies, auto-tagging) must NOT fire on our own messages.
        # (#own-device-sync)
        if from_me:
            return

        # Phase 6: group intro on first contact in a new group.
        try:
            self._maybe_send_group_intro(channel)
        except Exception:
            _logger.exception("Error sending group intro")

        # Phase 6: gate chatbot / auto-reply on @-mention if the group requires it.
        skip_bot = bool(
            channel and channel.is_whatsapp_group and channel.require_mention
            and not self._bot_mentioned(body)
        )

        # Phase 3: send ack reaction (👀 by default) right after the inbound
        # is recorded, before any agent or bot replies. Cheap, visible UX win.
        try:
            self._send_ack_reaction(msg_id, from_number, channel, body=body)
        except Exception:
            _logger.exception("Error sending ack reaction")

        # Check for blacklist keywords. Only for direct chats — one member
        # typing "stop" in a group must not opt out the whole group.
        stop_keywords = {'stop', 'unsubscribe', 'opt-out', 'optout'}
        if (body and body.strip().lower() in stop_keywords
                and not (channel and channel.is_whatsapp_group)):
            self.env['owa.blacklist'].sudo().add_to_blacklist(
                from_number, reason='User sent STOP'
            )
            _logger.info("Auto-blacklisted %s (sent STOP keyword)", from_number)

        # Phase 9: chat-side slash commands take priority over chatbot /
        # auto-reply when the user types '/menu', '/stop', '/agent' etc.
        try:
            if self.env['owa.slash.command'].sudo().parse_and_dispatch(
                account, from_number, body, channel,
            ):
                return
        except Exception:
            _logger.exception("Error in slash-command dispatch")

        # Phase 22C / 27B: inbound auto-create rule engine — fires before
        # chatbot so a created record + its auto_reply_template can
        # short-circuit the rest of the customer-facing reply chain
        # (avoids the customer receiving a rule reply AND a chatbot
        # reply AND an auto-reply for the same inbound).
        rule_replied = False
        if not skip_bot:
            try:
                # Reuse the partner already resolved by the channel
                # creation flow above. That path uses
                # _find_or_create_from_wa_number, which is normalised + safer
                # than a substring `phone ilike` (which trips on trailing-
                # digit collisions across accounts).
                partner = (channel and channel.whatsapp_partner_id) or False
                if not partner:
                    # Fallback for the unknown-sender case: don't search by
                    # phone ilike — that's the wrong-partner bug. Leave
                    # partner=False; rules with match_type='unknown_sender'
                    # are designed for exactly this path.
                    partner = self.env['res.partner']
                # Scope the env to the account's allowed companies so the
                # created record lands in the right company.
                env_scoped = self.env(
                    context=dict(
                        self.env.context,
                        allowed_company_ids=(
                            account.allowed_company_ids.ids
                            if account.allowed_company_ids
                            else [account.company_id.id]
                        ),
                    ),
                )
                is_group = bool(channel and channel.is_whatsapp_group)
                rec, rule_replied = env_scoped['owa.inbound.rule'].sudo()._evaluate_and_create(
                    account, partner or False, body, channel,
                    sender_phone=from_number, is_group=is_group,
                )
            except Exception:
                _logger.exception("Error in inbound-rule evaluation")

        # Phase 26E: auto-tag rules — drop res.partner.category tags on the
        # partner based on inbound message content. Runs regardless of
        # skip_bot and independently of the reply chain so tagging happens
        # even when bot replies are suppressed. (#labels)
        try:
            tag_partner = channel and channel.whatsapp_partner_id
            if tag_partner and body:
                self.env['owa.tag.rule'].sudo()._evaluate(tag_partner, body)
        except Exception:
            _logger.exception("Error in auto-tag rule evaluation")

        # If the rule fired AND queued an auto-reply, short-circuit the
        # chatbot + auto-reply pipeline so the customer doesn't get 2-3
        # replies for the same inbound.
        if rule_replied:
            return

        # Check chatbot sessions (skip if group requires mention and none present)
        if not skip_bot:
            try:
                if self._check_chatbot(from_number, body, is_new_channel=is_new_channel):
                    return  # Chatbot handled the message
            except Exception:
                _logger.exception("Error in chatbot processing")

        # AI agent (after-hours intelligent responder) — provided by the
        # open_whatsapp_connector_ai companion; base hook is a no-op. (#ai-agent)
        if not skip_bot:
            try:
                if self._check_ai_agent(from_number, body, is_new_channel=is_new_channel):
                    return  # AI agent handled the message
            except Exception:
                _logger.exception("Error in AI agent processing")

        # Check auto-reply rules (same gate)
        if not skip_bot:
            try:
                self._check_auto_replies(from_number, body, is_new_channel=is_new_channel)
            except Exception:
                _logger.exception("Error checking auto-reply rules")

    # ── Inbound-pipeline helpers (moved from controllers/main.py so both
    #    transports share them; ``self`` is the owa.account) ────────────
    def _bot_mentioned(self, body):
        """True if the inbound body contains a recognisable @-mention of this
        account. Matches phone_number (digits only) or name (substring)."""
        if not body:
            return False
        haystack = body.lower()
        if self.phone_number:
            digits = ''.join(c for c in self.phone_number if c.isdigit())
            if digits and digits in body:
                return True
        if self.name and self.name.lower() in haystack:
            return True
        return False

    def _maybe_send_group_intro(self, channel):
        """Send the group intro message the first time we see a group.
        Only the QR transport can push the intro (uses the gateway)."""
        if not channel or not channel.is_whatsapp_group:
            return
        if channel.group_intro_sent:
            return
        intro = (self.group_intro_message or '').strip()
        if not intro:
            channel.sudo().group_intro_sent = True
            return
        if self.connection_type != 'qr':
            # Cloud group intro is out of P1 scope; mark sent to avoid retries.
            channel.sudo().group_intro_sent = True
            return
        try:
            api = self._get_baileys_api()
            jid = channel.whatsapp_number or ''
            if not jid.endswith('@g.us'):
                jid = f"{jid}@g.us"
            api.send_text(jid, intro)
            channel.sudo().group_intro_sent = True
        except Exception:
            _logger.exception("Group intro send failed for channel %s", channel.id)

    def _send_ack_reaction(self, msg_id, from_number, channel, body=''):
        """Send the 👀 ack reaction to the inbound message. Gated by
        reaction_level + ack_reaction_dm/group + ack_reaction_emoji. Failures
        never block processing. Ack reactions use the QR gateway only."""
        if not msg_id:
            return
        if self.connection_type != 'qr':
            # Cloud reactions are a Phase-2 item.
            return
        if not self.reaction_level or self.reaction_level == 'off':
            return
        emoji = (self.ack_reaction_emoji or '').strip()
        if not emoji:
            return
        is_group = channel and getattr(channel, 'is_group', False)
        # Determine "is_group" without relying on a non-existent field —
        # fall back to inspecting channel name or remote JID.
        if is_group is None or is_group is False:
            is_group = channel and (
                (channel.name or '').endswith('@g.us')
                or '@g.us' in (channel.whatsapp_number or '')
            )
        if is_group:
            scope = self.ack_reaction_group or 'mentions'
            if scope == 'never':
                return
            # 'mentions' scope only fires when the inbound body actually
            # @-mentions the account, so we don't spam every group message.
            if scope == 'mentions' and not self._bot_mentioned(body or ''):
                return
        else:
            if not self.ack_reaction_dm:
                return

        from odoo.addons.open_whatsapp_connector.tools.baileys_exception import BaileysError
        try:
            api = self._get_baileys_api()
            chat_jid = (
                channel.whatsapp_number if channel and '@' in (channel.whatsapp_number or '')
                else f"{from_number.lstrip('+')}@s.whatsapp.net"
            )
            api.send_reaction(chat_jid, msg_id, emoji, from_me=False)
        except BaileysError as e:
            _logger.warning("Ack reaction failed for %s: %s", msg_id, e.error_message)

    def _is_within_office_hours(self):
        """True when 'now' falls inside this account's configured office hours
        (working days + hour window, evaluated in office_tz). Drives after-hours
        automation: a schedule-gated chatbot / AI agent runs only when this is
        False. Defaults to True (safe: no after-hours automation) on bad config."""
        import pytz
        from datetime import datetime
        self.ensure_one()
        try:
            tz = pytz.timezone(self.office_tz or 'UTC')
        except Exception:
            tz = pytz.UTC
        now_local = pytz.UTC.localize(datetime.utcnow()).astimezone(tz)
        days = [int(d.strip()) for d in (self.office_days or '').split(',')
                if d.strip().isdigit()]
        if days and now_local.weekday() not in days:
            return False
        current_hour = now_local.hour + now_local.minute / 60.0
        start = self.office_hours_start or 0.0
        end = self.office_hours_end if self.office_hours_end else 24.0
        return start <= current_hour < end

    def _check_ai_agent(self, from_number, message_text, is_new_channel=False):
        """Hook: let an AI agent answer this inbound message. Base is a no-op;
        the ``open_whatsapp_connector_ai`` companion overrides this to call the
        AI Connector. Return True if the AI handled it (skip auto-replies)."""
        return False

    def _owa_send_text(self, to_number, text, reply_to_mode_override=False):
        """Queue a plain-text WhatsApp reply from this account to a number,
        through owa.message — same audit trail / status tracking / send-queue as
        any other outbound. Returns the owa.message (empty on no-op)."""
        from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format
        from odoo.tools import plaintext2html
        self.ensure_one()
        if not text or self.session_state != 'connected':
            return self.env['owa.message']
        formatted = wa_phone_format(self.env, to_number) or to_number
        mail_message = self.env['mail.message'].create({
            'body': plaintext2html(text),
            'message_type': 'whatsapp_message',
        })
        vals = {
            'mobile_number': formatted,
            'message_type': 'outbound',
            'state': 'outgoing',
            'wa_account_id': self.id,
            'mail_message_id': mail_message.id,
        }
        if reply_to_mode_override:
            vals['reply_to_mode_override'] = reply_to_mode_override
        msg = self.env['owa.message'].create(vals)
        cron = self.env.ref('open_whatsapp_connector.ir_cron_send_owa_queue',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return msg

    def _check_chatbot(self, from_number, message_text, is_new_channel=False):
        """Check if there's an active chatbot session for this number.
        Returns True if the chatbot handled the message (skip auto-replies)."""
        from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format
        account = self
        ChatbotSession = self.env['owa.chatbot.session'].sudo()
        formatted_phone = wa_phone_format(self.env, from_number) or from_number

        # A human has taken over this conversation → every bot stays silent.
        channel = self.env['discuss.channel'].sudo().search([
            ('channel_type', '=', 'whatsapp'),
            ('owa_account_id', '=', account.id),
            '|', ('whatsapp_number', '=', (formatted_phone or '').lstrip('+')),
                 ('whatsapp_number', '=', (from_number or '').lstrip('+')),
        ], limit=1)
        if channel and (channel.owa_bot_paused
                        or getattr(channel, 'owa_ai_paused', False)):
            return False

        # Check for active session — match formatted or raw to cover legacy rows.
        session = ChatbotSession.search([
            ('chatbot_id.wa_account_id', '=', account.id),
            '|', ('phone_number', '=', formatted_phone),
                 ('phone_number', '=', from_number),
            ('state', '=', 'active'),
        ], limit=1)

        if session:
            return session._process_message(message_text)

        # No active session. The active bot for this account can still be
        # reached from an existing conversation via its global keywords.
        chatbot = self.env['owa.chatbot'].sudo().search([
            ('wa_account_id', '=', account.id),
            ('active', '=', True),
        ], limit=1, order='sequence, id')
        if not chatbot:
            return False

        Bot = self.env['owa.chatbot']
        # "agent"/"human" keyword → hand straight to a person, any time.
        if Bot._keyword_hit(chatbot.keyword_agent, message_text):
            chatbot._handoff_to_human(from_number)
            return True

        # Schedule gate: an "after-hours only" bot stays quiet during office
        # hours so human operators handle the chat live. (#office-hours)
        after_hours_block = (chatbot.active_outside_office_hours
                             and account._is_within_office_hours())

        # Start the bot on a brand-new conversation, or when an existing
        # contact sends a "menu"/"start" restart keyword.
        restart_hit = Bot._keyword_hit(chatbot.keyword_restart, message_text)
        if (is_new_channel or restart_hit) and not after_hours_block:
            chatbot._start_session(from_number)
            # Bot already sent its own welcome + first menu; treat the inbound
            # as handled so the auto-reply pipeline is skipped (otherwise a
            # "welcome" auto-reply also fires → two welcomes). (#chatbot)
            return True

        return False

    def _check_auto_replies(self, from_number, message_text, is_new_channel=False):
        """Check and send auto-replies for an inbound message."""
        account = self
        rules = self.env['owa.auto.reply'].sudo().search([
            ('active', '=', True),
            '|', ('wa_account_id', '=', account.id), ('wa_account_id', '=', False),
        ], order='sequence, id')

        for rule in rules:
            if rule._matches_message(message_text, from_number, is_new_channel=is_new_channel):
                rule._send_auto_reply(account, from_number)
                break  # Only first matching rule fires
