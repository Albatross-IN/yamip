# -*- coding: utf-8 -*-
{
    'name': 'Invoice UPI QR Code',
    'version': '1.0',
    'category': 'Accounting/Localizations',
    'summary': 'Generate Dynamic UPI QR Codes on Invoices',
    'description': """
        This module allows companies to configure their active UPI ID, Payee Name, 
        and Bank Account details on the Company settings. 
        Once configured, posted Out-Invoices will dynamically generate a UPI QR Code
        for precise total amounts that customers can scan to instantly populate their UPI App.
    """,
    'author': 'RNDGrid',
    'depends': ['base', 'account'],
    'data': [
        'views/res_company_views.xml',
        'views/report_invoice_document.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
