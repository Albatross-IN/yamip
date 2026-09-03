"""Tests for the rebuilt chatbot: question capture, human handoff + pause,
fallback, global keywords, and Discuss visibility."""
from odoo.tests import tagged

from .common import OwaTestCase


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestChatbotFlow(OwaTestCase):

    def setUp(self):
        super().setUp()
        self.number = '+919999990030'
        self.raw = '919999990030'
        # Deactivate any pre-seeded active bot on this account.
        self.env['owa.chatbot'].search([
            ('wa_account_id', '=', self.account.id), ('active', '=', True),
        ]).write({'active': False})
        # A contact + a WhatsApp Discuss channel so bot messages have somewhere
        # to post (visible in Discuss) and handoff has a channel to pause.
        self.partner = self.env['res.partner'].create({
            'name': 'Test Contact', 'phone': self.number,
        })
        self.channel = self.env['discuss.channel'].create({
            'name': self.number,
            'channel_type': 'whatsapp',
            'whatsapp_number': self.raw,
            'whatsapp_partner_id': self.partner.id,
            'owa_account_id': self.account.id,
        })

    def _bot(self, **vals):
        return self.env['owa.chatbot'].create(dict({
            'name': 'flow bot',
            'wa_account_id': self.account.id,
            'active': True,
            'typing_indicator': False,  # keep tests off the presence path
        }, **vals))

    def _step(self, bot, **vals):
        return self.env['owa.chatbot.step'].create(dict({
            'chatbot_id': bot.id, 'name': 'step',
        }, **vals))

    # ── Question capture ────────────────────────────────────────────────
    def test_question_captures_and_writes_partner_email(self):
        bot = self._bot()
        q = self._step(bot, name='ask email', step_type='question',
                       message='Your email?', answer_var='email',
                       answer_save_to='email')
        end = self._step(bot, name='bye', step_type='end', message='thanks')
        q.next_step_id = end.id
        bot.first_step_id = q.id

        with self.mock_sidecar():
            session = bot._start_session(self.number)
            # The question armed the session to capture the next reply.
            self.assertEqual(session.awaiting_answer_var, 'email')
            session._process_message('john@example.com')

        self.assertEqual(session.answers.get('email'), 'john@example.com')
        self.assertEqual(self.partner.email, 'john@example.com')
        self.assertEqual(session.state, 'completed')

    # ── Handoff pauses the bot + assigns ────────────────────────────────
    def test_handoff_pauses_channel_and_assigns(self):
        agent = self.env.ref('base.user_admin')
        bot = self._bot()
        h = self._step(bot, name='to human', step_type='handoff',
                       message='connecting you', handoff_user_id=agent.id)
        bot.first_step_id = h.id

        with self.mock_sidecar():
            bot._start_session(self.number)

        self.assertTrue(self.channel.owa_bot_paused,
                        "Handoff must pause the bot on the channel")
        self.assertEqual(self.channel.assignee_id, agent)
        # A paused channel must silence any further bot processing.
        with self.mock_sidecar():
            handled = self.account._check_chatbot(self.number, 'hello again',
                                                  is_new_channel=False)
        self.assertFalse(handled, "Paused channel must not be handled by the bot")

    # ── Fallback after too many misses ──────────────────────────────────
    def test_menu_fallback_after_retries(self):
        bot = self._bot(fallback_max_retries=1)
        menu = self._step(bot, name='menu', step_type='menu', message='pick')
        end = self._step(bot, name='end', step_type='end')
        fb = self._step(bot, name='fallback', step_type='handoff',
                        message='let me get someone')
        self.env['owa.chatbot.menu.option'].create({
            'step_id': menu.id, 'label': 'Sales', 'keyword': 'sales',
            'next_step_id': end.id,
        })
        bot.first_step_id = menu.id
        bot.fallback_step_id = fb.id

        with self.mock_sidecar():
            session = bot._start_session(self.number)
            session._process_message('gibberish one')   # miss 1 → re-ask
            self.assertFalse(self.channel.owa_bot_paused)
            session._process_message('gibberish two')   # miss 2 > 1 → fallback

        self.assertTrue(self.channel.owa_bot_paused,
                        "Exceeding retries must run the fallback handoff")

    # ── Global keywords ─────────────────────────────────────────────────
    def test_agent_keyword_hands_off_from_existing_chat(self):
        self._bot(keyword_agent='agent, human')
        with self.mock_sidecar():
            handled = self.account._check_chatbot(self.number, 'human',
                                                  is_new_channel=False)
        self.assertTrue(handled)
        self.assertTrue(self.channel.owa_bot_paused)

    def test_restart_keyword_starts_session_on_existing_chat(self):
        bot = self._bot(keyword_restart='menu, start')
        menu = self._step(bot, name='menu', step_type='menu', message='hi')
        bot.first_step_id = menu.id
        with self.mock_sidecar():
            handled = self.account._check_chatbot(self.number, 'menu',
                                                  is_new_channel=False)
        self.assertTrue(handled)
        session = self.env['owa.chatbot.session'].search([
            ('chatbot_id', '=', bot.id), ('state', '=', 'active')])
        self.assertTrue(session, "Restart keyword must open a bot session")

    # ── Discuss visibility ──────────────────────────────────────────────
    def test_bot_message_is_posted_to_discuss_channel(self):
        bot = self._bot()
        before = len(self.channel.message_ids)
        with self.mock_sidecar():
            bot._send_bot_message(self.number, 'Hello from the bot')
        self.channel.invalidate_recordset(['message_ids'])
        msgs = self.channel.message_ids.filtered(
            lambda m: m.message_type == 'whatsapp_message')
        self.assertTrue(msgs, "Bot message must be visible in the Discuss thread")
        self.assertGreater(len(self.channel.message_ids), before)

    # ── Flow health ─────────────────────────────────────────────────────
    def test_flow_problems_flags_dead_end_option(self):
        bot = self._bot()
        menu = self._step(bot, name='menu', step_type='menu')
        bot.first_step_id = menu.id
        self.env['owa.chatbot.menu.option'].create({
            'step_id': menu.id, 'label': 'Nowhere',  # no next_step_id
        })
        problems = bot._flow_problems()
        self.assertTrue(any('points nowhere' in p for p in problems),
                        "A menu option with no target must be reported")
