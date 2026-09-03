from unittest.mock import patch

from odoo.addons.open_whatsapp_connector.tools.baileys_api import BaileysApi
from .common import OwaTestCase


class TestImportJob(OwaTestCase):
    """Background WhatsApp history import — contacts, conversations, recent
    messages drained from the (mocked) sidecar history buffer."""

    def _page(self):
        return {
            'contacts': [{'id': '15559876543@s.whatsapp.net', 'name': 'Bob'}],
            'chats': [{'id': '15551234567@s.whatsapp.net', 'name': 'Alice'}],
            'messages': [{
                'key': {'remoteJid': '15551234567@s.whatsapp.net',
                        'id': 'HIST-M1', 'fromMe': False},
                'messageTimestamp': 1700000000,
                'message': {'conversation': 'hi from history'},
            }],
            'nextCursor': 1,
            'done': True,
        }

    def test_import_job_creates_records(self):
        # import_communities/channels default True and would trigger the
        # structure-sync step (live groups/newsletter feeds), out of scope
        # for this history-drain test — disable so we exercise only the
        # mocked history buffer.
        job = self.env['owa.import.job'].create({
            'account_id': self.account.id,
            'import_communities': False,
            'import_channels': False,
        })
        status = {'available': True, 'syncComplete': True, 'progress': 100, 'counts': {}}
        with patch.object(BaileysApi, 'history_status', return_value=status), \
                patch.object(BaileysApi, 'fetch_history_page', return_value=self._page()), \
                patch.object(BaileysApi, 'history_resync', return_value={'started': True}):
            job._cron_run()

        self.assertEqual(job.state, 'done')
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.count_contacts, 1)
        self.assertEqual(job.count_chats, 1)
        self.assertEqual(job.count_messages, 1)

        channel = self.account._find_active_channel('15551234567')
        self.assertTrue(channel, "a whatsapp conversation should have been created")
        owa_msgs = self.env['owa.message'].search([
            ('msg_uid', '=', 'HIST-M1'), ('wa_account_id', '=', self.account.id)])
        self.assertEqual(len(owa_msgs), 1)
        self.assertEqual(owa_msgs.message_type, 'inbound')
        self.assertIn('hi from history', owa_msgs.mail_message_id.body or '')

    def test_import_is_idempotent(self):
        """Re-importing the same page must not duplicate anything."""
        job = self.env['owa.import.job'].create({'account_id': self.account.id})
        page = self._page()
        job._import_page(page)
        partners_after_first = self.env['res.partner'].search_count([])
        msgs_after_first = self.env['owa.message'].search_count([('msg_uid', '=', 'HIST-M1')])

        result = job._import_page(page)
        self.assertEqual(result, (0, 0, 0))
        self.assertEqual(self.env['res.partner'].search_count([]), partners_after_first)
        self.assertEqual(
            self.env['owa.message'].search_count([('msg_uid', '=', 'HIST-M1')]),
            msgs_after_first)

    def test_wizard_history_creates_job(self):
        wiz = self.env['owa.import.wizard'].create({
            'wa_account_id': self.account.id,
            'import_groups': False,
            'import_contacts': False,
            'import_history': True,
            'import_communities': False,
            'import_channels': False,
        })
        # action_import auto-starts the job, whose first run polls the
        # sidecar; mock those calls so the history-buffer path runs offline.
        status = {'available': True, 'syncComplete': True, 'progress': 100, 'counts': {}}
        with patch.object(BaileysApi, 'history_status', return_value=status), \
                patch.object(BaileysApi, 'fetch_history_page',
                             return_value={'done': True, 'nextCursor': 0}), \
                patch.object(BaileysApi, 'history_resync', return_value={'started': True}):
            action = wiz.action_import()
        self.assertEqual(action.get('res_model'), 'owa.import.job')
        job = self.env['owa.import.job'].browse(action['res_id'])
        self.assertTrue(job.exists())
        self.assertEqual(job.account_id, self.account)
        self.assertTrue(job.import_messages)
