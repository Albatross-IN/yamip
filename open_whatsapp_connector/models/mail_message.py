from odoo import fields, models
from odoo.addons.mail.tools.discuss import Store


class MailMessage(models.Model):
    _inherit = 'mail.message'

    message_type = fields.Selection(
        selection_add=[('whatsapp_message', 'WhatsApp Message')],
        ondelete={'whatsapp_message': 'set default'})
    owa_message_ids = fields.One2many(
        'owa.message', 'mail_message_id', string="WhatsApp Messages")

    def _to_store(self, store, *args, **kwargs):
        # Inject the delivery state (whatsappStatus) for WhatsApp messages so
        # the ✓✓ status icon renders on HISTORICAL messages too — previously
        # only messages updated live via the status bus push carried it, so
        # older messages showed "unknown" until the next live update.
        # *args/**kwargs keeps the override signature-compatible across v18
        # (fields is keyword-only) and v19 (fields is positional). store.add
        # accepts a {field: value} dict on both versions. (#discuss)
        super()._to_store(store, *args, **kwargs)
        wa_msgs = self.filtered(lambda m: m.message_type == 'whatsapp_message')
        for mail_msg in wa_msgs:
            owa = mail_msg.sudo().owa_message_ids[:1]
            if owa:
                # Also expose, to the Discuss "Edit" message action, whether
                # THIS message can be edited on WhatsApp (own recently-sent
                # outbound on a connected QR account) and the owa.message id.
                # (#wa-edit-action)
                acc = owa.wa_account_id
                extras = {
                    'whatsappStatus': owa.state,
                    'owaMessageId': owa.id,
                    'owaEditable': bool(
                        owa.msg_uid
                        and owa.message_type == 'outbound'
                        and owa.state not in ('error', 'cancel')
                        and acc and acc.connection_type == 'qr'
                    ),
                }
                # On v19, Store.add(records, fields) re-invokes _to_store() ->
                # infinite recursion; add_records_fields() injects the value
                # without recursing (its documented purpose). v18/v17 Store has
                # no add_records_fields and its add(record, values) sets the
                # value directly, so fall back there.
                if hasattr(store, 'add_records_fields'):
                    store.add_records_fields(mail_msg, extras)
                else:
                    store.add(mail_msg, extras)
