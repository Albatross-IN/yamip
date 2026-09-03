import logging

from datetime import timedelta
from markupsafe import Markup

from odoo import api, Command, fields, models, tools, _
from odoo.addons.mail.tools.discuss import Store
from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


def is_whatsapp_channel(channel):
    return channel.channel_type == "whatsapp"


def _owa_name_is_jid(name):
    """True when ``name`` is a bare WhatsApp JID (e.g. '1203…@g.us') rather
    than a friendly conversation name. A friendly name such as 'John (12@lid)'
    contains a space and is NOT treated as a raw JID. (#wa-name)"""
    n = (name or '').strip()
    return ' ' not in n and n.endswith(
        ('@g.us', '@newsletter', '@lid', '@s.whatsapp.net'))


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    channel_type = fields.Selection(
        selection_add=[('whatsapp', 'WhatsApp Conversation')],
        ondelete={'whatsapp': 'cascade'})
    whatsapp_number = fields.Char(string="Phone Number")
    whatsapp_channel_valid_until = fields.Datetime(
        string="WhatsApp Channel Valid Until",
        compute="_compute_whatsapp_channel_valid_until")
    last_wa_mail_message_id = fields.Many2one(
        comodel_name='mail.message', string="Last WhatsApp Partner Message",
        index='btree_not_null')
    whatsapp_partner_id = fields.Many2one(
        comodel_name='res.partner', string="WhatsApp Partner",
        index='btree_not_null')
    owa_account_id = fields.Many2one(
        comodel_name='owa.account', string="WhatsApp Account")

    # ─── Phase 22A: conversation assignment & inbox triage ────────────
    assignee_id = fields.Many2one(
        'res.users', string="Assignee", index=True, tracking=True,
        # v19 idiom: relabel the empty group/value "None" -> "Unassigned" the
        # native way (v18 used a KanbanHeader._getEmptyGroupLabel JS patch; that
        # hook does not exist in v19, where the empty-group label comes from the
        # field's falsy_value_label — see project/helpdesk assignee fields).
        falsy_value_label="Unassigned",
        help="Salesperson who owns this WhatsApp conversation.")
    # Phase 22A: Sales-Team and Helpdesk-Ticket linkage stored as plain
    # integer ids so the addon installs without crm/helpdesk. Cross-app
    # bridges that depend on those modules can populate these from their
    # own glue addons.
    crm_team_id_int = fields.Integer(
        string="Sales Team id",
        help="Numeric id of a crm.team record, when crm is installed.")
    triage_state = fields.Selection([
        ('unassigned', 'Unassigned'),
        ('active',     'Active'),
        ('on_hold',    'On hold'),
        ('resolved',   'Resolved'),
    ], string="Triage", default='unassigned', tracking=True, index=True)
    sla_due_at = fields.Datetime(
        string="SLA due at", compute='_compute_sla_due_at', store=True,
        help="Computed from assigned_at + the account's sla_minutes.")
    assigned_at = fields.Datetime(string="Assigned at")
    resolved_at = fields.Datetime(string="Resolved at")
    sla_breached = fields.Boolean(
        string="SLA breached", compute='_compute_sla_breached', store=True)

    # ─── Human takeover: silences automated repliers ──────────────────
    # Set True the moment a conversation is handed to a person (by the
    # chatbot, the AI agent, or manually). While True the chatbot stays
    # quiet so it never talks over the human who took the chat. Shared,
    # neutral flag so every automation layer honours the same signal.
    owa_bot_paused = fields.Boolean(
        string="Human handling (bot paused)", default=False, copy=False,
        help="When a human takes over this conversation, automated replies "
             "(chatbot / AI agent) stop until this is cleared.")

    # ─── Phase 23A: helpdesk bridge ───────────────────────────────────
    helpdesk_ticket_id_int = fields.Integer(
        string="Helpdesk Ticket id",
        help="Numeric id of a helpdesk.ticket record, when the helpdesk "
             "module is installed.")

    # ─── CRM bridge (open_whatsapp_connector_crm) ─────────────────────
    crm_lead_id_int = fields.Integer(
        string="CRM Lead id", index=True,
        help="Numeric id of the linked crm.lead, when crm is installed. "
             "Populated by the WhatsApp CRM glue addon / inbound rules so the "
             "conversation and the lead cross-link both ways.")

    # ─── Phase 26B: conversation labels ───────────────────────────────
    wa_label_ids = fields.Many2many(
        'owa.channel.label', 'owa_channel_label_rel',
        'channel_id', 'label_id', string="Labels")
    whatsapp_channel_active = fields.Boolean(
        string="Is WhatsApp Channel Active",
        compute="_compute_whatsapp_channel_active")
    # Phase 6: group support
    is_whatsapp_group = fields.Boolean(
        string="Is WhatsApp group", compute='_compute_is_whatsapp_group', store=True,
        help="Computed True for WhatsApp channels whose JID ends with @g.us.")
    # ─── Conversation-type classification (Discuss sidebar grouping) ──────
    # Reverse links used by the whatsapp_kind compute. owa.community already
    # owns `channel_id`; owa.newsletter gains it for channel-history import.
    owa_community_ids = fields.One2many(
        'owa.community', 'channel_id', string="WhatsApp Communities")
    owa_newsletter_ids = fields.One2many(
        'owa.newsletter', 'channel_id', string="WhatsApp Channels")
    whatsapp_kind = fields.Selection([
        ('dm', 'Direct'),
        ('group', 'Group'),
        ('community', 'Community'),
        ('channel', 'Channel'),
    ], string="WhatsApp Type", compute='_compute_whatsapp_kind', store=True,
        help="Classifies a WhatsApp conversation as a 1:1 chat, group, "
             "community or channel so the Discuss sidebar can group them.")
    wa_display_number = fields.Char(
        string="Number", compute='_compute_wa_display_number',
        help="The contact's phone number for 1:1 WhatsApp chats; blank for "
             "groups / communities / channels (whose JID is internal and of "
             "no use to a user).")
    require_mention = fields.Boolean(
        string="Bot requires @mention", default=False,
        help="When set, chatbots and auto-replies in this group only fire when "
             "the bot's name or number is @-mentioned in the inbound text.")
    group_intro_sent = fields.Boolean(
        string="Group intro sent", default=False,
        help="Set after the bot has greeted this group with the configured "
             "intro message; prevents re-sending.")
    member_label = fields.Char(
        string="My display label", size=30,
        help="Custom name (≤30 chars) shown in this WhatsApp group beside the "
             "bot's number. Leave empty to keep the default.")

    def action_push_member_label(self):
        """Push the value of ``member_label`` to WhatsApp. Available only on
        WhatsApp groups. The label is sent as a GROUP_MEMBER_LABEL_CHANGE
        protocol message and applies to the connected account itself."""
        for channel in self:
            if not channel.is_whatsapp_group:
                raise UserError(_("Display label can only be set on WhatsApp groups."))
            if not channel.owa_account_id:
                raise UserError(_("Channel has no linked WhatsApp account."))
            if channel.owa_account_id.session_state != 'connected':
                raise UserError(_("WhatsApp account is not connected."))
            api = BaileysApi(channel.owa_account_id)
            api.update_member_label(channel.whatsapp_number, channel.member_label or '')
        return True

    @api.depends('whatsapp_number', 'channel_type')
    def _compute_is_whatsapp_group(self):
        for ch in self:
            ch.is_whatsapp_group = (
                ch.channel_type == 'whatsapp'
                and (ch.whatsapp_number or '').endswith('@g.us')
            )

    @api.depends('channel_type', 'whatsapp_number',
                 'owa_community_ids', 'owa_newsletter_ids')
    def _compute_whatsapp_kind(self):
        """Classify a WhatsApp conversation for the grouped Discuss sidebar.
        Community is checked before group because a community's Announcements
        conversation is itself an @g.us group."""
        for ch in self:
            if ch.channel_type != 'whatsapp':
                ch.whatsapp_kind = False
                continue
            number = ch.whatsapp_number or ''
            if ch.owa_community_ids:
                ch.whatsapp_kind = 'community'
            elif ch.owa_newsletter_ids or number.endswith('@newsletter'):
                ch.whatsapp_kind = 'channel'
            elif number.endswith('@g.us'):
                ch.whatsapp_kind = 'group'
            else:
                ch.whatsapp_kind = 'dm'

    @api.depends('whatsapp_number', 'whatsapp_kind')
    def _compute_wa_display_number(self):
        """Show a human phone only for 1:1 chats — never the raw group/channel
        JID. DMs store bare digits, so prefix '+' for an E.164-style display. (#wa-name)"""
        for ch in self:
            num = ch.whatsapp_number or ''
            ch.wa_display_number = (
                '+' + num if (ch.whatsapp_kind == 'dm' and num) else False)

    def _owa_placeholder_name(self, whatsapp_number):
        """Friendly fallback name for a WhatsApp conversation whose real
        subject/contact name isn't known yet — never expose the raw JID
        (``…@g.us`` / ``…@newsletter``) as the conversation title. (#wa-name)"""
        num = whatsapp_number or ''
        if num.endswith('@newsletter'):
            return _("WhatsApp Channel")
        if num.endswith('@g.us'):
            return _("WhatsApp Group")
        if '@' in num:
            return _("WhatsApp Chat")  # private LID etc.
        return num  # bare phone number for a 1:1 chat — acceptable

    _group_public_id_check = models.Constraint(
        "CHECK (channel_type = 'channel' OR channel_type = 'whatsapp' OR group_public_id IS NULL)",
        "Group authorization and group auto-subscription are only supported on channels and whatsapp.",
    )

    def _auto_init(self):
        """Add a partial unique index so two concurrent inbound webhooks
        on the same (account, number) can't both win the create-channel
        race. Cleanup of existing duplicates must run before this index
        will install — see ``owa.account.cleanup_duplicate_whatsapp_channels``."""
        res = super()._auto_init()
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discuss_channel_whatsapp_uniq
            ON discuss_channel (owa_account_id, whatsapp_number)
            WHERE channel_type = 'whatsapp' AND whatsapp_number IS NOT NULL
        """)
        return res

    # ------------------------------------------------------------------
    # COMPUTES
    # ------------------------------------------------------------------

    @api.depends('whatsapp_partner_id', 'whatsapp_number', 'name')
    def _compute_display_name(self):
        # Cover ALL WhatsApp channels (not just partner-backed 1:1s) so a
        # group/community/channel conversation never shows its raw JID. (#wa-name)
        wa_channels = self.filtered(lambda c: c.channel_type == 'whatsapp')
        for channel in wa_channels:
            number = channel.whatsapp_number or ''
            partner = channel.whatsapp_partner_id
            partner_name = (partner.name if partner and partner.name != partner.phone
                            else False)
            if partner_name:
                channel.display_name = f'{partner_name} ({number})'
            elif channel.name and not _owa_name_is_jid(channel.name):
                # Friendly group/community/channel name (or a 1:1 bare number).
                channel.display_name = channel.name
            else:
                channel.display_name = channel._owa_placeholder_name(number)
        super(DiscussChannel, self - wa_channels)._compute_display_name()

    @api.depends('last_wa_mail_message_id')
    def _compute_whatsapp_channel_valid_until(self):
        for channel in self:
            if channel.channel_type == "whatsapp" and channel.last_wa_mail_message_id:
                # The gateway has no 24h limit, but we keep this for UI consistency
                # Set a generous window (7 days) since the gateway can send anytime
                channel.whatsapp_channel_valid_until = (
                    channel.last_wa_mail_message_id.create_date + timedelta(days=7)
                )
            else:
                channel.whatsapp_channel_valid_until = False

    @api.depends('whatsapp_channel_valid_until')
    def _compute_whatsapp_channel_active(self):
        for channel in self:
            channel.whatsapp_channel_active = (
                channel.whatsapp_channel_valid_until
                and channel.whatsapp_channel_valid_until > fields.Datetime.now()
            )

    @api.constrains('channel_type', 'whatsapp_number')
    def _check_whatsapp_number(self):
        missing = self.filtered(
            lambda c: c.channel_type == 'whatsapp' and not c.whatsapp_number
        )
        if missing:
            raise ValidationError(
                _("A phone number is required for WhatsApp channels")
            )

    @api.constrains('group_public_id', 'group_ids')
    def _constraint_group_id_channel(self):
        valid_channels = self.filtered(lambda c: c.channel_type == 'whatsapp')
        super(DiscussChannel, self - valid_channels)._constraint_group_id_channel()

    def _compute_group_public_id(self):
        wa_channels = self.filtered(lambda c: c.channel_type == "whatsapp")
        wa_channels.filtered(lambda c: not c.group_public_id).group_public_id = self.env.ref('base.group_user')
        super(DiscussChannel, self - wa_channels)._compute_group_public_id()

    # ------------------------------------------------------------------
    # MAILING
    # ------------------------------------------------------------------

    def _get_notify_valid_parameters(self):
        if self.channel_type == 'whatsapp':
            return super()._get_notify_valid_parameters() | {'owa_inbound_msg_uid', 'owa_sender_jid'}
        return super()._get_notify_valid_parameters()

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        parent_msg_id = kwargs.pop('parent_msg_id', False)
        if kwargs.get('owa_inbound_msg_uid') and self.channel_type == 'whatsapp':
            self.env['owa.message'].create({
                'mail_message_id': message.id,
                'message_type': 'inbound',
                'mobile_number': f'+{self.whatsapp_number}',
                'msg_uid': kwargs['owa_inbound_msg_uid'],
                'sender_jid': kwargs.get('owa_sender_jid') or False,
                'parent_id': parent_msg_id,
                'state': 'received',
                'wa_account_id': self.owa_account_id.id,
            })
            if parent_msg_id:
                self.env['owa.message'].browse(parent_msg_id).state = 'replied'
        return super()._notify_thread(message, msg_vals=msg_vals, **kwargs)

    def message_post(self, *args, body='', attachment_ids=None,
                     message_type='notification', parent_id=False,
                     quick_reply_id=False, **kwargs):
        # Own-device sync: when set, record the created outbound owa.message as
        # an ALREADY-SENT message (carrying its WhatsApp id) and never re-send
        # it. Popped so it never reaches super().message_post. (#own-device-sync)
        recorded_outbound_uid = kwargs.pop('owa_recorded_outbound_uid', None)
        # Validate parent message for WhatsApp replies
        valid_parent_id = False
        if parent_id and self.whatsapp_number:
            parent_wa_msg = self.env['mail.message'].browse(parent_id).owa_message_ids
            # Accept BOTH inbound and outbound parents: an agent normally
            # replies to the customer's INBOUND message, and the dependent
            # block below needs that inbound counterpart to link the outbound
            # owa.message (for quoted replies + ack-clearing). Requiring
            # "outbound" here made valid_parent_id never set for the common
            # case, so quoted replies and ack-clearing were dead. (#F050)
            if (parent_wa_msg and len(parent_wa_msg) == 1
                    and parent_wa_msg.message_type in ("inbound", "outbound")
                    and parent_wa_msg.mobile_number_formatted == self.whatsapp_number):
                valid_parent_id = parent_id

        if message_type != 'whatsapp_message' or self.channel_type != 'whatsapp':
            message = super().message_post(
                *args, body=body, attachment_ids=attachment_ids,
                message_type=message_type, parent_id=parent_id, **kwargs
            )
            if valid_parent_id:
                message.parent_id = valid_parent_id
            return message

        # Defense-in-depth for a LEGACY orphan: a WhatsApp channel whose account
        # was NULLed before delete-reassign existed. Re-point it to a live
        # fallback (the configured Default WhatsApp Account, else the first
        # connected one) so the reply is not a dead end, and persist it so the
        # account selector reflects it. Only for genuine agent sends (never while
        # recording an inbound / own-device message), and only when the account
        # is truly MISSING — a present-but-disconnected account falls through to
        # the normal "not connected" error instead of silently switching the
        # conversation's number. (#delete-reassign)
        if (not kwargs.get('owa_inbound_msg_uid') and not recorded_outbound_uid
                and not self.owa_account_id):
            fallback = self.env['owa.account']._owa_fallback_account()
            # Skip if the fallback account already has a conversation with this
            # number (would violate the per-(account, number) unique index) —
            # rare (a legacy orphan whose number also lives on the fallback);
            # the send then fails cleanly via the not-connected guard.
            if fallback and not self.sudo().search_count([
                    ('channel_type', '=', 'whatsapp'),
                    ('owa_account_id', '=', fallback.id),
                    ('whatsapp_number', '=', self.whatsapp_number),
                    ('id', '!=', self.id)]):
                self.sudo().owa_account_id = fallback.id

        # Handle audio splitting — audio must be sent separately
        messages = None
        if (not kwargs.get('owa_inbound_msg_uid') and not recorded_outbound_uid
                and attachment_ids and body and not tools.is_html_empty(body)):
            AUDIO_TYPES = ('audio/aac', 'audio/mp4', 'audio/mpeg', 'audio/amr', 'audio/ogg',
                           'audio/ogg; codecs=opus')
            attachment_records = self.env['ir.attachment'].browse(attachment_ids)
            audio_attachments = attachment_records.filtered(lambda x: x.mimetype in AUDIO_TYPES)
            if audio_attachments:
                body_message = super().message_post(
                    *args, message_type=message_type, body=body,
                    attachment_ids=(attachment_records - audio_attachments).ids,
                    parent_id=parent_id, **kwargs,
                )
                audio_message = super().message_post(
                    *args, message_type=message_type,
                    attachment_ids=audio_attachments.ids,
                    parent_id=parent_id, **kwargs,
                )
                messages = body_message + audio_message

        if not messages:
            messages = super().message_post(
                *args, body=body, message_type=message_type,
                attachment_ids=attachment_ids, parent_id=parent_id, **kwargs,
            )

        # Create owa.message records for outbound. When this is a reply to
        # a specific inbound message, link the outbound owa.message to its
        # inbound counterpart via ``parent_id`` so ``_maybe_clear_ack_reaction``
        # can find the original to clear the 👀 ack from.
        outbound_parent_owa_id = False
        if valid_parent_id:
            parent_wa_msgs = self.env['mail.message'].browse(valid_parent_id).owa_message_ids
            inbound_parent = parent_wa_msgs.filtered(lambda m: m.message_type == 'inbound')
            if inbound_parent:
                outbound_parent_owa_id = inbound_parent[:1].id
        owa_message_vals = []
        for new_msg in messages:
            if not new_msg.owa_message_ids:
                msg_vals = {
                    'body': new_msg.body,
                    'mail_message_id': new_msg.id,
                    'message_type': 'outbound',
                    'mobile_number': f'+{self.whatsapp_number}',
                    'wa_account_id': self.owa_account_id.id,
                    'parent_id': outbound_parent_owa_id,
                    # Carry the template id so _send_single_message dispatches
                    # button/list templates as interactive content. (#compose)
                    'quick_reply_id': quick_reply_id or False,
                    # Deferred send (top-menu Compose "Schedule Send"): the send
                    # cron transmits it once due, instead of the synchronous
                    # send below. (#F102)
                    'scheduled_date': self.env.context.get('owa_scheduled_date') or False,
                }
                if recorded_outbound_uid:
                    # Sent from the phone / another device — record it as a
                    # delivered outbound message carrying its WhatsApp id; never
                    # re-send. Delivery/read receipts later upgrade the state by
                    # msg_uid. (#own-device-sync)
                    msg_vals.update({
                        'msg_uid': recorded_outbound_uid,
                        'wa_message_uid': recorded_outbound_uid,
                        'state': 'sent',
                        'scheduled_date': False,
                    })
                owa_message_vals.append(msg_vals)

        # Track last inbound message for valid_until computation
        if messages.author_id == self.whatsapp_partner_id:
            self.last_wa_mail_message_id = messages[-1]
            Store(bus_channel=self).add(self, "whatsapp_channel_valid_until").bus_send()

        if owa_message_vals:
            queued = self.env['owa.message'].create(owa_message_vals)
            # Phone-originated messages are already on WhatsApp — create the
            # record only, never re-send. (#own-device-sync)
            if recorded_outbound_uid:
                pass
            # When scheduled, leave the message 'outgoing' for the periodic send
            # cron to transmit at its scheduled_date — do NOT send now. (#F102)
            elif not self.env.context.get('owa_scheduled_date'):
                queued._send_message()

        if valid_parent_id:
            messages.parent_id = valid_parent_id

        # Phase 22A: an inbound on a resolved channel reopens it
        if (self.channel_type == 'whatsapp'
                and self.triage_state == 'resolved'
                and self.whatsapp_partner_id
                and messages.author_id == self.whatsapp_partner_id):
            self.action_reopen()

        # Phase 27A: relay inbound WhatsApp messages to the linked helpdesk
        # ticket's chatter as an internal note. This block was previously a
        # SECOND `message_post` definition further down the class, which
        # silently shadowed this whole method (Python keeps the last def) and
        # killed all outbound send logic above. Folded here so both behaviours
        # coexist. Guarded against loops (skip_helpdesk_relay) and never blocks
        # the primary post. Only inbound (customer-authored) messages relay.
        if (self.helpdesk_ticket_id_int
                and 'helpdesk.ticket' in self.env
                and not self.env.context.get('skip_helpdesk_relay')
                and messages.author_id == self.whatsapp_partner_id):
            try:
                ticket = self.env['helpdesk.ticket'].sudo().browse(
                    self.helpdesk_ticket_id_int).exists()
                if ticket:
                    author = self.whatsapp_partner_id or messages[:1].author_id
                    prefix = _("WhatsApp inbound from %s:") % (
                        author.display_name if author
                        else (self.whatsapp_number or '?'))
                    relay_body = Markup("<p><em>%s</em></p>%s") % (
                        prefix, Markup(messages[:1].body or ''))
                    ticket.with_context(skip_helpdesk_relay=True).message_post(
                        body=relay_body,
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                        author_id=author.id if author else False,
                    )
            except Exception:
                _logger.exception(
                    "WhatsApp->Helpdesk relay failed for channel %s ticket %s",
                    self.id, self.helpdesk_ticket_id_int)

        # CRM bridge: relay inbound WhatsApp messages to the linked crm.lead's
        # chatter as an internal note, so the salesperson sees the conversation
        # on the lead. Same shape as the helpdesk relay above — guarded against
        # loops (skip_crm_relay) and never blocks the primary post. Only inbound
        # (customer-authored) messages relay. (#wa-crm-bridge)
        if (self.crm_lead_id_int
                and 'crm.lead' in self.env
                and not self.env.context.get('skip_crm_relay')
                and messages.author_id == self.whatsapp_partner_id):
            try:
                lead = self.env['crm.lead'].sudo().browse(
                    self.crm_lead_id_int).exists()
                if lead:
                    author = self.whatsapp_partner_id or messages[:1].author_id
                    prefix = _("WhatsApp inbound from %s:") % (
                        author.display_name if author
                        else (self.whatsapp_number or '?'))
                    relay_body = Markup("<p><em>%s</em></p>%s") % (
                        prefix, Markup(messages[:1].body or ''))
                    lead.with_context(skip_crm_relay=True).message_post(
                        body=relay_body,
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                        author_id=author.id if author else False,
                    )
            except Exception:
                _logger.exception(
                    "WhatsApp->CRM relay failed for channel %s lead %s",
                    self.id, self.crm_lead_id_int)

        return messages[0]

    # ------------------------------------------------------------------
    # CHANNEL MANAGEMENT
    # ------------------------------------------------------------------

    def _owa_topup_notify_members(self, channel, account):
        """Ensure the account's notify_user_ids are members of an EXISTING
        WhatsApp channel (covers channels created before an agent was added to
        the notify list). Shared mode only — own_agent keeps channels isolated
        to their assignee. Cheap: only writes when there is a real gap. (#S4-2)"""
        if not channel or not account:
            return
        if self.env['ir.config_parameter'].sudo().get_param(
                'open_whatsapp_connector.chat_visibility', 'shared') != 'shared':
            return
        partners = account._owa_access_partners()
        missing = partners - channel.channel_member_ids.partner_id
        if missing:
            channel.sudo().channel_member_ids = [
                Command.create({'partner_id': p.id}) for p in missing]
            channel._broadcast(missing.ids)

    def _get_owa_whatsapp_channel(self, whatsapp_number, wa_account_id,
                                  sender_name=False, create_if_not_found=False,
                                  related_message=False):
        """Find or create an Open WhatsApp Connector discuss channel."""
        # Group/community/newsletter JIDs are not phone numbers; passing them
        # through `wa_phone_format` would have phonenumbers reinterpret the
        # leading digits as a US number and silently drop the `@g.us` /
        # `@newsletter` / `@broadcast` suffix, which would route every group
        # inbound to a per-sender personal channel and surface as separate
        # 1-on-1 threads instead of one group conversation.
        if '@' in (whatsapp_number or ''):
            wa_formatted = whatsapp_number.lstrip('+')
        else:
            base_number = whatsapp_number if whatsapp_number.startswith('+') else f'+{whatsapp_number}'
            wa_number = base_number.lstrip('+')
            wa_formatted = wa_phone_format(self.env, base_number) or wa_number

        related_record = False
        responsible_partners = self.env['res.partner']
        channel_domain = [
            ('whatsapp_number', '=', wa_formatted),
            ('owa_account_id', '=', wa_account_id.id),
        ]

        if related_message:
            related_record = self.env[related_message.model].browse(related_message.res_id)
            if hasattr(related_record, '_owa_get_responsible'):
                responsible_partners = related_record._owa_get_responsible(
                    related_message=related_message,
                    related_record=related_record,
                    whatsapp_account=wa_account_id,
                ).partner_id

        channel = self.sudo().search(channel_domain, order='create_date desc', limit=1)
        if responsible_partners:
            channel = channel.filtered(
                lambda c: all(r in c.channel_member_ids.partner_id for r in responsible_partners)
            )

        partners_to_notify = responsible_partners
        if not channel and create_if_not_found:
            recipient_partner = self.env['res.partner']._find_or_create_from_wa_number(
                wa_formatted, sender_name
            )
            recipient_name = (
                recipient_partner.name
                if recipient_partner and recipient_partner.name != recipient_partner.phone
                else False
            )
            # Per-agent visibility (optional): when the admin has switched
            # "WhatsApp Conversation Visibility" to "own_agent", stamp the new
            # conversation with an owning agent so it is isolated immediately.
            # Resolved BEFORE create and folded into the create dict so the
            # assignee_id tracking message fires exactly once (not against an
            # empty channel) and we save a write round-trip. In the default
            # 'shared' mode assignee_id stays empty -> zero behaviour change.
            auto_agent = self.env['res.users']
            if self.env['ir.config_parameter'].sudo().get_param(
                    'open_whatsapp_connector.chat_visibility', 'shared') == 'own_agent':
                auto_agent = (responsible_partners[:1].user_ids[:1]
                              or wa_account_id.notify_user_ids[:1])
            channel = self.sudo().with_context(tools.clean_context(self.env.context)).create({
                'name': (f"{recipient_name} ({wa_formatted})" if recipient_name
                         else self._owa_placeholder_name(wa_formatted)),
                'channel_type': 'whatsapp',
                'whatsapp_number': wa_formatted,
                'whatsapp_partner_id': recipient_partner.id if recipient_partner else False,
                'owa_account_id': wa_account_id.id,
                'assignee_id': auto_agent.id if auto_agent else False,
            })
            if recipient_partner:
                partners_to_notify |= recipient_partner

            if related_message and related_record:
                # Add related document link in channel
                info = _("Related %(model_name)s: ",
                         model_name=self.env['ir.model']._get(related_message.model).display_name)
                url = Markup('{base_url}/odoo/{model}/{res_id}').format(
                    base_url=self.get_base_url(),
                    model=related_message.model,
                    res_id=related_message.res_id)
                channel.message_post(
                    body=Markup('<p>{info}<a target="_blank" href="{url}">{name}</a></p>').format(
                        info=info, url=url, name=related_message.record_name),
                    message_type='comment',
                    author_id=self.env.ref('base.partner_root').id,
                    subtype_xmlid='mail.mt_note',
                )
                if hasattr(related_record, 'message_post'):
                    info2 = _("A new WhatsApp channel is created for this document")
                    ch_url = Markup('{base_url}/odoo/discuss.channel/{id}').format(
                        base_url=self.get_base_url(), id=channel.id)
                    related_record.message_post(
                        author_id=self.env.ref('base.partner_root').id,
                        body=Markup(
                            '<p>{info} <a target="_blank" class="o_whatsapp_channel_redirect"'
                            ' data-oe-id="{id}" href="{url}">{name}</a></p>'
                        ).format(info=info2, url=ch_url, id=channel.id, name=channel.display_name),
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )

            if partners_to_notify == (recipient_partner or self.env['res.partner']):
                # Pure inbound (no responsible record): add who is allowed to
                # see it. Shared mode -> the whole access pool (Users to Notify +
                # Allowed Group members) so every agent sees the chat. Own_agent
                # mode -> keep the original notify-user behaviour (the read rule
                # isolates it to the auto-assigned agent). (#allowed-group)
                shared = self.env['ir.config_parameter'].sudo().get_param(
                    'open_whatsapp_connector.chat_visibility', 'shared') != 'own_agent'
                pool = (wa_account_id._owa_access_partners() if shared
                        else wa_account_id.notify_user_ids.partner_id)
                if pool:
                    partners_to_notify |= pool
            channel.channel_member_ids = (
                [Command.clear()]
                + [Command.create({'partner_id': p.id}) for p in partners_to_notify]
            )
            channel._broadcast(partners_to_notify.ids)
        elif channel:
            self._owa_topup_notify_members(channel, wa_account_id)

        return channel

    def _get_whatsapp_channel(self, whatsapp_number, wa_account_id,
                              sender_name=False, create_if_not_found=False,
                              related_message=False):
        """Dispatch helper: routes owa.account calls to _get_owa_whatsapp_channel,
        and delegates to super() for standard enterprise whatsapp.account calls."""
        if wa_account_id and getattr(wa_account_id, '_name', False) == 'owa.account':
            return self._get_owa_whatsapp_channel(
                whatsapp_number, wa_account_id,
                sender_name=sender_name, create_if_not_found=create_if_not_found,
                related_message=related_message,
            )
        if hasattr(super(), '_get_whatsapp_channel'):
            return super()._get_whatsapp_channel(
                whatsapp_number, wa_account_id,
                sender_name=sender_name, create_if_not_found=create_if_not_found,
                related_message=related_message,
            )
        return self.env['discuss.channel']

    # ------------------------------------------------------------------
    # OVERRIDES
    # ------------------------------------------------------------------

    def write(self, vals):
        # Guard the per-(account, number) unique index: switching a WhatsApp
        # conversation to an account that ALREADY has a conversation with the
        # same number would violate discuss_channel_whatsapp_uniq and surface as
        # a raw DB error. Turn it into a clear message. Callers that must not
        # fail — the delete-reassign in owa.account.unlink() and the orphan
        # send-fallback in message_post() — pre-skip collisions, so this only
        # ever fires for an interactive account switch (the composer wizard or
        # the editable Account field). (#reply-account)
        if vals.get('owa_account_id'):
            new_acct = vals['owa_account_id']
            for c in self:
                if (c.channel_type == 'whatsapp' and c.whatsapp_number
                        and c.owa_account_id.id != new_acct
                        and self.sudo().search_count([
                            ('channel_type', '=', 'whatsapp'),
                            ('owa_account_id', '=', new_acct),
                            ('whatsapp_number', '=', c.whatsapp_number),
                            ('id', '!=', c.id)])):
                    raise UserError(_(
                        "This contact already has a conversation on that "
                        "WhatsApp account. Open that conversation (or merge the "
                        "two from the “Merge” action) instead of switching the "
                        "account here."))
        # Protect the integrity of per-agent visibility ("own_agent" mode).
        # The visibility ir.rule is READ-only, and the stock discuss.channel
        # write rule lets any member write the channel — so without this guard a
        # non-admin agent could clear or hand off `assignee_id` on a WhatsApp
        # conversation (even one the read-rule hides from them) and change who
        # can see it. In restricted mode a non-admin may only CLAIM a
        # conversation for themselves; clearing/reassigning is admin-only.
        if ('assignee_id' in vals and self and not self.env.su
                and not self.env.user.has_group(
                    'open_whatsapp_connector.group_owa_admin')
                and any(c.channel_type == 'whatsapp' for c in self)
                and self.env['ir.config_parameter'].sudo().get_param(
                    'open_whatsapp_connector.chat_visibility', 'shared') == 'own_agent'
                and vals.get('assignee_id') != self.env.uid):
            raise AccessError(_(
                "In restricted WhatsApp visibility mode you can only claim a "
                "conversation for yourself. Ask a WhatsApp Administrator to "
                "reassign or release it."))
        # Keep resolved_at consistent with triage_state on EVERY path — the
        # clickable status bar, a kanban drag (when grouped by Triage), or an
        # inline edit — not only the action buttons. When the caller already
        # supplies resolved_at (the buttons do) we respect it and don't override.
        if 'triage_state' in vals and 'resolved_at' not in vals:
            vals = dict(vals)
            vals['resolved_at'] = (
                fields.Datetime.now() if vals['triage_state'] == 'resolved'
                else False)
        res = super().write(vals)
        # Keep triage coherent with ownership: a conversation that now HAS an
        # assignee must not still read "Unassigned" (so the Conversations
        # kanban's no-owner column — labelled "Unassigned" — only ever holds
        # truly unowned conversations). When an assignee is set via any path
        # (inline edit, kanban drag, form) and the caller didn't set triage
        # itself, move records still sitting at 'unassigned' to 'active'.
        # on_hold/resolved conversations keep their state when reassigned. The
        # context flag guards against recursion (the nested write re-enters here).
        if ('assignee_id' in vals and 'triage_state' not in vals
                and not self.env.context.get('owa_skip_triage_sync')):
            stale = self.filtered(
                lambda c: c.channel_type == 'whatsapp' and c.assignee_id
                and c.triage_state == 'unassigned')
            if stale:
                stale.with_context(owa_skip_triage_sync=True).write(
                    {'triage_state': 'active'})
        # Chat transfer: a WhatsApp conversation (re)assigned to an agent must
        # also make that agent a channel MEMBER — otherwise, in own_agent
        # visibility mode, the new assignee cannot open the chat handed to them
        # (the discuss read rule needs membership). Runs on every assign path
        # (form, kanban drag, "Assign to me"); no-op when already a member, so
        # the claiming agent is never double-added. The previous assignee is
        # left as-is — the read rule already hides it from them. (#transfer)
        if vals.get('assignee_id'):
            assignee_partner = self.env['res.users'].browse(
                vals['assignee_id']).partner_id
            for ch in self.filtered(lambda c: c.channel_type == 'whatsapp'):
                if (assignee_partner
                        and assignee_partner not in ch.channel_member_ids.partner_id):
                    ch.sudo().channel_member_ids = [
                        Command.create({'partner_id': assignee_partner.id})]
        return res

    def _action_unfollow(self, partner=None, guest=None, post_leave_message=True):
        if (partner and self.channel_type == "whatsapp"
                and next(
                    (m.partner_id for m in self.channel_member_ids if not m.partner_id.partner_share),
                    self.env["res.partner"]
                ) == partner):
            msg = _("You can't leave this channel. As the owner, you can only delete it.")
            partner._bus_send_transient_message(self, msg)
            return
        super()._action_unfollow(partner, guest, post_leave_message)

    def _to_store_defaults(self, target):
        return super()._to_store_defaults(target) + [
            Store.Attr("whatsapp_channel_valid_until", predicate=is_whatsapp_channel),
            Store.One("whatsapp_partner_id", [], predicate=is_whatsapp_channel),
            Store.One("owa_account_id", ["name"], predicate=is_whatsapp_channel, sudo=True),
            Store.Attr("whatsapp_kind", predicate=is_whatsapp_channel),
        ]

    def _types_allowing_seen_infos(self):
        return super()._types_allowing_seen_infos() + ["whatsapp"]

    def execute_command_leave(self, **kwargs):
        if self.channel_type == 'whatsapp':
            self.action_unfollow()
        else:
            super().execute_command_leave(**kwargs)

    # ─── Phase 22A: triage helpers ───────────────────────────────────────

    @api.depends('assigned_at', 'owa_account_id.sla_minutes')
    def _compute_sla_due_at(self):
        for ch in self:
            if ch.assigned_at and ch.owa_account_id and ch.owa_account_id.sla_minutes:
                ch.sla_due_at = ch.assigned_at + timedelta(
                    minutes=ch.owa_account_id.sla_minutes)
            else:
                ch.sla_due_at = False

    @api.depends('sla_due_at', 'triage_state', 'resolved_at')
    def _compute_sla_breached(self):
        now = fields.Datetime.now()
        for ch in self:
            ch.sla_breached = bool(
                ch.sla_due_at and ch.sla_due_at < now
                and ch.triage_state in ('unassigned', 'active'))

    def action_assign_to_me(self):
        for ch in self.filtered(lambda c: c.channel_type == 'whatsapp'):
            ch.write({
                'assignee_id': self.env.user.id,
                'triage_state': 'active' if ch.triage_state == 'unassigned' else ch.triage_state,
                'assigned_at': ch.assigned_at or fields.Datetime.now(),
            })
        return True

    def action_mark_resolved(self):
        for ch in self.filtered(lambda c: c.channel_type == 'whatsapp'):
            ch.write({
                'triage_state': 'resolved',
                'resolved_at': fields.Datetime.now(),
            })
            # Phase 23B: trigger CSAT if account has it enabled
            if (ch.owa_account_id and getattr(ch.owa_account_id, 'auto_csat', False)
                    and ch.whatsapp_partner_id):
                try:
                    self.env['owa.satisfaction.score'].sudo()._send_csat_invite(ch)
                except Exception:
                    _logger.exception("CSAT invite failed for channel %s", ch.id)
        return True

    def action_reopen(self):
        for ch in self.filtered(lambda c: c.channel_type == 'whatsapp'):
            ch.write({
                'triage_state': 'active',
                'resolved_at': False,
            })
        return True

    def action_put_on_hold(self):
        self.filtered(lambda c: c.channel_type == 'whatsapp').write({
            'triage_state': 'on_hold',
        })
        return True

    def action_open_in_discuss(self):
        """Jump from the Conversations management view straight into the live
        conversation in the Discuss app."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/odoo/discuss?active_id=discuss.channel_%s' % self.id,
            'target': 'self',
        }

    # ─── Phase 27A: helpdesk bridge ───────────────────────────────────
    def action_open_helpdesk_ticket(self):
        """Smart-button action: open the linked helpdesk.ticket form. Raises
        if no ticket linked or helpdesk module not installed."""
        self.ensure_one()
        if 'helpdesk.ticket' not in self.env:
            raise UserError(_(
                "Helpdesk module is not installed on this database."))
        if not self.helpdesk_ticket_id_int:
            raise UserError(_("No helpdesk ticket is linked to this conversation."))
        ticket = self.env['helpdesk.ticket'].browse(self.helpdesk_ticket_id_int)
        if not ticket.exists():
            raise UserError(_(
                "The linked helpdesk ticket (id %s) no longer exists.",
                self.helpdesk_ticket_id_int))
        return {
            'type': 'ir.actions.act_window',
            'name': ticket.display_name or _("Helpdesk Ticket"),
            'res_model': 'helpdesk.ticket',
            'res_id': ticket.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ─── CRM bridge (open_whatsapp_connector_crm) ─────────────────────
    def action_open_crm_lead(self):
        """Smart-button action: open the linked crm.lead form. Raises if no
        lead linked or crm module not installed."""
        self.ensure_one()
        if 'crm.lead' not in self.env:
            raise UserError(_("CRM module is not installed on this database."))
        if not self.crm_lead_id_int:
            raise UserError(_("No CRM lead is linked to this conversation."))
        lead = self.env['crm.lead'].browse(self.crm_lead_id_int)
        if not lead.exists():
            raise UserError(_(
                "The linked CRM lead (id %s) no longer exists.",
                self.crm_lead_id_int))
        return {
            'type': 'ir.actions.act_window',
            'name': lead.display_name or _("CRM Lead"),
            'res_model': 'crm.lead',
            'res_id': lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _cron_check_sla(self):
        """Phase 22A: surface SLA breaches as activities for the assignee."""
        breached = self.search([
            ('channel_type', '=', 'whatsapp'),
            ('triage_state', 'in', ['unassigned', 'active']),
            ('sla_due_at', '<', fields.Datetime.now()),
        ])
        for ch in breached:
            existing = self.env['mail.activity'].search([
                ('res_model', '=', 'discuss.channel'),
                ('res_id', '=', ch.id),
                ('summary', '=', 'WhatsApp SLA breached'),
            ], limit=1)
            if existing:
                continue
            # discuss.channel does not inherit mail.activity.mixin in every
            # install (e.g. Community without an app that adds it). The breach
            # activity is best-effort: skip it (rather than crash the SLA cron
            # with AttributeError every run) when the mixin is absent — the
            # chatbot route guards the same call. (#sla-activity-guard)
            if not hasattr(ch, 'activity_schedule'):
                continue
            target = ch.assignee_id or self.env.user
            ch.activity_schedule(
                'mail.mail_activity_data_warning',
                user_id=target.id,
                summary='WhatsApp SLA breached',
                note=Markup(
                    '<p>This WhatsApp conversation has exceeded its SLA window. '
                    'Either reply, reassign, or mark as resolved.</p>'
                ),
            )

