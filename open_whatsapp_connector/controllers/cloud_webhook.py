"""Official WhatsApp Cloud API (Meta Graph API) webhook.

One shared callback URL for every cloud account. GET handles Meta's
verification handshake (``hub.challenge``); POST verifies the
``X-Hub-Signature-256`` HMAC per account, routes by WABA id +
phone-number-id, and feeds inbound messages into the transport-neutral
``owa.account._handle_inbound`` and status receipts into
``owa.account._apply_cloud_status``.
"""

import hashlib
import hmac
import json
import logging
from http import HTTPStatus

from odoo import http
from odoo.http import request
from odoo.tools import consteq

_logger = logging.getLogger(__name__)


class CloudWebhook(http.Controller):

    @http.route('/open_whatsapp_connector/cloud/webhook',
                methods=['GET'], type='http', auth='public', csrf=False)
    def cloud_verify(self, **kw):
        """Meta webhook verification handshake. Echo ``hub.challenge`` when a
        cloud account with the matching ``cloud_verify_token`` exists."""
        token = kw.get('hub.verify_token')
        mode = kw.get('hub.mode')
        challenge = kw.get('hub.challenge')
        if mode == 'subscribe' and token:
            acc = request.env['owa.account'].sudo().search([
                ('connection_type', '=', 'cloud'),
                ('cloud_verify_token', '=', token),
            ], limit=1)
            if acc:
                resp = request.make_response(challenge or '')
                resp.status_code = HTTPStatus.OK
                return resp
        return request.make_response('', status=HTTPStatus.FORBIDDEN)

    @http.route('/open_whatsapp_connector/cloud/webhook',
                methods=['POST'], type='http', auth='public', csrf=False)
    def cloud_inbound(self, **kw):
        """Receive Meta change notifications. Verifies the per-account HMAC
        signature, then dispatches messages + statuses + template events.

        ``**kw`` swallows the ``?db=`` query arg (and any extras Meta appends)
        so the router doesn't log "called ignoring args {'db'}"; the db is
        already resolved by the ir_http patch before dispatch.
        """
        raw = request.httprequest.get_data()
        try:
            data = json.loads(raw or b'{}')
        except ValueError:
            return request.make_response('', status=HTTPStatus.BAD_REQUEST)
        for entry in data.get('entry', []):
            waba_id = entry.get('id')
            # A single WABA can host several of OUR numbers (each its own
            # owa.account). Resolve ALL of them, then route every change to the
            # one matching its phone-number-id — the old limit=1 silently
            # dropped inbound for every number after the first. (#F002/F058)
            accounts = request.env['owa.account'].sudo().search([
                ('connection_type', '=', 'cloud'),
                ('cloud_waba_id', '=', waba_id),
            ])
            if not accounts:
                _logger.warning("Cloud webhook: no account for WABA %s", waba_id)
                continue
            # X-Hub-Signature-256 is computed with the app secret, shared by
            # every number under one WABA/app — accept if ANY of them verifies.
            if not any(self._verify_signature(a, raw) for a in accounts):
                return request.make_response('', status=HTTPStatus.FORBIDDEN)
            for change in entry.get('changes', []):
                value = change.get('value', {})
                field = change.get('field')
                pnid = (value.get('metadata') or {}).get('phone_number_id')
                if pnid:
                    account = accounts.filtered(
                        lambda a: a.cloud_phone_number_id == pnid)[:1]
                    if not account:
                        _logger.warning(
                            "Cloud webhook: WABA %s has no account for "
                            "phone_number_id %s", waba_id, pnid)
                        continue
                else:
                    # Template / WABA-level events carry no phone metadata.
                    account = accounts[:1]
                self._process_change(account, value, field)
        return request.make_response('', status=HTTPStatus.OK)

    def _verify_signature(self, account, raw):
        sig = request.httprequest.headers.get('X-Hub-Signature-256', '')
        if not sig.startswith('sha256=') or not account.cloud_app_secret:
            return False
        expected = hmac.new(
            account.cloud_app_secret.encode(), raw, hashlib.sha256).hexdigest()
        return consteq(sig[7:], expected)

    def _process_change(self, account, value, field):
        from odoo.addons.open_whatsapp_connector.tools.meta_inbound import (
            normalize_meta_message)
        # The caller (cloud_inbound) has already routed this change to the
        # account matching value.metadata.phone_number_id. (#F002/F058)
        # Each item runs in its own savepoint: Meta retries the WHOLE batch on
        # a 500, so without isolation one poison payload makes every retry
        # fail until Meta gives up — dropping the GOOD messages with it. With
        # it, good items commit, the bad one is logged and skipped, and the
        # dedupe claim of a failed inbound rolls back with its savepoint.
        if field == 'messages':
            for status in value.get('statuses', []):
                try:
                    with request.env.cr.savepoint():
                        account._apply_cloud_status(status)
                except Exception:
                    _logger.exception("Cloud webhook: error applying status")
            for message in value.get('messages', []):
                try:
                    with request.env.cr.savepoint():
                        account._handle_inbound(
                            normalize_meta_message(value, message))
                except Exception:
                    _logger.exception("Cloud webhook: error processing inbound")
                    continue
                # Read receipts on Cloud: mark the customer's message read
                # (mirrors the QR sidecar's send_read_receipts behaviour).
                # Best-effort — a Graph hiccup must not fail the webhook.
                if message.get('id') and account._effective_send_read_receipts():
                    try:
                        account._get_cloud_api().mark_read(message['id'])
                    except Exception:
                        _logger.warning(
                            "Cloud webhook: mark_read failed for %s",
                            message.get('id'))
        elif field == 'calls':
            # Inbound WhatsApp Business calls -> shared owa.call.log pipeline
            # (call log, ringing toast, missed-call auto-reply). Subscribe the
            # Meta app to the 'calls' webhook field for these to arrive.
            for call in value.get('calls', []):
                try:
                    with request.env.cr.savepoint():
                        account._handle_cloud_call(call)
                except Exception:
                    _logger.exception("Cloud webhook: error processing call")
        elif field in ('message_template_status_update',
                       'message_template_quality_update',
                       'template_category_update'):
            # Cloud template management lands in Phase 3.
            if 'owa.cloud.template' in request.env:
                request.env['owa.cloud.template'].sudo()._apply_meta_event(
                    field, value)
