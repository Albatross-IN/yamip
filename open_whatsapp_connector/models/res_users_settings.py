from odoo import fields, models


class ResUsersSettings(models.Model):
    _inherit = 'res.users.settings'

    # Backs the WhatsApp Discuss sidebar category open/closed state. The OWL
    # DiscussAppCategory for WhatsApp uses
    # serverStateKey='is_discuss_sidebar_category_whatsapp_open'; without a real
    # field, set_res_users_settings drops the toggle ("if setting in
    # self._fields") and the category renders collapsed on every page load with
    # the open/closed state never persisting. (#F141)
    is_discuss_sidebar_category_whatsapp_open = fields.Boolean(
        string="Is discuss sidebar category whatsapp open?", default=True)
    # The WhatsApp sidebar is split into grouped sections (Chats / Groups /
    # Communities / Channels); each collapsible section needs its own backing
    # field so its open/closed state persists across reloads (same reason as
    # the original whatsapp field above). (#wa-sidebar-sections)
    is_discuss_sidebar_category_whatsapp_group_open = fields.Boolean(
        string="Is discuss sidebar category whatsapp groups open?", default=True)
    is_discuss_sidebar_category_whatsapp_community_open = fields.Boolean(
        string="Is discuss sidebar category whatsapp communities open?", default=True)
    is_discuss_sidebar_category_whatsapp_channel_open = fields.Boolean(
        string="Is discuss sidebar category whatsapp channels open?", default=True)
