"""WhatsApp Official Cloud API message templates.

In-Odoo authoring + Meta submit/sync for the approved-template messaging
path. Templates are the escape hatch for messaging a contact outside the
24h customer-service window.
"""

import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import html_escape

_logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r'\{\{\s*(\d+)\s*\}\}')

# "Start from a sample" presets — friendly, pre-approved-style starting
# points so users never face an empty, technical form. (#template-ux)
_TEMPLATE_SAMPLES = {
    'order_update': {
        'name': 'order_update', 'category': 'utility',
        'body': "Hi {{1}}, good news! Your order {{2}} has been shipped "
                "and should arrive by {{3}}.",
        'footer': "Thank you for shopping with us",
        'samples': ['Rahul', 'SO1234', 'Friday'],
        'buttons': [('quick_reply', 'Track my order', '', '')],
    },
    'payment_receipt': {
        'name': 'payment_receipt', 'category': 'utility',
        'body': "Hi {{1}}, we've received your payment of {{2}} for "
                "invoice {{3}}. Thank you!",
        'footer': "Keep this message as your receipt",
        'samples': ['Rahul', '₹4,999', 'INV/2026/0042'],
        'buttons': [],
    },
    'appointment_reminder': {
        'name': 'appointment_reminder', 'category': 'utility',
        'body': "Hi {{1}}, this is a friendly reminder of your appointment "
                "on {{2}} at {{3}}. Reply here if you need to reschedule.",
        'footer': "",
        'samples': ['Rahul', 'Monday 7 July', '3:30 PM'],
        'buttons': [('quick_reply', 'Confirm', '', ''),
                    ('quick_reply', 'Reschedule', '', '')],
    },
    'delivery_today': {
        'name': 'delivery_today', 'category': 'utility',
        'body': "Hi {{1}}, your delivery {{2}} is out for delivery and will "
                "reach you today between {{3}}.",
        'footer': "",
        'samples': ['Rahul', 'SO1234', '2 PM and 5 PM'],
        'buttons': [('phone_number', 'Call driver', '', '+919999999999')],
    },
    'otp_code': {
        'name': 'otp_code', 'category': 'authentication',
        'body': "{{1}} is your verification code. For your security, do not "
                "share this code.",
        'footer': "",
        'samples': ['482913'],
        'buttons': [],
    },
    'promo_offer': {
        'name': 'promo_offer', 'category': 'marketing',
        'body': "Hi {{1}}! 🎉 This week only: {{2}} off storewide. Show this "
                "message in store or order online before {{3}}.",
        'footer': "Reply STOP to unsubscribe",
        'samples': ['Rahul', '20%', 'Sunday'],
        'buttons': [('url', 'Shop now', 'https://example.com/shop', '')],
    },
}

# Meta message_template_status_update event → local status.
_META_EVENT_STATUS = {
    'APPROVED': 'approved',
    'REJECTED': 'rejected',
    'PENDING': 'pending',
    'IN_APPEAL': 'pending',
    'PENDING_DELETION': 'pending',
    'DELETED': 'rejected',
    'DISABLED': 'rejected',
    'PAUSED': 'approved',
    'FLAGGED': 'approved',
}

# Meta quality score → local quality selection.
_META_QUALITY = {
    'GREEN': 'green',
    'YELLOW': 'yellow',
    'RED': 'red',
    'UNKNOWN': 'none',
}


class OwaCloudTemplate(models.Model):
    _name = 'owa.cloud.template'
    _description = 'WhatsApp Cloud API Template'
    _inherit = ['mail.thread']
    _order = 'name, lang_code'

    name = fields.Char(required=True, tracking=True)
    lang_code = fields.Char(
        string="Language", default='en', required=True,
        help="BCP-47 language/locale code, e.g. en, en_US, pt_BR.")
    category = fields.Selection([
        ('utility', 'Utility'),
        ('marketing', 'Marketing'),
        ('authentication', 'Authentication'),
    ], default='utility', required=True, tracking=True)
    wa_account_id = fields.Many2one(
        'owa.account', string="WhatsApp Account", required=True,
        domain=[('connection_type', '=', 'cloud')],
        help="Cloud API account this template is authored / submitted under.")
    header_type = fields.Selection([
        ('none', 'None'),
        ('text', 'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
        ('document', 'Document'),
    ], default='none')
    header_text = fields.Char()
    body_text = fields.Text(
        string="Body",
        help="Body text. Use {{1}}, {{2}}, … placeholders for variables.")
    footer_text = fields.Char()
    wa_template_uid = fields.Char(
        string="Meta Template ID", copy=False, tracking=True)
    status = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], default='draft', copy=False, tracking=True)
    quality = fields.Selection([
        ('none', 'None'),
        ('green', 'High'),
        ('yellow', 'Medium'),
        ('red', 'Low'),
    ], default='none', copy=False)
    error_msg = fields.Char(copy=False)
    variable_ids = fields.One2many(
        'owa.cloud.template.variable', 'template_id', string="Variables")
    button_ids = fields.One2many(
        'owa.cloud.template.button', 'template_id', string="Buttons")

    # ── Guided authoring (#template-ux) ────────────────────────────────
    sample_pick = fields.Selection([
        ('order_update', "📦 Order shipped"),
        ('delivery_today', "🚚 Out for delivery"),
        ('payment_receipt', "🧾 Payment received"),
        ('appointment_reminder', "📅 Appointment reminder"),
        ('otp_code', "🔐 Verification code (OTP)"),
        ('promo_offer', "🎉 Promotional offer"),
    ], string="Start from a sample", store=False,
        help="Pick a ready-made template to start from — everything stays "
             "editable. Placeholders like {{1}} are filled per customer "
             "when the message is sent.")
    preview_html = fields.Html(
        string="Preview", compute='_compute_preview_html', sanitize=False)
    submit_checklist = fields.Html(
        string="Ready to submit?", compute='_compute_submit_checklist',
        sanitize=False)

    @api.onchange('sample_pick')
    def _onchange_sample_pick(self):
        sample = _TEMPLATE_SAMPLES.get(self.sample_pick)
        if not sample:
            return
        self.name = sample['name']
        self.category = sample['category']
        self.header_type = 'none'
        self.body_text = sample['body']
        self.footer_text = sample['footer']
        self.variable_ids = [(5, 0, 0)] + [
            (0, 0, {'component': 'body', 'index': i + 1, 'sample': s})
            for i, s in enumerate(sample['samples'])]
        self.button_ids = [(5, 0, 0)] + [
            (0, 0, {'button_type': btype, 'text': text,
                    'url': url, 'phone_number': phone})
            for btype, text, url, phone in sample['buttons']]
        self.sample_pick = False

    @api.onchange('name')
    def _onchange_name_slugify(self):
        """Meta template names must be lowercase [a-z0-9_] — turn whatever
        the user types ("Order Update!") into a valid id silently."""
        if self.name:
            slug = re.sub(r'[^a-z0-9_]+', '_', self.name.strip().lower())
            self.name = re.sub(r'_+', '_', slug).strip('_')

    @api.onchange('body_text', 'header_text', 'header_type')
    def _onchange_sync_variables(self):
        """Keep the Variables list in lock-step with the {{n}} placeholders
        actually present in the text — users never manage rows by hand.
        Existing sample values are preserved."""
        keep = {}
        for var in self.variable_ids:
            keep[(var.component, var.index)] = var.sample
        commands = [(5, 0, 0)]
        body_idx = sorted({int(m) for m in
                           _PLACEHOLDER_RE.findall(self.body_text or '')})
        for idx in body_idx:
            commands.append((0, 0, {
                'component': 'body', 'index': idx,
                'sample': keep.get(('body', idx), '')}))
        if self.header_type == 'text':
            head_idx = sorted({int(m) for m in
                               _PLACEHOLDER_RE.findall(self.header_text or '')})
            for idx in head_idx:
                commands.append((0, 0, {
                    'component': 'header', 'index': idx,
                    'sample': keep.get(('header', idx), '')}))
        self.variable_ids = commands

    def _substitute_samples(self, text, component):
        """Replace {{n}} with the matching sample value (highlighted) for the
        preview; unmatched placeholders stay visible in amber."""
        if not text:
            return ''
        samples = {v.index: v.sample for v in self.variable_ids
                   if v.component == component}

        def repl(match):
            idx = int(match.group(1))
            value = samples.get(idx)
            if value:
                return ('<span style="background:#d9fdd3;border-radius:3px;'
                        'padding:0 2px">%s</span>' % html_escape(value))
            return ('<span style="background:#fff3cd;border-radius:3px;'
                    'padding:0 2px">{{%s}}</span>' % idx)

        return _PLACEHOLDER_RE.sub(
            repl, html_escape(text)).replace('\n', '<br/>')

    @api.depends('header_type', 'header_text', 'body_text', 'footer_text',
                 'variable_ids.sample', 'variable_ids.index',
                 'variable_ids.component', 'button_ids.text',
                 'button_ids.button_type')
    def _compute_preview_html(self):
        """Live WhatsApp-style bubble so authors see exactly what the
        customer will receive, with sample values filled in."""
        icons = {'url': '🔗', 'phone_number': '📞', 'quick_reply': '↩️'}
        for tmpl in self:
            parts = []
            if tmpl.header_type == 'text' and tmpl.header_text:
                parts.append(
                    '<div style="font-weight:600;margin-bottom:4px">%s</div>'
                    % tmpl._substitute_samples(tmpl.header_text, 'header'))
            elif tmpl.header_type in ('image', 'video', 'document'):
                label = {'image': '🖼️ Image', 'video': '🎥 Video',
                         'document': '📄 Document'}[tmpl.header_type]
                parts.append(
                    '<div style="background:#e9edef;border-radius:6px;'
                    'padding:28px 0;text-align:center;color:#54656f;'
                    'margin-bottom:6px">%s</div>' % label)
            parts.append('<div style="white-space:normal">%s</div>'
                         % (tmpl._substitute_samples(tmpl.body_text, 'body')
                            or '<span style="color:#8696a0">Your message '
                              'appears here…</span>'))
            if tmpl.footer_text:
                parts.append(
                    '<div style="color:#8696a0;font-size:11px;'
                    'margin-top:4px">%s</div>' % html_escape(tmpl.footer_text))
            parts.append(
                '<div style="text-align:right;color:#8696a0;font-size:10px;'
                'margin-top:2px">12:30 PM</div>')
            buttons = ''.join(
                '<div style="border-top:1px solid #e9edef;color:#00a5f4;'
                'text-align:center;padding:7px 0;font-size:13px">%s %s</div>'
                % (icons.get(b.button_type, ''), html_escape(b.text or ''))
                for b in tmpl.button_ids)
            tmpl.preview_html = (
                '<div style="background:#efeae2;border-radius:8px;'
                'padding:16px;max-width:360px">'
                '<div style="background:#fff;border-radius:8px;'
                'box-shadow:0 1px 1px rgba(0,0,0,.13);padding:8px 10px 4px;'
                'font-size:13.5px;color:#111b21">%s</div>'
                '<div style="background:#fff;border-radius:0 0 8px 8px;'
                'max-width:100%%;margin-top:1px">%s</div></div>'
                % (''.join(parts), buttons))

    # ── Friendly pre-submit validation (#template-ux) ───────────────────
    def _submit_problems(self):
        """Return (errors, warnings): plain-language issues Meta would
        reject / frown on. Errors block submission; warnings don't."""
        self.ensure_one()
        errors, warnings = [], []
        if not self.name or not re.fullmatch(r'[a-z0-9_]{1,512}', self.name):
            errors.append(_("Name must contain only lowercase letters, "
                            "numbers and underscores (e.g. order_update)."))
        if not self.body_text:
            errors.append(_("Write the message body."))
        elif len(self.body_text) > 1024:
            errors.append(_("Body is over 1024 characters (%s).")
                          % len(self.body_text))
        body_idx = sorted({int(m) for m in
                           _PLACEHOLDER_RE.findall(self.body_text or '')})
        if body_idx and body_idx != list(range(1, len(body_idx) + 1)):
            errors.append(_("Placeholders must be numbered 1, 2, 3… without "
                            "gaps (found: %s).")
                          % ', '.join('{{%s}}' % i for i in body_idx))
        missing = [v.index for v in self.variable_ids if not v.sample]
        if missing:
            errors.append(_("Give a sample value for placeholder(s) %s — "
                            "Meta reviews templates with the samples "
                            "filled in.")
                          % ', '.join('{{%s}}' % i for i in sorted(missing)))
        if self.header_type == 'text':
            if len(self.header_text or '') > 60:
                errors.append(_("Header text is over 60 characters."))
            if len(_PLACEHOLDER_RE.findall(self.header_text or '')) > 1:
                errors.append(_("The header may contain at most ONE "
                                "placeholder."))
        if len(self.footer_text or '') > 60:
            errors.append(_("Footer is over 60 characters."))
        n_url = len(self.button_ids.filtered(
            lambda b: b.button_type == 'url'))
        n_phone = len(self.button_ids.filtered(
            lambda b: b.button_type == 'phone_number'))
        if len(self.button_ids) > 10:
            errors.append(_("At most 10 buttons are allowed."))
        if n_url > 2:
            errors.append(_("At most 2 website (URL) buttons are allowed."))
        if n_phone > 1:
            errors.append(_("At most 1 call (phone) button is allowed."))
        for btn in self.button_ids:
            if btn.text and len(btn.text) > 25:
                errors.append(_("Button “%s” text is over 25 characters.")
                              % btn.text)
            if btn.button_type == 'url' and not btn.url:
                errors.append(_("Button “%s” needs a website address.")
                              % (btn.text or 'URL'))
            if btn.button_type == 'phone_number' and not btn.phone_number:
                errors.append(_("Button “%s” needs a phone number.")
                              % (btn.text or 'Call'))
        if self.category == 'marketing' and 'stop' not in (
                (self.footer_text or '') + (self.body_text or '')).lower():
            warnings.append(_("Marketing templates approve faster with an "
                              "opt-out hint (e.g. footer “Reply STOP to "
                              "unsubscribe”)."))
        if self.category == 'authentication' and len(body_idx) != 1:
            warnings.append(_("Authentication templates are standardized by "
                              "Meta: keep the body to the code ({{1}}) plus "
                              "a short safety line."))
        return errors, warnings

    @api.depends('name', 'category', 'header_type', 'header_text',
                 'body_text', 'footer_text', 'variable_ids.sample',
                 'button_ids.text', 'button_ids.button_type',
                 'button_ids.url', 'button_ids.phone_number')
    def _compute_submit_checklist(self):
        for tmpl in self:
            errors, warnings = tmpl._submit_problems()
            rows = []
            for err in errors:
                rows.append('<div style="color:#d9534f">✗ %s</div>'
                            % html_escape(err))
            for warn in warnings:
                rows.append('<div style="color:#b8860b">⚠ %s</div>'
                            % html_escape(warn))
            if not errors:
                rows.insert(0, '<div style="color:#28a745">✓ %s</div>'
                            % html_escape(_("Ready to submit to Meta.")))
            tmpl.submit_checklist = '<div style="font-size:12.5px">%s</div>' \
                % ''.join(rows)

    # ------------------------------------------------------------------
    # Meta payload assembly
    # ------------------------------------------------------------------
    def _build_meta_payload(self):
        """Assemble the Meta ``POST /{waba_id}/message_templates`` payload
        (name + language + category + components) from this record."""
        self.ensure_one()
        components = []

        # Header
        if self.header_type == 'text' and self.header_text:
            header = {'type': 'HEADER', 'format': 'TEXT',
                      'text': self.header_text}
            header_vars = self.variable_ids.filtered(
                lambda v: v.component == 'header').sorted('index')
            if header_vars:
                header['example'] = {
                    'header_text': [v.sample or '' for v in header_vars]}
            components.append(header)
        elif self.header_type in ('image', 'video', 'document'):
            components.append({
                'type': 'HEADER',
                'format': self.header_type.upper(),
            })

        # Body
        if self.body_text:
            body = {'type': 'BODY', 'text': self.body_text}
            body_vars = self.variable_ids.filtered(
                lambda v: v.component == 'body').sorted('index')
            if body_vars:
                body['example'] = {
                    'body_text': [[v.sample or '' for v in body_vars]]}
            components.append(body)

        # Footer
        if self.footer_text:
            components.append({'type': 'FOOTER', 'text': self.footer_text})

        # Buttons
        if self.button_ids:
            buttons = []
            for btn in self.button_ids:
                if btn.button_type == 'quick_reply':
                    buttons.append({'type': 'QUICK_REPLY', 'text': btn.text})
                elif btn.button_type == 'url':
                    buttons.append({'type': 'URL', 'text': btn.text,
                                    'url': btn.url or ''})
                elif btn.button_type == 'phone_number':
                    buttons.append({'type': 'PHONE_NUMBER', 'text': btn.text,
                                    'phone_number': btn.phone_number or ''})
            if buttons:
                components.append({'type': 'BUTTONS', 'buttons': buttons})

        return {
            'name': self.name,
            'language': self.lang_code or 'en',
            'category': (self.category or 'utility').upper(),
            'components': components,
        }

    # ------------------------------------------------------------------
    # Actions — submit / sync
    # ------------------------------------------------------------------
    def action_submit(self):
        """Submit this template to Meta for review."""
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApiError)
        for tmpl in self:
            if not tmpl.wa_account_id:
                raise UserError(_("Set a Cloud API account first."))
            errors, __ = tmpl._submit_problems()
            if errors:
                raise UserError(
                    _("Please fix these before submitting:") +
                    '\n\n• ' + '\n• '.join(errors))
            payload = tmpl._build_meta_payload()
            try:
                resp = tmpl.wa_account_id._get_cloud_api().submit_template(
                    payload)
            except CloudApiError as e:
                tmpl.error_msg = e.message
                _logger.warning(
                    "Template submit failed for %s: %s", tmpl.name, e)
                continue
            meta_status = str(resp.get('status') or '').upper()
            tmpl.write({
                'wa_template_uid': resp.get('id') or tmpl.wa_template_uid,
                'status': _META_EVENT_STATUS.get(meta_status, 'pending'),
                'error_msg': False,
            })
        return True

    def action_sync(self):
        """Refresh this single template's status from Meta."""
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApiError)
        for tmpl in self.filtered('wa_template_uid'):
            try:
                data = tmpl.wa_account_id._get_cloud_api().get_template(
                    tmpl.wa_template_uid)
            except CloudApiError as e:
                tmpl.error_msg = e.message
                continue
            tmpl._apply_meta_template_dict(data)
        return True

    def action_delete_template(self):
        """Delete this template from Meta, then mark it rejected locally."""
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApiError)
        for tmpl in self:
            if tmpl.wa_account_id and tmpl.name:
                try:
                    tmpl.wa_account_id._get_cloud_api().delete_template(
                        tmpl.name, tmpl.wa_template_uid)
                except CloudApiError as e:
                    raise UserError(
                        _("Could not delete template: %s", e.message))
            tmpl.write({'status': 'rejected',
                        'error_msg': 'Deleted from Meta'})
        return True

    def action_edit_template(self):
        """Push the current body/header/footer/buttons of an already-created
        template back to Meta as an edit. Meta only allows editing templates
        that are APPROVED or REJECTED, and re-reviews the result — so the
        local status returns to 'pending'."""
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApiError)
        for tmpl in self.filtered('wa_template_uid'):
            errors, __ = tmpl._submit_problems()
            if errors:
                raise UserError(
                    _("Please fix these before editing:") +
                    '\n\n• ' + '\n• '.join(errors))
            payload = tmpl._build_meta_payload()
            try:
                tmpl.wa_account_id._get_cloud_api().edit_template(
                    tmpl.wa_template_uid,
                    components=payload.get('components'),
                    category=payload.get('category'))
            except CloudApiError as e:
                tmpl.error_msg = e.message
                raise UserError(_("Could not edit template: %s", e.message))
            tmpl.write({'status': 'pending', 'error_msg': False})
        return True

    # ------------------------------------------------------------------
    # Meta event / sync ingestion
    # ------------------------------------------------------------------
    def _apply_meta_template_dict(self, data):
        """Write status / quality / category from a Meta template dict
        (as returned by the message_templates list / single GET)."""
        self.ensure_one()
        vals = {}
        meta_status = str(data.get('status') or '').upper()
        if meta_status in _META_EVENT_STATUS:
            vals['status'] = _META_EVENT_STATUS[meta_status]
        category = str(data.get('category') or '').lower()
        if category in ('utility', 'marketing', 'authentication'):
            vals['category'] = category
        q = data.get('quality_score') or {}
        qscore = str((q.get('score') if isinstance(q, dict) else q)
                     or '').upper()
        if qscore in _META_QUALITY:
            vals['quality'] = _META_QUALITY[qscore]
        if data.get('id'):
            vals['wa_template_uid'] = data['id']
        if vals:
            self.write(vals)

    @api.model
    def _apply_meta_event(self, field, value):
        """Apply a Meta webhook template event (message_template_status_update
        / quality / category) to the matching local template row.

        ``value`` carries ``message_template_id`` (the wa_template_uid),
        ``message_template_name`` / ``message_template_language`` and an
        ``event`` (APPROVED/REJECTED/…) or ``new_quality_score`` /
        ``new_category``.
        """
        uid = (value.get('message_template_id')
               or value.get('message_template_ID'))
        name = value.get('message_template_name')
        lang = value.get('message_template_language')
        tmpl = self.sudo()
        rec = self.browse()
        if uid:
            rec = tmpl.search([('wa_template_uid', '=', str(uid))], limit=1)
        if not rec and name:
            dom = [('name', '=', name)]
            if lang:
                dom.append(('lang_code', '=', lang))
            rec = tmpl.search(dom, limit=1)
        if not rec:
            _logger.info(
                "Cloud template event %s: no local row for id=%s name=%s",
                field, uid, name)
            return False

        vals = {}
        if field == 'message_template_status_update':
            event = str(value.get('event') or '').upper()
            if event in _META_EVENT_STATUS:
                vals['status'] = _META_EVENT_STATUS[event]
            reason = value.get('reason')
            if event == 'REJECTED':
                vals['error_msg'] = reason or 'rejected'
        elif field == 'message_template_quality_update':
            q = str(value.get('new_quality_score') or '').upper()
            if q in _META_QUALITY:
                vals['quality'] = _META_QUALITY[q]
        elif field == 'template_category_update':
            cat = str(value.get('new_category')
                      or value.get('correct_category') or '').lower()
            if cat in ('utility', 'marketing', 'authentication'):
                vals['category'] = cat

        if vals:
            rec.write(vals)
            # Post a chatter note on rejection so the author sees the reason.
            if vals.get('status') == 'rejected':
                try:
                    rec.message_post(body=_(
                        "Template rejected by Meta: %s")
                        % (rec.error_msg or value.get('reason') or 'no reason'))
                except Exception:  # pragma: no cover -- chatter best-effort
                    _logger.exception(
                        "Rejection chatter post failed for template %s", rec.id)
        return True

    # ------------------------------------------------------------------
    # Send-by-template (24h-window escape hatch)
    # ------------------------------------------------------------------
    def _build_send_components(self, values=None):
        """Build the Meta send-time ``components`` array from this template's
        variables + a ``{index: value}`` (or ``[value, …]``) mapping.

        Only text body/header parameters are wired here; media-header
        parameters and button URL/payload params can be layered on later.
        """
        self.ensure_one()
        values = values or {}

        def _val(var):
            if isinstance(values, dict):
                return (values.get(var.index)
                        or values.get(str(var.index))
                        or var.sample or '')
            try:
                return values[var.index - 1]
            except (IndexError, TypeError):
                return var.sample or ''

        components = []
        header_vars = self.variable_ids.filtered(
            lambda v: v.component == 'header').sorted('index')
        if header_vars:
            components.append({
                'type': 'header',
                'parameters': [
                    {'type': 'text', 'text': str(_val(v))}
                    for v in header_vars],
            })
        body_vars = self.variable_ids.filtered(
            lambda v: v.component == 'body').sorted('index')
        if body_vars:
            components.append({
                'type': 'body',
                'parameters': [
                    {'type': 'text', 'text': str(_val(v))}
                    for v in body_vars],
            })
        return components

    def action_send(self, number, values=None):
        """Send this approved template to ``number``.

        Builds the send-time components from ``values`` (a {index: value}
        dict or ordered list), dispatches via the cloud transport, and writes
        an outbound owa.message flagged so the 24h-window guard is skipped
        (templates are the legitimate way to message outside the window).
        Returns the created owa.message.
        """
        self.ensure_one()
        if self.status != 'approved':
            raise UserError(_(
                "Template '%s' is not approved yet (status: %s).")
                % (self.name, self.status))
        account = self.wa_account_id
        if not account or account.connection_type != 'cloud':
            raise UserError(_("Template is not bound to a Cloud API account."))
        to = (number or '').lstrip('+')
        if not to:
            raise UserError(_("No recipient number."))

        components = self._build_send_components(values)
        # Create the outbound owa.message + its mail.message so the send is
        # visible in the conversation + logs, mirroring a normal outbound.
        mail = self.env['mail.message'].create({
            'body': self.body_text or self.name,
            'message_type': 'whatsapp_message',
        })
        msg = self.env['owa.message'].create({
            'mobile_number': to,
            'message_type': 'outbound',
            'state': 'outgoing',
            'wa_account_id': account.id,
            'mail_message_id': mail.id,
        })
        from odoo.addons.open_whatsapp_connector.tools.cloud_api import (
            CloudApiError)
        try:
            wamid = account._dispatch_send(
                'template', to=to, name=self.name,
                lang=self.lang_code or 'en', components=components or None)
        except (CloudApiError, Exception) as e:
            reason = getattr(e, 'message', None) or str(e)
            msg.write({
                'state': 'error',
                'failure_type': 'recoverable',
                'failure_reason': reason,
                'error_message': reason,
            })
            raise UserError(_("Template send failed: %s") % reason)
        msg.write({
            'state': 'sent',
            'msg_uid': wamid,
            'wa_message_uid': wamid,
        })
        return msg


class OwaCloudTemplateVariable(models.Model):
    _name = 'owa.cloud.template.variable'
    _description = 'WhatsApp Cloud Template Variable'
    _order = 'component, index'

    template_id = fields.Many2one(
        'owa.cloud.template', required=True, ondelete='cascade')
    index = fields.Integer(string="Placeholder #", required=True)
    component = fields.Selection([
        ('body', 'Body'),
        ('header', 'Header'),
    ], default='body', required=True)
    sample = fields.Char(string="Sample value")


class OwaCloudTemplateButton(models.Model):
    _name = 'owa.cloud.template.button'
    _description = 'WhatsApp Cloud Template Button'

    template_id = fields.Many2one(
        'owa.cloud.template', required=True, ondelete='cascade')
    button_type = fields.Selection([
        ('quick_reply', 'Quick Reply'),
        ('url', 'URL'),
        ('phone_number', 'Call'),
    ], default='quick_reply', required=True)
    text = fields.Char(required=True)
    url = fields.Char()
    phone_number = fields.Char()
