import { patch } from "@web/core/utils/patch";
import { DiscussAppCategory } from "@mail/discuss/core/public_web/discuss_app_category_model";
import { compareDatetime } from "@mail/utils/common/misc";

patch(DiscussAppCategory.prototype, {
    /**
     * @param {import("models").Thread} t1
     * @param {import("models").Thread} t2
     */
    sortThreads(t1, t2) {
        if (this.id?.startsWith("whatsapp")) {
            // WhatsApp-Web-style ordering: contacts currently online (the
            // reactive owaOnline flag fed by the owa_presence bus) float to the
            // top; the rest fall below sorted by most-recent activity.
            const online1 = t1.owaOnline ? 1 : 0;
            const online2 = t2.owaOnline ? 1 : 0;
            if (online1 !== online2) {
                return online2 - online1; // online first
            }
            return compareDatetime(t2.lastInterestDt, t1.lastInterestDt) || t2.id - t1.id;
        }
        return super.sortThreads(t1, t2);
    },
});
