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
 *    you, waiting for a just-applied plan) renders an explicit line;
 *  - a row that cannot be given a task (a demo/seeder-fabricated row, an agent
 *    stopped by you) still renders — it is evidence about the fleet — but its
 *    action is DISABLED with the reason beside it, so the reader is never sent
 *    down a path that ends in a 404 (task t_df3120a8);
 *  - a refusal that does arrive is drawn as the controller's answer: code,
 *    message, request_id and every details[0] field, with a headline that says
 *    whether the id is unknown or the node holding it could not be read.
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

    const boxes = new Map();
    for (const group of groups) {
      const box = this._group(group);
      boxes.set(group.nodeId, box);
      root.appendChild(box);
    }

    // A just-applied plan's instances that telemetry has not listed yet. They go
    // INSIDE their node's group when that node is on screen — the plan's outcome
    // must appear where the reader looks, not orphaned below the last node — and
    // fall back to the panel root only when the node is not in the view.
    for (const row of this.expected) {
      const group = groups.find((g) => g.nodeId === row.nodeId);
      if (group && group.agents.some((a) => a.vmId === row.vmId)) continue; // appeared
      const host = (row.nodeId && boxes.get(row.nodeId)) || root;
      host.appendChild(this._expectedRow(row, group));
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
    const demo = group.synthetic === true;
    const head = el("div", { class: "agent-group-head" }, [
      el("span", { class: "agent-group-name", text: group.label, title: group.nodeId }),
      el("span", { class: `badge status-${group.status}`, text: group.status }),
      demo
        ? el("span", {
            class: "badge synthetic",
            text: "demo",
            title: "fabricated by the demo seeder (node.synthetic === true) — its rows are evidence, not hardware",
          })
        : null,
      el("span", {
        class: "muted small",
        text: demo
          ? `${group.agents.length} demo row${group.agents.length === 1 ? "" : "s"} — seeder-fabricated`
          : group.inventory === INVENTORY.REPORTED
            ? `${group.agents.filter((a) => !a.retained).length} reported running`
            : group.inventory === INVENTORY.ABSENT
              ? "inventory not reported"
              : "no telemetry yet",
      }),
      group.updatedAt ? el("span", { class: "muted small", text: `sample ${ago(group.updatedAt)}` }) : null,
    ]);

    const box = el("div", { class: "agent-group", dataset: { node: group.nodeId } }, [head]);

    if (demo) {
      // The row is drawn (it is what the panel was shown) and the reason it
      // cannot be tasked is stated here, once, instead of per row.
      box.appendChild(
        el("div", { class: "agent-edge" }, [
          el("strong", { text: "Demo node — these rows have no agent runtime behind them." }),
          el("div", {
            class: "muted small",
            text:
              "This node is fabricated by the demo seeder (node.synthetic === true), so its instance ids are " +
              "sample data. They are shown as evidence about what reported them; they cannot be given a task.",
          }),
        ]),
      );
    }

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

    // A demo row has no instance at all, so start/stop/restart would fail
    // exactly like the task would: they are disabled with the same reason.
    const blocked = agent.actionsAvailable === false ? agent.taskableReason || "this row cannot be actioned" : null;
    const actions = el("div", { class: "btn-row agent-actions" }, [
      el("button", {
        class: "btn btn-sm",
        type: "button",
        text: busy === "start" ? "starting…" : "Start",
        disabled: offline || busy || running || blocked ? "" : null,
        title: blocked || "Start this agent (audited as vm.start)",
        onclick: () => this.handlers.onAction && this.handlers.onAction(agent.vmId, "start", agent),
      }),
      el("button", {
        class: "btn btn-sm btn-warn",
        type: "button",
        text: busy === "stop" ? "stopping…" : "Stop",
        disabled: offline || busy || !running || blocked ? "" : null,
        title: blocked || "Stop this agent (audited as vm.stop)",
        onclick: () => this.handlers.onAction && this.handlers.onAction(agent.vmId, "stop", agent),
      }),
      el("button", {
        class: "btn btn-sm btn-warn",
        type: "button",
        text: busy === "restart" ? "restarting…" : "Restart",
        disabled: offline || busy || blocked ? "" : null,
        title: blocked || "Restart this agent (audited as vm.restart)",
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
      // The explicit line the card asks for: why this row's action is not
      // available. It is visible text, not only a tooltip.
      agent.taskable === false
        ? el("div", {
            class: "agent-task-reason muted small",
            id: `agent-task-reason-${agent.vmId}`,
            text: agent.taskableReason || "this row cannot be given a task",
          })
        : null,
      this._taskBox(agent),
    ]);
  }

  _taskToggle(agent) {
    // A row nothing can answer gets a DISABLED action, with the reason on its
    // own line beneath the controls (rendered by `_row`): not a button that
    // fails after the reader has typed a prompt (t_df3120a8).
    if (agent.taskable === false) {
      const reason = agent.taskableReason || "this row cannot be given a task";
      return el("span", { class: "agent-task-off" }, [
        el("button", {
          class: "btn btn-sm btn-ghost agent-task-toggle",
          type: "button",
          text: "Give a task…",
          disabled: "",
          title: reason,
          "aria-describedby": `agent-task-reason-${agent.vmId}`,
          dataset: { refused: reason },
        }),
      ]);
    }
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
    // Never draw a prompt for a row that cannot be tasked, even if it was
    // opened before the row's provenance changed under a poll.
    if (agent.taskable === false) return taskEl;
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
        this.handlers.onTask && this.handlers.onTask(agent.vmId, prompt, agent);
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
        task.refusal
          ? this._refusal(task.refusal)
          : el("div", { class: "agent-task-error" }, [
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

  /**
   * A REFUSED task, drawn as the controller's own answer: the code it answered
   * with, its message, the `request_id` that ties it to the operator log, and
   * every `details[0]` field it sent. The headline names WHY, in the reader's
   * terms — a 503 that could not read a node must not read like a 404.
   */
  _refusal(refusal) {
    const code = refusal.status ? `${refusal.status} · ${refusal.code}` : refusal.code;
    const head = el("div", { class: "agent-refusal-head" }, [
      el("span", { class: `badge agent-refusal-code agent-refusal-${refusal.kind}`, text: code }),
      refusal.requestId
        ? el("span", { class: "muted small", text: `request_id ${refusal.requestId}`, title: "quote this to the operator; it names the exact request in their log" })
        : null,
    ]);
    const parts = [
      head,
      el("div", { class: "agent-refusal-headline", text: refusal.headline }),
      el("div", { class: "muted small", text: refusal.message }),
    ];
    const fields = Array.isArray(refusal.fields) ? refusal.fields : [];
    if (fields.length) {
      parts.push(
        el(
          "div",
          { class: "agent-refusal-fields" },
          fields.map((f) =>
            el("div", { class: "agent-refusal-field", dataset: { field: f.name } }, [
              el("span", { class: "agent-refusal-name", text: f.name }),
              el("span", { class: "agent-refusal-value", text: f.value }),
            ]),
          ),
        ),
      );
    }
    return el("div", { class: `agent-task-error agent-refusal agent-refusal-kind-${refusal.kind}` }, parts);
  }

  _expectedRow(row, group) {
    const nodeLabel = group ? group.label : row.nodeId ? shortId(row.nodeId) : "node not reported";
    const state = row.state || "provisioning";
    const unconfirmed = state !== "provisioning";
    const parts = [
      `applied plan · instance ${row.name || "?"} on ${nodeLabel} — `,
      unconfirmed
        ? "no node has reported this instance yet. The apply may have been rolled back, or its node " +
          "cannot read its own LXD; telemetry is the only inventory, so this row is not proof it exists."
        : "waiting for the node's next telemetry sample to report it RUNNING. " +
          "The controller returns the instance name; the node reports its derived id, so the row is matched by that id.",
    ];
    if (row.hashed === false) {
      parts.push(
        " This page cannot derive the id (crypto.subtle needs a secure context), so this row will not " +
          "be matched to the reported one automatically.",
      );
    }
    return el("div", { class: "agent-row agent-row-expected", dataset: { expected: row.name || "" } }, [
      el("div", { class: "agent-row-head" }, [
        el("span", { class: "agent-name", text: row.vmId ? shortId(row.vmId) : "new agent" }),
        el("span", {
          class: `badge agent-state agent-state-${unconfirmed ? "unconfirmed" : "provisioning"}`,
          text: unconfirmed ? "not yet reported" : "provisioning",
        }),
      ]),
      el("div", { class: "muted small" }, parts),
    ]);
  }
}

function fact(name, value, title) {
  return el("div", { class: "agent-fact", title: title || null }, [
    el("span", { class: "agent-fact-name", text: name }),
    el("span", { class: "agent-fact-value", text: value === null || value === undefined ? "—" : String(value) }),
  ]);
}
