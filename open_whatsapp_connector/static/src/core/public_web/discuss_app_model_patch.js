import { fields } from "@mail/core/common/record";
import { DiscussApp } from "@mail/core/public_web/discuss_app_model";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(DiscussApp.prototype, {
    setup(env) {
        super.setup(...arguments);
        // WhatsApp is split into grouped sections so Chats / Groups /
        // Communities / Channels are easy to identify. Each conversation is
        // filed into one by its `whatsapp_kind` (see thread_model_patch).
        this.whatsapp = fields.One("DiscussAppCategory", {
            compute() {
                return {
                    addTitle: _t("Search WhatsApp Chat"),
                    extraClass: "o-mail-DiscussSidebarCategory-whatsapp",
                    hideWhenEmpty: true,
                    icon: "fa fa-whatsapp",
                    id: "whatsapp",
                    name: _t("WhatsApp Chats"),
                    sequence: 20,
                    serverStateKey: "is_discuss_sidebar_category_whatsapp_open",
                };
            },
            eager: true,
        });
        this.whatsapp_group = fields.One("DiscussAppCategory", {
            compute() {
                return {
                    addTitle: _t("Search WhatsApp Group"),
                    extraClass: "o-mail-DiscussSidebarCategory-whatsapp",
                    hideWhenEmpty: true,
                    icon: "fa fa-users",
                    id: "whatsapp_group",
                    name: _t("WhatsApp Groups"),
                    sequence: 21,
                    serverStateKey: "is_discuss_sidebar_category_whatsapp_group_open",
                };
            },
            eager: true,
        });
        this.whatsapp_community = fields.One("DiscussAppCategory", {
            compute() {
                return {
                    addTitle: _t("Search WhatsApp Community"),
                    extraClass: "o-mail-DiscussSidebarCategory-whatsapp",
                    hideWhenEmpty: true,
                    icon: "fa fa-sitemap",
                    id: "whatsapp_community",
                    name: _t("WhatsApp Communities"),
                    sequence: 22,
                    serverStateKey: "is_discuss_sidebar_category_whatsapp_community_open",
                };
            },
            eager: true,
        });
        this.whatsapp_channel = fields.One("DiscussAppCategory", {
            compute() {
                return {
                    addTitle: _t("Search WhatsApp Channel"),
                    extraClass: "o-mail-DiscussSidebarCategory-whatsapp",
                    hideWhenEmpty: true,
                    icon: "fa fa-bullhorn",
                    id: "whatsapp_channel",
                    name: _t("WhatsApp Channels"),
                    sequence: 23,
                    serverStateKey: "is_discuss_sidebar_category_whatsapp_channel_open",
                };
            },
            eager: true,
        });
    },
});
