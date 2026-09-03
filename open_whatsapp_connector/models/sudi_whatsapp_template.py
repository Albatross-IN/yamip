# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _


class SudiWhatsappTemplate(models.Model):
    _name = "sudi.whatsapp.template"
    _description = "Sudi WhatsApp Template"
    _order = "sequence, id"

    name = fields.Char(string="Template Name", required=True)
    event = fields.Selection(
        [
            ("pickup_scheduled", "Pickup Task Assignment (To Operator)"),
            ("pickup_request_confirmation", "Pickup Request Confirmation (To Customer)"),
            ("pickup_confirmed", "Pickup Completion Notice (To Customer)"),
            ("pickup_admin_intimation", "Pickup Admin Intimation (To Admin)"),
            ("pickup_cancelled", "Pickup Cancellation Notice (To Customer)"),
            ("delivery_assigned", "Out for Delivery Update (To Customer)"),
            ("delivery_dispatch", "Delivery Task Assignment (To Delivery Person)"),
            ("delivery_completed", "Delivery Completion Notice (To Customer)"),
            ("delivery_admin_intimation", "Delivery Admin Intimation (To Admin)"),
        ],
        string="Event Trigger",
        required=True,
        index=True,
    )
    body = fields.Text(string="Message Body", required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
    )

    _event_company_unique = models.Constraint(
        "UNIQUE(event, company_id)",
        "A Sudi WhatsApp Template for this event and company already exists!",
    )

    def init(self):
        super().init()
        # Automatically clean up legacy/escaped %%(var)s placeholders into clean {{var}} braces
        self.env.cr.execute("""
            UPDATE sudi_whatsapp_template
            SET body = REPLACE(REPLACE(REPLACE(body, '%%(', '{{'), ')s', '}}'), '%(', '{{')
            WHERE body LIKE '%%(%' OR body LIKE '%(%';
        """)
