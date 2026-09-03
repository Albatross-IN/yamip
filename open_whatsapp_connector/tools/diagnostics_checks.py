"""Built-in sidecar setup diagnostics, registered on the @register_diagnostic
seam (tools/diagnostics_registry.py) and surfaced by the account's
``Run Diagnostics`` button.

Each check answers one question a QR connection depends on — is the sidecar
path configured, is it built, is Node.js available — and returns actionable
remediation text so a non-developer admin knows exactly what to fix. Cloud API
accounts have no sidecar, so the checks report N/A for them.
"""
import os
import shutil

from odoo.tools import config

from . import diagnostics_registry


def _sidecar_path(account):
    return account.env['ir.config_parameter'].sudo().get_param(
        'open_whatsapp_connector.sidecar_path', '')


@diagnostics_registry.register_diagnostic("setup.node")
def _check_node(account):
    if account.connection_type != 'qr':
        return ("Node.js on PATH", 'ok', 'N/A (Cloud API account)')
    node = shutil.which('node')
    if node:
        return ("Node.js on PATH", 'ok', node)
    return ("Node.js on PATH", 'error',
            "Not found - install Node.js 22+ from nodejs.org on the Odoo server.")


@diagnostics_registry.register_diagnostic("setup.sidecar_path")
def _check_sidecar_path(account):
    if account.connection_type != 'qr':
        return ("Sidecar path", 'ok', 'N/A (Cloud API account)')
    path = _sidecar_path(account)
    if not path:
        return ("Sidecar path", 'error',
                "Blank - set it in Settings > Open WhatsApp Connector.")
    if not os.path.isdir(path):
        return ("Sidecar path", 'error', "Folder does not exist: %s" % path)
    return ("Sidecar path", 'ok', path)


@diagnostics_registry.register_diagnostic("setup.sidecar_build")
def _check_sidecar_build(account):
    if account.connection_type != 'qr':
        return ("Sidecar build", 'ok', 'N/A (Cloud API account)')
    path = _sidecar_path(account)
    if not path:
        return ("Sidecar build", 'warn', "Set the sidecar path first.")
    index_js = os.path.join(path, 'dist', 'index.js')
    if os.path.isfile(index_js):
        return ("Sidecar build", 'ok', 'dist/index.js present')
    return ("Sidecar build", 'error',
            "Not built - run sidecar/setup.bat (Windows) or setup.sh (Linux).")


@diagnostics_registry.register_diagnostic("realtime.bus")
def _check_realtime_bus(account):
    """Live Discuss updates ride Odoo's websocket bus. The connector posts
    inbound messages correctly through message_post, so "I must refresh to see
    new messages" is almost always a server/proxy issue, not connector code.
    Surface the relevant server config so an admin knows where to look — the
    classic cause is a reverse proxy that doesn't forward /websocket."""
    workers = config.get('workers') or 0
    proxy_mode = bool(config.get('proxy_mode'))
    gevent_port = config.get('gevent_port') or config.get('longpolling_port') or 8072
    label = "Real-time chat (websocket)"
    if proxy_mode and workers > 0:
        return (label, 'warn',
                "Multi-worker behind a proxy: make sure the reverse proxy forwards "
                "/websocket (and /longpolling) to gevent port %s, otherwise new "
                "messages only appear after a manual page refresh." % gevent_port)
    if workers > 0:
        return (label, 'ok',
                "Multi-worker mode (%s workers, gevent port %s). If new messages "
                "need a refresh, forward /websocket to that port at your proxy."
                % (workers, gevent_port))
    return (label, 'ok',
            "Threaded mode (workers=0): live updates are served on the main HTTP "
            "port. Fine for testing; use multi-worker plus a proxied /websocket "
            "route in production.")
