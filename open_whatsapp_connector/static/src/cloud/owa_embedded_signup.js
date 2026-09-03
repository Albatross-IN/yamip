/** @odoo-module */
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

// A view widget (<widget name="owa_embedded_signup"/>) that runs Meta's
// Embedded Signup: loads the Facebook JS SDK, opens the signup popup, captures
// the returned auth code + phone_number_id + waba_id, and hands them to the
// server to mint an access token and configure this account. Needs the global
// Meta App config (Settings) and an HTTPS host — Meta blocks the SDK on http.
export class OwaEmbeddedSignup extends Component {
    static template = "open_whatsapp_connector.OwaEmbeddedSignup";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ busy: false });
        this._esData = null; // {phone_number_id, waba_id} from the popup message
    }

    _loadSdk(appId, version) {
        return new Promise((resolve, reject) => {
            if (window.FB) {
                resolve();
                return;
            }
            window.fbAsyncInit = () => {
                window.FB.init({ appId, cookie: true, xfbml: false, version });
                resolve();
            };
            const s = document.createElement("script");
            s.src = "https://connect.facebook.net/en_US/sdk.js";
            s.async = true;
            s.defer = true;
            s.crossOrigin = "anonymous";
            s.onerror = () => reject(new Error("sdk"));
            document.body.appendChild(s);
        });
    }

    _onMessage(ev) {
        // Only trust messages from Facebook's own origins. Match the hostname
        // EXACTLY (apex or a real *.facebook.com subdomain) — a suffix test like
        // /facebook\.com$/ would also accept "evilfacebook.com". (#es-origin)
        let host;
        try {
            host = new URL(ev.origin).hostname;
        } catch {
            return;
        }
        if (host !== "facebook.com" && !host.endsWith(".facebook.com")) {
            return;
        }
        try {
            const data = JSON.parse(ev.data);
            if (data.type === "WA_EMBEDDED_SIGNUP" && data.event === "FINISH") {
                this._esData = {
                    phone_number_id: data.data.phone_number_id,
                    waba_id: data.data.waba_id,
                };
            }
        } catch {
            /* non-JSON message from FB — ignore */
        }
    }

    async onConnect() {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const cfg = await this.orm.call(
                "owa.account", "get_embedded_signup_config", []
            );
            if (!cfg.app_id || !cfg.config_id) {
                this.notification.add(
                    _t("Set the Meta App ID and Embedded Signup Config ID in "
                        + "Settings → Open WhatsApp Connector first."),
                    { type: "warning" }
                );
                return;
            }
            await this._loadSdk(cfg.app_id, cfg.version);
            const listener = (ev) => this._onMessage(ev);
            window.addEventListener("message", listener);
            const response = await new Promise((resolve) => {
                window.FB.login(resolve, {
                    config_id: cfg.config_id,
                    response_type: "code",
                    override_default_response_type: true,
                    extras: { setup: {}, featureType: "", sessionInfoVersion: "3" },
                });
            });
            window.removeEventListener("message", listener);
            const code = response.authResponse && response.authResponse.code;
            if (!code || !this._esData) {
                this.notification.add(
                    _t("Signup was cancelled or did not return a number."),
                    { type: "warning" }
                );
                return;
            }
            const recId = this.props.record && this.props.record.resId;
            const res = await rpc("/open_whatsapp_connector/cloud/embedded_signup", {
                code,
                phone_number_id: this._esData.phone_number_id,
                waba_id: this._esData.waba_id,
                account_id: recId || false,
            });
            this.notification.add(
                _t("Connected %s (%s).", res.name, res.state),
                { type: "success" }
            );
            await this.action.doAction({ type: "ir.actions.act_window_close" });
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "owa.account",
                res_id: res.account_id,
                views: [[false, "form"]],
                target: "current",
            });
        } catch (e) {
            this.notification.add(
                _t("Facebook signup failed: %s", e.message || e),
                { type: "danger" }
            );
        } finally {
            this.state.busy = false;
        }
    }
}

registry.category("view_widgets").add("owa_embedded_signup", {
    component: OwaEmbeddedSignup,
});
