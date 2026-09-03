import { Message } from "@mail/core/common/message_model";
import { patch } from "@web/core/utils/patch";

/** @type {import("models").Message} */
const messagePatch = {
    setup() {
        super.setup();
        // Keep whatsappStatus a tracked field so live status-bus updates
        // (sent -> delivered -> read) re-render the ticks. The value is ingested
        // by the base Store from the server's _to_store extras
        // (whatsappStatus = owa.message.state).
        if (this.whatsappStatus === undefined) {
            this.whatsappStatus = false;
        }
        // Tracked so the Discuss "Edit" message action shows/hides reactively
        // (ingested from the server _to_store extras). (#wa-edit-action)
        if (this.owaEditable === undefined) {
            this.owaEditable = false;
        }
        if (this.owaMessageId === undefined) {
            this.owaMessageId = false;
        }
    },
    get editable() {
        if (this.thread?.channel_type === "whatsapp") {
            return false;
        }
        return super.editable;
    },
    /** @override */
    canReplyTo(thread) {
        return super.canReplyTo(thread) && !this.thread?.composer?.threadExpired;
    },
};
patch(Message.prototype, messagePatch);
