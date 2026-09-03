"""Post-migration hook for 19.0.26.1.0.

This release makes the two default notification rules attach the relevant
PDF out of the box:
  - "Sale Order Confirmed" -> Quotation / Order PDF (sale.action_report_saleorder)
  - "Invoice Posted"       -> Invoice PDF          (account.account_invoices)

The post_init_hook only runs on a *fresh* install, so existing databases
won't pick up the new defaults automatically. This migration enables the
PDF attachment on those two rules IF they still look like the shipped
defaults (i.e. the admin hasn't already turned attach_pdf on / off
themselves — we only touch rows where attach_pdf is currently False and
report_id is empty, so we never clobber a manual choice).
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# (rule name, model_name, report xml id)
_DEFAULTS = [
    ('Sale Order Confirmed', 'sale.order', 'sale.action_report_saleorder'),
    ('Invoice Posted', 'account.move', 'account.account_invoices'),
]


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Rule = env['owa.notification.rule']
    for rule_name, model_name, report_xmlid in _DEFAULTS:
        report = env.ref(report_xmlid, raise_if_not_found=False)
        if not report:
            continue
        rules = Rule.search([
            ('name', '=', rule_name),
            ('model_name', '=', model_name),
            ('attach_pdf', '=', False),
            ('report_id', '=', False),
        ])
        if rules:
            rules.write({'attach_pdf': True, 'report_id': report.id})
            _logger.info(
                "19.0.26.1.0: enabled PDF attachment on %d '%s' rule(s) -> %s",
                len(rules), rule_name, report_xmlid,
            )
