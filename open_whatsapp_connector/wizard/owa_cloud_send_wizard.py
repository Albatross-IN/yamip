"""Cloud-only composer for the interactive message types the free-text path
can't produce: reply buttons, list menus, call-to-action URL buttons, a
location request, and WhatsApp Flows. Sends through the account's cloud
transport and threads the result into the contact's Discuss conversation.

Ported from open_whatsapp_call/wizard/owc_cloud_send_wizard.py, adapted to the
connector's seam: the CloudApi send methods return a bare wamid string (not a
dict), and the outbound is threaded + recorded via discuss.channel.message_post
with ``owa_recorded_outbound_uid`` (records the message as already-sent and
never re-sends it).
"""
import re

from odoo import _, fields, models
from odoo.exceptions import UserError


class OwaCloudSendWizard(models.TransientModel):
    _name = 'owa.cloud.send.wizard'
    _description = 'Send WhatsApp Interactive Message (Cloud API)'

    account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account", required=True,
        domain=[('connection_type', '=', 'cloud')])
    to_number = fields.Char(
        string="To (number)", required=True,
        help="Recipient in international format, digits only.")
    kind = fields.Selection([
        ('buttons', 'Reply buttons'),
        ('list', 'List menu'),
        ('cta_url', 'Call-to-action URL button'),
        ('location_request', 'Request location'),
        ('flow', 'WhatsApp Flow'),
    ], required=True, default='buttons')
    body_text = fields.Text(string="Message", required=True)
    footer_text = fields.Char(string="Footer")
    # buttons / list
    buttons_text = fields.Text(
        string="Buttons (one per line, max 3)",
        help="One button title per line.")
    list_button = fields.Char(string="List button label", default="Menu")
    list_rows_text = fields.Text(
        string="List rows (title | description, one per line)")
    # cta url
    cta_display_text = fields.Char(string="Button text")
    cta_url = fields.Char(string="URL")
    # flow
    flow_id = fields.Char(string="Flow ID")
    flow_cta = fields.Char(string="Flow button", default="Open")

    def action_send(self):
        self.ensure_one()
        acc = self.account_id
        if acc.connection_type != 'cloud':
            raise UserError(_("Pick a Cloud API account."))
        to = ''.join(c for c in (self.to_number or '') if c.isdigit())
        if not to:
            raise UserError(_("Enter a valid recipient number."))
        api = acc._get_cloud_api()
        footer = self.footer_text or None
        if self.kind == 'buttons':
            btns = [{'id': re.sub(r'\W+', '_', l.strip().lower())[:250],
                     'text': l.strip()}
                    for l in (self.buttons_text or '').splitlines()
                    if l.strip()]
            if not btns:
                raise UserError(_("Add at least one button."))
            wamid = api.send_buttons(to, self.body_text, btns, footer=footer)
        elif self.kind == 'list':
            rows = []
            for line in (self.list_rows_text or '').splitlines():
                if not line.strip():
                    continue
                title, _sep, desc = line.partition('|')
                rows.append({'title': title.strip(),
                             'description': desc.strip()})
            if not rows:
                raise UserError(_("Add at least one list row."))
            wamid = api.send_list(
                to, self.body_text, self.list_button or 'Menu',
                [{'title': '', 'rows': rows}], footer=footer)
        elif self.kind == 'cta_url':
            if not (self.cta_display_text and self.cta_url):
                raise UserError(_("Set the button text and URL."))
            wamid = api.send_cta_url(to, self.body_text, self.cta_display_text,
                                     self.cta_url, footer=footer)
        elif self.kind == 'location_request':
            wamid = api.send_location_request(to, self.body_text)
        else:  # flow
            if not self.flow_id:
                raise UserError(_("Set the Flow ID."))
            wamid = api.send_flow(to, self.flow_id, self.flow_cta or 'Open',
                                  self.body_text, footer=footer)
        # Thread into the contact's conversation and record the outbound as
        # already-sent (owa_recorded_outbound_uid → no re-send).
        channel = self.env['discuss.channel'].sudo()._get_owa_whatsapp_channel(
            to, acc, create_if_not_found=True)
        if channel:
            channel.with_context(mail_create_nosubscribe=True).message_post(
                body=self.body_text, message_type='whatsapp_message',
                owa_recorded_outbound_uid=wamid or False)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("WhatsApp"), 'type': 'success',
                       'message': _("Interactive message sent.")}}
