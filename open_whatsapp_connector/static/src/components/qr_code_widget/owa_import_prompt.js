/** @odoo-module **/

import { Component, useEffect, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Invisible, ALWAYS-MOUNTED widget on the WhatsApp account form that auto-opens
 * the "Import WhatsApp Data" dialog once after a fresh QR connection.
 *
 * Why not do this in the QR widget: the QR widget lives inside a div that is
 * only rendered while session_state is qr_pending/connecting, so OWL UNMOUNTS it
 * the instant the account becomes connected — it can never reliably fire the
 * post-connect prompt (the JS latch dies with the component). This widget is
 * rendered unconditionally and reacts to the server-set owa.account
 * .import_prompt_pending flag, so the dialog pops on EVERY fresh scan and
 * survives form reloads, remounts and missed bus pushes. After opening it clears
 * the flag (server-side) so it never re-pops on a plain reconnect or reload;
 * the server re-arms the flag on the next fresh QR pairing. (#import_prompt_flag)
 */
export class OwaImportPromptWidget extends Component {
    static template = xml`<span class="o_owa_import_prompt" style="display:none"/>`;
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this._opening = false;
        // Runs once after mount (covers opening the form when it is already
        // connected + flagged) and again whenever either value changes (covers
        // the live connecting -> connected transition while the form is open).
        useEffect(
            () => {
                this._maybePrompt();
            },
            () => [
                this.props.record.data.session_state,
                this.props.record.data.import_prompt_pending,
            ],
        );
    }

    _maybePrompt() {
        const data = this.props.record.data;
        const resId = this.props.record.resId;
        if (!resId || this._opening) {
            return;
        }
        if (data.session_state !== "connected" || !data.import_prompt_pending) {
            return;
        }
        this._opening = true;
        // Clear the server flag FIRST so a concurrent observer or a later form
        // reload cannot re-open the dialog. The server re-arms it on the next
        // fresh QR connection.
        this.orm
            .write("owa.account", [resId], { import_prompt_pending: false })
            .catch(() => {});
        this.action
            .doAction({
                type: "ir.actions.act_window",
                name: "Import WhatsApp Data",
                res_model: "owa.import.wizard",
                views: [[false, "form"]],
                target: "new",
                // Pre-check "Chat history": the scan that just completed already
                // captured the history (armed at Connect), so it imports in one
                // click with no second scan.
                context: {
                    default_wa_account_id: resId,
                    default_import_history: true,
                },
            })
            .catch((e) => console.warn("WhatsApp import auto-prompt failed:", e))
            .finally(() => {
                this._opening = false;
            });
    }
}

registry.category("fields").add("wa_import_prompt", {
    component: OwaImportPromptWidget,
    supportedTypes: ["boolean"],
});
