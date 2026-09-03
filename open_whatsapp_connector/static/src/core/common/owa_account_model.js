import { Record, fields } from "@mail/core/common/record";

import { _t } from "@web/core/l10n/translation";

const KIND_META = {
    chat: { icon: "fa fa-whatsapp", seqOff: 0 },
    group: { icon: "fa fa-users", seqOff: 1 },
    community: { icon: "fa fa-sitemap", seqOff: 2 },
    channel: { icon: "fa fa-bullhorn", seqOff: 3 },
};

export class OwaAccount extends Record {
    static id = "id";
    static _name = "owa.account";

    /** @type {number} */
    id;
    /** @type {string} */
    name;

    setup() {
        super.setup(...arguments);
        // One sidebar section per (account, kind) so two WhatsApp accounts'
        // conversations are never mixed together — the same dynamic-category
        // pattern the stock livechat app uses. Sections hide when empty and
        // only kick in when more than one account is present (see
        // thread_model_patch in core/public_web). (#per-account-sections)
        this.appCategoryChat = fields.One("DiscussAppCategory", {
            compute() {
                return this._categoryData("chat");
            },
            eager: true,
        });
        this.appCategoryGroup = fields.One("DiscussAppCategory", {
            compute() {
                return this._categoryData("group");
            },
            eager: true,
        });
        this.appCategoryCommunity = fields.One("DiscussAppCategory", {
            compute() {
                return this._categoryData("community");
            },
            eager: true,
        });
        this.appCategoryChannel = fields.One("DiscussAppCategory", {
            compute() {
                return this._categoryData("channel");
            },
            eager: true,
        });
    }

    _kindLabel(kind) {
        switch (kind) {
            case "group":
                return ` · ${_t("Groups")}`;
            case "community":
                return ` · ${_t("Communities")}`;
            case "channel":
                return ` · ${_t("Channels")}`;
            default:
                return "";
        }
    }

    _categoryData(kind) {
        const meta = KIND_META[kind] || KIND_META.chat;
        // Account-major ordering: all of account A's sections, then B's.
        const ids = Object.values(this.store["owa.account"]?.records ?? {})
            .map((r) => r.id)
            .sort((a, b) => a - b);
        const idx = Math.max(0, ids.indexOf(this.id));
        return {
            extraClass: "o-mail-DiscussSidebarCategory-whatsapp",
            hideWhenEmpty: true,
            icon: meta.icon,
            id: `whatsapp_acc_${this.id}_${kind}`,
            name: `${this.name || _t("WhatsApp")}${this._kindLabel(kind)}`,
            sequence: 30 + idx * 4 + meta.seqOff,
        };
    }

    _categoryFor(kind) {
        switch (kind) {
            case "group":
                return this.appCategoryGroup;
            case "community":
                return this.appCategoryCommunity;
            case "channel":
                return this.appCategoryChannel;
            default:
                return this.appCategoryChat;
        }
    }
}

OwaAccount.register();
