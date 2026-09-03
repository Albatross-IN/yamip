# -*- coding: utf-8 -*-
import random
import logging
import uuid
from datetime import datetime, timedelta

from odoo import http, fields, _, SUPERUSER_ID
from odoo.http import request
from odoo.exceptions import UserError
from odoo.addons.web.controllers.home import ensure_db

_logger = logging.getLogger(__name__)

class WebAuthOtpController(http.Controller):

    @http.route('/web/auth/otp/send', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    def send_otp(self, phone):
        ensure_db()
        if not phone:
            return {'success': False, 'error': _('Phone number is required.')}

        # Normalize phone number (keep digits and leading plus)
        phone_clean = ''.join(c for c in phone if c.isdigit() or c == '+')
        if not phone_clean or len(phone_clean) < 8:
            return {'success': False, 'error': _('Please enter a valid phone number.')}

        # Build dynamic search domain for res.partner based on available fields
        partner_fields = request.env['res.partner'].with_user(SUPERUSER_ID)._fields
        partner_domain = [('phone', '=', phone_clean)]
        if 'mobile' in partner_fields:
            partner_domain = ['|', ('phone', '=', phone_clean), ('mobile', '=', phone_clean)]

        partner = request.env['res.partner'].with_user(SUPERUSER_ID).search(partner_domain, limit=1)

        # Fallback to trailing match (last 10 digits) if no exact match found
        if not partner and len(phone_clean) >= 10:
            last_10 = phone_clean[-10:]
            fallback_domain = [('phone', 'like', last_10)]
            if 'mobile' in partner_fields:
                fallback_domain = ['|', ('phone', 'like', last_10), ('mobile', 'like', last_10)]
            partner = request.env['res.partner'].with_user(SUPERUSER_ID).search(fallback_domain, limit=1)

        user = False
        if partner:
            # Find active user associated with the partner
            user = request.env['res.users'].with_user(SUPERUSER_ID).search([
                ('partner_id', '=', partner.id),
                ('active', '=', True)
            ], limit=1)
            
            # Security restriction: Internal users (employees/admins) cannot log in via OTP
            if user and user.has_group('base.group_user'):
                return {'success': False, 'error': _('Internal employee accounts must log in using Email and Password.')}
        else:
            # Auto-register guest partner for new customer phone number
            create_vals = {
                'name': _('Guest (%s)') % phone_clean,
                'phone': phone_clean,
            }
            if 'mobile' in partner_fields:
                create_vals['mobile'] = phone_clean
            partner = request.env['res.partner'].with_user(SUPERUSER_ID).create(create_vals)

        template = request.env.ref('web_auth_otp_login.wa_template_otp_auth_generic', raise_if_not_found=False)
        if template:
            template = template.with_user(SUPERUSER_ID)
        if not template:
            template = request.env['whatsapp.template'].with_user(SUPERUSER_ID).search([
                ('status', '=', 'approved'),
                ('template_name', '=', 'otp_auth_generic')
            ], limit=1)

        if not template:
            return {'success': False, 'error': _('WhatsApp sending failed: No approved WhatsApp template found in the system.')}

        # Generate a secure 6-digit OTP
        otp = str(random.randint(100000, 999999))

        # Store OTP and verification data in the session
        request.session['otp_code'] = otp
        request.session['otp_phone'] = phone_clean
        request.session['otp_time'] = fields.Datetime.to_string(fields.Datetime.now())
        request.session['otp_user_id'] = user.id if user else False
        request.session['otp_partner_id'] = partner.id
        request.session.touch()

        target_model = template.model or 'res.partner'
        res_id = partner.id
        if target_model != 'res.partner':
            target_record = request.env[target_model].with_user(SUPERUSER_ID).search([], limit=1)
            if target_record:
                res_id = target_record.id

        # Determine if we need the partner name hack (i.e. if the template variables are field-bound)
        # Wholesale Intro and Sale 2 use field variables, so we temporarily set the name to the OTP code.
        use_name_hack = (template.template_name in ['wholesale_intro', 'sale_2']) or not any(v.field_type == 'free_text' for v in template.variable_ids)
        original_name = partner.name

        try:
            if use_name_hack:
                partner.write({'name': otp})

            mail_message = request.env['mail.message'].with_user(SUPERUSER_ID).create({
                'model': target_model,
                'res_id': res_id,
                'body': _('WhatsApp OTP code %s sent for login verification.') % otp,
                'message_type': 'whatsapp_message',
            })

            # Dynamically populate free_text_json based on the template's variable count
            free_text_json = {}
            if template.variable_ids:
                # 1. Body free text variables
                body_vars = template.variable_ids.filtered(lambda v: v.line_type == 'body' and v.field_type == 'free_text')
                body_vars = body_vars.sorted(key=lambda v: v._extract_variable_index() or 0)
                for i, var in enumerate(body_vars):
                    free_text_json[f'free_text_{i+1}'] = otp if i == 0 else (var.demo_value or otp)

                # 2. Header free text variables
                header_vars = template.variable_ids.filtered(lambda v: v.line_type == 'header' and v.field_type == 'free_text')
                if header_vars:
                    free_text_json['header_text'] = header_vars[0].demo_value or _('Login Verification')

                # 3. Button free text variables (dynamic url buttons)
                button_vars = template.variable_ids.filtered(lambda v: v.line_type == 'button' and v.field_type == 'free_text')
                button_vars = button_vars.sorted(key=lambda v: v.button_id.sequence or 0)
                for i, var in enumerate(button_vars):
                    val = var.demo_value or '/'
                    if 'code=' in val:
                        val = val.replace('123456', otp).replace('???', otp)
                    free_text_json[f'button_dynamic_url_{i+1}'] = val

            # Create the whatsapp.message record
            wa_msg = request.env['whatsapp.message'].with_user(SUPERUSER_ID).create({
                'mail_message_id': mail_message.id,
                'mobile_number': phone_clean,
                'free_text_json': free_text_json,
                'wa_template_id': template.id,
                'wa_account_id': template.wa_account_id.id,
            })
            wa_msg._send()
            if wa_msg.state == 'error':
                reason = wa_msg.failure_reason or _('Unknown gateway error.')
                return {'success': False, 'error': _('WhatsApp sending failed: %s') % reason}
        except Exception as e:
            _logger.exception("Exception during WhatsApp OTP send")
            return {'success': False, 'error': _('WhatsApp sending error: %s') % str(e)}
        finally:
            if use_name_hack:
                partner.write({'name': original_name})

        return {'success': True, 'message': _('OTP sent successfully via WhatsApp.')}

    @http.route('/web/auth/otp/verify', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    def verify_otp(self, phone, otp_code, redirect=None):
        ensure_db()
        if not phone or not otp_code:
            return {'success': False, 'error': _('Phone number and OTP code are required.')}

        phone_clean = ''.join(c for c in phone if c.isdigit() or c == '+')
        
        # Retrieve session values
        stored_otp = request.session.get('otp_code')
        stored_phone = request.session.get('otp_phone')
        stored_time = request.session.get('otp_time')
        stored_user_id = request.session.get('otp_user_id')
        stored_partner_id = request.session.get('otp_partner_id')

        if not stored_otp or not stored_phone or not stored_time or not stored_partner_id:
            return {'success': False, 'error': _('Session expired. Please request a new OTP.')}

        # Check phone number matches (compare last 10 digits for formatting flexibility)
        if stored_phone[-10:] != phone_clean[-10:]:
            return {'success': False, 'error': _('Phone number mismatch.')}

        # Check OTP expiration (5 minutes validity)
        if isinstance(stored_time, str):
            stored_time = fields.Datetime.from_string(stored_time)
        
        if fields.Datetime.now() - stored_time > timedelta(minutes=5):
            return {'success': False, 'error': _('OTP code has expired. Please request a new one.')}

        # Verify OTP code
        if stored_otp != otp_code.strip():
            return {'success': False, 'error': _('Invalid OTP code. Please check and try again.')}

        # Successful verification: Authenticate session programmatically
        if stored_user_id:
            user = request.env['res.users'].with_user(SUPERUSER_ID).browse(stored_user_id)
        else:
            # Complete the auto-registration for new guest customer
            partner = request.env['res.partner'].with_user(SUPERUSER_ID).browse(stored_partner_id)
            if partner.name.startswith('Guest ('):
                partner.write({'name': _('Customer (%s)') % phone_clean})
            
            portal_group = request.env.ref('base.group_portal')
            login_name = phone_clean
            
            # Double check to prevent duplicate login constraint failure
            existing_user = request.env['res.users'].with_user(SUPERUSER_ID).search([('login', '=', login_name)], limit=1)
            if existing_user:
                user = existing_user
                if user.partner_id != partner:
                    user.write({'partner_id': partner.id})
            else:
                user = request.env['res.users'].with_user(SUPERUSER_ID).create({
                    'name': partner.name,
                    'login': login_name,
                    'partner_id': partner.id,
                    'group_ids': [(6, 0, [portal_group.id])],
                    'password': str(uuid.uuid4()),
                    'active': True,
                })

        if not user.active:
            return {'success': False, 'error': _('User account is deactivated.')}

        # Initialize Odoo session authentication
        request.session.uid = user.id
        request.session.login = user.login
        request.session.db = request.env.registry.db_name
        request.session.context = dict(user.env['res.users'].context_get())
        
        # Generate session token and trigger soft rotation
        request.env.registry.clear_cache()
        request.session.session_token = user._compute_session_token(request.session.sid)
        request.session.should_rotate = True

        # Link session cart to the logged-in user
        sale_order_id = request.session.get('sale_order_id')
        if sale_order_id:
            sale_order = request.env['sale.order'].with_user(SUPERUSER_ID).browse(sale_order_id)
            if sale_order.exists() and sale_order.state == 'draft':
                old_partner = sale_order.partner_id
                fields_to_update = ['partner_id']
                if sale_order.partner_invoice_id == old_partner:
                    fields_to_update.append('partner_invoice_id')
                if sale_order.partner_shipping_id == old_partner:
                    fields_to_update.append('partner_shipping_id')
                sale_order.with_user(SUPERUSER_ID)._update_address(user.partner_id.id, fields_to_update)

        # Clear OTP and registration values from session
        request.session.pop('otp_code', None)
        request.session.pop('otp_phone', None)
        request.session.pop('otp_time', None)
        request.session.pop('otp_user_id', None)
        request.session.pop('otp_partner_id', None)
        request.session.touch()

        # Determine target redirect
        redirect_url = redirect or request.params.get('redirect') or '/shop/checkout'
        if not redirect_url.startswith('/'):
            redirect_url = '/shop/checkout'

        return {
            'success': True,
            'message': _('Logged in successfully.'),
            'redirect': redirect_url
        }







