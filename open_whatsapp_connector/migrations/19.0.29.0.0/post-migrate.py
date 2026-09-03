# -*- coding: utf-8 -*-
"""post-migrate for open_whatsapp_connector 19.0.29.0.0.

Removes a stale top-level menuitem (``menu_owa_group_participant_wizard``) that
older v19 builds shipped and that errored on click. The manage-participants
wizard is now reached only via the "Manage Participants" smart button on the
Group form.

This replaces a hard ``<delete>`` that used to live in
``views/owa_baileys_views.xml`` — a ``<delete>`` raises "External ID not found"
(a full traceback in the log) on every fresh install and on any database that
never had that menu. Doing the cleanup here with ``raise_if_not_found=False``
is tolerant and idempotent: it unlinks the record only when it actually exists.

Safe to re-run.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    menu = env.ref(
        'open_whatsapp_connector.menu_owa_group_participant_wizard',
        raise_if_not_found=False)
    if menu:
        menu.unlink()
        _logger.info(
            "[owc 19.0.29.0.0] removed stale menu_owa_group_participant_wizard")
