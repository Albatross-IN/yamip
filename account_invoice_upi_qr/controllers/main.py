# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request

class SaleOrderPaymentController(http.Controller):
    
    @http.route('/so/payment/<int:order_id>', type='http', auth='public', website=True)
    def so_payment_page(self, order_id, **kw):
        print(">>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        order = request.env['sale.order'].sudo().browse(order_id)
        
        if not order.exists():
            return request.not_found()
            
        return request.render('account_invoice_upi_qr.so_public_payment_page', {
            'order': order,
        })
