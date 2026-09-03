"""Small transient wizard for the Cloud API phone-registration steps that need
a value: register the number (two-step PIN), verify an SMS/voice code, or set
the two-step-verification PIN.

Ported from open_whatsapp_call/wizard/owc_cloud_phone_wizard.py (owc→owa).
"""
from odoo import _, fields, models
from odoo.exceptions import UserError


class OwaCloudPhoneWizard(models.TransientModel):
    _name = 'owa.cloud.phone.wizard'
    _description = 'WhatsApp Cloud Phone Registration'

    account_id = fields.Many2one('owa.account', required=True,
                                 ondelete='cascade')
    mode = fields.Selection([
        ('register', 'Register number (set PIN)'),
        ('verify_code', 'Verify SMS/voice code'),
        ('set_pin', 'Set two-step PIN'),
    ], required=True, default='register')
    code = fields.Char(string="Verification code")
    pin = fields.Char(string="Two-step PIN (6 digits)")

    def action_apply(self):
        self.ensure_one()
        api = self.account_id._get_cloud_api()
        if self.mode == 'register':
            if not (self.pin or '').isdigit() or len(self.pin or '') != 6:
                raise UserError(_("Enter a 6-digit PIN."))
            api.register_phone(self.pin)
            msg = _("Number registered on the Cloud API.")
        elif self.mode == 'verify_code':
            if not self.code:
                raise UserError(_("Enter the verification code."))
            api.verify_code(self.code)
            msg = _("Code verified.")
        else:  # set_pin
            if not (self.pin or '').isdigit() or len(self.pin or '') != 6:
                raise UserError(_("Enter a 6-digit PIN."))
            api.set_two_step_pin(self.pin)
            msg = _("Two-step PIN updated.")
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("WhatsApp Cloud"), 'message': msg,
                       'type': 'success'}}
