# -*- coding: utf-8 -*-
"""post-migrate for open_whatsapp_connector 19.0.80.0.0.

The post_init_hook (_create_default_notification_rules) only runs on a FRESH
install. This version adds the ready-to-use CHATBOT TEMPLATE library (Lead
Capture, Support Triage, FAQ — all inactive, alongside the existing Main Menu
Bot). Seeding lives inside the hook, so re-run it here for upgrading databases.
It is idempotent: a template whose name already exists is left untouched, so
user edits are never clobbered.

Safe to re-run.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return  # fresh install — post_init_hook already handled it
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        from odoo.addons.open_whatsapp_connector import _create_default_notification_rules
        _create_default_notification_rules(env)
        bots = env['owa.chatbot'].with_context(active_test=False).search_count([])
        _logger.info(
            "[owc 19.0.80.0.0] seeded chatbot templates — %s chatbots now present",
            bots)
    except Exception:
        _logger.exception(
            "[owc 19.0.80.0.0] failed to seed chatbot templates")
