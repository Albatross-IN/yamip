"""Post-migration hook for 19.0.2.0.0.

This release adds 4 new tables (owa.dashboard is AbstractModel — no table;
owa.inbound.dedupe, owa.allowlist.entry, owa.pending.pair, owa.slash.command)
and 25+ new columns on owa.account / discuss.channel / owa.message /
owa.notification.rule / owa.auto.reply / owa.campaign.

Odoo's ORM creates new columns + tables automatically during the upgrade,
so this script is mostly a defensive sanity check + log message. Run only
once when crossing the version boundary.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "open_whatsapp_connector 19.0.2.0.0 post-migration: verifying new "
        "tables exist after Phases 0-11 schema changes."
    )
    expected_tables = (
        'owa_inbound_dedupe',     # Phase 2
        'owa_allowlist_entry',    # Phase 5
        'owa_pending_pair',       # Phase 7
        'owa_slash_command',      # Phase 9
    )
    cr.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name IN %s
    """, (expected_tables,))
    found = {r[0] for r in cr.fetchall()}
    missing = set(expected_tables) - found
    if missing:
        _logger.warning(
            "Expected tables still missing after upgrade: %s. "
            "Try restarting Odoo and running -u open_whatsapp_connector again.",
            sorted(missing),
        )
    else:
        _logger.info("All Phase 0-11 schema additions present.")
