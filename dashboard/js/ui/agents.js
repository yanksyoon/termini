/*
 * Agents panel — the AI agents installed for this client, grouped by node.
 *
 * Rendering rules that matter (the card's honesty requirements):
 *  - each row is one `lxd_instances[]` entry; nothing is synthesised;
 *  - a field the telemetry contract does not carry (instance name, IP, image
 *    alias, vCPU count) is shown as "not reported", never guessed;
 *  - the inference label comes from the task's own `provenance`, so a forwarded
 *    managed model is named as such and never presented as local GPU inference;
 *  - every non-list state (no agents yet, node offline, no inventory, stopped by
 *    you, waiting for a just-applied plan) renders an explicit line.
 *
 * Re-rendering every poll must not eat a half-typed prompt, so drafts, open
 * task boxes and in-flight submissions live on the panel instance, not in the
 * DOM.
 */

import { el, clear } from "./dom.js";
import { ago, bytes, pct, uptime, shortId } from "../core/format.js";
import { INVENTORY, agentsSummary, inferenceLabel } from "../core/agents.js";

export class AgentsPanel {
  constructor(dom, handlers = {}) {
    this.dom = dom;
    this.handlers = handlers;
    this.drafts = new Map();   // vm_id -> prompt text
    this.open = new Set();     // vm_id -> task box expanded
    this.busy = new Map();     // vm_id -> "stop" | "start" | "restart" | "task"
    this.view = null;
    this.tasks = new Map();    // vm_id -> taskView
    this.expected = [];        // rows for a just-applied plan
    this.inference = null;     // panel-level label {kind,text,detail}
    if (dom.refreshBtn) dom.refreshBtn.addEventListener("click", () => this.handlers.onRefresh && this.handlers.onRefresh());
  }

  setInference(label) {
    this.inference = label;
    this._renderInference();
  }

  setTasks(tasks) {
    this.tasks = tasks instanceof Map ? tasks : new Map(Object.entries(tasks || {}));
  }

  setExpected(rows) {
    this.expected = Array.isArray(rows) ? rows : [];
    this.render();
  }

  setBusy(vmId, what) {
    if (what) this.busy.set(vmId, what);
    else this.busy.delete(vmId);
    this.render();
  }

  /** Full refresh of the panel from a computed `agentGroups()` view. */
  render(view, tasks) {
    if (view) this.view = view;
    if (tasks) this.setTasks(tasks);
    const root = this.dom.groups;
    if (!root) return;
    clear(root);
    this._renderInference();

    const v = this.view;
    const empty = this.dom.empty;
    const summary = agentsSummary(v);
    if (this.dom.count) this.dom.count.textContent = summary.text;

    const groups = (v && v.groups) || [];
    const hasRows = groups.some((g) => g.agents.length) || this.expected.length > 0;

    if (empty) {
      const show = v && v.nodes > 0 && !hasRows && v.known;
      empty.hidden = !show;
      clear(empty);
      if (show) {
        empty.appendChild(
          el("div", { class: "empty-title", text: "No agents yet." }),
          el("div", {
            class: "muted small",
            text:
              "Nothing on your nodes is running an agent instance. Deploy one with Scale intent — " +
              "the plan's instances appear here as soon as the node reports them.",
          }),
          el("div", { class: "btn-row" }, [
            el("button", {
              class: "btn btn-primary btn-sm",
              type: "button",
              text: "Deploy an agent with Scale intent",
              onclick: () => this.handlers.onDeployIntent && this.handlers.onDeployIntent(),
            }),
          ]),
        );
      }
    }

    if (!v || v.nodes === 0) {
      root.appendChild(
        el("div", { class: "agent-edge" }, [
          el("strong", { text: "No nodes registered yet." }),
          el("div", { class: "muted small", text: "Agents are read from each node's telemetry, so a node must register and report before one can be shown." }),
        ]),
      );
      return;
    }

    for (const group of groups) {
      root.appendChild(this._group(group));
    }

    // A just-applied plan's instances that telemetry has not listed yet.
    for (const row of this.expected) {
      const group = groups.find((g) => g.nodeId === row.nodeId);
      if (group && group.agents.some((a) => a.vmId === row.vmId)) continue; // appeared
      root.appendChild(this._expectedRow(row, group));
    }
  }

  /* ------------------------------------------------------------- internals */

  _renderInference() {
    const host = this.dom.inference;
    if (!host) return;
    const label = this.inference || inferenceLabel(null, null);
    host.className = `agent-inference agent-inference-${label.kind}`;
    host.textContent = `inference: ${label.text}`;
    host.title = label.detail || "";
  }

  _group(group) {
    const head = el("div", { class: "agent-group-head" }, [
      el("span", { class: "agent-group-name", text: group.label, title: group.nodeId }),
      el("span", { class: `badge status-${group.status}`, text: group.status }),
      el("span", {
        class: "muted small",
        text:
          group.inventory === INVENTORY.REPORTED
            ? `${group.agents.filter((a) => !a.retained).length} reported running`
            : group.inventory === INVENTORY.ABSENT
              ? "inventory not reported"
              : "no telemetry yet",
      }),
      group.updatedAt ? el("span", { class: "muted small", text: `sample ${ago(group.updatedAt)}` }) : null,
    ]);

    const box = el("div", { class: "agent-group", dataset: { node: group.nodeId } }, [head]);

    if (group.inventory === INVENTORY.ABSENT) {
      box.appendChild(
        el("div", { class: "agent-edge warn" }, [
          el("strong", { text: "This node did not report its instance inventory." }),
          el("div", {
            class: "muted small",
            text:
              "Telemetry omitted `lxd_instances` — the node cannot read its own LXD. " +
              "That is NOT the same as “no agents”: the row set is unknown until the node can answer.",
          }),
        ]),
      );
    } else if (group.inventory === INVENTORY.NO_SAMPLE) {
      box.appendChild(
        el("div", { class: "agent-edge" }, [
          el("strong", { text: "No telemetry sample from this node yet." }),
          el("div", { class: "muted small", text: "Nothing can be said about its agents until its first sample arrives." }),
        ]),
      );
    }

    const offline = group.status === "offline";
    if (offline) {
      box.appendChild(
        el("div", { class: "agent-edge warn" }, [
          el("strong", { text: "Node offline." }),
          el("div", { class: "muted small", text: "Actions are refused (503 NODE_OFFLINE) while the node is offline; the last reported agents are shown as they were." }),
        ]),
      );
    }

    if (group.inventory === INVENTORY.REPORTED && group.agents.length === 0) {
      box.appendChild(
        el("div", { class: "agent-edge" }, [
          el("span", { class: "muted small", text: "The node reports zero running agent instances." }),
        ]),
      );
    }

    for (const agent of group.agents) box.appendChild(this._row(agent, offline));
    return box;
  }

  _row(agent, offline) {
    const busy = this.busy.get(agent.vmId) || null;
    const running = agent.state === "running";
    const stopped = agent.state === "stopped";
    const stateBadge = el("span", {
      class: `badge agent-state agent-state-${agent.state}`,
      text: agent.retained ? "stopped by you" : agent.state,
      title: agent.retained
        ? "you stopped this agent from this panel; telemetry lists only RUNNING instances, so it is kept here with that label"
        : `LXD state ${agent.state}`,
    });

    const facts = el("div", { class: "agent-facts" }, [
      fact("id", agent.vmId, agent.vmId),
      fact("node", agent.nodeLabel, agent.nodeId),
      fact("CPU", agent.cpuPct === null ? "—" : pct(agent.cpuPct)),
      fact("RAM", agent.ramUsed === null ? "—" : `${bytes(agent.ramUsed)}${agent.ramLimit ? ` / ${bytes(agent.ramLimit)}` : ""}`),
      fact("disk used", agent.diskUsed === null ? "—" : bytes(agent.diskUsed), "root volume used, as LXD reports it (0 on the dir driver)"),
      fact("state age", agent.stateAgeS === null ? "not reported" : uptime(agent.stateAgeS), "telemetry §4.6: state_age_sec is omitted when LXD cannot expose started_at"),
      fact("restarts", agent.restarts === null ? "not reported" : String(agent.restarts), "restarts observed by the node daemon"),
      // Fields the card asks for that §4.6 does not carry: said out loud.
      fact("image alias", "not reported", "per-instance image alias is not a node-telemetry §4.6 field; adding it is its own contract change"),
      fact("IP", "not reported", "instance addresses are not on the wire (governance: instance identifiers stay on the premises)"),
      fact("vCPU", "not reported", "per-instance vCPU count is not a §4.6 field; ram_limit_bytes is"),
    ]);

    const actions = el("div", { class: "btn-row agent-actions" }, [
      el("button", {
        class: "btn btn-sm",
        type: "button",
        text: busy === "start" ? "starting…" : "Start",
        disabled: offline || busy || running ? "" : null,
        title: "Start this agent (audited as vm.start)",
        onclick: () => this.handlers.onAction && this.handlers.onAction(agent.vmId, "start", agent),
      }),
      el("button", {
        class: "btn btn-sm btn-warn",
        type: "button",
        text: busy === "stop" ? "stopping…" : "Stop",
        disabled: offline || busy || !running ? "" : null,
        title: "Stop this agent (audited as vm.stop)",
        onclick: () => this.handlers.onAction && this.handlers.onAction(agent.vmId, "stop", agent),
      }),
      el("button", {
        class: "btn btn-sm btn-warn",
        type: "button",
        text: busy === "restart" ? "restarting…" : "Restart",
        disabled: offline || busy ? "" : null,
        title: "Restart this agent (audited as vm.restart)",
        onclick: () => this.handlers.onAction && this.handlers.onAction(agent.vmId, "restart", agent),
      }),
      this._taskToggle(agent),
    ]);

    return el("div", { class: "agent-row", dataset: { agent: agent.vmId } }, [
      el("div", { class: "agent-row-head" }, [
        el("span", { class: "agent-name", text: agent.retained ? `${agent.label}` : agent.label, title: agent.vmId }),
        stateBadge,
      ]),
      facts,
      actions,
      this._taskBox(agent),
    ]);
  }

  _taskToggle(agent) {
    const open = this.open.has(agent.vmId);
    return el("button", {
      class: "btn btn-sm btn-ghost agent-task-toggle",
      type: "button",
      text: open ? "Hide task box" : "Give a task…",
      onclick: () => {
        if (open) this.open.delete(agent.vmId);
        else this.open.add(agent.vmId);
        this.render();
      },
    });
  }

  _taskBox(agent) {
    const task = this.tasks.get(agent.vmId) || null;
    const taskEl = task ? this._taskResult(task) : null;
    const open = this.open.has(agent.vmId);
    if (!open) {
      // A finished answer stays visible collapsed: the client must be able to
      // see which agent produced which answer without re-opening the box.
      return taskEl;
    }
    const draft = this.drafts.get(agent.vmId) || "";
    const busy = this.busy.get(agent.vmId) === "task";

    const input = el("textarea", {
      class: "agent-prompt",
      rows: "2",
      maxlength: "8192",
      placeholder: "e.g. Summarise this node's health in one sentence",
      "aria-label": `prompt for agent ${agent.label}`,
    });
    input.value = draft;
    input.addEventListener("input", () => this.drafts.set(agent.vmId, input.value));

    const submit = el("button", {
      class: "btn btn-primary btn-sm",
      type: "button",
      text: busy ? "sending…" : "Send task",
      disabled: busy ? "" : null,
      onclick: () => {
        const prompt = (this.drafts.get(agent.vmId) || "").trim();
        if (!prompt) return;
        this.busy.set(agent.vmId, "task");
        this.render();
        this.handlers.onTask && this.handlers.onTask(agent.vmId, prompt);
      },
    });

    return el("div", { class: "agent-task" }, [
      el("div", { class: "muted small", text: "The prompt is sent through the controller to this agent only; its answer and provenance are shown below." }),
      input,
      el("div", { class: "btn-row" }, [submit]),
      taskEl,
    ]);
  }

  _taskResult(task) {
    const status = el("span", { class: `badge agent-task-state agent-task-${task.state}`, text: task.state });
    const meta = el("span", {
      class: "muted small",
      text: [
        task.durationMs !== null && task.durationMs !== undefined ? `${task.durationMs} ms` : null,
        task.taskId ? `task ${shortId(task.taskId)}` : null,
      ].filter(Boolean).join(" · "),
    });
    const parts = [el("div", { class: "agent-task-head" }, [status, meta])];

    if (task.state === "error") {
      parts.push(
        el("div", { class: "agent-task-error" }, [
          el("strong", { text: task.errorCode || "ERROR" }),
          el("div", { class: "muted small", text: task.errorMessage || "the task failed" }),
        ]),
      );
    }
    if (task.answer) {
      const label = inferenceLabel(task.provenance, task.model);
      parts.push(el("pre", { class: "agent-answer", text: task.answer }));
      parts.push(
        el("div", { class: `agent-inference agent-inference-${label.kind}` }, [
          el("div", { class: "agent-inference-text", text: `answered by: ${label.text}` }),
          el("div", { class: "muted small", text: label.detail }),
        ]),
      );
    } else if (task.state === "queued" || task.state === "running") {
      parts.push(el("div", { class: "muted small", text: `task ${task.state} — the answer appears here when it is done` }));
    }
    return el("div", { class: "agent-task-result" }, parts);
  }

  _expectedRow(row, group) {
    const nodeLabel = group ? group.label : (row.nodeId || "node");
    return el("div", { class: "agent-row agent-row-expected", dataset: { expected: row.name || "" } }, [
      el("div", { class: "agent-row-head" }, [
        el("span", { class: "agent-name", text: row.vmId ? shortId(row.vmId) : "new agent" }),
        el("span", { class: "badge agent-state agent-state-provisioning", text: "provisioning" }),
      ]),
      el("div", { class: "muted small" }, [
        `applied plan · instance ${row.name || "?"} on ${nodeLabel} — `,
        "waiting for the node's next telemetry sample to report it RUNNING. " +
          "The controller returns the instance name; the node reports its derived id, so the row is matched by that id.",
      ]),
    ]);
  }
}

function fact(name, value, title) {
  return el("div", { class: "agent-fact", title: title || null }, [
    el("span", { class: "agent-fact-name", text: name }),
    el("span", { class: "agent-fact-value", text: value === null || value === undefined ? "—" : String(value) }),
  ]);
}
