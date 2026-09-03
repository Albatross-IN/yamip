/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

// ── Step type metadata (icon / colour / label / has single output) ─────────
const STEP_TYPES = {
    message:       { label: _t("Message"),   icon: "fa-comment",         color: "#0891b2", out: "single" },
    menu:          { label: _t("Menu"),      icon: "fa-list-ul",         color: "#7c3aed", out: "options" },
    question:      { label: _t("Question"),  icon: "fa-question-circle", color: "#d97706", out: "single" },
    handoff:       { label: _t("Hand off"),  icon: "fa-user",            color: "#dc2626", out: "none" },
    route_to_user: { label: _t("Route"),     icon: "fa-user",            color: "#dc2626", out: "none" },
    create_lead:   { label: _t("Create Lead"), icon: "fa-star",          color: "#059669", out: "single" },
    end:           { label: _t("End"),       icon: "fa-flag-checkered",  color: "#6b7280", out: "none" },
};

const NODE_W = 210;
const HEADER_H = 34;
const ROW_H = 24;
const BODY_PAD = 34; // message snippet / footer area

function m2oId(val) {
    // searchRead returns [id, name] for a m2o; normalise to a numeric id or null.
    if (Array.isArray(val)) return val[0] || null;
    return val || null;
}

export class OwaChatbotDesigner extends Component {
    static template = "open_whatsapp_connector.OwaChatbotDesigner";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        const action = this.props.action || {};
        const params = action.params || action.context || {};
        this.chatbotId =
            params.chatbot_id || (action.context && action.context.active_id) || null;

        this.canvasRef = useRef("canvas");
        this.state = useState({
            loading: true,
            bot: {},
            nodes: [],
            selectedId: null,
            view: { panX: 40, panY: 40, zoom: 1 },
            problems: [],
            users: [],
            teams: [],
            linking: null, // {fromStepId, fromOptionId|null, x2, y2}
            test: { open: false, log: [], current: null, awaiting: null, answers: {}, ended: false },
            saving: false,
        });

        // Window-level drag handlers, attached only while dragging/linking.
        this._onMove = this.onPointerMove.bind(this);
        this._onUp = this.onPointerUp.bind(this);
        this._drag = null; // {mode:'node'|'pan'|'link', ...}

        onMounted(() => this.loadFlow());
        onWillUnmount(() => this.detachWindow());
    }

    // ── Data loading ────────────────────────────────────────────────────
    async loadFlow() {
        if (!this.chatbotId) {
            this.notification.add(_t("No chatbot selected."), { type: "danger" });
            this.state.loading = false;
            return;
        }
        const [bot] = await this.orm.read(
            "owa.chatbot", [this.chatbotId],
            ["name", "first_step_id", "fallback_step_id", "welcome_message"]
        );
        this.state.bot = {
            id: bot.id,
            name: bot.name,
            first_step_id: m2oId(bot.first_step_id),
            fallback_step_id: m2oId(bot.fallback_step_id),
            welcome_message: bot.welcome_message || "",
        };

        const steps = await this.orm.searchRead(
            "owa.chatbot.step", [["chatbot_id", "=", this.chatbotId]],
            ["name", "step_type", "message", "sequence", "pos_x", "pos_y",
             "next_step_id", "answer_var", "answer_save_to", "answer_validation",
             "handoff_user_id", "handoff_team_id", "target_user_id"],
            { order: "sequence, id" }
        );
        const stepIds = steps.map((s) => s.id);
        let options = [];
        if (stepIds.length) {
            options = await this.orm.searchRead(
                "owa.chatbot.menu.option", [["step_id", "in", stepIds]],
                ["step_id", "label", "keyword", "sequence", "next_step_id"],
                { order: "sequence, id" }
            );
        }
        const optByStep = {};
        for (const o of options) {
            const sid = m2oId(o.step_id);
            (optByStep[sid] = optByStep[sid] || []).push({
                id: o.id, label: o.label || "", keyword: o.keyword || "",
                sequence: o.sequence, next_step_id: m2oId(o.next_step_id),
            });
        }
        this.state.nodes = steps.map((s) => ({
            id: s.id,
            name: s.name || _t("Untitled"),
            step_type: s.step_type,
            message: s.message || "",
            sequence: s.sequence,
            pos_x: s.pos_x || 0,
            pos_y: s.pos_y || 0,
            next_step_id: m2oId(s.next_step_id),
            answer_var: s.answer_var || "",
            answer_save_to: s.answer_save_to || "none",
            answer_validation: s.answer_validation || "none",
            handoff_user_id: m2oId(s.handoff_user_id),
            handoff_team_id: m2oId(s.handoff_team_id),
            target_user_id: m2oId(s.target_user_id),
            options: optByStep[s.id] || [],
        }));

        this.autoLayoutIfNeeded();

        // Reference data for the inspector selects.
        this.state.users = await this.orm.searchRead(
            "res.users", [["share", "=", false]], ["name"], { limit: 200, order: "name" });
        try {
            this.state.teams = await this.orm.searchRead(
                "crm.team", [], ["name"], { limit: 200, order: "name" });
        } catch {
            this.state.teams = [];
        }

        await this.refreshProblems();
        this.state.loading = false;
    }

    autoLayoutIfNeeded() {
        // If most nodes sit at the origin (never dragged), lay them out in
        // simple left→right columns by reachability depth so the first open
        // looks tidy instead of a pile at (0,0).
        const nodes = this.state.nodes;
        const atOrigin = nodes.filter((n) => !n.pos_x && !n.pos_y).length;
        if (nodes.length < 2 || atOrigin < nodes.length - 1) return;

        const byId = {};
        nodes.forEach((n) => (byId[n.id] = n));
        const depth = {};
        const first = this.state.bot.first_step_id || (nodes[0] && nodes[0].id);
        const queue = first ? [[first, 0]] : [];
        while (queue.length) {
            const [id, d] = queue.shift();
            if (id in depth || !byId[id]) continue;
            depth[id] = d;
            const n = byId[id];
            const outs = [];
            if (n.next_step_id) outs.push(n.next_step_id);
            n.options.forEach((o) => o.next_step_id && outs.push(o.next_step_id));
            outs.forEach((t) => queue.push([t, d + 1]));
        }
        const colCount = {};
        nodes.forEach((n) => {
            const d = depth[n.id] === undefined ? 0 : depth[n.id];
            colCount[d] = colCount[d] || 0;
            n.pos_x = 60 + d * 280;
            n.pos_y = 60 + colCount[d] * 150;
            colCount[d] += 1;
        });
        // Persist the computed layout (fire-and-forget).
        nodes.forEach((n) =>
            this.orm.write("owa.chatbot.step", [n.id], { pos_x: n.pos_x, pos_y: n.pos_y }));
    }

    async refreshProblems() {
        try {
            this.state.problems = await this.orm.call(
                "owa.chatbot", "_flow_problems", [[this.chatbotId]]);
        } catch {
            this.state.problems = [];
        }
    }

    // ── Geometry helpers ────────────────────────────────────────────────
    typeMeta(type) { return STEP_TYPES[type] || STEP_TYPES.message; }

    nodeHeight(node) {
        if (node.step_type === "menu") {
            return HEADER_H + Math.max(1, node.options.length) * ROW_H + 8;
        }
        return HEADER_H + BODY_PAD;
    }

    // World-space port coordinates.
    inPort(node) {
        return { x: node.pos_x, y: node.pos_y + this.nodeHeight(node) / 2 };
    }
    outPort(node) {
        return { x: node.pos_x + NODE_W, y: node.pos_y + HEADER_H + BODY_PAD / 2 };
    }
    optionPort(node, idx) {
        return { x: node.pos_x + NODE_W, y: node.pos_y + HEADER_H + idx * ROW_H + ROW_H / 2 };
    }

    get edges() {
        const byId = {};
        this.state.nodes.forEach((n) => (byId[n.id] = n));
        const out = [];
        for (const n of this.state.nodes) {
            const meta = this.typeMeta(n.step_type);
            if (meta.out === "single" && n.next_step_id && byId[n.next_step_id]) {
                out.push({
                    key: `s${n.id}`,
                    from: this.outPort(n),
                    to: this.inPort(byId[n.next_step_id]),
                });
            } else if (meta.out === "options") {
                n.options.forEach((o, i) => {
                    if (o.next_step_id && byId[o.next_step_id]) {
                        out.push({
                            key: `o${o.id}`,
                            from: this.optionPort(n, i),
                            to: this.inPort(byId[o.next_step_id]),
                        });
                    }
                });
            }
        }
        return out;
    }

    edgePath(e) {
        const dx = Math.max(40, Math.abs(e.to.x - e.from.x) / 2);
        return `M ${e.from.x},${e.from.y} C ${e.from.x + dx},${e.from.y} ${e.to.x - dx},${e.to.y} ${e.to.x},${e.to.y}`;
    }

    get linkingPath() {
        const l = this.state.linking;
        if (!l) return "";
        const dx = Math.max(40, Math.abs(l.x2 - l.from.x) / 2);
        return `M ${l.from.x},${l.from.y} C ${l.from.x + dx},${l.from.y} ${l.x2 - dx},${l.y2} ${l.x2},${l.y2}`;
    }

    get worldStyle() {
        const v = this.state.view;
        return `transform: translate(${v.panX}px, ${v.panY}px) scale(${v.zoom});`;
    }
    nodeStyle(node) {
        const meta = this.typeMeta(node.step_type);
        return `left:${node.pos_x}px; top:${node.pos_y}px; width:${NODE_W}px; border-top-color:${meta.color};`;
    }

    // Screen → world coordinate conversion.
    toWorld(clientX, clientY) {
        const rect = this.canvasRef.el.getBoundingClientRect();
        const v = this.state.view;
        return {
            x: (clientX - rect.left - v.panX) / v.zoom,
            y: (clientY - rect.top - v.panY) / v.zoom,
        };
    }

    get selectedNode() {
        return this.state.nodes.find((n) => n.id === this.state.selectedId) || null;
    }

    // Local (in-node) port positions, kept in sync with the world-space port
    // math above so the visual dots line up with the drawn edges.
    inPortStyle(node) { return `top:${this.nodeHeight(node) / 2 - 6}px;`; }
    outPortStyle() { return `top:${HEADER_H + BODY_PAD / 2 - 6}px;`; }
    optPortStyle(i) { return `top:${HEADER_H + i * ROW_H + ROW_H / 2 - 6}px;`; }
    optRowStyle() { return `height:${ROW_H}px;`; }
    stepLabel(type) { return this.typeMeta(type).label; }
    stepIcon(type) { return this.typeMeta(type).icon; }

    // ── Pointer interactions ────────────────────────────────────────────
    attachWindow() {
        window.addEventListener("pointermove", this._onMove);
        window.addEventListener("pointerup", this._onUp);
    }
    detachWindow() {
        window.removeEventListener("pointermove", this._onMove);
        window.removeEventListener("pointerup", this._onUp);
    }

    onCanvasPointerDown(ev) {
        if (ev.target.closest(".owa_fd_node") || ev.target.closest(".owa_fd_port")) return;
        this.state.selectedId = null;
        this._drag = { mode: "pan", startX: ev.clientX, startY: ev.clientY,
                       panX: this.state.view.panX, panY: this.state.view.panY };
        this.attachWindow();
    }

    onNodePointerDown(ev, node) {
        if (ev.target.closest(".owa_fd_port")) return; // ports handle their own drag
        ev.stopPropagation();
        this.state.selectedId = node.id;
        const w = this.toWorld(ev.clientX, ev.clientY);
        this._drag = { mode: "node", node, offX: w.x - node.pos_x, offY: w.y - node.pos_y, moved: false };
        this.attachWindow();
    }

    onPortPointerDown(ev, node, optionId = null) {
        ev.stopPropagation();
        ev.preventDefault();
        const from = optionId
            ? this.optionPort(node, node.options.findIndex((o) => o.id === optionId))
            : this.outPort(node);
        const w = this.toWorld(ev.clientX, ev.clientY);
        this.state.linking = { fromStepId: node.id, fromOptionId: optionId, from, x2: w.x, y2: w.y };
        this._drag = { mode: "link" };
        this.attachWindow();
    }

    onPointerMove(ev) {
        if (!this._drag) return;
        if (this._drag.mode === "pan") {
            this.state.view.panX = this._drag.panX + (ev.clientX - this._drag.startX);
            this.state.view.panY = this._drag.panY + (ev.clientY - this._drag.startY);
        } else if (this._drag.mode === "node") {
            const w = this.toWorld(ev.clientX, ev.clientY);
            this._drag.node.pos_x = Math.round(w.x - this._drag.offX);
            this._drag.node.pos_y = Math.round(w.y - this._drag.offY);
            this._drag.moved = true;
        } else if (this._drag.mode === "link" && this.state.linking) {
            const w = this.toWorld(ev.clientX, ev.clientY);
            this.state.linking.x2 = w.x;
            this.state.linking.y2 = w.y;
        }
    }

    async onPointerUp(ev) {
        const drag = this._drag;
        this._drag = null;
        this.detachWindow();
        if (!drag) return;

        if (drag.mode === "node" && drag.moved) {
            await this.orm.write("owa.chatbot.step", [drag.node.id],
                { pos_x: drag.node.pos_x, pos_y: drag.node.pos_y });
        } else if (drag.mode === "link" && this.state.linking) {
            const link = this.state.linking;
            this.state.linking = null;
            const target = this.nodeAt(ev.clientX, ev.clientY);
            if (target && target.id !== link.fromStepId) {
                await this.setLink(link, target.id);
            } else if (!target) {
                // Dropped on empty canvas → create a new step there and link it.
                const w = this.toWorld(ev.clientX, ev.clientY);
                const newId = await this.createStep("message", w.x - NODE_W / 2, w.y - HEADER_H);
                if (newId) await this.setLink(link, newId);
            }
        }
    }

    nodeAt(clientX, clientY) {
        const w = this.toWorld(clientX, clientY);
        for (const n of this.state.nodes) {
            const h = this.nodeHeight(n);
            if (w.x >= n.pos_x && w.x <= n.pos_x + NODE_W &&
                w.y >= n.pos_y && w.y <= n.pos_y + h) {
                return n;
            }
        }
        return null;
    }

    async setLink(link, targetStepId) {
        if (link.fromOptionId) {
            await this.orm.write("owa.chatbot.menu.option", [link.fromOptionId],
                { next_step_id: targetStepId });
            const node = this.state.nodes.find((n) => n.id === link.fromStepId);
            const opt = node && node.options.find((o) => o.id === link.fromOptionId);
            if (opt) opt.next_step_id = targetStepId;
        } else {
            await this.orm.write("owa.chatbot.step", [link.fromStepId],
                { next_step_id: targetStepId });
            const node = this.state.nodes.find((n) => n.id === link.fromStepId);
            if (node) node.next_step_id = targetStepId;
        }
        await this.refreshProblems();
    }

    onWheel(ev) {
        ev.preventDefault();
        const delta = ev.deltaY < 0 ? 1.1 : 0.9;
        const z = Math.min(1.6, Math.max(0.4, this.state.view.zoom * delta));
        this.state.view.zoom = Math.round(z * 100) / 100;
    }

    // ── Node CRUD ───────────────────────────────────────────────────────
    async createStep(type = "message", x = null, y = null) {
        if (x === null) {
            // Drop near the current viewport centre.
            const rect = this.canvasRef.el.getBoundingClientRect();
            const c = this.toWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
            x = c.x - NODE_W / 2;
            y = c.y - HEADER_H;
        }
        const label = this.typeMeta(type).label;
        const id = await this.orm.create("owa.chatbot.step", [{
            chatbot_id: this.chatbotId,
            name: label.toString(),
            step_type: type,
            pos_x: Math.round(x),
            pos_y: Math.round(y),
            sequence: 10 + this.state.nodes.length * 10,
        }]);
        const newId = Array.isArray(id) ? id[0] : id;
        this.state.nodes.push({
            id: newId, name: label.toString(), step_type: type, message: "",
            sequence: 10 + this.state.nodes.length * 10,
            pos_x: Math.round(x), pos_y: Math.round(y), next_step_id: null,
            answer_var: "", answer_save_to: "none", answer_validation: "none",
            handoff_user_id: null, handoff_team_id: null, target_user_id: null,
            options: [],
        });
        this.state.selectedId = newId;
        // First node created becomes the entry point by default.
        if (this.state.nodes.length === 1 && !this.state.bot.first_step_id) {
            await this.setFirstStep(newId);
        }
        await this.refreshProblems();
        return newId;
    }

    async deleteStep(node) {
        await this.orm.unlink("owa.chatbot.step", [node.id]);
        // Clear inbound refs locally (DB handles it via ondelete='set null').
        this.state.nodes.forEach((n) => {
            if (n.next_step_id === node.id) n.next_step_id = null;
            n.options.forEach((o) => { if (o.next_step_id === node.id) o.next_step_id = null; });
        });
        this.state.nodes = this.state.nodes.filter((n) => n.id !== node.id);
        if (this.state.selectedId === node.id) this.state.selectedId = null;
        if (this.state.bot.first_step_id === node.id) this.state.bot.first_step_id = null;
        await this.refreshProblems();
    }

    async setFirstStep(stepId) {
        await this.orm.write("owa.chatbot", [this.chatbotId], { first_step_id: stepId });
        this.state.bot.first_step_id = stepId;
        await this.refreshProblems();
    }

    // ── Inspector edits (write-through) ─────────────────────────────────
    async writeStep(node, vals) {
        Object.assign(node, vals);
        await this.orm.write("owa.chatbot.step", [node.id], vals);
        await this.refreshProblems();
    }
    onStepField(node, field, ev) {
        let value = ev.target.value;
        if (["handoff_user_id", "handoff_team_id", "target_user_id"].includes(field)) {
            value = value ? parseInt(value, 10) : false;
        }
        this.writeStep(node, { [field]: value });
    }

    async addOption(node) {
        const id = await this.orm.create("owa.chatbot.menu.option", [{
            step_id: node.id,
            label: _t("New option").toString(),
            sequence: 10 + node.options.length * 10,
        }]);
        const newId = Array.isArray(id) ? id[0] : id;
        node.options.push({ id: newId, label: _t("New option").toString(),
            keyword: "", sequence: 10 + node.options.length * 10, next_step_id: null });
        await this.refreshProblems();
    }
    async writeOption(node, opt, vals) {
        Object.assign(opt, vals);
        await this.orm.write("owa.chatbot.menu.option", [opt.id], vals);
        await this.refreshProblems();
    }
    async deleteOption(node, opt) {
        await this.orm.unlink("owa.chatbot.menu.option", [opt.id]);
        node.options = node.options.filter((o) => o.id !== opt.id);
        await this.refreshProblems();
    }

    // ── Preview (approximate, client-side) ──────────────────────────────
    nodeById(id) { return this.state.nodes.find((n) => n.id === id) || null; }

    botSay(text) { if (text) this.state.test.log.push({ who: "bot", text }); }

    presentNode(node, guard = 0) {
        if (!node || guard > 25) return node;
        const t = node.step_type;
        if (t === "message") {
            this.botSay(node.message);
            return node.next_step_id ? this.presentNode(this.nodeById(node.next_step_id), guard + 1) : node;
        }
        if (t === "question") {
            this.botSay(node.message);
            this.state.test.awaiting = node.answer_var || node.name || "answer";
            return node;
        }
        if (t === "menu") {
            let text = node.message || "";
            if (node.options.length) {
                const opts = node.options.map((o, i) => `${i + 1}. ${o.label}`).join("\n");
                text = text ? `${text}\n\n${opts}` : opts;
            }
            this.botSay(text);
            return node;
        }
        if (t === "create_lead") {
            this.botSay(node.message);
            this.state.test.log.push({ who: "sys", text: _t("(a CRM lead would be created here)") });
            return node.next_step_id ? this.presentNode(this.nodeById(node.next_step_id), guard + 1) : node;
        }
        if (t === "handoff" || t === "route_to_user") {
            this.botSay(node.message);
            this.state.test.log.push({ who: "sys", text: _t("→ handed off to a human, bot paused") });
            this.state.test.ended = true;
            return node;
        }
        if (t === "end") {
            this.botSay(node.message);
            this.state.test.ended = true;
            return node;
        }
        return node;
    }

    startTest() {
        const t = this.state.test;
        t.open = true;
        t.log = [];
        t.awaiting = null;
        t.answers = {};
        t.ended = false;
        this.botSay(this.state.bot.welcome_message);
        const firstId = this.state.bot.first_step_id ||
            (this.state.nodes[0] && this.state.nodes[0].id);
        t.current = this.presentNode(this.nodeById(firstId));
    }

    onTestKeydown(ev) {
        if (ev.key === "Enter") this.sendTest(ev);
    }
    sendTest(ev) {
        const input = ev.target.closest(".owa_fd_test").querySelector("input");
        const text = (input.value || "").trim();
        if (!text) return;
        input.value = "";
        const t = this.state.test;
        t.log.push({ who: "user", text });
        if (t.ended || !t.current) return;

        let node = t.current;
        if (t.awaiting) {
            const mode = node.answer_validation && node.answer_validation !== "none"
                ? node.answer_validation
                : (node.answer_save_to === "email" ? "email" : "none");
            if (mode === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(text)) {
                this.botSay(_t("That doesn't look like a valid email — please send it again, e.g. name@example.com"));
                return; // stay on the question
            }
            t.answers[t.awaiting] = text;
            t.awaiting = null;
            const next = node.next_step_id ? this.nodeById(node.next_step_id) : null;
            t.current = next ? this.presentNode(next) : node;
            return;
        }
        if (node.step_type === "menu") {
            let idx = -1;
            const low = text.toLowerCase();
            const asNum = parseInt(text, 10);
            if (!isNaN(asNum) && asNum >= 1 && asNum <= node.options.length) {
                idx = asNum - 1;
            } else {
                idx = node.options.findIndex(
                    (o) => o.label.toLowerCase() === low ||
                           (o.keyword && o.keyword.toLowerCase() === low));
            }
            if (idx >= 0 && node.options[idx].next_step_id) {
                t.current = this.presentNode(this.nodeById(node.options[idx].next_step_id));
            } else {
                this.botSay(_t("Sorry, I didn't understand that. Please choose from the options above."));
            }
            return;
        }
        // Non-interactive step: advance.
        const next = node.next_step_id ? this.nodeById(node.next_step_id) : null;
        t.current = next ? this.presentNode(next) : node;
    }

    // ── Navigation ──────────────────────────────────────────────────────
    backToBot() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "owa.chatbot",
            res_id: this.chatbotId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("owa_chatbot_designer", OwaChatbotDesigner);
