"""Meta Embedded Signup — server-side token exchange.

The account form's "Connect with Facebook" button (static/src/cloud/
owa_embedded_signup.js) runs Meta's Embedded Signup popup, which returns an
authorization ``code`` plus the selected ``phone_number_id`` / ``waba_id``.
This controller exchanges the code for a business access token, subscribes the
app to the WABA, and writes the credentials onto an owa.account (creating one
if no account id is passed), flipping it to a connected Cloud account.

Requires the global Meta App config (Settings → Open WhatsApp Connector): App
ID, App Secret, and API version. Callable only by a WhatsApp administrator.

Ported from open_whatsapp_call/controllers/owc_cloud_onboarding.py (owc→owa).
"""
import logging

import requests

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)
_TIMEOUT = 20


class OwaCloudOnboarding(http.Controller):

    def _param(self, key, default=''):
        return request.env['ir.config_parameter'].sudo().get_param(key, default)

    @http.route('/open_whatsapp_connector/cloud/embedded_signup',
                type='json', auth='user', methods=['POST'])
    def embedded_signup(self, code=None, phone_number_id=None, waba_id=None,
                        account_id=None, **kw):
        if not request.env.user.has_group(
                'open_whatsapp_connector.group_owa_admin') \
                and not request.env.su:
            raise UserError(_("Only WhatsApp administrators can connect a "
                              "Cloud account."))
        if not (code and phone_number_id and waba_id):
            raise UserError(_("The Embedded Signup did not return a phone "
                              "number / business account. Try again."))
        app_id = self._param('open_whatsapp_connector.meta_app_id')
        app_secret = self._param('open_whatsapp_connector.meta_app_secret')
        version = self._param(
            'open_whatsapp_connector.meta_api_version', 'v23.0')
        if not (app_id and app_secret):
            raise UserError(_("Set the Meta App ID and App Secret in "
                              "Settings → Open WhatsApp Connector first."))
        # 1) code -> long-lived business access token
        try:
            resp = requests.get(
                f"https://graph.facebook.com/{version}/oauth/access_token",
                params={'client_id': app_id, 'client_secret': app_secret,
                        'code': code}, timeout=_TIMEOUT)
            token_data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise UserError(_("Token exchange with Meta failed: %s", e))
        token = token_data.get('access_token')
        if not token:
            raise UserError(_("Meta did not return an access token: %s",
                              token_data.get('error', token_data)))
        # 2) upsert the account with the cloud credentials
        Account = request.env['owa.account'].sudo()
        acc = Account.browse(int(account_id)) if account_id else Account.browse()
        vals = {
            'connection_type': 'cloud',
            'cloud_phone_number_id': phone_number_id,
            'cloud_waba_id': waba_id,
            'cloud_app_id': app_id,
            'cloud_app_secret': app_secret,
            'cloud_access_token': token,
            'cloud_api_version': version,
        }
        if acc.exists():
            acc.write(vals)
        else:
            vals['name'] = kw.get('name') or 'WhatsApp Cloud'
            acc = Account.create(vals)
        # 3) subscribe our app to the WABA so inbound webhooks flow, then verify
        try:
            acc._get_cloud_api().subscribe_app()
        except Exception as e:  # noqa: BLE001
            _logger.warning("Embedded signup: subscribe_app failed: %s", e)
        try:
            acc.action_verify_connection()
        except Exception as e:  # noqa: BLE001
            _logger.warning("Embedded signup: verify failed: %s", e)
        return {'account_id': acc.id, 'name': acc.name,
                'state': acc.session_state}
