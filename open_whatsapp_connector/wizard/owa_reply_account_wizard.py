"""Pick which connected WhatsApp account a conversation replies from.

Opened from the Discuss composer (the "Reply account" action) so an agent with
more than one WhatsApp account can choose, per conversation, which number the
outbound goes from. Writing the channel's ``owa_account_id`` is all that is
needed — ``discuss.channel.message_post`` routes every reply by that field.
Also lets an orphaned conversation (its account deleted) be re-pointed to a
live account so it becomes repliable again. (#reply-account)"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class OwaReplyAccountWizard(models.TransientModel):
    _name = 'owa.reply.account.wizard'
    _description = 'Choose WhatsApp Reply Account'

    channel_id = fields.Many2one(
        'discuss.channel', string="Conversation", required=True,
        domain=[('channel_type', '=', 'whatsapp')])
    wa_account_id = fields.Many2one(
        'owa.account', string="Reply from account", required=True,
        domain=[('session_state', '=', 'connected')],
        help="Outbound messages in this conversation will be sent from this "
             "WhatsApp account.")

    def action_apply(self):
        self.ensure_one()
        if self.channel_id.channel_type != 'whatsapp':
            raise UserError(_("This is not a WhatsApp conversation."))
        self.channel_id.owa_account_id = self.wa_account_id.id
        return {'type': 'ir.actions.act_window_close'}
