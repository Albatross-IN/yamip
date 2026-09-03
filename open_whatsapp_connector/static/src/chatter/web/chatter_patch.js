import { Chatter } from "@mail/chatter/web_portal/chatter";
import { patch } from "@web/core/utils/patch";
import { useRef, useState } from "@odoo/owl";

patch(Chatter.prototype, {
    setup() {
        super.setup();
        // Holds the WhatsApp conversation (a discuss.channel Thread) rendered
        // INLINE inside the chatter when the 'WhatsApp' button is toggled on.
        // Stays in the chatter — never opens a compose dialog nor redirects to
        // the Discuss app. (#wa-open-chat)
        this.owaState = useState({ thread: null });
        // Scroll container for the inline conversation; passed to the Thread so
        // it auto-scrolls to the newest message (at the bottom).
        this.owaScroller = useRef("owaScroller");
    },
    // Toggle the inline WhatsApp conversation for this record's contact.
    async toggleWhatsapp() {
        if (this.owaState.thread) {
            this.owaState.thread = null; // toggle back to the record log
            return;
        }
        const reveal = async (recordThread) => {
            const channelId = await this.env.services.orm.call(
                recordThread.model,
                "action_owa_get_channel_id",
                [[recordThread.id]]
            );
            if (!channelId) {
                return;
            }
            const channel = await this.store.Thread.getOrFetch({
                model: "discuss.channel",
                id: channelId,
            });
            if (channel) {
                this.owaState.thread = channel;
                // The embedded Thread isn't the "active" Discuss thread, so load
                // its recent messages explicitly (they render reactively).
                channel.fetchNewMessages();
            }
        };
        if (this.state.thread.id) {
            await reveal(this.state.thread);
        } else {
            // New, unsaved record: save it first so it has an id + contact.
            this.onThreadCreated = reveal;
            this.props.saveRecord?.();
        }
    },
});
