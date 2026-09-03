"""Reply-to / react-to a contact's WhatsApp status.

A WhatsApp status reply is a normal DM to the poster that quotes the status;
a reaction is an emoji on the status message. Both target the status message
key captured on ``owa.status.update``.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OwaStatusInteractWizard(models.TransientModel):
    _name = 'owa.status.interact.wizard'
    _description = 'Reply / React to a WhatsApp Status'

    status_id = fields.Many2one(
        'owa.status.update', required=True, ondelete='cascade')
    mode = fields.Selection([
        ('reply', 'Reply'),
        ('react', 'React'),
    ], required=True, default='reply')
    contact_display = fields.Char(related='status_id.name', string="Status")
    reply_text = fields.Text(string="Your reply")
    emoji = fields.Char(string="Emoji", default='👍', size=8)

    def action_confirm(self):
        from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
        self.ensure_one()
        status = self.status_id
        account = status.wa_account_id
        if account.session_state != 'connected':
            raise UserError(_(
                "WhatsApp account '%s' is not connected.") % account.name)
        api = BaileysApi(account)
        key = status._status_key()
        digits = (status.contact_number or '').lstrip('+').strip()
        to_jid = status.wa_participant or (
            '%s@s.whatsapp.net' % digits if digits else '')
        if self.mode == 'react':
            api.status_react(key, self.emoji or '👍')
            msg = _("Reaction sent.")
        else:
            if not (self.reply_text or '').strip():
                raise UserError(_("Type a reply."))
            if not to_jid:
                raise UserError(_("Can't resolve the contact to reply to."))
            api.status_reply(to_jid, key, self.reply_text)
            msg = _("Reply sent.")
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("WhatsApp Status"), 'message': msg,
                       'type': 'success'},
        }
