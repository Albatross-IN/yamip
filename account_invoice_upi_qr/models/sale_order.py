# -*- coding: utf-8 -*-
from odoo import models, fields, api
import base64
import urllib.parse

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    qr_code_image = fields.Binary(
        string="UPI QR Code",
        compute='_compute_qr_code_image',
        store=False,
    )

    @api.depends('amount_total', 'currency_id', 'company_id.l10n_in_upi_id')
    def _compute_qr_code_image(self):
        report_action = self.env['ir.actions.report']
        for record in self:
            if record.amount_total > 0 and record.company_id.l10n_in_upi_id and record.state in ['draft', 'sent', 'sale']:
                try:
                    upi_id = record.company_id.l10n_in_upi_id
                    payee_name = urllib.parse.quote_plus(record.company_id.name or "")
                    amount = "{:.2f}".format(record.amount_total)
                    currency = record.currency_id.name or 'INR'
                    
                    upi_uri = f"upi://pay?pa={upi_id}&pn={payee_name}&am={amount}&cu={currency}"
                    
                    qr_image_data = report_action.barcode(
                        barcode_type='QR', value=upi_uri, width=250, height=250, humanreadable=0
                    )
                    record.qr_code_image = base64.b64encode(qr_image_data)
                except Exception:
                    record.qr_code_image = False
            else:
                record.qr_code_image = False
