"""Payload-shape tests for the 2026 Meta Cloud API feature batch:
interactive buttons/lists, flows, media carousels, block API, business
profile management, Cloud groups and the typing indicator.

No HTTP fires: CloudApi's low-level _post/_get/_delete are patched and the
captured payloads are asserted against Meta's documented shapes.
"""
from unittest.mock import patch

from odoo.tests import Form, tagged

from odoo.addons.open_whatsapp_connector.tools.cloud_api import CloudApi
from odoo.addons.open_whatsapp_connector.tools.meta_inbound import (
    normalize_meta_message,
)

from .common import OwaTestCase


@tagged('post_install', '-at_install', 'open_whatsapp_connector')
class TestCloudGapFeatures(OwaTestCase):

    def setUp(self):
        super().setUp()
        self.cloud = self.Account.create({
            'name': 'cloud-fixture', 'webhook_secret': 'cloud-secret',
            'connection_type': 'cloud', 'session_state': 'connected',
            'cloud_phone_number_id': '111222333',
            'cloud_waba_id': '444555666',
            'cloud_access_token': 'test-token',
            'notify_user_ids': [(6, 0, [self.env.ref('base.user_admin').id])],
        })
        self.api = CloudApi(self.cloud)
        self.calls = []

        def fake_post(path, json=None, **kw):
            self.calls.append(('POST', path, json))
            return {'messages': [{'id': 'wamid.TEST'}], 'id': 'grp1',
                    'data': [{}], 'success': True}

        def fake_get(path, params=None):
            self.calls.append(('GET', path, params))
            return {'data': [{'profile_picture_url': ''}]}

        def fake_delete(path, json=None):
            self.calls.append(('DELETE', path, json))
            return {'success': True}

        self._patches = [
            patch.object(CloudApi, '_post', side_effect=fake_post),
            patch.object(CloudApi, '_get', side_effect=fake_get),
            patch.object(CloudApi, '_delete', side_effect=fake_delete),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _last(self, method='POST'):
        return next(c for c in reversed(self.calls) if c[0] == method)

    # == interactive buttons / list =======================================
    def test_buttons_payload(self):
        uid = self.api.send_buttons(
            '919812345678', 'Pick one',
            buttons=[{'id': 'b1', 'text': 'A very long button title over 20'},
                     {'id': 'b2', 'text': 'Two'},
                     {'id': 'b3', 'text': 'Three'},
                     {'id': 'b4', 'text': 'Dropped (max 3)'}],
            footer='foot')
        self.assertEqual(uid, 'wamid.TEST')
        _, path, payload = self._last()
        self.assertIn('/111222333/messages', path)
        inter = payload['interactive']
        self.assertEqual(inter['type'], 'button')
        btns = inter['action']['buttons']
        self.assertEqual(len(btns), 3)
        self.assertEqual(btns[0]['type'], 'reply')
        self.assertLessEqual(len(btns[0]['reply']['title']), 20)
        self.assertEqual(inter['footer']['text'], 'foot')

    def test_list_payload_row_cap(self):
        rows = [{'id': f'r{i}', 'title': f'Row {i}', 'description': ''}
                for i in range(12)]
        self.api.send_list('919812345678', 'Menu body', 'Open',
                           sections=[{'title': 'S1', 'rows': rows}])
        _, _, payload = self._last()
        inter = payload['interactive']
        self.assertEqual(inter['type'], 'list')
        total = sum(len(s['rows']) for s in inter['action']['sections'])
        self.assertEqual(total, 10)          # Meta hard cap
        self.assertEqual(inter['action']['button'], 'Open')

    # == flows ============================================================
    def test_flow_payload(self):
        self.api.send_flow('919812345678', '123456', 'Book now',
                           'Schedule your visit', screen='WELCOME',
                           flow_token='tok-1')
        _, _, payload = self._last()
        inter = payload['interactive']
        self.assertEqual(inter['type'], 'flow')
        params = inter['action']['parameters']
        self.assertEqual(params['flow_message_version'], '3')
        self.assertEqual(params['flow_id'], '123456')
        self.assertEqual(params['flow_cta'], 'Book now')
        self.assertEqual(params['flow_action_payload'], {'screen': 'WELCOME'})
        self.assertEqual(params['flow_token'], 'tok-1')

    def test_flow_inbound_nfm_reply_normalized(self):
        value = {'contacts': [{'profile': {'name': 'Cust'}}],
                 'metadata': {'phone_number_id': '111222333'}}
        message = {
            'from': '919812345678', 'id': 'wamid.NFM', 'type': 'interactive',
            'timestamp': '1780000000',
            'interactive': {'type': 'nfm_reply', 'nfm_reply': {
                'name': 'flow', 'body': 'Sent',
                'response_json': '{"appointment":"tomorrow"}'}},
        }
        norm = normalize_meta_message(value, message)
        self.assertIn('Sent', norm['body'])
        self.assertIn('appointment', norm['body'])

    # == carousel =========================================================
    def test_carousel_payload(self):
        self.api.send_carousel('919812345678', 'Our catalog', cards=[
            {'media_id': 'm1', 'media_type': 'image', 'body': 'Card one',
             'buttons': [{'id': 'buy1', 'text': 'Buy'}]},
            {'link': 'https://x/y.mp4', 'media_type': 'video'},
        ])
        _, _, payload = self._last()
        inter = payload['interactive']
        self.assertEqual(inter['type'], 'carousel')
        cards = inter['action']['cards']
        self.assertEqual(cards[0]['card_index'], 0)
        self.assertEqual(cards[0]['header']['image'], {'id': 'm1'})
        self.assertEqual(cards[1]['header']['video'],
                         {'link': 'https://x/y.mp4'})

    # == block API ========================================================
    def test_block_unblock_payloads(self):
        self.api.block_users(['+919812345678'])
        _, path, payload = self._last('POST')
        self.assertIn('/111222333/block_users', path)
        self.assertEqual(payload['block_users'], [{'user': '919812345678'}])
        self.api.unblock_users(['919812345678'])
        _, path, payload = self._last('DELETE')
        self.assertIn('/111222333/block_users', path)

    # == business profile ================================================
    def test_business_profile_update_and_picture(self):
        self.api.update_business_profile(about='We reply fast')
        _, path, payload = self._last()
        self.assertIn('/111222333/whatsapp_business_profile', path)
        self.assertEqual(payload['about'], 'We reply fast')
        with patch.object(CloudApi, 'upload_resumable_handle',
                          return_value='HANDLE-1'):
            self.api.update_profile_picture(b'rawjpegbytes')
        _, _, payload = self._last()
        self.assertEqual(payload['profile_picture_handle'], 'HANDLE-1')

    # == groups ==========================================================
    def test_group_create_payload(self):
        self.api.create_group('Support VIPs', description='Top customers')
        _, path, payload = self._last()
        self.assertIn('/111222333/groups', path)
        self.assertEqual(payload['subject'], 'Support VIPs')
        self.assertEqual(payload['description'], 'Top customers')

    # == typing indicator ================================================
    def test_typing_payload(self):
        self.api.send_typing('wamid.INBOUND1')
        _, path, payload = self._last()
        self.assertIn('/111222333/messages', path)
        self.assertEqual(payload['status'], 'read')
        self.assertEqual(payload['typing_indicator'], {'type': 'text'})

    # == template authoring UX ===========================================
    def test_template_slugify_and_variable_autosync(self):
        f = Form(self.env['owa.cloud.template'])
        f.name = 'Order Update!'
        f.wa_account_id = self.cloud
        f.body_text = 'Hi {{1}}, your order {{2}} has shipped.'
        self.assertEqual(f.name, 'order_update')
        tmpl = f.save()
        self.assertEqual(
            sorted(tmpl.variable_ids.mapped('index')), [1, 2])
        # Editing the body keeps existing samples and drops removed vars.
        tmpl.variable_ids.filtered(lambda v: v.index == 1).sample = 'Rahul'
        with Form(tmpl) as f2:
            f2.body_text = 'Hi {{1}}, thanks!'
        self.assertEqual(tmpl.variable_ids.mapped('index'), [1])
        self.assertEqual(tmpl.variable_ids.sample, 'Rahul')

    def test_template_sample_pick_fills_form(self):
        f = Form(self.env['owa.cloud.template'])
        f.wa_account_id = self.cloud
        f.sample_pick = 'order_update'
        self.assertEqual(f.name, 'order_update')
        self.assertIn('{{1}}', f.body_text)
        tmpl = f.save()
        self.assertEqual(len(tmpl.variable_ids), 3)
        self.assertTrue(all(tmpl.variable_ids.mapped('sample')))

    def test_template_preview_substitutes_samples(self):
        f = Form(self.env['owa.cloud.template'])
        f.wa_account_id = self.cloud
        f.sample_pick = 'payment_receipt'
        tmpl = f.save()
        self.assertIn('Rahul', tmpl.preview_html)          # sample filled in
        self.assertNotIn('{{1}}', tmpl.preview_html)       # placeholder gone

    def test_template_friendly_validation_blocks_submit(self):
        from odoo.exceptions import UserError
        tmpl = self.env['owa.cloud.template'].create({
            'name': 'order_update', 'wa_account_id': self.cloud.id,
            'body_text': 'Hi {{1}}, order {{3}} shipped.',   # gap: no {{2}}
        })
        errors, _w = tmpl._submit_problems()
        self.assertTrue(any('numbered' in e for e in errors))
        with self.assertRaises(UserError):
            tmpl.action_submit()
        self.assertIn('✗', tmpl.submit_checklist)

    # == outbound routing: quick-reply buttons over Cloud =================
    def test_cloud_send_routes_interactive_template(self):
        template = self.env['owa.quick.reply'].create({
            'name': 'Menu', 'template_type': 'button', 'body': 'Choose:',
            'button_ids': [(0, 0, {'text': 'Sales'}),
                           (0, 0, {'text': 'Support'})],
        })
        msg = self.Message.create({
            'mobile_number': '+919812345678', 'message_type': 'outbound',
            'state': 'outgoing', 'wa_account_id': self.cloud.id,
            'quick_reply_id': template.id,
        })
        # skip_cloud_window: no inbound exists to open the 24h window.
        msg.with_context(skip_cloud_window=True)._send_cloud_message(
            self.cloud, '919812345678', 'Choose:',
            self.env['ir.attachment'])
        kinds = [c for c in self.calls
                 if c[0] == 'POST' and '/messages' in c[1]
                 and (c[2] or {}).get('type') == 'interactive']
        self.assertTrue(kinds, "interactive payload was not sent")
        self.assertEqual(kinds[-1][2]['interactive']['type'], 'button')
        self.assertEqual(msg.state, 'sent')
