# -*- coding: utf-8 -*-
"""post-migrate for open_whatsapp_connector 19.0.47.0.0.

The post_init_hook (_create_default_notification_rules) only runs on a FRESH
install, never on upgrade. This version adds the ready-to-use notification rule
LIBRARY (~90 events across the whole Odoo app suite — sales, subscriptions,
accounting, purchase, inventory, repair, projects, events, HR, fleet and more,
all shipped inactive) and stamps the recommended/recipient_type metadata on the
curated core. Re-run the (idempotent) helper here so upgrading databases pick
up the library and the metadata: it skips models that aren't installed and
selection triggers whose value isn't valid in this DB, dedupes existing rows,
and only backfills metadata on rules that already exist.

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
        total = env['owa.notification.rule'].with_context(active_test=False).search_count([])
        _logger.info(
            "[owc 19.0.47.0.0] applied notification rule library — %s rules now present",
            total)
    except Exception:
        _logger.exception(
            "[owc 19.0.47.0.0] failed to apply notification rule library")
