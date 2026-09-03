/** @odoo-module **/

import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

/**
 * List controller whose standard "New" button opens the model's create
 * *wizard* (its ``action_open_create_wizard`` method) instead of inline-creating
 * a blank record.
 *
 * WhatsApp groups / communities / newsletters can only be created through the
 * sidecar, so a plain inline record would be an orphan that never exists on
 * WhatsApp — which is why creation has always gone through a wizard. This lets
 * the familiar, always-visible list "New" button drive that wizard, so the
 * separate "New X" menu items next to "X" are no longer needed. The button is
 * naturally admin-gated by the model's create access right. (#wa-create-button)
 */
export class OwaWizardCreateListController extends ListController {
    async createRecord() {
        // action_open_create_wizard is an @api.model method (takes only self),
        // so call it with empty args — passing [[]] would add a spurious
        // positional arg and raise a TypeError. (#wa-create-button)
        const action = await this.orm.call(
            this.props.resModel,
            "action_open_create_wizard",
            [],
            { context: this.props.context }
        );
        return this.actionService.doAction(action, {
            onClose: () => this.model.root.load(),
        });
    }
}

registry.category("views").add("owa_wizard_create_list", {
    ...listView,
    Controller: OwaWizardCreateListController,
});
