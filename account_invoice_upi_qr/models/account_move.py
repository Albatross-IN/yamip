# -*- coding: utf-8 -*-
from odoo import models, fields, api
import base64
import urllib.parse

class AccountMove(models.Model):
    _inherit = 'account.move'

    qr_code_image = fields.Binary(
        string="UPI QR Code",
        compute='_compute_qr_code_image',
        store=False,
    )

    @api.depends('amount_total', 'currency_id', 'move_type', 'company_id.upi_id', 'company_id.upi_payee_name')
    def _compute_qr_code_image(self):
        report_action = self.env['ir.actions.report']
        width = 250
        height = 250
        
        for record in self:
            if record.move_type in ('out_invoice', 'out_receipt') and record.amount_total > 0 and record.company_id.upi_id:
                try:
                    upi_id = record.company_id.upi_id
                    payee_name = record.company_id.upi_payee_name or record.company_id.name or ""
                    payee_name_encoded = urllib.parse.quote_plus(payee_name)
                    
                    amount = "{:.2f}".format(record.amount_total)
                    currency = record.currency_id.name or 'INR'
                    
                    # Create UPI URI
                    upi_uri = f"upi://pay?pa={upi_id}&pn={payee_name_encoded}&am={amount}&cu={currency}"
                    
                    # Generate QR Code using Odoo's internal helper
                    qr_image_data = report_action.barcode(
                        barcode_type='QR',
                        value=upi_uri,
                        width=width,
                        height=height,
                        humanreadable=0
                    )
                    record.qr_code_image = base64.b64encode(qr_image_data)
                except Exception:
                    record.qr_code_image = False
            else:
                record.qr_code_image = False
