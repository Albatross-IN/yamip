import { ComposerAction, registerComposerAction } from "@mail/core/common/composer_actions";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";

// Module-scoped key — `revive-whatsapp-conversation` is also registered by
// Odoo's native `whatsapp` (Meta Cloud API) module; using the same id would
// throw `it already exists` and crash the OWL boot for any user who has
// both modules installed.
registerComposerAction("owa-revive-whatsapp-conversation", {
    condition: ({ composer, owner }) =>
        composer.thread?.channel_type === "whatsapp" && !owner.state.active,
    icon: "fa fa-whatsapp",
    name: _t("Revive WhatsApp Conversation"),
    onSelected: ({ owner }) => owner.onclickWhatsAppChat(),
    sequenceQuick: 10,
});

// Choose which connected WhatsApp account this conversation replies from
// (multi-account support). Only shown for this module's WhatsApp threads.
registerComposerAction("owa-select-reply-account", {
    condition: ({ owner }) => owner.isOwaWhatsappThread,
    icon: "fa fa-server",
    name: _t("Reply account"),
    onSelected: ({ owner }) => owner.openOwaAccountPicker(),
    sequenceQuick: 20,
});

patch(ComposerAction.prototype, {
    _condition({ composer, owner }) {
        if (
            ["upload-files", "voice-start"].includes(this.id) &&
            composer.targetThread?.channel_type === "whatsapp" &&
            (composer.attachments.length > 0 || owner.voiceRecorder?.recording)
        ) {
            return false;
        }
        return super._condition(...arguments);
    },
    _disabledCondition({ composer, owner }) {
        const inactiveActions = ["owa-revive-whatsapp-conversation", "more-actions"];
        if (
            composer.targetThread?.channel_type === "whatsapp" &&
            owner.state &&
            !owner.state.active &&
            !inactiveActions.includes(this.id)
        ) {
            return true;
        }
        return super._disabledCondition(...arguments);
    },
});
