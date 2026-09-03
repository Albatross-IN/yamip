// WhatsApp-style "Edit" in the Discuss message hover toolbar / ⋮ menu, so an
// agent can edit a sent WhatsApp message right from the conversation instead of
// hunting for it in the backend Messages form. Gated server-side via
// `message.owaEditable` (own, recently-sent, outbound, on a connected QR
// account — see mail_message._to_store) and opens the existing edit wizard
// pre-filled with the current text. v19 message-action API:
// condition/onSelected receive a {message, owner, store} object. (#wa-edit-action)
import { messageActionsRegistry } from "@mail/core/common/message_actions";
import { _t } from "@web/core/l10n/translation";

messageActionsRegistry.add("owa-edit-message", {
    condition: ({ message }) => Boolean(message?.owaEditable),
    icon: "fa fa-pencil",
    name: _t("Edit"),
    sequence: 15,
    onSelected: ({ message, owner, store }) => {
        owner?.optionsDropdown?.close?.();
        store.env.services.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Edit WhatsApp Message"),
            res_model: "owa.message.edit.wizard",
            view_mode: "form",
            views: [[false, "form"]],
            target: "new",
            context: { default_owa_message_id: message.owaMessageId },
        });
    },
});
