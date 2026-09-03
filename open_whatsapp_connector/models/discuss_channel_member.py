from datetime import datetime, timedelta

from odoo import api, models
from odoo.fields import Domain
from odoo.addons.mail.tools.discuss import Store


class DiscussChannelMember(models.Model):
    _inherit = 'discuss.channel.member'

    @api.autovacuum
    def _gc_unpin_whatsapp_channels(self):
        """Declutter Discuss by unpinning ONE-TO-ONE WhatsApp chats that have
        been read and left idle for a long time (30 days).

        Groups, communities and channels are never auto-unpinned: like WhatsApp
        Web, a conversation you belong to stays in the sidebar until you leave
        it, so your WhatsApp groups and communities don't disappear from under
        their account. Only read, long-idle 1-to-1 chats are collapsed away."""
        idle_since = datetime.now() - timedelta(days=30)
        members = self.search(Domain.AND([
            [("is_pinned", "=", True)],
            [("channel_id.channel_type", "=", "whatsapp")],
            # Only 1-to-1 chats are eligible; keep groups / communities /
            # channels pinned so they stay visible under each account.
            [("channel_id.whatsapp_kind", "=", "dm")],
            Domain.OR([
                [("last_seen_dt", "<", idle_since)],
                [
                    ("last_seen_dt", "=", False),
                    ("channel_id.create_date", "<=", idle_since),
                ],
            ]),
        ]), limit=1000)
        # Never collapse a chat that still has unread messages.
        members_to_unpin = members.filtered(
            lambda m: m.message_unread_counter == 0
        )
        members_to_unpin.unpin_dt = datetime.now()
        for member in members_to_unpin:
            Store(bus_channel=member._bus_channel()).add(
                member.channel_id, {"close_chat_window": True}
            ).bus_send()
