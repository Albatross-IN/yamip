# -*- coding: utf-8 -*-

from odoo import models, fields

class WhatsAppTemplate(models.Model):
    _inherit = 'whatsapp.template'

    def _get_template_body_component(self):
        """Override to handle 'authentication' templates for Meta."""
        if self.template_type == 'authentication':
            # Meta requires this specific format for authentication body
            return {
                'type': 'BODY',
                'add_security_recommendation': True
            }
        return super()._get_template_body_component()

    def _get_template_footer_component(self):
        """Override to handle 'authentication' templates for Meta."""
        if self.template_type == 'authentication':
            return {
                'type': 'FOOTER',
                'code_expiration_minutes': 30
            }
        return super()._get_template_footer_component()

    def _get_template_button_component(self):
        """Override to handle OTP button type for 'authentication' templates."""
        if self.template_type == 'authentication' and self.button_ids:
            buttons = []
            for button in self.button_ids:
                buttons.append({
                    'type': 'OTP',
                    'otp_type': 'COPY_CODE',
                    'text': button.name
                })
            return {'type': 'BUTTONS', 'buttons': buttons}
        return super()._get_template_button_component()

    def _get_template_vals_from_response(self, remote_template_vals, wa_account):
        """Override to handle syncing authentication templates back to Odoo."""
        template_vals = super()._get_template_vals_from_response(remote_template_vals, wa_account)
        
        if template_vals.get('template_type') == 'authentication':
            # Meta won't return text in the body for auth templates. 
            # We must manually set it so Odoo creates the {{1}} body variable properly in the UI.
            if not template_vals.get('body'):
                template_vals['body'] = "*{{1}}* is your verification code. For your security, do not share this code."
            
            # Reconstruct the footer text
            footer_text = "Expires in 30 minutes."
            for component in remote_template_vals.get('components', []):
                if component.get('type') == 'FOOTER' and component.get('code_expiration_minutes'):
                    footer_text = f"Expires in {component.get('code_expiration_minutes')} minutes."
            template_vals['footer_text'] = footer_text

            # Handle OTP buttons returned by Meta
            for component in remote_template_vals.get('components', []):
                if component.get('type') == 'BUTTONS':
                    for index, button in enumerate(component['buttons']):
                        if button.get('type') == 'OTP':
                            # Map it back to a standard Odoo url button with a dynamic url_type
                            button_vals = {
                                'sequence': index,
                                'name': button['text'],
                                'button_type': 'url',
                                'url_type': 'dynamic',
                                'website_url': 'https://www.whatsapp.com/otp/code/?otp_type=COPY_CODE&code_expiration_minutes=30&code=',
                                'variable_ids': [{
                                    'name': '{{1}}',
                                    'demo_value': '123456',
                                    'line_type': 'button',
                                }]
                            }
                            template_vals['button_ids'].append(button_vals)
                            
        return template_vals
