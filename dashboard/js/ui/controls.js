/*
 * Manual-intervention controls (contract §4):
 *   - node actions: restart (reboot) / drain, both behind confirm dialogs
 *   - scale intent: validate -> review plan (clamps = warnings, rejects = errors) -> apply
 *   - pending AI remediation: approve / deny
 *
 * Every action reports its outcome inline and as a toast; nothing is silent.
 */

import { el, clear } from "./dom.js";
import { formatError, summarizeAllocations } from "./errors.js";
import { shortId } from "../core/format.js";

export class ControlsPanel {
  constructor(dom, handlers) {
    this.dom = dom;
    this.handlers = handlers;
    this.target = null;
    this.planId = null;
    this.dom.rebootBtn.addEventListener("click", () => handlers.onReboot());
    this.dom.drainBtn.addEventListener("click", () => handlers.onDrain());
    this.dom.intentBtn.addEventListener("click", () => handlers.onValidateIntent(this.dom.intentPrompt.value.trim()));
    this.dom.applyBtn.addEventListener("click", () => {
      if (this.planId) handlers.onApply(this.planId);
    });
  }

  setTarget(node) {
    this.target = node;
    const label = node ? node.registration_id || node.node_id : "—";
    this.dom.nodeLabel.textContent = label;
    this.dom.controlsTarget.textContent = node ? `target: ${label}` : "no node selected";
    const offline = node && node.status === "offline";
    this.dom.rebootBtn.disabled = !node;
    this.dom.drainBtn.disabled = !node;
    this.dom.rebootBtn.title = offline ? "node is offline — reboot returns 503 NODE_OFFLINE" : "restart this node";
  }

  nodeResult(kind, message, ok) {
    this.dom.nodeActionResult.className = `result ${ok ? "ok" : "bad"}`;
    this.dom.nodeActionResult.textContent = message;
    void kind;
  }

  intentResult(kind, message, ok) {
    this.dom.intentResult.className = `result ${ok ? "ok" : "bad"}`;
    clear(this.dom.intentResult);
    if (typeof message === "string") {
      this.dom.intentResult.textContent = message;
    } else {
      this.dom.intentResult.appendChild(message);
    }
  }

  remResult(message, ok) {
    this.dom.remediationResult.className = `result ${ok ? "ok" : "bad"}`;
    this.dom.remediationResult.textContent = message;
  }

  /** Render a validated AllocationPlan; sets apply availability. */
  showPlan(plan) {
    const nodes = [];
    const outcome = plan && plan.outcome ? plan.outcome : "unknown";
    const rejected = outcome !== "accepted";
    nodes.push(
      el("div", { class: "plan" }, [
        el("div", {}, [
          el("strong", { text: `plan ${plan && plan.plan_id ? plan.plan_id.slice(0, 10) + "…" : "—"}` }),
          el("span", { class: `badge ${rejected ? "status-offline" : "status-online"}`, text: outcome }),
        ]),
      ]),
    );
    const allocs = summarizeAllocations(plan);
    for (const a of allocs) {
      const bits = [`#${a.index} ${a.type}×${a.count} → ${a.status}`];
      if (a.node_id) bits.push(`node ${a.node_id.slice(0, 8)}…`);
      const line = el("div", { class: `alloc ${a.status === "accepted" ? "accepted" : "rejected"}`, text: bits.join("  ") });
      if (a.clamped.length) {
        line.appendChild(
          el("div", { class: "warn-tag small", text: `clamped: ${a.clamped.map((c) => `${c.field} ${c.from}→${c.to}`).join(", ")}` }),
        );
      }
      if (a.rejected.length) {
        line.appendChild(el("div", { class: "err-tag small", text: `rejected: ${a.rejected.join(", ")}` }));
      }
      nodes.push(line);
    }
    this.dom.intentResult.className = "result";
    clear(this.dom.intentResult);
    for (const n of nodes) this.dom.intentResult.appendChild(n);
    this.planId = !rejected && plan ? plan.plan_id : null;
    this.dom.applyBtn.disabled = !this.planId;
  }

  intentError(e) {
    this.planId = null;
    this.dom.applyBtn.disabled = true;
    this.intentResult("", formatError(e), false);
  }

  applyResult(job) {
    const text = job
      ? `applied: state=${job.state || "?"} job=${job.job_id || "?"} vms=${(job.vm_ids || []).length}`
      : "applied";
    this.intentResult("", text, true);
    this.dom.applyBtn.disabled = true;
    this.planId = null;
  }

  renderPending(items) {
    const host = this.dom.pendingList;
    clear(host);
    if (!items || items.length === 0) {
      host.appendChild(el("div", { class: "empty small", text: "No pending approvals." }));
      return;
    }
    for (const it of items) {
      const item = el("div", { class: "pending-item", dataset: { commandId: it.command_id || "" } }, [
        el("div", { class: "row1" }, [
          el("strong", { text: it.type || "command" }),
          el("span", { class: `rem-status ${it.status || ""}`, text: it.status || "" }),
        ]),
        el("div", { class: "msg", text: it.message || it.command_id || "" }),
      ]);
      const row = el("div", { class: "btn-row" });
      const approve = el("button", { class: "btn btn-ok btn-sm", type: "button", text: "Approve" });
      const deny = el("button", { class: "btn btn-warn btn-sm", type: "button", text: "Deny" });
      approve.addEventListener("click", () => {
        approve.disabled = true;
        deny.disabled = true;
        this.handlers.onApprove(it.command_id);
      });
      deny.addEventListener("click", () => {
        approve.disabled = true;
        deny.disabled = true;
        this.handlers.onDeny(it.command_id);
      });
      row.append(approve, deny);
      // "Ask AI" on a pending AI remediation: attaches `remediation` + the
      // command_id (§4.10 resolves that id to the command), so the follow-up
      // question keeps pointing at the exact command the owner is deciding on.
      if (this.handlers.onAskAi) {
        const ask = el("button", {
          class: "btn btn-sm btn-ask",
          type: "button",
          text: "Ask AI",
          title: `ask the assistant about command ${it.command_id}`,
          dataset: { askAiKind: "remediation", commandId: it.command_id || "" },
        });
        ask.addEventListener("click", () =>
          this.handlers.onAskAi({
            kind: "remediation",
            id: it.command_id,
            label: `${it.type || "command"} · ${shortId(it.command_id, 10, 4)}`,
            summary: `remediation · ${it.type || "command"} on ${it.node_id || "node"} · ${it.status}`,
            meta: { nodeId: it.node_id, type: it.type, status: it.status },
          }),
        );
        row.appendChild(ask);
      }
      item.appendChild(row);
      host.appendChild(item);
    }
  }
}
