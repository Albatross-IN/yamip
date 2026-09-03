import { Message } from "@mail/core/common/message";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(Message.prototype, {
    get showSeenIndicator() {
        return super.showSeenIndicator && this.message.whatsappStatus !== "error";
    },
    /**
     * @param {MouseEvent} ev
     */
    async onClick(ev) {
        const id = Number(ev.target.dataset.oeId);
        if (ev.target.closest(".o_whatsapp_channel_redirect")) {
            ev.preventDefault();
            let thread = await this.store.Thread.getOrFetch({ model: "discuss.channel", id });
            if (!thread?.hasSelfAsMember) {
                await this.env.services.orm.call("discuss.channel", "add_members", [[id]], {
                    partner_ids: [this.store.self.id],
                });
                thread = await this.store.Thread.getOrFetch({ model: "discuss.channel", id });
            }
            thread.open();
            return;
        }
        super.onClick(ev);
    },

    getWhatsappStatusClass() {
        // WhatsApp tick colours: pending/sent/delivered = grey, read = blue,
        // failures = red. (Single ✓ = sent, double ✓✓ = delivered, blue ✓✓ = read;
        // the glyph itself is chosen in the template.)
        const statusClasses = {
            outgoing: "text-muted",
            sent: "text-muted",
            delivered: "text-muted",
            read: "text-info",
            replied: "text-info",
            received: "text-muted",
            error: "text-danger",
            bounced: "text-danger",
            cancel: "text-danger",
        };
        return statusClasses[this.message.whatsappStatus] || "text-muted";
    },

    getWhatsappStatusStyle() {
        // Force the authentic WhatsApp read-blue on the "read"/"replied" tick
        // via an inline style. Bootstrap's `.text-info` (set by
        // getWhatsappStatusClass) is re-themed by Odoo to a dark teal
        // (~#0180A5) that is visually indistinguishable from the grey delivered
        // tick, so a read message never looks "blue". `.text-info` carries
        // !important, so the inline colour must also be !important to win the
        // cascade. (#read-tick-blue)
        const status = this.message.whatsappStatus;
        if (status === "read" || status === "replied") {
            return "color:#53bdeb !important";
        }
        return "";
    },

    getWhatsappStatusTitle() {
        const statusTitles = {
            outgoing: _t("The message is being processed."),
            sent: _t("The message has been sent."),
            delivered: _t("The message has been successfully delivered."),
            read: _t("The message has been read by the recipient."),
            replied: _t("The recipient has replied to the message."),
            received: _t("The message has been successfully received."),
            error: _t("There was an issue sending this message."),
            bounced: _t("The message has been bounced."),
            cancel: _t("The message has been canceled."),
        };
        return (
            statusTitles[this.message.whatsappStatus] ||
            _t("The status of this message is currently unknown.")
        );
    },
});
