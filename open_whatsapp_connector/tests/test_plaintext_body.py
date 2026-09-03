"""S3-1: outbound senders must wrap plain-text bodies in plaintext2html so the
stored mail.message body is valid HTML — multi-line bodies render line breaks in
chatter, and a body containing '<' round-trips cleanly back to plain text
(plaintext2html -> stored HTML -> html2plaintext) without being eaten as a tag."""
from odoo.tests import tagged
from odoo.tools import html2plaintext
from odoo.addons.open_whatsapp_connector.tests.common import OwaTestCase


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestPlaintextBody(OwaTestCase):

    def test_autoreply_multiline_and_lt_survive(self):
        rule = self.env['owa.auto.reply'].create({
            'name': 'T',
            'wa_account_id': self.account.id,
            'trigger_type': 'all',
            'active': True,
            'response_body': 'Line one\nprice<100 today',
        })
        with self.mock_sidecar():
            rule._send_auto_reply(self.account, '15551112222')

        msg = self.env['owa.message'].search(
            [('wa_account_id', '=', self.account.id)], order='id desc', limit=1)
        self.assertTrue(msg, "an outbound owa.message should have been created")

        html = msg.mail_message_id.body
        self.assertIn('<br', html, "newline must become <br> in stored HTML")
        self.assertIn('price<100', html2plaintext(html),
                      "'<' must survive, not be eaten as a tag")
