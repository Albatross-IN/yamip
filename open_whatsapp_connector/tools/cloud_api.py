"""WhatsApp Business Cloud API (Meta Graph API) client.

A thin per-account wrapper used as the "cloud" transport alongside the QR /
Baileys transport. Its public method names mirror BaileysApi so the send
dispatch in owa.account._dispatch_send can swap transports cleanly.
"""

import logging
import requests

_logger = logging.getLogger(__name__)

# Meta error codes meaning "you are outside the 24h customer window — only an
# approved template may be sent now".
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
REENGAGEMENT_CODES = {131047, 131026, 131051}
DEFAULT_TIMEOUT = 20
_MEDIA_TYPES = ('image', 'video', 'audio', 'document', 'sticker')


class CloudApiError(Exception):
    def __init__(self, code, message, fbtrace_id=None, http_status=None):
        self.code = code
        self.message = message
        self.fbtrace_id = fbtrace_id
        self.http_status = http_status
        super().__init__(f"[{code}] {message} (fbtrace={fbtrace_id})")

    @property
    def is_reengagement_window(self):
        return self.code in REENGAGEMENT_CODES


class CloudApi:
    """Cloud (Graph) API client bound to one owa.account."""

    def __init__(self, account):
        self.account = account
        self.version = account.cloud_api_version or 'v23.0'
        self.phone_number_id = account.cloud_phone_number_id
        self.waba_id = account.cloud_waba_id
        self.app_id = account.cloud_app_id
        self.token = account.cloud_access_token
        self.base = f"https://graph.facebook.com/{self.version}"

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------
    def _headers(self, oauth=False):
        scheme = 'OAuth' if oauth else 'Bearer'
        return {'Authorization': f'{scheme} {self.token}'}

    def _post(self, path, json=None, **kw):
        return self._handle(requests.post(
            self.base + path, headers=self._headers(), json=json,
            timeout=DEFAULT_TIMEOUT, **kw))

    def _get(self, path, params=None):
        return self._handle(requests.get(
            self.base + path, headers=self._headers(), params=params,
            timeout=DEFAULT_TIMEOUT))

    def _handle(self, resp):
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or 'error' in data:
            err = data.get('error', {})
            raise CloudApiError(
                err.get('code', resp.status_code),
                err.get('message', (resp.text or '')[:200]),
                err.get('fbtrace_id'), resp.status_code)
        return data

    def _messages(self, payload):
        payload = dict(payload, messaging_product='whatsapp')
        data = self._post(f"/{self.phone_number_id}/messages", json=payload)
        msgs = data.get('messages') or [{}]
        return msgs[0].get('id', '')

    # ------------------------------------------------------------------
    # messaging
    # ------------------------------------------------------------------
    def health(self):
        self._get(f"/{self.phone_number_id}")
        return True

    def send_text(self, to, body, preview_url=False):
        return self._messages({
            'to': to, 'type': 'text',
            'text': {'body': body or '', 'preview_url': bool(preview_url)},
        })

    def send_media(self, to, media_type, link=None, media_id=None,
                   caption=None, filename=None):
        obj = {}
        if media_id:
            obj['id'] = media_id
        elif link:
            obj['link'] = link
        if caption and media_type in ('image', 'video', 'document'):
            obj['caption'] = caption
        if filename and media_type == 'document':
            obj['filename'] = filename
        return self._messages({'to': to, 'type': media_type, media_type: obj})

    def send_reaction(self, to, message_id, emoji):
        return self._messages({
            'to': to, 'type': 'reaction',
            'reaction': {'message_id': message_id, 'emoji': emoji or ''}})

    def send_template(self, to, name, lang, components=None):
        tmpl = {'name': name, 'language': {'code': lang or 'en'}}
        if components:
            tmpl['components'] = components
        return self._messages({'to': to, 'type': 'template', 'template': tmpl})

    def mark_read(self, message_id):
        return self._post(f"/{self.phone_number_id}/messages", json={
            'messaging_product': 'whatsapp', 'status': 'read',
            'message_id': message_id})

    def send_typing(self, message_id):
        """Show a typing indicator in the customer's chat (lasts up to 25s or
        until a message is sent). Rides on the mark-as-read call, so it also
        turns the customer's last message blue."""
        return self._post(f"/{self.phone_number_id}/messages", json={
            'messaging_product': 'whatsapp', 'status': 'read',
            'message_id': message_id,
            'typing_indicator': {'type': 'text'}})

    def send_location(self, to, latitude, longitude, name=None, address=None):
        loc = {'latitude': latitude, 'longitude': longitude}
        if name:
            loc['name'] = name
        if address:
            loc['address'] = address
        return self._messages({'to': to, 'type': 'location', 'location': loc})

    # ------------------------------------------------------------------
    # interactive messages (non-template): buttons / list / flow / carousel
    # Signatures mirror the QR client (BaileysApi.send_buttons/send_list) so
    # owa.message._send_interactive_template works with either transport.
    # ------------------------------------------------------------------
    _MIME_TO_MEDIA = (('image/', 'image'), ('video/', 'video'),
                      ('audio/', 'audio'), ('', 'document'))

    def _media_type_for_mime(self, mimetype):
        for prefix, mtype in self._MIME_TO_MEDIA:
            if (mimetype or '').startswith(prefix):
                return mtype
        return 'document'

    def _interactive_header(self, header):
        """Convert the renderer's header dict ({'type': 'text', 'text': ...}
        or media with base64) into Meta's interactive-header shape. Media
        headers are uploaded first and referenced by id."""
        if not header:
            return None
        htype = header.get('type')
        if htype == 'text':
            text = (header.get('text') or '').strip()
            return {'type': 'text', 'text': text[:60]} if text else None
        b64 = header.get('media_base64')
        if not b64:
            return None
        import base64 as _b64
        mimetype = header.get('mimetype') or 'application/octet-stream'
        mtype = self._media_type_for_mime(mimetype)
        if mtype == 'audio':          # audio can't be an interactive header
            return None
        media_id = self.upload_media(
            _b64.b64decode(b64), mimetype, header.get('filename') or 'file')
        return {'type': mtype, mtype: {'id': media_id}}

    @staticmethod
    def _reply_buttons(buttons):
        """[{'id','text'|'title'}] -> Meta reply-button list (max 3)."""
        out = []
        for btn in (buttons or [])[:3]:
            title = (btn.get('text') or btn.get('title') or '')[:20]
            out.append({'type': 'reply', 'reply': {
                'id': str(btn.get('id') or title)[:256], 'title': title}})
        return out

    def send_buttons(self, to, body, buttons, header=None, footer=None):
        """Interactive reply-button message (max 3 buttons)."""
        interactive = {
            'type': 'button',
            'body': {'text': (body or '')[:1024]},
            'action': {'buttons': self._reply_buttons(buttons)},
        }
        hdr = self._interactive_header(header)
        if hdr:
            interactive['header'] = hdr
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    def send_list(self, to, body, button_text, sections, footer=None):
        """Interactive list message (max 10 rows across all sections)."""
        norm_sections = []
        total_rows = 0
        for sec in (sections or []):
            rows = []
            for row in sec.get('rows') or []:
                if total_rows >= 10:
                    break
                rows.append({
                    'id': str(row.get('id') or row.get('rowId')
                              or row.get('title'))[:200],
                    'title': (row.get('title') or '')[:24],
                    'description': (row.get('description') or '')[:72],
                })
                total_rows += 1
            if rows:
                norm_sections.append({
                    'title': (sec.get('title') or '')[:24], 'rows': rows})
        interactive = {
            'type': 'list',
            'body': {'text': (body or '')[:4096]},
            'action': {'button': (button_text or 'Menu')[:20],
                       'sections': norm_sections},
        }
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    def send_flow(self, to, flow_id, flow_cta, body, screen=None,
                  flow_token=None, footer=None, draft=False):
        """Send a WhatsApp Flow (in-chat form) created in WhatsApp Manager.
        The customer's submission arrives on the messages webhook as an
        ``interactive.nfm_reply`` payload."""
        params = {
            'flow_message_version': '3',
            'flow_id': str(flow_id),
            'flow_cta': (flow_cta or 'Open')[:20],
        }
        if screen:
            params['flow_action'] = 'navigate'
            params['flow_action_payload'] = {'screen': screen}
        if flow_token:
            params['flow_token'] = flow_token
        if draft:
            params['mode'] = 'draft'
        interactive = {
            'type': 'flow',
            'body': {'text': (body or '')[:1024]},
            'action': {'name': 'flow', 'parameters': params},
        }
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    def send_carousel(self, to, body, cards):
        """Interactive media carousel (Feb-2026 Cloud API feature): up to 10
        swipeable cards, each with a media header, its own body text and up
        to 2 reply buttons.

        ``cards``: [{'media_id' | 'link', 'media_type': 'image'|'video',
                     'body': str, 'buttons': [{'id','text'}]}]
        """
        norm_cards = []
        for idx, card in enumerate((cards or [])[:10]):
            mtype = card.get('media_type') or 'image'
            media = ({'id': card['media_id']} if card.get('media_id')
                     else {'link': card.get('link')})
            entry = {
                'card_index': idx,
                'type': 'card',
                'header': {'type': mtype, mtype: media},
            }
            if card.get('body'):
                entry['body'] = {'text': card['body'][:160]}
            if card.get('buttons'):
                entry['action'] = {
                    'buttons': self._reply_buttons(card['buttons'])[:2]}
            norm_cards.append(entry)
        return self._messages({
            'to': to, 'type': 'interactive',
            'interactive': {
                'type': 'carousel',
                'body': {'text': (body or '')[:1024]},
                'action': {'cards': norm_cards},
            }})

    def send_cta_url(self, to, body, display_text, url, header=None,
                     footer=None):
        """Interactive Call-To-Action URL button (opens a link in-app)."""
        interactive = {
            'type': 'cta_url', 'body': {'text': (body or '')[:1024]},
            'action': {'name': 'cta_url',
                       'parameters': {'display_text': display_text[:20],
                                      'url': url}}}
        hdr = self._interactive_header(header)
        if hdr:
            interactive['header'] = hdr
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    def send_location_request(self, to, body):
        """Ask the customer to share their location (a tap-to-send button)."""
        return self._messages({
            'to': to, 'type': 'interactive',
            'interactive': {'type': 'location_request_message',
                            'body': {'text': (body or '')[:1024]},
                            'action': {'name': 'send_location'}}})

    def send_address(self, to, body, country='IN', values=None,
                     saved_addresses=None):
        """Request a shipping address (India/Singapore/Brazil address flow)."""
        params = {'country': country}
        if values:
            params['values'] = values
        if saved_addresses:
            params['saved_addresses'] = saved_addresses
        return self._messages({
            'to': to, 'type': 'interactive',
            'interactive': {'type': 'address_message',
                            'body': {'text': (body or '')[:1024]},
                            'action': {'name': 'address_message',
                                       'parameters': params}}})

    # -- catalog / product messages (require a connected Meta catalog) --
    def send_product(self, to, catalog_id, product_retailer_id, body=None,
                     footer=None):
        interactive = {
            'type': 'product',
            'action': {'catalog_id': str(catalog_id),
                       'product_retailer_id': str(product_retailer_id)}}
        if body:
            interactive['body'] = {'text': body[:1024]}
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    def send_product_list(self, to, catalog_id, header_text, body, sections,
                          footer=None):
        norm = [{'title': (s.get('title') or '')[:24],
                 'product_items': [{'product_retailer_id': str(p)}
                                   for p in (s.get('products') or [])]}
                for s in (sections or [])]
        interactive = {
            'type': 'product_list',
            'header': {'type': 'text', 'text': (header_text or '')[:60]},
            'body': {'text': (body or '')[:1024]},
            'action': {'catalog_id': str(catalog_id), 'sections': norm}}
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    def send_catalog(self, to, body, thumbnail_product_retailer_id=None,
                     footer=None):
        params = {}
        if thumbnail_product_retailer_id:
            params['thumbnail_product_retailer_id'] = str(
                thumbnail_product_retailer_id)
        interactive = {
            'type': 'catalog_message', 'body': {'text': (body or '')[:1024]},
            'action': {'name': 'catalog_message', 'parameters': params}}
        if footer:
            interactive['footer'] = {'text': footer[:60]}
        return self._messages({'to': to, 'type': 'interactive',
                               'interactive': interactive})

    # ------------------------------------------------------------------
    # blocklist (Cloud Block API)
    # ------------------------------------------------------------------
    def _delete(self, path, json=None, params=None):
        return self._handle(requests.delete(
            self.base + path, headers=self._headers(), json=json,
            params=params, timeout=DEFAULT_TIMEOUT))

    def block_users(self, wa_ids):
        """Block one or more customers (list of digit wa_ids)."""
        return self._post(f"/{self.phone_number_id}/block_users", json={
            'messaging_product': 'whatsapp',
            'block_users': [{'user': str(w).lstrip('+')} for w in wa_ids]})

    def unblock_users(self, wa_ids):
        return self._delete(f"/{self.phone_number_id}/block_users", json={
            'messaging_product': 'whatsapp',
            'block_users': [{'user': str(w).lstrip('+')} for w in wa_ids]})

    def get_blocked_users(self):
        data = self._get(f"/{self.phone_number_id}/block_users")
        return (data.get('data') or [{}])[0].get('block_users', []) \
            if isinstance(data.get('data'), list) else data.get('data', [])

    # -- compat shims so the sidecar-shaped console calls work on cloud too --
    def blocklist_update(self, jid, block=True):
        """Mirror BaileysApi.blocklist_update(jid, block) using the Cloud Block
        API — lets the shared block/unblock console code run on cloud too."""
        digits = ''.join(c for c in (jid or '').split('@')[0] if c.isdigit())
        if not digits:
            return {}
        return (self.block_users([digits]) if block
                else self.unblock_users([digits]))

    def blocklist_get(self):
        """Mirror the sidecar shape ``{'blocklist': {'JIDs': [...]}}``."""
        jids = []
        for row in self.get_blocked_users() or []:
            wa = (row.get('wa_id') or row.get('user')) \
                if isinstance(row, dict) else row
            digits = ''.join(c for c in str(wa or '') if c.isdigit())
            if digits:
                jids.append(f'{digits}@s.whatsapp.net')
        return {'blocklist': {'JIDs': jids}}

    # ------------------------------------------------------------------
    # business profile
    # ------------------------------------------------------------------
    _PROFILE_FIELDS = ('about,address,description,email,profile_picture_url,'
                       'websites,vertical')

    def get_business_profile(self):
        data = self._get(
            f"/{self.phone_number_id}/whatsapp_business_profile",
            params={'fields': self._PROFILE_FIELDS})
        rows = data.get('data') or [{}]
        return rows[0]

    def update_business_profile(self, **fields):
        """Update about/address/description/email/websites/vertical and/or
        ``profile_picture_handle`` (from ``upload_resumable_handle``)."""
        payload = {'messaging_product': 'whatsapp'}
        payload.update({k: v for k, v in fields.items() if v is not None})
        return self._post(
            f"/{self.phone_number_id}/whatsapp_business_profile", json=payload)

    def update_profile_picture(self, data_bytes, mime_type='image/jpeg'):
        """Set the business profile photo (resumable upload -> handle)."""
        handle = self.upload_resumable_handle(data_bytes, mime_type, 'profile')
        return self.update_business_profile(profile_picture_handle=handle)

    def fetch_business_profile_picture_b64(self):
        """Download the current business profile photo, base64-encoded
        (ready for a fields.Image), or None when none is set."""
        url = self.get_business_profile().get('profile_picture_url')
        if not url:
            return None
        import base64 as _b64
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400 or not resp.content:
            return None
        return _b64.b64encode(resp.content).decode('ascii')

    # ------------------------------------------------------------------
    # Cloud Groups API (2026; requires an Official Business Account)
    # ------------------------------------------------------------------
    def create_group(self, subject, description=None):
        payload = {'messaging_product': 'whatsapp', 'subject': subject}
        if description:
            payload['description'] = description
        return self._post(f"/{self.phone_number_id}/groups", json=payload)

    def get_groups(self):
        data = self._get(f"/{self.phone_number_id}/groups")
        return data.get('data', [])

    def get_group(self, group_id):
        return self._get(f"/{group_id}",
                         params={'fields': 'subject,description,'
                                           'participants,invite_link'})

    def update_group(self, group_id, subject=None, description=None):
        payload = {'messaging_product': 'whatsapp'}
        if subject:
            payload['subject'] = subject
        if description is not None:
            payload['description'] = description
        return self._post(f"/{group_id}", json=payload)

    def group_invite_link(self, group_id):
        data = self._get(f"/{group_id}/invite_link")
        return data.get('invite_link') or data.get('link')

    def remove_group_participants(self, group_id, wa_ids):
        return self._delete(f"/{group_id}/participants", json={
            'messaging_product': 'whatsapp',
            'participants': [{'user': str(w).lstrip('+')} for w in wa_ids]})

    # ------------------------------------------------------------------
    # media transfer
    # ------------------------------------------------------------------
    def upload_media(self, data_bytes, mime_type, filename='file'):
        """Upload media for SENDING in a message and return the Meta media id.

        Uses the messaging media endpoint (multipart POST
        ``/{phone_number_id}/media``). The returned id is what goes in a
        message's media object ``{'id': ...}``. (#F001/F011)

        NOTE: this is NOT the Resumable-Upload handle — that handle is only
        valid as a template ``example.header_handle`` and Meta rejects it as a
        message media id. Use ``upload_resumable_handle`` for template headers.
        """
        files = {
            'messaging_product': (None, 'whatsapp'),
            'type': (None, mime_type or 'application/octet-stream'),
            'file': (filename or 'file', data_bytes,
                     mime_type or 'application/octet-stream'),
        }
        # requests sets the multipart Content-Type (with boundary) itself when
        # ``files`` is given — do not override it.
        resp = requests.post(
            f"{self.base}/{self.phone_number_id}/media",
            headers=self._headers(), files=files, timeout=DEFAULT_TIMEOUT)
        return self._handle(resp).get('id')

    def upload_resumable_handle(self, data_bytes, mime_type, filename='file'):
        """Resumable upload; returns the Meta asset HANDLE (``h``).

        Valid ONLY as a template ``example.header_handle`` when creating
        message templates — never as a message media id (use ``upload_media``
        for sending). Kept for the media-header template flow."""
        sess = self._post(
            f"/{self.app_id}/uploads",
            params={'file_length': len(data_bytes), 'file_type': mime_type,
                    'file_name': filename})
        upload_id = sess.get('id')
        resp = requests.post(
            f"{self.base}/{upload_id}",
            headers={**self._headers(oauth=True), 'file_offset': '0'},
            data=data_bytes, timeout=DEFAULT_TIMEOUT)
        return self._handle(resp).get('h')

    def download_media(self, media_id):
        """Return (bytes, mime_type) for an inbound media id."""
        meta = self._get(f"/{media_id}")
        url = meta.get('url')
        resp = requests.get(url, headers=self._headers(),
                            timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.content, meta.get('mime_type')

    def delete_media(self, media_id):
        """Delete an uploaded media asset from Meta's servers."""
        return self._delete(f"/{media_id}")

    # ------------------------------------------------------------------
    # templates (management)
    # ------------------------------------------------------------------
    def submit_template(self, payload):
        return self._post(f"/{self.waba_id}/message_templates", json=payload)

    def sync_templates(self):
        data = self._get(
            f"/{self.waba_id}/message_templates",
            params={'fields': 'name,language,status,category,id,'
                    'quality_score,components', 'limit': 200})
        return data.get('data', [])

    def get_template(self, wa_template_uid):
        return self._get(
            f"/{wa_template_uid}",
            params={'fields': 'name,components,language,status,category,id,'
                    'quality_score'})

    def edit_template(self, wa_template_uid, components=None, category=None):
        """Edit an approved/rejected template's components or category."""
        payload = {}
        if components is not None:
            payload['components'] = components
        if category:
            payload['category'] = category
        return self._post(f"/{wa_template_uid}", json=payload)

    def delete_template(self, name, wa_template_uid=None):
        """Delete a template by name (all languages) or a specific id."""
        params = {'name': name}
        if wa_template_uid:
            params['hsm_id'] = wa_template_uid
        return self._delete(
            f"/{self.waba_id}/message_templates", params=params)

    # ------------------------------------------------------------------
    # phone-number & WABA management
    # ------------------------------------------------------------------
    def register_phone(self, pin):
        return self._post(f"/{self.phone_number_id}/register", json={
            'messaging_product': 'whatsapp', 'pin': str(pin)})

    def deregister_phone(self):
        return self._post(f"/{self.phone_number_id}/deregister", json={})

    def request_code(self, code_method='SMS', language='en_US'):
        return self._post(f"/{self.phone_number_id}/request_code", json={
            'code_method': code_method, 'language': language})

    def verify_code(self, code):
        return self._post(f"/{self.phone_number_id}/verify_code", json={
            'code': str(code)})

    def set_two_step_pin(self, pin):
        return self._post(f"/{self.phone_number_id}", json={'pin': str(pin)})

    def list_phone_numbers(self):
        data = self._get(f"/{self.waba_id}/phone_numbers", params={
            'fields': 'id,display_phone_number,verified_name,'
                      'quality_rating,code_verification_status'})
        return data.get('data', [])

    def get_waba_info(self):
        return self._get(f"/{self.waba_id}", params={
            'fields': 'id,name,currency,timezone_id,'
                      'message_template_namespace,account_review_status'})

    def subscribe_app(self):
        return self._post(f"/{self.waba_id}/subscribed_apps", json={})

    def list_subscribed_apps(self):
        return self._get(f"/{self.waba_id}/subscribed_apps").get('data', [])

    # ------------------------------------------------------------------
    # calling (signaling only — we never carry media)
    # ------------------------------------------------------------------
    def reject_call(self, call_id):
        """Reject a still-ringing inbound call via the WhatsApp Calling API.

        POST /{phone_number_id}/calls with action ``reject`` so the caller's
        phone stops ringing. Voice-only product; we never accept (no WebRTC
        media stack) — log, reject, and auto-reply only.
        """
        return self._post(
            f"/{self.phone_number_id}/calls",
            json={
                'messaging_product': 'whatsapp',
                'call_id': call_id,
                'action': 'reject',
            })
