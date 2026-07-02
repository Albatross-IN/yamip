# -*- coding: utf-8 -*-
{
    'name': 'Web WhatsApp OTP Login',
    'version': '1.0',
    'category': 'Website/Website',
    'summary': 'Passwordless login using phone number and WhatsApp OTP',
    'description': """
        This module introduces passwordless authentication for the Yami Pins e-commerce website.
        Users can log in with their phone number via a dynamic One-Time Password (OTP)
        sent directly to their WhatsApp.
    """,
    'author': 'RNDGrid',
    'depends': [
        'base',
        'web',
        'portal',
        'website',
        'whatsapp',
    ],
    'data': [
        'data/whatsapp_template_data.xml',
        'views/web_auth_otp_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'web_auth_otp_login/static/src/css/otp_login.css',
            'web_auth_otp_login/static/src/js/otp_login.js',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
