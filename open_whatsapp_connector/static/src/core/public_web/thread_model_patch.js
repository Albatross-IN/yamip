import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    // Map a WhatsApp conversation to its grouped sidebar section by kind.
    _owaWhatsappCategory(discuss, kind) {
        switch (kind) {
            case "group":
                return discuss.whatsapp_group;
            case "community":
                return discuss.whatsapp_community;
            case "channel":
                return discuss.whatsapp_channel;
            default:
                return discuss.whatsapp;
        }
    },
    _computeDiscussAppCategory() {
        if (this.channel_type !== "whatsapp") {
            return super._computeDiscussAppCategory();
        }
        // With several WhatsApp accounts, every account gets its OWN sidebar
        // sections (per kind) so two accounts' conversations are never mixed.
        // A single account keeps the familiar shared sections.
        // (#per-account-sections)
        const account = this.owa_account_id;
        const nAccounts = Object.keys(
            this.store["owa.account"]?.records ?? {}
        ).length;
        if (account && nAccounts > 1) {
            return account._categoryFor(this.whatsapp_kind);
        }
        return this._owaWhatsappCategory(this.store.discuss, this.whatsapp_kind);
    },
});
