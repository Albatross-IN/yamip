import logging
import re
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools import plaintext2html, html2plaintext
from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format

_logger = logging.getLogger(__name__)


class OwaChatbot(models.Model):
    _name = 'owa.chatbot'
    _description = 'WhatsApp Chatbot'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(string="Chatbot Name", required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    sequence = fields.Integer(default=10)
    wa_account_id = fields.Many2one('owa.account', string="WhatsApp Account",
        help="Required before the chatbot can be activated. Pre-built starter "
             "bots ship without an account so you just pick one and tick Active.")
    welcome_message = fields.Text(string="Welcome Message",
        default="Hello! How can I help you today?",
        help="Sent when the chatbot first engages with a contact")
    step_ids = fields.One2many('owa.chatbot.step', 'chatbot_id', string="Steps")
    step_count = fields.Integer(string="Steps", compute='_compute_step_count')
    # Explicit, user-settable entry point. When blank we fall back to the
    # lowest-sequence step (legacy behaviour), so old bots and freshly-seeded
    # ones keep working without a migration. Set from the flow designer.
    first_step_id = fields.Many2one('owa.chatbot.step', string="First Step",
        domain="[('chatbot_id', '=', id)]", ondelete='set null',
        help="Where a new conversation enters the flow. Leave empty to use the "
             "first step by sequence.")
    session_timeout_minutes = fields.Integer(string="Session Timeout (minutes)",
        default=30, help="Chatbot session expires after this many minutes of inactivity")
    active_outside_office_hours = fields.Boolean(
        string="Only outside office hours", default=False,
        help="When on, this bot engages a new conversation ONLY outside the "
             "WhatsApp account's office hours (Account → Office Hours). During "
             "working hours it stays quiet so your operators handle chats live.")
    typing_indicator = fields.Boolean(
        string="Show typing…", default=True,
        help="Send a 'typing…' presence to WhatsApp just before each bot reply "
             "so the conversation feels natural (QR accounts only).")

    # ── Fallback (too many unrecognised replies) ──────────────────────
    fallback_max_retries = fields.Integer(
        string="Retries before fallback", default=2,
        help="After this many replies the bot could not understand at a menu, "
             "it runs the fallback step (or hands off to a human).")
    fallback_step_id = fields.Many2one('owa.chatbot.step', string="Fallback Step",
        domain="[('chatbot_id', '=', id)]", ondelete='set null',
        help="Where to go once the retry limit is hit — typically a 'Hand off "
             "to Human' step. Leave empty to hand off to the account owner.")

    # ── Global keywords (reachable from an existing conversation) ─────
    keyword_restart = fields.Char(
        string="Restart keywords", default="menu, start, hi, hello",
        help="Comma-separated words a contact can send at any time to (re)open "
             "the bot from the beginning.")
    keyword_agent = fields.Char(
        string="Agent keywords", default="agent, human, help, support",
        help="Comma-separated words that immediately hand the conversation to a "
             "human, at any point.")

    @api.depends('step_ids')
    def _compute_step_count(self):
        for bot in self:
            bot.step_count = len(bot.step_ids)

    def _effective_first_step(self):
        """The step a new conversation starts on: the explicit First Step if
        set and still attached to this bot, else the lowest-sequence step."""
        self.ensure_one()
        if self.first_step_id and self.first_step_id.chatbot_id == self:
            return self.first_step_id
        return self.step_ids.sorted('sequence')[:1]

    @api.constrains('active', 'wa_account_id')
    def _check_account_required_when_active(self):
        # wa_account_id is optional so pre-built starter bots can ship without
        # an account, but a bot cannot actually run until one is assigned.
        for bot in self:
            if bot.active and not bot.wa_account_id:
                raise ValidationError(_(
                    "Select a WhatsApp account before activating this chatbot."
                ))

    @api.constrains('active', 'wa_account_id')
    def _check_single_active_per_account(self):
        for bot in self:
            if not bot.active or not bot.wa_account_id:
                continue
            if self.search_count([
                ('id', '!=', bot.id),
                ('active', '=', True),
                ('wa_account_id', '=', bot.wa_account_id.id),
            ]):
                raise ValidationError(_(
                    "Only one chatbot can be active per WhatsApp account. "
                    "Deactivate the other chatbot first."
                ))

    # ── Flow health (shown as a banner in the designer & form) ─────────
    def _flow_problems(self):
        """Return a list of human-readable problems with the flow so the admin
        knows the bot is not silently broken. Empty list == healthy."""
        self.ensure_one()
        problems = []
        steps = self.step_ids
        if not steps:
            problems.append(_("This bot has no steps yet."))
            return problems
        first = self._effective_first_step()
        if not first:
            problems.append(_("No entry point — set a First Step."))
        # Reachability from the first step.
        reachable = set()
        frontier = first.ids
        while frontier:
            sid = frontier.pop()
            if sid in reachable:
                continue
            reachable.add(sid)
            step = steps.browse(sid)
            nxt = set()
            if step.next_step_id:
                nxt.add(step.next_step_id.id)
            for opt in step.option_ids:
                if opt.next_step_id:
                    nxt.add(opt.next_step_id.id)
            frontier.extend(nxt - reachable)
        for step in steps:
            if step.id not in reachable:
                problems.append(_("Step '%s' can't be reached from the start.")
                                % (step.name or step.id))
            if step.step_type == 'menu':
                if not step.option_ids:
                    problems.append(_("Menu '%s' has no options.") % (step.name or step.id))
                else:
                    for opt in step.option_ids:
                        if not opt.next_step_id:
                            problems.append(
                                _("Menu option '%s' points nowhere.") % (opt.label or '?'))
        return problems

    def action_open_designer(self):
        """Open the visual flow designer (OWL client action)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'owa_chatbot_designer',
            'name': _("Flow Designer — %s") % self.name,
            'params': {'chatbot_id': self.id},
        }

    # ── Global keyword helpers ─────────────────────────────────────────
    @staticmethod
    def _keyword_hit(csv_keywords, message_text):
        """True when the (trimmed, lower-cased) message equals one of the
        comma-separated keywords."""
        text = (message_text or '').strip().lower()
        if not text:
            return False
        words = [w.strip().lower() for w in (csv_keywords or '').split(',') if w.strip()]
        return text in words

    def _handoff_to_human(self, from_number):
        """Pause the bot on this conversation and route it to the account owner,
        used when a contact types an 'agent' keyword. Any active session is
        marked routed so the bot stops driving it."""
        self.ensure_one()
        formatted = wa_phone_format(self.env, from_number) or from_number
        self.env['owa.chatbot.session'].search([
            ('chatbot_id', '=', self.id),
            '|', ('phone_number', '=', formatted), ('phone_number', '=', from_number),
            ('state', '=', 'active'),
        ]).write({'state': 'routed'})
        channel = self._find_channel(from_number)
        if channel:
            channel.owa_bot_paused = True
            agent = self.wa_account_id.user_id
            if agent and agent.partner_id:
                try:
                    channel.add_members(partner_ids=agent.partner_id.ids)
                except Exception:
                    _logger.exception("chatbot agent-keyword: add_members failed on %s",
                                      channel.id)
                if 'assignee_id' in channel._fields and not channel.assignee_id:
                    channel.assignee_id = agent.id
            if 'triage_state' in channel._fields and channel.triage_state != 'resolved':
                channel.triage_state = 'active'
            channel.message_post(
                body=_("Contact asked for a human — bot paused."),
                message_type='comment', subtype_xmlid='mail.mt_note')
        self._send_bot_message(from_number, _(
            "Sure — I'm connecting you with a member of our team. "
            "They'll be with you shortly."))

    # ── Duplicate (graph-aware clone, for "use a template, edit a copy") ──
    def copy(self, default=None):
        """Duplicate the bot AND its step graph, re-pointing every internal
        link (next_step_id, menu-option next_step_id, first/fallback step) at
        the cloned steps — a plain super().copy() would leave the copies
        pointing back at the original's steps."""
        new_bots = self.env['owa.chatbot']
        for bot in self:
            new_bots |= bot._copy_one(default)
        return new_bots

    def _copy_one(self, default=None):
        self.ensure_one()
        vals = dict(default or {})
        vals.update({
            'step_ids': [],          # clone the steps ourselves, below
            'first_step_id': False,
            'fallback_step_id': False,
            'active': False,         # a fresh copy never auto-runs
        })
        vals.setdefault('name', _("%s (copy)") % (self.name or ''))
        new_bot = super().copy(vals)

        id_map = {}
        for step in self.step_ids.sorted('id'):
            new_step = step.copy({
                'chatbot_id': new_bot.id,
                'next_step_id': False,
                'option_ids': [],
            })
            id_map[step.id] = new_step
        for step in self.step_ids:
            new_step = id_map[step.id]
            if step.next_step_id and step.next_step_id.id in id_map:
                new_step.next_step_id = id_map[step.next_step_id.id].id
            for opt in step.option_ids.sorted('id'):
                self.env['owa.chatbot.menu.option'].create({
                    'step_id': new_step.id,
                    'label': opt.label,
                    'keyword': opt.keyword,
                    'sequence': opt.sequence,
                    'next_step_id': (id_map[opt.next_step_id.id].id
                                     if opt.next_step_id and opt.next_step_id.id in id_map
                                     else False),
                })
        rewire = {}
        if self.first_step_id and self.first_step_id.id in id_map:
            rewire['first_step_id'] = id_map[self.first_step_id.id].id
        if self.fallback_step_id and self.fallback_step_id.id in id_map:
            rewire['fallback_step_id'] = id_map[self.fallback_step_id.id].id
        if rewire:
            new_bot.write(rewire)
        return new_bot

    def action_duplicate(self):
        """Clone this bot (e.g. a shipped template) and open the copy so the
        user can edit their own version without touching the original."""
        self.ensure_one()
        new_bot = self.copy()
        return {
            'type': 'ir.actions.act_window',
            'name': _("New Chatbot"),
            'res_model': 'owa.chatbot',
            'res_id': new_bot.id,
            'views': [[False, 'form']],
            'target': 'current',
        }

    # ── Session lifecycle ──────────────────────────────────────────────
    def _start_session(self, from_number):
        """Start a new chatbot session for a contact."""
        self.ensure_one()
        formatted_phone = wa_phone_format(self.env, from_number) or from_number
        # Close any existing active session (match either the formatted or raw
        # phone so legacy unformatted sessions are still found).
        existing = self.env['owa.chatbot.session'].search([
            ('chatbot_id', '=', self.id),
            '|', ('phone_number', '=', formatted_phone),
                 ('phone_number', '=', from_number),
            ('state', '=', 'active'),
        ])
        existing.write({'state': 'expired'})

        first_step = self._effective_first_step()
        session = self.env['owa.chatbot.session'].create({
            'chatbot_id': self.id,
            'phone_number': formatted_phone,
            'current_step_id': first_step.id if first_step else False,
            'state': 'active',
        })

        # Send welcome message
        if self.welcome_message:
            self._send_bot_message(from_number, self.welcome_message)

        # Present the first step. Persist where the auto-advance chain actually
        # STOPPED (e.g. the menu after a welcome 'message' step), not just the
        # first step — otherwise the user's next reply is matched against the
        # wrong step and the menu is re-sent. (#F117)
        if first_step:
            final_step = first_step._present_step(from_number, session=session)
            if final_step and session.current_step_id and final_step.id != session.current_step_id.id:
                session.current_step_id = final_step.id

        return session

    # ── Sending (through the Discuss channel so it is visible there) ────
    def _bot_author(self):
        """Partner used as the author of bot messages so they render as
        outbound in Discuss (i.e. NOT the contact)."""
        self.ensure_one()
        if self.wa_account_id.user_id.partner_id:
            return self.wa_account_id.user_id.partner_id
        return self.env.ref('base.partner_root', raise_if_not_found=False) \
            or self.env.user.partner_id

    def _find_channel(self, to_number):
        self.ensure_one()
        if not self.wa_account_id:
            return self.env['discuss.channel']
        formatted = wa_phone_format(self.env, to_number) or to_number
        return self.env['discuss.channel'].sudo().search([
            ('channel_type', '=', 'whatsapp'),
            ('owa_account_id', '=', self.wa_account_id.id),
            '|', ('whatsapp_number', '=', (formatted or '').lstrip('+')),
                 ('whatsapp_number', '=', (to_number or '').lstrip('+')),
        ], limit=1)

    def _send_typing(self, to_number):
        """Best-effort 'typing…' presence so the reply feels human. QR only."""
        self.ensure_one()
        account = self.wa_account_id
        if not self.typing_indicator or not account or account.connection_type != 'qr':
            return
        if account.session_state != 'connected':
            return
        formatted = wa_phone_format(self.env, to_number) or to_number
        number = (formatted or '').lstrip('+')
        if not number:
            return
        try:
            account._get_baileys_api().send_presence_update(number, 'composing')
        except Exception:  # pragma: no cover - presence is cosmetic
            _logger.debug("chatbot typing indicator failed for %s", number)

    def _send_bot_message(self, to_number, text):
        """Send a chatbot message.

        Posts it through the WhatsApp Discuss channel (``message_type=
        'whatsapp_message'``) so it is BOTH visible in the conversation thread
        (agents who take over see the whole bot exchange) AND queued+sent to
        WhatsApp with the normal audit trail. Falls back to a bare outbound
        ``owa.message`` only when no channel exists yet."""
        self.ensure_one()
        if not text:
            return
        if not self.wa_account_id or self.wa_account_id.session_state != 'connected':
            return
        self._send_typing(to_number)
        channel = self._find_channel(to_number)
        if channel:
            channel.with_context(mail_create_nosubscribe=True).message_post(
                body=plaintext2html(text),
                message_type='whatsapp_message',
                author_id=self._bot_author().id,
                subtype_xmlid='mail.mt_comment',
            )
            return
        # No channel yet — queue a raw outbound message (legacy path).
        formatted_phone = wa_phone_format(self.env, to_number) or to_number
        mail_message = self.env['mail.message'].create({
            'body': plaintext2html(text),
            'message_type': 'whatsapp_message',
        })
        self.env['owa.message'].create({
            'mobile_number': formatted_phone,
            'message_type': 'outbound',
            'state': 'outgoing',
            'wa_account_id': self.wa_account_id.id,
            'mail_message_id': mail_message.id,
        })
        cron = self.env.ref(
            'open_whatsapp_connector.ir_cron_send_owa_queue',
            raise_if_not_found=False,
        )
        if cron:
            cron.sudo()._trigger()


class OwaChatbotStep(models.Model):
    _name = 'owa.chatbot.step'
    _description = 'Chatbot Step'
    _order = 'sequence, id'

    chatbot_id = fields.Many2one('owa.chatbot', string="Chatbot",
        required=True, ondelete='cascade')
    name = fields.Char(string="Step Name", required=True)
    sequence = fields.Integer(default=10)

    # Canvas position for the visual flow designer.
    pos_x = fields.Float(string="X", default=0.0)
    pos_y = fields.Float(string="Y", default=0.0)

    step_type = fields.Selection([
        ('message', 'Send Message'),
        ('menu', 'Show Menu'),
        ('question', 'Ask a Question'),
        ('handoff', 'Hand off to Human'),
        ('route_to_user', 'Route to User'),  # legacy alias of handoff
        ('create_lead', 'Create CRM Lead'),
        ('end', 'End Conversation'),
    ], string="Step Type", required=True, default='menu')

    # Message content / prompt
    message = fields.Text(string="Message",
        help="Message to send at this step")

    # Menu options
    option_ids = fields.One2many('owa.chatbot.menu.option', 'step_id',
        string="Menu Options")

    # Question capture
    answer_var = fields.Char(string="Store answer as",
        help="A short name for the captured answer (e.g. 'email', 'order_no'). "
             "Available later in the conversation and on the created lead.")
    answer_save_to = fields.Selection([
        ('none', 'Just remember it'),
        ('name', "Contact's name"),
        ('email', "Contact's email"),
        ('note', 'Post as internal note'),
        ('lead', 'Keep for the CRM lead'),
    ], string="Save answer to", default='none')
    answer_validation = fields.Selection([
        ('none', 'No validation'),
        ('email', 'Must be an email'),
        ('phone', 'Must be a phone number'),
        ('number', 'Must be a number'),
    ], string="Validate answer", default='none',
        help="For 'Ask a Question' steps: if the reply doesn't match, the bot "
             "asks again instead of storing it. Email is auto-validated when "
             "the answer is saved to the contact's email.")

    # Handoff / routing
    target_user_id = fields.Many2one('res.users', string="Route to User",
        domain=[('share', '=', False)],
        help="Legacy 'Route to User' target. Prefer 'Hand off to Human'.")
    handoff_user_id = fields.Many2one('res.users', string="Assign to",
        domain=[('share', '=', False)])
    handoff_team_id = fields.Many2one('crm.team', string="Assign to Team")

    next_step_id = fields.Many2one('owa.chatbot.step', string="Next Step",
        ondelete='set null',
        help="For 'message'/'question'/'create lead': auto-advance to this step")

    _MAX_AUTO_ADVANCE = 20

    def _present_step(self, from_number, session=None):
        """Present this step (and any auto-advancing successors) to the user.

        ``message`` and ``create_lead`` auto-advance via ``next_step_id``.
        ``question`` sends its prompt and waits for the next reply. ``menu``
        sends its options and waits. ``handoff``/``route_to_user`` route to a
        human and pause the bot. We walk the auto-advance chain iteratively
        with a depth cap and a visited-set so a user-configured cycle can't
        recurse forever."""
        self.ensure_one()
        formatted_phone = wa_phone_format(self.env, from_number) or from_number
        visited = set()
        step = self
        for _i in range(self._MAX_AUTO_ADVANCE):
            if step.id in visited:
                _logger.warning(
                    "Chatbot '%s' has a cycle starting at step %s; aborting auto-advance",
                    step.chatbot_id.name, step.id,
                )
                return step
            visited.add(step.id)

            if step.step_type == 'message':
                if step.message:
                    step.chatbot_id._send_bot_message(from_number, step.message)
                if step.next_step_id:
                    step = step.next_step_id
                    continue
                return step

            if step.step_type == 'question':
                if step.message:
                    step.chatbot_id._send_bot_message(from_number, step.message)
                # Arm the session to capture the NEXT reply into answer_var.
                if session:
                    session.awaiting_answer_var = step.answer_var or step.name or 'answer'
                return step

            if step.step_type == 'menu':
                text = step.message or ''
                if step.option_ids:
                    options_text = '\n'.join(
                        f'{i}. {opt.label}'
                        for i, opt in enumerate(step.option_ids.sorted('sequence'), 1)
                    )
                    text = f"{text}\n\n{options_text}" if text else options_text
                if text:
                    step.chatbot_id._send_bot_message(from_number, text)
                return step

            if step.step_type in ('handoff', 'route_to_user'):
                if step.message:
                    step.chatbot_id._send_bot_message(from_number, step.message)
                agent = step.handoff_user_id or step.target_user_id
                step._handoff(formatted_phone, agent=agent, team=step.handoff_team_id)
                if session:
                    session.state = 'routed'
                return step

            if step.step_type == 'create_lead':
                step._create_crm_lead(from_number, session=session)
                if step.message:
                    step.chatbot_id._send_bot_message(from_number, step.message)
                if step.next_step_id:
                    step = step.next_step_id
                    continue
                return step

            if step.step_type == 'end':
                if step.message:
                    step.chatbot_id._send_bot_message(from_number, step.message)
                if session:
                    session.state = 'completed'
                return step

            return step  # unknown step_type — stop

        _logger.warning(
            "Chatbot '%s' hit max auto-advance depth (%d) at step %s",
            step.chatbot_id.name, self._MAX_AUTO_ADVANCE, step.id,
        )
        return step

    def _handoff(self, formatted_phone, agent=None, team=None):
        """Hand the conversation to a human: pause the bot, assign, subscribe
        the operator(s) so the chat surfaces in their Discuss, and notify.

        Falls back to the account owner when nothing specific is configured."""
        self.ensure_one()
        account = self.chatbot_id.wa_account_id
        if not agent and not team:
            agent = account.user_id
        channel = self.env['discuss.channel'].sudo().search([
            ('channel_type', '=', 'whatsapp'),
            ('whatsapp_number', '=', (formatted_phone or '').lstrip('+')),
            ('owa_account_id', '=', account.id),
        ], limit=1)
        if not channel:
            return
        # Pause automated replies — a human is taking over.
        channel.owa_bot_paused = True

        # Figure out who is being subscribed so the conversation appears in
        # their Discuss side bar, and who to assign.
        members = self.env['res.users']
        if agent and agent.active:
            members |= agent
        if team:
            members |= team.member_ids.filtered('active')
            if team.user_id.active:
                members |= team.user_id
        partner_ids = members.partner_id.ids
        if partner_ids:
            try:
                channel.add_members(partner_ids=partner_ids)
            except Exception:
                _logger.exception("chatbot handoff: add_members failed on channel %s",
                                  channel.id)

        vals = {}
        assignee = agent or (team.user_id if team else False)
        if assignee and 'assignee_id' in channel._fields and not channel.assignee_id:
            vals['assignee_id'] = assignee.id
        if team and 'crm_team_id_int' in channel._fields:
            vals['crm_team_id_int'] = team.id
        if 'triage_state' in channel._fields and channel.triage_state != 'resolved':
            vals['triage_state'] = 'active'
        if 'assigned_at' in channel._fields and not channel.assigned_at:
            vals['assigned_at'] = fields.Datetime.now()
        if vals:
            channel.write(vals)

        if team and agent:
            body = _("Chatbot handed this conversation to %s (team: %s).") % (
                agent.display_name, team.name)
        elif team:
            body = _("Chatbot handed this conversation to the %s team.") % team.name
        elif agent:
            body = _("Chatbot handed this conversation to %s.") % agent.display_name
        else:
            body = _("Chatbot handed this conversation to a human.")
        channel.message_post(body=body, message_type='comment',
                             subtype_xmlid='mail.mt_note')
        # discuss.channel does not inherit mail.activity.mixin on all versions;
        # only schedule a to-do where the model actually supports it.
        if assignee and hasattr(channel, 'activity_schedule'):
            try:
                channel.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=assignee.id,
                    summary=_("WhatsApp conversation needs your attention"))
            except Exception:
                _logger.exception(
                    "chatbot handoff: activity_schedule failed for channel %s", channel.id)

    # Back-compat: older data/tests call _route_to_agent(phone, user).
    def _route_to_agent(self, formatted_phone, agent):
        self.ensure_one()
        self._handoff(formatted_phone, agent=agent)

    def _create_crm_lead(self, from_number, session=None):
        """Create a CRM lead from the chatbot conversation, enriched with any
        answers the bot captured along the way."""
        self.ensure_one()
        if 'crm.lead' not in self.env:
            _logger.warning("CRM module not installed, cannot create lead")
            return
        formatted = wa_phone_format(self.env, from_number) or from_number
        partner = self.env['res.partner'].search([
            '|', ('phone', '=', formatted), ('phone', '=', from_number),
        ], limit=1)
        answers = dict(session.answers or {}) if session else {}
        description = _('Lead created from WhatsApp chatbot "%s".') % self.chatbot_id.name
        if answers:
            description += "\n\n" + "\n".join(
                "%s: %s" % (k, v) for k, v in answers.items())
        vals = {
            'name': answers.get('name')
                    or (_('WhatsApp Lead - %s') % (partner.name if partner else formatted)),
            'partner_id': partner.id if partner else False,
            'phone': formatted,
            'description': description,
        }
        if answers.get('email'):
            vals['email_from'] = answers['email']
        self.env['crm.lead'].create(vals)

    _EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

    def _effective_validation(self):
        """Validation to apply to a 'question' answer: the explicit
        answer_validation, else 'email' when the answer is saved to the
        contact's email (so existing email-capture steps validate too)."""
        self.ensure_one()
        if self.answer_validation and self.answer_validation != 'none':
            return self.answer_validation
        if self.answer_save_to == 'email':
            return 'email'
        return 'none'

    def _validate_answer(self, value):
        """Return (ok, error_message) for a captured question answer."""
        self.ensure_one()
        v = (value or '').strip()
        mode = self._effective_validation()
        if mode == 'none' or not v:
            return True, ''
        if mode == 'email':
            if self._EMAIL_RE.match(v):
                return True, ''
            return False, _("That doesn't look like a valid email — please "
                            "send it again, e.g. name@example.com")
        if mode == 'phone':
            if len(re.sub(r'\D', '', v)) >= 7:
                return True, ''
            return False, _("That doesn't look like a valid phone number — "
                            "please send it again.")
        if mode == 'number':
            if re.match(r'^-?\d+(\.\d+)?$', v.replace(',', '').replace(' ', '')):
                return True, ''
            return False, _("Please send a number.")
        return True, ''

    def _process_user_input(self, from_number, message_text, session=None):
        """Process user input for this step and return the next step (or None
        to keep waiting on the current step)."""
        self.ensure_one()
        if self.step_type == 'menu' and self.option_ids:
            options = self.option_ids.sorted('sequence')
            # Match an interactive tap echo, a bare number, or the option label.
            from odoo.addons.open_whatsapp_connector.tools.menu_match import (
                match_menu_choice)
            idx = match_menu_choice([o.label for o in options], message_text)
            if idx is None:
                # Fall back to a per-option keyword match.
                low = (message_text or '').strip().lower()
                for j, option in enumerate(options):
                    if option.keyword and low == option.keyword.lower():
                        idx = j
                        break
            if idx is not None and 0 <= idx < len(options):
                if session:
                    session.miss_count = 0
                return options[idx].next_step_id
            # No match — count the miss and either re-present or fall back.
            if session:
                session.miss_count = (session.miss_count or 0) + 1
                bot = self.chatbot_id
                if session.miss_count > (bot.fallback_max_retries or 2):
                    session.miss_count = 0
                    if bot.fallback_step_id:
                        return bot.fallback_step_id
                    # No fallback step configured — hand off directly.
                    self._handoff(
                        wa_phone_format(self.env, from_number) or from_number,
                        agent=bot.wa_account_id.user_id)
                    session.state = 'routed'
                    return None
            self.chatbot_id._send_bot_message(
                from_number,
                _("Sorry, I didn't understand that. Please choose from the options above.")
            )
            self._present_step(from_number, session=session)
            return None

        # For non-menu steps, just advance to next
        return self.next_step_id


class OwaChatbotMenuOption(models.Model):
    _name = 'owa.chatbot.menu.option'
    _description = 'Chatbot Menu Option'
    _order = 'sequence, id'

    step_id = fields.Many2one('owa.chatbot.step', string="Step",
        required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    label = fields.Char(string="Label", required=True,
        help="Text shown to user, e.g. '1. Sales' or '2. Support'")
    keyword = fields.Char(string="Keyword",
        help="Keyword that also triggers this option (e.g. 'sales')")
    next_step_id = fields.Many2one('owa.chatbot.step', string="Next Step",
        ondelete='set null',
        help="Step this option leads to. Draw it in the flow designer.")


class OwaChatbotSession(models.Model):
    _name = 'owa.chatbot.session'
    _description = 'Chatbot Session'
    _order = 'id desc'

    chatbot_id = fields.Many2one('owa.chatbot', string="Chatbot",
        required=True, ondelete='cascade')
    phone_number = fields.Char(string="Phone Number", required=True, index=True)
    current_step_id = fields.Many2one('owa.chatbot.step', string="Current Step")
    state = fields.Selection([
        ('active', 'Active'),
        ('routed', 'Routed to Agent'),
        ('completed', 'Completed'),
        ('expired', 'Expired'),
    ], string="State", default='active', required=True)
    last_activity = fields.Datetime(string="Last Activity",
        default=fields.Datetime.now)
    # Free-text captured from 'question' steps, keyed by answer_var.
    answers = fields.Json(string="Captured Answers", default=dict)
    # When set, the NEXT inbound reply is captured under this variable name
    # instead of being matched as a menu choice.
    awaiting_answer_var = fields.Char(string="Awaiting answer for")
    # Consecutive unrecognised replies at a menu (drives the fallback).
    miss_count = fields.Integer(string="Misses", default=0)

    def _capture_answer(self, var, message_text, step):
        """Store a captured answer on the session and optionally write it to
        the contact / post it as a note."""
        self.ensure_one()
        value = (message_text or '').strip()
        data = dict(self.answers or {})
        data[var] = value
        self.answers = data
        save_to = step.answer_save_to if step else 'none'
        if save_to == 'none' or not step:
            return
        channel = step.chatbot_id._find_channel(self.phone_number)
        if save_to in ('name', 'email'):
            # Use the conversation's OWN contact first: a WhatsApp partner
            # frequently has no matching res.partner.phone, so a phone lookup
            # misses it. Fall back to a phone search only if the channel has
            # no linked partner yet.
            partner = channel.whatsapp_partner_id if channel else False
            if not partner:
                formatted = self.phone_number
                partner = self.env['res.partner'].sudo().search([
                    '|', ('phone', '=', formatted),
                         ('phone', '=', (formatted or '').lstrip('+')),
                ], limit=1)
            if partner and value:
                if save_to == 'name':
                    partner.name = value
                elif save_to == 'email':
                    partner.email = value
        elif save_to == 'note':
            if channel:
                channel.message_post(
                    body=plaintext2html("%s: %s" % (var, value)),
                    message_type='comment', subtype_xmlid='mail.mt_note')

    def _process_message(self, message_text):
        """Process an incoming message in this session."""
        self.ensure_one()
        if self.state != 'active':
            return False

        # Update last activity
        self.last_activity = fields.Datetime.now()

        step = self.current_step_id
        if not step:
            return False

        # A 'question' step is waiting for this reply — validate, capture, advance.
        if self.awaiting_answer_var:
            ok, err = step._validate_answer(message_text)
            if not ok:
                # Re-ask (stay on this step, keep awaiting) — but never trap the
                # contact: after fallback_max_retries bad tries, accept anyway.
                limit = self.chatbot_id.fallback_max_retries or 2
                if (self.miss_count or 0) < limit:
                    self.miss_count = (self.miss_count or 0) + 1
                    step.chatbot_id._send_bot_message(self.phone_number, err)
                    return True
            self.miss_count = 0
            self._capture_answer(self.awaiting_answer_var, message_text, step)
            self.awaiting_answer_var = False
            next_step = step.next_step_id
        else:
            next_step = step._process_user_input(
                self.phone_number, message_text, session=self)

        if next_step:
            self.current_step_id = next_step
            # Persist the step the chain stopped on (e.g. a menu reached by
            # auto-advancing through a 'message' step), not just next_step, so
            # the following reply is matched against the right step. (#F117)
            final_step = next_step._present_step(self.phone_number, session=self)
            if final_step and final_step.id != next_step.id:
                self.current_step_id = final_step.id

        return True

    @api.model
    def _cron_expire_sessions(self):
        """Expire inactive chatbot sessions."""
        bots = self.env['owa.chatbot'].search([('active', '=', True)])
        for bot in bots:
            timeout = timedelta(minutes=bot.session_timeout_minutes or 30)
            threshold = fields.Datetime.now() - timeout
            sessions = self.search([
                ('chatbot_id', '=', bot.id),
                ('state', '=', 'active'),
                ('last_activity', '<', threshold),
            ])
            if sessions:
                sessions.write({'state': 'expired'})
                _logger.info("Expired %d chatbot sessions for '%s'",
                           len(sessions), bot.name)
