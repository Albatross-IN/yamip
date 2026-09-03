import { registerThreadAction } from "@mail/core/common/thread_actions";

import { _t } from "@web/core/l10n/translation";

// Module-scoped id — Odoo's native `whatsapp` (Meta Cloud API) module
// also registers a `view-contact` thread action; using the same id would
// throw `it already exists` on installs that have both modules enabled.
registerThreadAction("owa-view-contact", {
    condition: ({ owner, thread }) =>
        thread?.channel_type === "whatsapp" &&
        thread.whatsapp_partner_id &&
        // optional-chain so an undefined owner never crashes the ChatWindow
        // render (defensive; harmless on v19 where owner is defined).
        !owner?.isDiscussSidebarChannelActions,
    open: ({ store, thread }) => {
        if (store.env.isSmall) {
            store?.ChatWindow.get({ thread }).fold();
        } else {
            thread.openChatWindow({ focus: true });
        }
        store.env.services.action.doAction({
            type: "ir.actions.act_window",
            res_model: "res.partner",
            views: [[false, "form"]],
            res_id: thread.whatsapp_partner_id.id,
        });
    },
    icon: "fa fa-fw fa-address-book",
    name: _t("View Contact"),
    sequenceGroup: 1,
});

// "Assign to me" — lets an agent claim the open WhatsApp conversation right
// from the chat header (calls discuss.channel.action_assign_to_me, which is
// guarded so a non-admin can only assign it to themselves). Pairs with the
// WhatsApp > Conversations management view for bulk/admin reassignment.
registerThreadAction("owa-assign-to-me", {
    condition: ({ owner, thread }) =>
        thread?.channel_type === "whatsapp" &&
        !owner?.isDiscussSidebarChannelActions,
    open: async ({ store, thread }) => {
        await store.env.services.orm.call(
            "discuss.channel", "action_assign_to_me", [[thread.id]]);
        store.env.services.notification.add(
            _t("Conversation assigned to you."), { type: "success" });
    },
    icon: "fa fa-fw fa-user-plus",
    name: _t("Assign to me"),
    sequenceGroup: 1,
});
