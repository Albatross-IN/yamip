"""Ready-to-use chatbot templates, seeded inactive (like the notification-rule
library). A non-technical admin can pick one, assign a WhatsApp account and tick
Active — or duplicate it (Save as New Bot) and edit the copy in the flow
designer, or start from scratch.

Templates are built programmatically (not XML) so the step graph is rewired in
code — no fragile forward ``ref=`` ordering. Seeding is idempotent: a template
whose name already exists is left untouched (user edits are never clobbered).
"""

# Each template: name, welcome, first (step key), optional fallback (step key),
# and steps. A step is a dict with key/type/name/message + type-specific extras:
#   question: var (answer_var), save (answer_save_to), next
#   message / create_lead: next
#   menu: options=[{label, keyword, next}]
#   handoff / end: (terminal)
# x/y are the flow-designer canvas coordinates.
TEMPLATES = [
    {
        'name': "Lead Capture Bot",
        'welcome': "Hi! 👋 Thanks for reaching out. Let me grab a couple of "
                   "details so the right person can help you.",
        'first': 'name',
        'steps': [
            {'key': 'name', 'type': 'question', 'name': "Ask name", 'seq': 10,
             'x': 60, 'y': 60, 'message': "First, what's your name?",
             'var': 'name', 'save': 'name', 'next': 'interest'},
            {'key': 'interest', 'type': 'question', 'name': "Ask interest", 'seq': 20,
             'x': 340, 'y': 60, 'var': 'interest', 'save': 'note', 'next': 'email',
             'message': "Thanks! What are you interested in — a product, "
                        "pricing, or something else?"},
            {'key': 'email', 'type': 'question', 'name': "Ask email", 'seq': 30,
             'x': 620, 'y': 60, 'var': 'email', 'save': 'email', 'validate': 'email',
             'next': 'lead',
             'message': "Could I get your email so we can follow up?"},
            {'key': 'lead', 'type': 'create_lead', 'name': "Create lead", 'seq': 40,
             'x': 900, 'y': 60, 'next': 'handoff',
             'message': "Perfect — I've logged your enquiry."},
            {'key': 'handoff', 'type': 'handoff', 'name': "Hand off", 'seq': 50,
             'x': 1180, 'y': 60,
             'message': "Connecting you with our team now — they'll be in "
                        "touch shortly."},
        ],
    },
    {
        'name': "Support Triage Bot",
        'welcome': "Hi! You've reached support. How can we help today?",
        'first': 'menu',
        'fallback': 'handoff',
        'steps': [
            {'key': 'menu', 'type': 'menu', 'name': "Support menu", 'seq': 10,
             'x': 60, 'y': 140, 'message': "Please choose an option:",
             'options': [
                 {'label': "Billing", 'keyword': '1', 'next': 'billing'},
                 {'label': "Technical issue", 'keyword': '2', 'next': 'tech'},
                 {'label': "Something else", 'keyword': '3', 'next': 'other'},
             ]},
            {'key': 'billing', 'type': 'question', 'name': "Billing details", 'seq': 20,
             'x': 360, 'y': 20, 'var': 'details', 'save': 'note', 'next': 'handoff',
             'message': "Please describe your billing question and I'll pass "
                        "it to the right team."},
            {'key': 'tech', 'type': 'question', 'name': "Technical details", 'seq': 30,
             'x': 360, 'y': 150, 'var': 'details', 'save': 'note', 'next': 'handoff',
             'message': "Please describe the issue — what happened, and any "
                        "error message you saw."},
            {'key': 'other', 'type': 'question', 'name': "Other details", 'seq': 40,
             'x': 360, 'y': 280, 'var': 'details', 'save': 'note', 'next': 'handoff',
             'message': "Sure — please describe what you need help with."},
            {'key': 'handoff', 'type': 'handoff', 'name': "Hand off", 'seq': 50,
             'x': 700, 'y': 150,
             'message': "Thank you — a support agent will continue with you "
                        "shortly."},
        ],
    },
    {
        'name': "FAQ Bot",
        'welcome': "Hi! Here are some quick answers. You can type \"agent\" any "
                   "time to reach a person.",
        'first': 'menu',
        'steps': [
            {'key': 'menu', 'type': 'menu', 'name': "FAQ menu", 'seq': 10,
             'x': 60, 'y': 140, 'message': "What would you like to know?",
             'options': [
                 {'label': "Opening hours", 'keyword': '1', 'next': 'hours'},
                 {'label': "Where you're located", 'keyword': '2', 'next': 'location'},
                 {'label': "Pricing", 'keyword': '3', 'next': 'pricing'},
                 {'label': "Talk to an agent", 'keyword': '4', 'next': 'agent'},
             ]},
            {'key': 'hours', 'type': 'message', 'name': "Hours", 'seq': 20,
             'x': 360, 'y': 20, 'next': 'menu',
             'message': "We're open Monday–Friday, 9 AM–6 PM."},
            {'key': 'location', 'type': 'message', 'name': "Location", 'seq': 30,
             'x': 360, 'y': 120, 'next': 'menu',
             'message': "You'll find us at your address here — edit this step "
                        "to add it."},
            {'key': 'pricing', 'type': 'message', 'name': "Pricing", 'seq': 40,
             'x': 360, 'y': 220, 'next': 'menu',
             'message': "Our pricing depends on what you need — a team member "
                        "can share the details."},
            {'key': 'agent', 'type': 'handoff', 'name': "Talk to agent", 'seq': 50,
             'x': 360, 'y': 320,
             'message': "Sure — connecting you with someone now."},
        ],
    },
]


def seed_chatbot_templates(env):
    """Idempotently create the default chatbot templates (inactive). Skips a
    template whose name already exists so user edits are preserved."""
    Bot = env['owa.chatbot']
    Step = env['owa.chatbot.step']
    Option = env['owa.chatbot.menu.option']
    for tmpl in TEMPLATES:
        if Bot.with_context(active_test=False).search_count(
                [('name', '=', tmpl['name'])]):
            continue
        bot = Bot.create({
            'name': tmpl['name'],
            'active': False,
            'welcome_message': tmpl.get('welcome', ''),
        })
        keymap = {}
        for s in tmpl['steps']:
            keymap[s['key']] = Step.create({
                'chatbot_id': bot.id,
                'name': s['name'],
                'step_type': s['type'],
                'sequence': s.get('seq', 10),
                'pos_x': s.get('x', 0),
                'pos_y': s.get('y', 0),
                'message': s.get('message') or False,
                'answer_var': s.get('var') or False,
                'answer_save_to': s.get('save', 'none'),
                'answer_validation': s.get('validate', 'none'),
            })
        for s in tmpl['steps']:
            step = keymap[s['key']]
            if s.get('next'):
                step.next_step_id = keymap[s['next']].id
            for o in s.get('options', []):
                Option.create({
                    'step_id': step.id,
                    'label': o['label'],
                    'keyword': o.get('keyword') or False,
                    'sequence': o.get('seq', 10),
                    'next_step_id': keymap[o['next']].id if o.get('next') else False,
                })
        if tmpl.get('first') and tmpl['first'] in keymap:
            bot.first_step_id = keymap[tmpl['first']].id
        if tmpl.get('fallback') and tmpl['fallback'] in keymap:
            bot.fallback_step_id = keymap[tmpl['fallback']].id
