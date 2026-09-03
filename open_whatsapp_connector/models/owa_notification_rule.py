import base64
import logging
import re

from odoo import api, fields, models, tools, _
from odoo.exceptions import ValidationError
from odoo.tools import plaintext2html
from odoo.addons.open_whatsapp_connector.tools.phone_validation import wa_phone_format

_logger = logging.getLogger(__name__)


class OwaNotificationRule(models.Model):
    _name = 'owa.notification.rule'
    _description = 'WhatsApp Notification Rule'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(string="Name", required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    sequence = fields.Integer(default=10)
    recommended = fields.Boolean(
        string="Recommended", default=False,
        help="Part of the curated starter set that the one-click "
             "“Enable recommended automations” button activates. The wider "
             "ready-to-use rule library ships inactive for you to switch on as needed.")
    recipient_type = fields.Selection([
        ('customer', 'Customer'),
        ('vendor', 'Vendor / Supplier'),
        ('employee', 'Employee'),
        ('internal', 'Internal team'),
    ], string="Notifies",
       help="Who this rule typically messages — used to filter and group the rule library.")

    # Target model + trigger
    model_id = fields.Many2one('ir.model', string="Model", required=True, ondelete='cascade',
        domain=[('is_mail_thread', '=', True)],
        help="Model to watch for changes (e.g. Sale Order, Invoice)")
    model_name = fields.Char(related='model_id.model', store=True, readonly=True)
    trigger_field_id = fields.Many2one('ir.model.fields', string="Trigger Field",
        required=True, ondelete='cascade',
        domain="[('model_id', '=', model_id), ('ttype', 'in', ('selection', 'char', 'boolean', 'many2one'))]",
        help="Field to watch for changes (e.g. 'state', 'invoice_status')")
    trigger_field_name = fields.Char(related='trigger_field_id.name', store=True, readonly=True)
    trigger_value = fields.Char(string="Trigger Value", required=True,
        help="Value that triggers the notification (e.g. 'sale', 'posted', 'done')")

    # Message config
    # Phase 11: optional per-rule overrides for account-level defaults.
    reply_to_mode_override = fields.Selection([
        ('off', 'Off (never quote)'),
        ('first', 'Quote first chunk only'),
        ('all', 'Quote every chunk'),
    ], string="Reply quoting (override)",
       help="If set, overrides the account's reply_to_mode for messages "
            "produced by this rule.")
    # No connected-only domain: a lagging status cron would empty the picker
    # (#23) — the send path re-checks the live connection state anyway.
    wa_account_id = fields.Many2one('owa.account', string="WhatsApp Account",
        help="Select the WhatsApp account to send notifications from. Required to activate the rule.")
    quick_reply_id = fields.Many2one('owa.quick.reply', string="Message Template",
        help="Quick reply template to use. If empty, a default message is sent.")
    custom_body = fields.Text(string="Custom Message",
        help="Used if no quick reply is selected. Supports {{record_name}}, {{partner_name}}")
    phone_field = fields.Char(string="Phone Field", default='partner_id.phone',
        required=True,
        help="Dot-separated field path to recipient phone (e.g. 'partner_id.phone', 'phone')")

    # PDF attachment
    attach_pdf = fields.Boolean(string="Attach PDF Report")
    report_id = fields.Many2one('ir.actions.report', string="Report",
        domain="[('model', '=', model_name)]",
        help="PDF report to attach (e.g. Sale Order report, Invoice report)")

    # Limits
    notify_once = fields.Boolean(string="Notify Once Per Record", default=True,
        help="Only send one notification per record for this rule")
    log_ids = fields.One2many('owa.notification.log', 'rule_id', string="Notification Log")

    @api.constrains('phone_field')
    def _check_phone_field(self):
        for rule in self:
            if not rule.phone_field:
                raise ValidationError(_("Phone field is required"))

    # ── trigger-model cache ──────────────────────────────────────────────
    # mail.thread.write() runs on EVERY write of every mail.thread model, so we
    # must cheaply skip models that have no notification rules. Cache the set of
    # model names that carry an active rule; invalidate on rule CRUD.
    @api.model
    @tools.ormcache()
    def _owa_models_with_rules(self):
        rules = self.sudo().search([('active', '=', True)])
        return tuple(sorted({m for m in rules.mapped('model_name') if m}))

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        self.env.registry.clear_cache()
        return recs

    def write(self, vals):
        res = super().write(vals)
        if {'active', 'model_id', 'model_name', 'trigger_field_id',
                'trigger_value'} & set(vals):
            self.env.registry.clear_cache()
        return res

    def unlink(self):
        self.env.registry.clear_cache()
        return super().unlink()

    def _get_phone_from_record(self, record):
        """Extract phone number from a record using the configured field path.

        ``phone_field`` may list several fallback paths separated by ``|``
        (e.g. ``partner_id.mobile|partner_id.phone``); the first path that
        resolves to a non-empty value wins.
        """
        for path in (self.phone_field or '').split('|'):
            path = path.strip()
            if not path:
                continue
            try:
                obj = record
                ok = True
                for part in path.split('.'):
                    obj = getattr(obj, part, False)
                    if not obj:
                        ok = False
                        break
                if not ok:
                    continue
                phone = str(obj)
                return wa_phone_format(self.env, phone) or phone
            except (AttributeError, TypeError):
                continue
        return False

    def _get_message_body(self, record):
        """Build message body from quick reply or custom body."""
        free_text = self._get_record_variables(record)
        if self.quick_reply_id:
            return self.quick_reply_id.render_body(record=record, free_text_values=free_text)
        body = self.custom_body or ''
        for key, value in free_text.items():
            body = body.replace('{{' + key + '}}', str(value))
        # Drop any unresolved {{placeholder}} so it never leaks into the
        # message, then tidy the whitespace/punctuation left behind (e.g. a
        # body that referenced {{amount_total}} on a model without that field).
        body = re.sub(r'\{\{\s*\w+\s*\}\}', '', body)
        body = re.sub(r' {2,}', ' ', body).replace(' .', '.').replace(' ,', ',')
        return body.strip()

    def _get_record_variables(self, record):
        """Extract common variables from a record for template rendering.

        All keys are always present (defaulting to '') so an unresolved
        variable never leaks as a literal ``{{...}}`` into the message.
        """
        values = {
            'record_name': record.display_name or '',
            'partner_name': '',
            'amount_total': '',
            'currency': '',
            'date': '',
        }
        # Recipient name — res.partner itself, else partner_id (sale.order,
        # account.move, stock.picking, ...), else employee_id for employee-facing
        # models (hr.leave, hr.expense.sheet, hr.appraisal, hr.contract, ...).
        if record._name == 'res.partner':
            values['partner_name'] = record.name or ''
        elif getattr(record, 'partner_id', False):
            values['partner_name'] = record.partner_id.name or ''
        elif getattr(record, 'employee_id', False):
            values['partner_name'] = record.employee_id.name or ''
        # Amount — sale/purchase/invoice/payment expose amount_total; expense
        # sheets use total_amount; fall back so the variable always resolves.
        for amt_field in ('amount_total', 'total_amount', 'amount'):
            if hasattr(record, amt_field) and getattr(record, amt_field) is not False:
                values['amount_total'] = str(getattr(record, amt_field) or 0)
                break
        # Currency
        if getattr(record, 'currency_id', False):
            values['currency'] = record.currency_id.symbol or record.currency_id.name or ''
        # Date (try common date fields)
        for date_field in ('date_order', 'invoice_date', 'date', 'scheduled_date'):
            if getattr(record, date_field, False):
                values['date'] = str(getattr(record, date_field))
                break
        return values

    def _vals_matches_trigger(self, vals):
        """Return True iff a write's vals dict triggers this rule."""
        self.ensure_one()
        field_name = self.trigger_field_name
        if not field_name or field_name not in vals:
            return False
        new_val = vals.get(field_name)
        if new_val is None:
            return False
        return self._value_matches_trigger(new_val)

    def _value_matches_trigger(self, value):
        """Return True iff a live field VALUE equals this rule's trigger value.

        Accepts both the raw form found in a write's vals (an int id for
        many2one, a string for selection/char) AND a live ORM read (a recordset
        for many2one). This lets the engine fire on computed / indirect trigger
        fields (e.g. stock.picking.state set by delivery validation) that never
        appear in a write's vals. Handles boolean and many2one (id or display
        name) in addition to plain selection/char.
        """
        self.ensure_one()
        target = (self.trigger_value or '').strip()
        ttype = self.trigger_field_id.ttype if self.trigger_field_id else 'char'
        if ttype == 'many2one':
            rid = value.id if hasattr(value, 'ids') else value
            if not rid:
                return False
            try:
                return int(rid) == int(target)
            except (TypeError, ValueError):
                related = self.env[self.trigger_field_id.relation].sudo().search(
                    [('display_name', '=', target)], limit=1)
                return bool(related) and int(rid) == related.id
        if ttype == 'boolean':
            return str(bool(value)).lower() == target.lower()
        if value is None or value is False:
            return False
        return str(value) == target

    def _check_already_notified(self, record):
        """Check if this rule already fired for this record."""
        if not self.notify_once:
            return False
        return self.env['owa.notification.log'].search_count([
            ('rule_id', '=', self.id),
            ('res_model', '=', record._name),
            ('res_id', '=', record.id),
        ]) > 0

    def _send_notification(self, record):
        """Send a WhatsApp notification for a record."""
        self.ensure_one()
        if not self.active:
            return
        if self._check_already_notified(record):
            return
        if not self.wa_account_id or self.wa_account_id.session_state != 'connected':
            _logger.warning("Notification rule %s: account not connected", self.name)
            return

        phone = self._get_phone_from_record(record)
        if not phone:
            _logger.warning("Notification rule %s: no phone for record %s/%s",
                          self.name, record._name, record.id)
            return

        body = self._get_message_body(record)
        if not body:
            _logger.warning("Notification rule %s: empty message body", self.name)
            return

        # PDF attachment is DEFERRED to send time. Rendering wkhtmltopdf here
        # runs synchronously inside the action that triggered this rule
        # (confirming a sale order, posting an invoice, validating a delivery)
        # and freezes the user's screen until the PDF is built. Instead we
        # stamp the report on the owa.message and let the send cron render +
        # attach it off the request thread, just before dispatch. (#async-notif)
        mail_message = self.env['mail.message'].create({
            'model': record._name,
            'res_id': record.id,
            'body': plaintext2html(body),
            'message_type': 'whatsapp_message',
        })

        # Create owa.message
        msg_vals = {
            'mobile_number': phone,
            'message_type': 'outbound',
            'state': 'outgoing',
            'wa_account_id': self.wa_account_id.id,
            'mail_message_id': mail_message.id,
        }
        if self.attach_pdf and self.report_id:
            msg_vals['pending_report_id'] = self.report_id.id
        if self.quick_reply_id:
            msg_vals['quick_reply_id'] = self.quick_reply_id.id
        if self.reply_to_mode_override:
            msg_vals['reply_to_mode_override'] = self.reply_to_mode_override
        self.env['owa.message'].create(msg_vals)

        # Log notification
        self.env['owa.notification.log'].create({
            'rule_id': self.id,
            'res_model': record._name,
            'res_id': record.id,
        })

        # Kick the send cron so the user sees delivery within seconds rather
        # than waiting up to a minute for the next scheduled tick.
        cron = self.env.ref(
            'open_whatsapp_connector.ir_cron_send_owa_queue',
            raise_if_not_found=False,
        )
        if cron:
            cron.sudo()._trigger()

        _logger.info("Notification rule '%s' triggered for %s/%s -> %s",
                     self.name, record._name, record.id, phone)


class OwaNotificationLog(models.Model):
    _name = 'owa.notification.log'
    _description = 'WhatsApp Notification Log'
    _order = 'id desc'

    rule_id = fields.Many2one('owa.notification.rule', string="Rule", required=True, ondelete='cascade')
    res_model = fields.Char(string="Model", required=True)
    res_id = fields.Many2oneReference(string="Record", model_field='res_model')
    create_date = fields.Datetime(string="Sent At", readonly=True)
