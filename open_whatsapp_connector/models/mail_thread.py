import logging

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.addons.mail.tools.discuss import Store

_logger = logging.getLogger(__name__)


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _thread_to_store(self, store: Store, fields, *, request_list=None):
        super()._thread_to_store(store, fields, request_list=request_list)
        if request_list:
            can_send = bool(self.env['owa.account'].search([
                ('session_state', '=', 'connected'),
            ], limit=1))
            store.add(self, {"canSendWhatsapp": can_send}, as_thread=True)

    def action_owa_get_channel_id(self):
        """Return the id of THIS record's WhatsApp conversation channel
        (find-or-create), so the chatter 'WhatsApp' button can render the live
        conversation inline. Works on res.partner (self) or any record carrying
        a contact (partner_id / commercial_partner_id). (#wa-open-chat)"""
        self.ensure_one()
        if self._name == 'res.partner':
            partner = self
        else:
            partner = (getattr(self, 'partner_id', False)
                       or getattr(self, 'commercial_partner_id', False))
        if not partner:
            raise UserError(_(
                "No contact is linked to this record to open a WhatsApp chat."))
        return partner._owa_get_or_create_channel().id

    def write(self, vals):
        """Fire WhatsApp notification rules on field changes — INCLUDING
        computed / indirect stored trigger fields (e.g. stock.picking.state set
        by delivery validation) that never appear in ``vals``. The previous
        implementation only inspected ``vals`` keys, so a delivery/receipt
        validation (state recomputed, not written) never triggered."""
        if self.env.context.get('owa_skip_notification') or not vals or not self:
            return super().write(vals)
        Rule = self.env['owa.notification.rule'].sudo()
        # Cheap cached gate: skip models that carry no active rule.
        if self._name not in Rule._owa_models_with_rules():
            return super().write(vals)
        rules = Rule.search([
            ('model_name', '=', self._name), ('active', '=', True),
        ])
        # Snapshot before-values ONLY for trigger fields present in this write's
        # vals, so a directly-written field fires on a genuine transition.
        in_vals = {r.trigger_field_name for r in rules
                   if r.trigger_field_name in vals
                   and r.trigger_field_name in self._fields}
        before = ({rec.id: {f: rec[f] for f in in_vals} for rec in self}
                  if in_vals else {})
        result = super().write(vals)
        try:
            self._owa_fire_rules(rules, vals, before)
        except Exception:
            _logger.exception("Error checking WhatsApp notification rules")
        return result

    def _owa_fire_rules(self, rules, vals, before):
        """Evaluate each active rule against the just-written records."""
        for record in self:
            for rule in rules:
                field = rule.trigger_field_name
                if not field or field not in record._fields:
                    continue
                cur = record[field]
                if not rule._value_matches_trigger(cur):
                    continue
                if field in vals:
                    # Directly written: require an actual transition this write
                    # so a no-op re-save doesn't re-fire.
                    old = (before.get(record.id, {}) or {}).get(field)
                    if hasattr(old, 'ids') and hasattr(cur, 'ids'):
                        if tuple(old.ids) == tuple(cur.ids):
                            continue
                    elif old == cur:
                        continue
                else:
                    # Trigger field NOT in this write's vals. This branch exists
                    # ONLY for COMPUTED / indirect trigger fields (e.g.
                    # stock.picking.state, set by delivery validation, which
                    # never appears in a write's vals) — for those a real
                    # transition can't be captured above, so notify_once rules
                    # self-dedupe per record via the notification log.
                    #
                    # For a PLAIN stored field (e.g. account.move.state) the
                    # genuine transition already fired via the branch above.
                    # Firing AGAIN here on a LATER same-transaction write — one
                    # of the many writes account.move._post() performs, some of
                    # them BEFORE the computed invoice-sequence `name` is
                    # assigned — produced a DUPLICATE notification (and an early
                    # one with a blank invoice number), because the non-atomic
                    # log dedup races under those nested writes. Plain fields
                    # only ever change via a write that lists them, so the
                    # transition branch already covers them: skip the
                    # fall-through unless the trigger field is computed. (#dup-fire)
                    model_field = record._fields.get(field)
                    if not rule.notify_once or not (model_field and model_field.compute):
                        continue
                rule.with_context(owa_skip_notification=True)._send_notification(record)
