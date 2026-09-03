import { fields } from "@mail/core/common/record";
import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";
import { deserializeDateTime } from "@web/core/l10n/dates";

import { toRaw } from "@odoo/owl";

patch(Thread.prototype, {
    setup() {
        super.setup();
        this.owa_account_id = fields.One("owa.account");
        // Phase B3 — transient presence flags driven by the
        // `owa_presence` bus event. Not persisted; cleared on reload.
        this.owaOnline = false;
        this.owaTyping = false;
        // Conversation type (dm/group/community/channel) for the grouped
        // WhatsApp sidebar sections; populated from `_to_store`.
        this.whatsapp_kind = "";
    },
    get importantCounter() {
        if (this.channel_type === "whatsapp") {
            return this.self_member_id?.message_unread_counter || this.message_needaction_counter;
        }
        return super.importantCounter;
    },
    get autoOpenChatWindowOnNewMessage() {
        return this.channel_type === "whatsapp" || super.autoOpenChatWindowOnNewMessage;
    },
    get canLeave() {
        return this.channel_type !== "whatsapp" && super.canLeave;
    },
    get allowedToUnpinChannelTypes() {
        return [...super.allowedToUnpinChannelTypes, "whatsapp"];
    },
    get avatarUrl() {
        if (this.channel_type === "whatsapp" && this.correspondent?.persona?.avatarUrl) {
            return this.correspondent.persona.avatarUrl;
        }
        return super.avatarUrl;
    },

    get isChatChannel() {
        return this.channel_type === "whatsapp" || super.isChatChannel;
    },

    get displayName() {
        // WhatsApp-Web-style naming for the Discuss sidebar/header: prefer the
        // contact's name when it is a *real* name — either set manually in
        // Contacts or the WhatsApp display name (pushName) captured on inbound
        // (res.partner._find_or_create_from_wa_number keeps a curated name and
        // upgrades the +digits placeholder from the pushName). A name made up of
        // only phone characters is treated as "no real name yet", so we fall
        // back to the default (the bare number on the channel).
        if (this.channel_type === "whatsapp") {
            const partnerName = this.whatsapp_partner_id?.name;
            if (partnerName && !/^[\d\s()+.\-]*$/.test(partnerName)) {
                return partnerName;
            }
        }
        return super.displayName;
    },

    get whatsappChannelValidUntilDatetime() {
        if (!this.whatsapp_channel_valid_until) {
            return undefined;
        }
        return toRaw(deserializeDateTime(this.whatsapp_channel_valid_until));
    },
});
