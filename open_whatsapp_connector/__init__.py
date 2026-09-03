import os

from . import controllers
from . import models
from . import tools
from . import wizard


def _auto_configure_sidecar_path(env):
    """Pre-fill the sidecar path config parameter with this addon's bundled
    ``sidecar/`` directory on install, so QR accounts work without an admin
    hand-typing an absolute path. Never overwrites a value already set."""
    ICP = env['ir.config_parameter'].sudo()
    if ICP.get_param('open_whatsapp_connector.sidecar_path'):
        return
    sidecar_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'sidecar')
    if os.path.isdir(sidecar_dir):
        ICP.set_param('open_whatsapp_connector.sidecar_path', sidecar_dir)


def _create_default_notification_rules(env):
    """Create default notification rules for common business flows.

    Called as a post_init_hook. Rules are created inactive since they
    need a WhatsApp account to be selected before they can work.
    Only creates rules for models that are actually installed.
    """
    _auto_configure_sidecar_path(env)
    Rule = env['owa.notification.rule']
    QuickReply = env['owa.quick.reply']
    IrModel = env['ir.model']
    IrModelFields = env['ir.model.fields']

    rules_config = [
        {
            'name': 'Sale Order Confirmed',
            'model': 'sale.order',
            'field': 'state',
            'value': 'sale',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_sale_order_confirmed',
            'phone_field': 'partner_id.phone',
            # Attach the Quotation / Order PDF so the customer gets a copy of
            # their confirmed order on WhatsApp. (At SO-confirm time there is
            # no invoice yet — that PDF rides on the "Invoice Posted" rule.)
            'report_xmlid': 'sale.action_report_saleorder',
        },
        {
            'name': 'Invoice Posted',
            'model': 'account.move',
            'field': 'state',
            'value': 'posted',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_invoice_posted',
            'phone_field': 'partner_id.phone',
            # Attach the Invoice PDF the moment the invoice is validated.
            'report_xmlid': 'account.account_invoices',
        },
        {
            'name': 'Delivery Completed',
            'model': 'stock.picking',
            'field': 'state',
            'value': 'done',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_delivery_done',
            'phone_field': 'partner_id.phone',
        },
        {
            'name': 'Payment Received',
            'model': 'account.payment',
            'field': 'state',
            # Odoo 18/19 account.payment.state = draft/in_process/paid/canceled/
            # rejected — there is NO 'posted' state (that's account.move).
            # action_post() moves the payment to 'in_process' (bank/card/SEPA)
            # — that's the moment to notify "payment received". 'paid' only
            # happens later on reconciliation / for cash journals. (#notif)
            'value': 'in_process',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_payment_received',
            'phone_field': 'partner_id.phone',
        },
        # --- Additional ready-to-use rules (state-based: fire reliably) ---
        {
            'name': 'Quotation Sent',
            'model': 'sale.order',
            'field': 'state',
            'value': 'sent',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_quotation_sent',
            'phone_field': 'partner_id.phone',
            'report_xmlid': 'sale.action_report_saleorder',
        },
        {
            'name': 'Purchase Order Confirmed',
            'model': 'purchase.order',
            'field': 'state',
            'value': 'purchase',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_po_confirmed',
            'phone_field': 'partner_id.phone',
            'report_xmlid': 'purchase.report_purchase_order',
        },
        {
            'name': 'Repair Completed',
            'model': 'repair.order',
            'field': 'state',
            'value': 'done',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_repair_done',
            'phone_field': 'partner_id.phone',
        },
        # --- More most-used apps (state-based: fire reliably, so they also
        # join the one-click "Enable recommended automations" button) ---
        {
            'name': 'Sale Order Cancelled',
            'model': 'sale.order',
            'field': 'state',
            'value': 'cancel',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_sale_order_cancelled',
            'phone_field': 'partner_id.phone',
        },
        {
            'name': 'Request for Quotation Sent',
            'model': 'purchase.order',
            'field': 'state',
            'value': 'sent',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_rfq_sent',
            'phone_field': 'partner_id.phone',
            'report_xmlid': 'purchase.report_purchase_quotation',
        },
        {
            'name': 'Event Registration Confirmed',
            'model': 'event.registration',
            'field': 'state',
            'value': 'open',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_event_registered',
            'phone_field': 'phone',
        },
        # --- Stage-based rules: trigger on stage_id, whose NAME varies per DB.
        # Shipped with the caveat baked into the rule name so a non-technical
        # admin checks the trigger stage before activating. The bulk "Enable
        # recommended automations" button deliberately SKIPS these (it only
        # touches state-based rules that always fire). ---
        {
            'name': 'CRM — Opportunity Won (check your Won stage)',
            'model': 'crm.lead',
            'field': 'stage_id',
            'value': 'Won',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_crm_won',
            'phone_field': 'phone',
        },
        {
            'name': 'Project — Task Done (check your Done stage)',
            'model': 'project.task',
            'field': 'stage_id',
            'value': 'Done',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_task_done',
            'phone_field': 'partner_id.phone',
        },
        {
            'name': 'Helpdesk — Ticket Solved (check your Solved stage)',
            'model': 'helpdesk.ticket',
            'field': 'stage_id',
            'value': 'Solved',
            'template_xmlid': 'open_whatsapp_connector.quick_reply_ticket_solved',
            'phone_field': 'partner_id.phone',
        },
    ]

    def _selection_keys(model_name, field_name):
        """Resolve a selection field's valid keys (handles method/callable
        selections), or None when it can't be determined."""
        try:
            fld = env[model_name]._fields.get(field_name)
            if not fld:
                return None
            sel = fld.get_description(env).get('selection')
            if sel is None:
                return None
            return {str(k) for k, _label in sel}
        except Exception:
            return None

    def _seed_rule(cfg):
        """Idempotently create one notification rule (inactive). Skips models
        that aren't installed, fields that don't exist, and SELECTION triggers
        whose value isn't a real key in this DB/version (so the same catalog is
        safe across v17/v18/v19 and module configs). On a rule that already
        exists it only backfills the recommended/recipient_type metadata —
        never the user's account/active/body choices."""
        model = IrModel.search([('model', '=', cfg['model'])], limit=1)
        if not model:
            return
        field = IrModelFields.search([
            ('model_id', '=', model.id), ('name', '=', cfg['field']),
        ], limit=1)
        if not field:
            return
        if cfg.get('ttype') == 'selection':
            keys = _selection_keys(cfg['model'], cfg['field'])
            if keys is not None and str(cfg['value']) not in keys:
                return
        meta = {
            'recommended': cfg.get('recommended', False),
            'recipient_type': cfg.get('recipient_type') or False,
        }
        # Dedupe on the real FK model_id (model_name can be NULL on very old
        # rows, which would let this helper create duplicates on re-run).
        existing = Rule.with_context(active_test=False).search([
            ('name', '=', cfg['name']), ('model_id', '=', model.id),
        ], limit=1)
        if existing:
            existing.write(meta)
            return
        vals = {
            'name': cfg['name'],
            'active': False,
            'model_id': model.id,
            'trigger_field_id': field.id,
            'trigger_value': cfg['value'],
            'phone_field': cfg['phone_field'],
            'wa_account_id': False,
            'notify_once': True,  # required so computed/indirect triggers fire
        }
        vals.update(meta)
        if cfg.get('template_xmlid'):
            template = env.ref(cfg['template_xmlid'], raise_if_not_found=False)
            vals['quick_reply_id'] = template.id if template else False
        if cfg.get('body'):
            vals['custom_body'] = cfg['body']
        if cfg.get('report_xmlid'):
            report = env.ref(cfg['report_xmlid'], raise_if_not_found=False)
            vals['attach_pdf'] = bool(report)
            vals['report_id'] = report.id if report else False
        Rule.create(vals)

    # 1) Curated customer-facing core. State-based ones are 'recommended' and
    #    join the one-click "Enable recommended automations" button; stage-based
    #    ones (stage_id) need a per-DB stage check, so they are not recommended.
    for cfg in rules_config:
        _seed_rule(dict(
            cfg,
            ttype=('many2one' if cfg['field'] == 'stage_id' else 'selection'),
            recommended=(cfg['field'] != 'stage_id'),
            recipient_type='customer',
        ))

    # 2) The wider ready-to-use rule library (~90 events across the whole app
    #    suite — sales, subscriptions, accounting, purchase, inventory, repair,
    #    projects, events, HR, fleet, and more), all shipped inactive. See
    #    tools/notification_rules_catalog.py.
    from odoo.addons.open_whatsapp_connector.tools.notification_rules_catalog import RULES as _LIBRARY
    for r in _LIBRARY:
        _seed_rule({
            'name': r['name'],
            'model': r['model'],
            'field': r['field'],
            'value': r['value'],
            'ttype': r.get('ttype', 'selection'),
            'phone_field': r['phone_field'],
            'body': r['body'],
            'recommended': False,
            'recipient_type': r.get('recipient'),
        })

    # Create "Send WhatsApp" server actions for common models. Each opens the
    # composer pre-selecting the model's customer-facing report, so its PDF is
    # attached on send. report xmlids resolve at wizard-open time (guarded with
    # raise_if_not_found=False), so a missing report is harmless.
    ServerAction = env['ir.actions.server']
    action_models = [
        ('sale.order', 'Send WhatsApp', 'sale.action_report_saleorder'),
        ('account.move', 'Send WhatsApp', 'account.account_invoices'),
        ('stock.picking', 'Send WhatsApp', 'stock.action_report_delivery'),
        ('purchase.order', 'Send WhatsApp', 'purchase.report_purchase_quotation'),
    ]
    for model_name, action_name, report_xmlid in action_models:
        model = IrModel.search([('model', '=', model_name)], limit=1)
        if not model:
            continue
        code = ("action = env['owa.composer']._action_open_composer("
                "records, report_xmlid='%s')" % report_xmlid)
        existing = ServerAction.search([
            ('name', '=', action_name),
            ('model_id', '=', model.id),
            ('state', '=', 'code'),
        ], limit=1)
        if existing:
            # Older installs created the action WITHOUT report_xmlid — refresh
            # the code so the PDF-attach behaviour also reaches upgraded DBs
            # (the migration re-runs this helper).
            if 'report_xmlid' not in (existing.code or ''):
                existing.write({'code': code})
            continue
        ServerAction.create({
            'name': action_name,
            'model_id': model.id,
            'binding_model_id': model.id,
            'binding_view_types': 'list,form',
            'state': 'code',
            'code': code,
        })

    # Ready-to-use chatbot templates (inactive), same spirit as the rule
    # library: pick one, assign an account and activate — or duplicate and
    # edit the copy in the flow designer. Idempotent (dedupe on name).
    from odoo.addons.open_whatsapp_connector.tools.chatbot_templates_catalog import (
        seed_chatbot_templates)
    seed_chatbot_templates(env)
