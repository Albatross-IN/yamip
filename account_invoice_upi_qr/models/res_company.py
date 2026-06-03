# -*- coding: utf-8 -*-
from odoo import models, fields

class ResCompany(models.Model):
    _inherit = 'res.company'

    upi_id = fields.Char(string="UPI ID", help="The UPI ID to receive payments (e.g., example@oksbi)")
    upi_payee_name = fields.Char(string="UPI Payee Name", help="The Registered Payee Name on the UPI ID")
    upi_bank_name = fields.Char(string="Bank Name", help="The name of the bank for the UPI account")
    upi_account_number = fields.Char(string="Account Number", help="Bank Account Number")
    upi_ifsc = fields.Char(string="IFSC Code", help="Bank IFSC Code")
