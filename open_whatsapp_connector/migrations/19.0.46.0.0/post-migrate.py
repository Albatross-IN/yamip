# -*- coding: utf-8 -*-
"""post-migrate for open_whatsapp_connector 19.0.46.0.0.

The post_init_hook (_create_default_notification_rules) only runs on a FRESH
install, never on upgrade. This version broadens that helper with ready-to-use
notification rules for more of the most-used apps — Sale Order Cancelled,
Request for Quotation Sent (Purchase) and Event Registration Confirmed (Events).
Re-run the (idempotent) helper here so upgrading databases pick up the new
rules: it skips any rule that already exists and any model that is not
installed. The paired quick-reply templates load automatically from the data
files on upgrade (new records are created even under noupdate="1").

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
        _logger.info(
            "[owc 19.0.46.0.0] refreshed default notification rules "
            "(added Sale Order Cancelled / RFQ Sent / Event Registration Confirmed)")
    except Exception:
        _logger.exception(
            "[owc 19.0.46.0.0] failed to refresh default notification rules")
