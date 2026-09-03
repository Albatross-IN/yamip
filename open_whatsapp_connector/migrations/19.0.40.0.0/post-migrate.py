# -*- coding: utf-8 -*-
"""post-migrate 19.0.40.0.0 — make the visibility active-flags upgrade-safe.

The marketing-visibility ir.rules + ACLs and the account self-service ACL ship
inactive and are toggled at runtime by res.config.settings.set_values(). They
used to live in updatable XML, so every `-u` reloaded them and silently reset
active=False — undoing a configured 'own_team' restriction. They are now shipped
under <data noupdate="1">. For EXISTING databases this migration:

  1. re-asserts each record's active flag from the saved config parameter
     (repairs any reset from this or a prior upgrade), and
  2. flips the stored ir.model.data.noupdate flag to True so future upgrades
     stop reloading (and resetting) them.

Idempotent. (#F085)
"""
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return  # fresh install — XML already ships these correctly
    env = api.Environment(cr, SUPERUSER_ID, {})
    Settings = env['res.config.settings']
    ICP = env['ir.config_parameter'].sudo()

    marketing_own = ICP.get_param(
        'open_whatsapp_connector.marketing_visibility', 'shared') == 'own_team'
    account_own = ICP.get_param(
        'open_whatsapp_connector.account_visibility', 'shared') == 'own_team'

    marketing_xmlids = (list(Settings._OWA_VISIBILITY_RULE_XMLIDS)
                        + list(Settings._OWA_VISIBILITY_ACL_XMLIDS))
    account_xmlids = ['open_whatsapp_connector.access_owa_account_user_manage']

    # 1) Re-assert the active flags from the saved config.
    for xmlid in marketing_xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec and rec.active != marketing_own:
            rec.sudo().active = marketing_own
    for xmlid in account_xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec and rec.active != account_own:
            rec.sudo().active = account_own

    # 2) Set noupdate=True on the stored ir.model.data rows so subsequent `-u`
    #    no longer reloads/resets them (the XML noupdate attr only governs newly
    #    created rows; pre-existing rows keep noupdate=False until flipped here).
    names = [x.split('.', 1)[1] for x in (marketing_xmlids + account_xmlids)]
    imd = env['ir.model.data'].sudo().search([
        ('module', '=', 'open_whatsapp_connector'),
        ('name', 'in', names),
    ])
    imd.filtered(lambda d: not d.noupdate).write({'noupdate': True})

    env.registry.clear_cache()
    _logger.info(
        "[owc 19.0.40.0.0] visibility active re-asserted "
        "(marketing_own=%s account_own=%s); noupdate set on %d rows",
        marketing_own, account_own, len(imd))
