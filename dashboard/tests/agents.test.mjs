/*
 * Agents panel — pure contract tests (task t_44a2b87b).
 *
 * The panel's honesty rules are the point of these tests, so they are asserted
 * on the pure functions rather than eyeballed in a browser:
 *   - one row per `lxd_instances[]` entry (node-telemetry.md §4.6);
 *   - an OMITTED field is "inventory not reported", never "no agents";
 *   - an empty array is "zero running", which IS a report;
 *   - a client-stopped agent (no longer reported) stays visible, labelled;
 *   - the inference label never claims local inference when the endpoint did
 *     not say so, and names a forwarded managed model as forwarded;
 *   - the §4.6 id derivation in JS matches the controller/node literal;
 *   - a demo/seeder-fabricated row is RENDERED (it is evidence) but never
 *     offered a task, and a refusal is rendered as the controller's answer
 *     (code, message, request_id, every details[0] field) — task t_df3120a8.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  INVENTORY,
  TASK_REFUSAL,
  agentFromInstance,
  agentGroups,
  agentsSummary,
  inferenceLabel,
  refusalView,
  refusedLocally,
  taskView,
  taskability,
  vmIdForName,
} from "../js/core/agents.js";

const NODE_A = { node_id: "01NODEA", registration_id: "termini-node-a", role: "compute", status: "online" };
const NODE_B = { node_id: "01NODEB", registration_id: "termini-node-b", role: "compute", status: "online" };
const NODE_C = { node_id: "01NODEC", registration_id: "termini-node-c", role: "inference", status: "offline" };

function sample(instances) {
  const s = { seq: 1, ts: new Date().toISOString() };
  if (instances !== undefined) s.lxd_instances = instances;
  return s;
}

const VM = "7NM4JV8BSD1P8KYBYP8B7BT7CY";

test("one row per reported instance, with the ids the client is allowed to see", () => {
  const samples = new Map([
    [NODE_A.node_id, sample([{ vm_id: VM, state: "running", cpu_util_pct: 3.5, ram_used_bytes: 1024 ** 3, ram_limit_bytes: 2 * 1024 ** 3, disk_used_bytes: 0 }])],
  ]);
  const view = agentGroups({ nodes: [NODE_A], samples });
  assert.equal(view.total, 1);
  assert.equal(view.running, 1);
  const row = view.agents[0];
  assert.equal(row.vmId, VM);
  assert.equal(row.nodeId, NODE_A.node_id);
  assert.equal(row.cpuPct, 3.5);
  assert.equal(row.ramUsed, 1024 ** 3);
  // §4.6 fields the node did not send stay null -> the row renders "not reported"
  assert.equal(row.stateAgeS, null);
  assert.equal(row.restarts, null);
});

test("an omitted lxd_instances is 'not reported', never 'no agents'", () => {
  const samples = new Map([[NODE_B.node_id, sample(undefined)]]);
  const view = agentGroups({ nodes: [NODE_B], samples });
  assert.equal(view.groups[0].inventory, INVENTORY.ABSENT);
  assert.equal(view.total, 0);
  assert.equal(view.known, false, "no node reported an inventory, so 'zero' is not known");
  assert.match(agentsSummary(view).text, /unknown/);
});

test("an empty array is a real report of zero running instances", () => {
  const samples = new Map([[NODE_A.node_id, sample([])]]);
  const view = agentGroups({ nodes: [NODE_A], samples });
  assert.equal(view.groups[0].inventory, INVENTORY.REPORTED);
  assert.equal(view.total, 0);
  assert.equal(view.known, true);
  assert.equal(agentsSummary(view).text, "agents 0");
});

test("a node with no sample renders as unknown, not empty", () => {
  const view = agentGroups({ nodes: [NODE_A], samples: new Map() });
  assert.equal(view.groups[0].inventory, INVENTORY.NO_SAMPLE);
  assert.equal(view.known, false);
});

test("a retained (stopped-by-you) agent stays visible and is counted stopped", () => {
  const row = agentFromInstance(NODE_A, { vm_id: VM, state: "running" });
  const retained = new Map([[VM, { ...row, state: "stopped", retained: true }]]);
  const samples = new Map([[NODE_A.node_id, sample([])]]); // it is no longer reported
  const view = agentGroups({ nodes: [NODE_A], samples, retained });
  assert.equal(view.total, 1);
  assert.equal(view.stopped, 1);
  assert.equal(view.agents[0].retained, true);
  assert.match(agentsSummary(view).text, /1 stopped/);
});

test("synthetic (seeder) nodes are shown as evidence, never counted as the client's agents", () => {
  const fake = { ...NODE_A, node_id: "01DEMO", registration_id: "demo-node-a", synthetic: true };
  const samples = new Map([[fake.node_id, sample([{ vm_id: VM, state: "running" }])]]);
  const view = agentGroups({ nodes: [fake], samples });
  // Item 5 of t_df3120a8: do not hide rows. The row is what the panel was
  // shown; hiding it would claim a fleet the panel had not looked at.
  assert.equal(view.nodes, 1, "the demo node's group is rendered");
  assert.equal(view.realNodes, 0);
  assert.equal(view.groups[0].synthetic, true);
  assert.equal(view.demoAgents.length, 1, "its row is drawn");
  assert.equal(view.agents.length, 0, "but it is not one of the client's agents");
  assert.equal(view.total, 0);
  assert.equal(view.demo, 1);
  assert.equal(view.demoAgents[0].taskable, false, "and it cannot be given a task");
  assert.equal(view.demoAgents[0].taskableReason, TASK_REFUSAL.DEMO);
  assert.equal(view.demoAgents[0].actionsAvailable, false, "nor started/stopped/restarted: there is no instance at all");
  // No REAL node reported an inventory, so the pill still says "unknown" —
  // and it names the demo row rather than counting it as an agent.
  assert.match(agentsSummary(view).text, /agents unknown/);
  assert.match(agentsSummary(view).text, /1 demo/);
});

test("taskability comes from provenance, never from the shape of the id", () => {
  const demoNode = { ...NODE_A, synthetic: true };
  const demo = agentFromInstance(demoNode, { vm_id: "NNB9287WRHGFDPEZ995TKCXDNW", state: "running" });
  assert.equal(demo.synthetic, true);
  assert.equal(demo.taskable, false);
  assert.equal(demo.taskableReason, TASK_REFUSAL.DEMO);
  assert.match(demo.taskableReason, /demo row: seeder-fabricated instance, no agent runtime behind it/);

  // The SAME id attested by a REAL node is taskable: the flag decides, not the hash.
  const real = agentFromInstance(NODE_A, { vm_id: "NNB9287WRHGFDPEZ995TKCXDNW", state: "running" });
  assert.equal(real.synthetic, false);
  assert.equal(real.taskable, true);
  assert.equal(real.taskableReason, null);

  // An instance that is not running is not listening either.
  assert.equal(taskability({ vmId: VM, state: "running" }).taskable, true);
  assert.equal(taskability({ vmId: VM, state: "stopped" }).taskable, false);
  assert.equal(taskability({ vmId: VM, state: "running", retained: true }).taskable, false);
  assert.match(taskability({ retained: true }).reason, /stopped by you/);
  assert.equal(taskability(null).taskable, false);
});

test("a 503 that could not read a node reads as exactly that, and names the node", () => {
  const err = {
    status: 503,
    code: "NODE_UNREACHABLE",
    message: "the node holding agent NNB9287WRHGFDPEZ995TKCXDNW cannot be reached, so its state cannot be established",
    request_id: "01HXREQ000000000000000000A",
    details: [
      {
        field: "vm_id",
        reason: "node_unreachable",
        unscanned_nodes: [{ node_id: "01NODEB", why: "instance list not reachable (LXD did not answer)" }],
        attributed_nodes: ["01NODEA"],
      },
    ],
  };
  const view = refusalView(err);
  assert.equal(view.kind, "node_unreadable");
  assert.equal(view.status, 503);
  assert.equal(view.code, "NODE_UNREACHABLE");
  assert.equal(view.requestId, "01HXREQ000000000000000000A");
  assert.match(view.headline, /could not be read/);
  assert.match(view.headline, /nothing was dispatched/);
  assert.deepEqual(view.nodes, [{ nodeId: "01NODEB", why: "instance list not reachable (LXD did not answer)" }]);
  assert.deepEqual(view.attributed, ["01NODEA"]);
  // EVERY details[0] field is rendered, in reading order.
  assert.deepEqual(view.fields.map((f) => f.name), ["field", "reason", "unscanned_nodes", "attributed_nodes"]);
  // The rendered reason NAMES the unreadable node, with its own why.
  assert.match(view.text, /01NODEB — instance list not reachable \(LXD did not answer\)/);
  assert.match(view.text, /request_id: 01HXREQ000000000000000000A/);
});

test("a 404 says the id is not on this project, and renders the safe fields only", () => {
  const err = {
    status: 404,
    code: "AGENT_NOT_FOUND",
    message: "no agent NNB9287WRHGFDPEZ995TKCXDNW on this project's nodes",
    request_id: "01HX404000000000000000000A",
    details: [
      {
        field: "vm_id",
        reason: "unknown",
        literal_name: "NNB9287WRHGFDPEZ995TKCXDNW",
        derived_agent_id: "7NM4JV8BSD1P8KYBYP8B7BT7CY",
        sources: ["live_allocations", "live_lxd_enumeration", "node_telemetry"],
        known_agent_ids: ["7NM4JV8BSD1P8KYBYP8B7BT7CY"],
        unscanned_nodes: [],
      },
    ],
  };
  const view = refusalView(err);
  assert.equal(view.kind, "not_on_project");
  assert.match(view.headline, /not on this project/);
  assert.match(view.headline, /nothing was dispatched/);
  const byName = Object.fromEntries(view.fields.map((f) => [f.name, f.value]));
  assert.equal(byName.known_agent_ids, "7NM4JV8BSD1P8KYBYP8B7BT7CY", "the ids the controller CAN see are rendered");
  assert.equal(byName.derived_agent_id, "7NM4JV8BSD1P8KYBYP8B7BT7CY");
  assert.equal(byName.literal_name, "NNB9287WRHGFDPEZ995TKCXDNW");
  assert.match(byName.sources, /node_telemetry/);
  assert.equal(byName.unscanned_nodes, "none", "an empty list is what the controller said: it read every node");
  assert.equal(byName.known_vm_ids, undefined, "internal instance names left the contract — they are not invented here");
  assert.match(view.text, /request_id: 01HX404000000000000000000A/);
});

test("a refusal the panel makes itself uses the same shape, with no request_id", () => {
  const local = refusedLocally(TASK_REFUSAL.STOPPED, VM);
  assert.equal(local.kind, "not_taskable");
  assert.equal(local.requestId, null);
  assert.match(local.headline, /No task was sent/);
  const byName = Object.fromEntries(local.fields.map((f) => [f.name, f.value]));
  assert.equal(byName.reason, TASK_REFUSAL.STOPPED);
  assert.equal(byName.vm_id, VM);
});

test("an unreachable controller is a network refusal, not a 404", () => {
  const view = refusalView({ status: 0, code: "NETWORK", message: "Failed to fetch" });
  assert.equal(view.kind, "network");
  assert.equal(view.status, null);
  assert.equal(view.requestId, null);
  assert.match(view.headline, /could not be reached/);
  assert.deepEqual(view.fields, [], "nothing was claimed about details the response did not carry");
});

test("the honest inference label: forwarded managed model is named, not implied local", () => {
  const forwarded = inferenceLabel({
    backend: "gemini-proxy",
    answered_by: "forwarded-managed-model",
    destination: "aiplatform.googleapis.com",
    egress: "external",
    forwarded: true,
  }, "gemma");
  assert.equal(forwarded.kind, "forwarded");
  assert.match(forwarded.text, /forwarded managed model/);
  assert.match(forwarded.text, /gemini-proxy/);
  assert.match(forwarded.detail, /NOT local GPU inference/);

  const local = inferenceLabel({ backend: "local", answered_by: "llama.cpp", forwarded: false }, "gemma");
  assert.equal(local.kind, "local");
  assert.match(local.text, /local inference/);

  const unknown = inferenceLabel({ backend: "unknown", source: "unlabelled-endpoint" }, "gemma");
  assert.equal(unknown.kind, "unknown");
  assert.doesNotMatch(unknown.text, /local/);
  assert.match(unknown.detail, /does not claim it was served locally/);

  const none = inferenceLabel(null, null);
  assert.equal(none.kind, "unknown");
  assert.match(none.detail, /no task has answered/);
});

test("taskView exposes queued/running/done/error and the provenance with the answer", () => {
  assert.equal(taskView({ state: "queued", task_id: "01T" }).terminal, false);
  const done = taskView({
    state: "done", task_id: "01T", answer: "pong", model: "gemma",
    duration_ms: 915, provenance: { backend: "gemini-proxy", forwarded: true },
  });
  assert.equal(done.answer, "pong");
  assert.equal(done.terminal, true);
  assert.equal(inferenceLabel(done.provenance, done.model).kind, "forwarded");

  const failed = taskView({ state: "error", error_code: "INFERENCE_UNREACHABLE", error_message: "no endpoint" });
  assert.equal(failed.terminal, true);
  assert.equal(failed.errorCode, "INFERENCE_UNREACHABLE");
});

test("vmIdForName reproduces the §4.6 literals the node and controller use", async () => {
  assert.equal(await vmIdForName("termini-01m4wjhwqr3b8pcav2tvqbgg-0-0"), "7NM4JV8BSD1P8KYBYP8B7BT7CY");
  assert.equal(await vmIdForName("termini-01m4wjhwqr3b8pcav2tvqbgg-0-1"), "VW4XZD24288MCXGT4557CDYFF9");
  assert.equal(await vmIdForName("demo-workload"), "NNB9287WRHGFDPEZ995TKCXDNW");
});

test("grouping keeps nodes in fleet order and counts a stopped node's rows as they were", () => {
  const samples = new Map([
    [NODE_A.node_id, sample([{ vm_id: VM, state: "running" }])],
    [NODE_C.node_id, sample([{ vm_id: "VW4XZD24288MCXGT4557CDYFF9", state: "running" }])],
  ]);
  const view = agentGroups({ nodes: [NODE_A, NODE_B, NODE_C], samples });
  assert.deepEqual(view.groups.map((g) => g.nodeId), [NODE_A.node_id, NODE_B.node_id, NODE_C.node_id]);
  assert.equal(view.groups[2].status, "offline");
  assert.equal(view.total, 2);
  assert.equal(view.inventoryMissing, 1); // NODE_B omitted the field
});

test("the Agents sheet draws with theme tokens, so it is legible on the HIG light sheet", async () => {
  // The panel shipped before the HIG promotion made the root sheet LIGHT; its
  // hardcoded dark surfaces made a finished answer dark-on-dark (found live on
  // 2026-10-17). A surface or ink literal in this block is the bug, so pin it.
  const { readFile } = await import("node:fs/promises");
  const css = await readFile(new URL("../assets/styles.css", import.meta.url), "utf8");
  const start = css.indexOf("/* ------------------------------------------------------------------ agents */");
  assert.ok(start > -1, "agents block present");
  const block = css.slice(start);
  const literal = block.match(/(background|color)\s*:\s*#[0-9a-fA-F]{3,6}/g) || [];
  assert.deepEqual(literal, [], `hardcoded colours in the agents block: ${literal.join(", ")}`);
  assert.match(block, /\.agent-answer \{[^}]*background: var\(--bg\)/, "answer surface uses the theme surface");
  assert.match(block, /\.agent-inference-forwarded \{[^}]*color: var\(--warn\)/, "forwarded label uses the warn token");
});

/* --------------------------------------------- the panel's DOM (t_df3120a8) */

test("a demo row draws a disabled action and the reason, and no request is attempted", async () => {
  const { installDom } = await import("./helpers/minidom.mjs");
  installDom();
  const { AgentsPanel } = await import("../js/ui/agents.js");

  const host = document.createElement("div");
  const calls = [];
  const panel = new AgentsPanel(
    { groups: host, empty: document.createElement("div"), inference: null, status: null, refreshBtn: null },
    { onTask: (vmId, prompt, agent) => calls.push({ vmId, prompt, agent }) },
  );

  const DEMO_VM = "NNB9287WRHGFDPEZ995TKCXDNW";
  const demoNode = { node_id: "01DEMO", registration_id: "demo-node-a", role: "compute", status: "online", synthetic: true };
  const view = agentGroups({
    nodes: [NODE_A, demoNode],
    samples: new Map([
      [NODE_A.node_id, sample([{ vm_id: VM, state: "running" }])],
      [demoNode.node_id, sample([{ vm_id: DEMO_VM, state: "running" }])],
    ]),
  });
  panel.render(view, new Map());

  const rows = host.querySelectorAll(".agent-row");
  const demoRow = rows.find((r) => r.dataset.agent === DEMO_VM);
  assert.ok(demoRow, "the demo row is still on screen — it is evidence about the fleet");

  // The node group says the row is fabricated, and the row's action is refused
  // with the reason IN THE PANEL — not after a 404.
  const group = demoRow.parentElement;
  assert.equal(group.dataset.node, "01DEMO");
  assert.match(group.textContent, /fabricated by the demo seeder/);
  // Nothing on this row can work: no instance exists, so every control 404s.
  for (const btn of demoRow.querySelectorAll(".agent-actions .btn")) {
    assert.ok(btn.hasAttribute("disabled"), `${btn.textContent} is disabled on a demo row`);
    assert.equal(btn.getAttribute("title"), TASK_REFUSAL.DEMO);
  }
  const toggle = demoRow.querySelector(".agent-task-toggle");
  assert.ok(toggle, "the action is rendered, so the reader sees what is unavailable");
  assert.ok(toggle.hasAttribute("disabled"), "but it is disabled: nothing can answer it");
  assert.equal(toggle.getAttribute("title"), TASK_REFUSAL.DEMO);
  const reason = demoRow.querySelector(".agent-task-reason");
  assert.ok(reason, "the reason is a visible line, not only a tooltip");
  assert.match(reason.textContent, /demo row: seeder-fabricated instance, no agent runtime behind it/);
  assert.equal(toggle.getAttribute("aria-describedby"), reason.id);

  // No request is attempted: the control cannot be pressed, and no box opens.
  toggle.dispatchEvent({ type: "click" });
  assert.equal(calls.length, 0, "no task request was attempted for a demo row");
  assert.equal(demoRow.querySelector(".agent-prompt"), null, "and no prompt box opens");
  assert.equal(panel.open.size, 0);

  // A real node's row still works: the same id, a real provenance.
  const realRow = host.querySelectorAll(".agent-row").find((r) => r.dataset.agent === VM);
  const realToggle = realRow.querySelector(".agent-task-toggle");
  assert.equal(realToggle.hasAttribute("disabled"), false, "a real row offers the action");
  realToggle.dispatchEvent({ type: "click" });
  const box = host.querySelector(".agent-prompt");
  assert.ok(box, "the real row's task box opens");
  assert.equal(host.querySelectorAll(".agent-row").find((r) => r.dataset.agent === VM).querySelector(".agent-task-reason"), null);
});

test("the panel draws a refusal as the controller's answer: code, message, request_id, details", async () => {
  const { installDom } = await import("./helpers/minidom.mjs");
  installDom();
  const { AgentsPanel } = await import("../js/ui/agents.js");

  const host = document.createElement("div");
  const panel = new AgentsPanel(
    { groups: host, empty: document.createElement("div"), inference: null, status: null, refreshBtn: null },
    {},
  );
  const view = agentGroups({
    nodes: [NODE_A],
    samples: new Map([[NODE_A.node_id, sample([{ vm_id: VM, state: "running" }])]]),
  });

  const refusal = refusalView({
    status: 503,
    code: "NODE_UNREACHABLE",
    message: "the node holding agent NN B cannot be reached, so its state cannot be established",
    request_id: "01HXREQ000000000000000000A",
    details: [
      {
        field: "vm_id",
        reason: "node_unreachable",
        unscanned_nodes: [{ node_id: "01NODEB", why: "instance list not reachable (LXD did not answer)" }],
        attributed_nodes: ["01NODEA"],
      },
    ],
  });
  panel.render(view, new Map([[VM, taskView({ state: "error", error_code: "NODE_UNREACHABLE", error_message: refusal.message, refusal, task_id: null })]]));

  const block = host.querySelector(".agent-refusal");
  assert.ok(block, "the refusal is drawn");
  const body = block.textContent;
  assert.match(body, /could not be read/, "a 503 naming an unreadable node reads as that, not as a generic failure");
  assert.match(body, /503 · NODE_UNREACHABLE/, "the code and status are shown");
  assert.match(body, /the node holding agent NN B cannot be reached/, "the controller's own message is shown");
  assert.match(body, /request_id 01HXREQ000000000000000000A/, "the request id is shown");
  assert.match(body, /01NODEB — instance list not reachable/, "the unreadable node is named, with its why");
  assert.match(body, /ATTRIBUTED_NODES|attributed_nodes/, "every details[0] field is rendered");
  assert.match(body, /01NODEA/);

  // A 404 is the OTHER story: the id is not on this project.
  const missing = refusalView({
    status: 404,
    code: "AGENT_NOT_FOUND",
    message: "no agent 7NM4JV8BSD1P8KYBYP8B7BT7CY on this project's nodes",
    request_id: "01HX404000000000000000000A",
    details: [{ field: "vm_id", reason: "unknown", known_agent_ids: ["7NM4JV8BSD1P8KYBYP8B7BT7CY"], unscanned_nodes: [] }],
  });
  panel.render(view, new Map([[VM, taskView({ state: "error", error_code: "AGENT_NOT_FOUND", error_message: missing.message, refusal: missing, task_id: null })]]));
  const missingBody = host.querySelector(".agent-refusal").textContent;
  assert.match(missingBody, /not on this project/);
  assert.match(missingBody, /known_agent_ids/);
  assert.doesNotMatch(missingBody, /known_vm_ids/);
  assert.doesNotMatch(missingBody, /could not be read/);
});

test("a half-typed draft survives a re-render — drafts live on the panel, not the DOM", async () => {
  const { installDom } = await import("./helpers/minidom.mjs");
  installDom();
  const { AgentsPanel } = await import("../js/ui/agents.js");

  const host = document.createElement("div");
  const panel = new AgentsPanel(
    { groups: host, empty: document.createElement("div"), inference: null, status: null, refreshBtn: null },
    {},
  );
  const view = agentGroups({
    nodes: [NODE_A],
    samples: new Map([[NODE_A.node_id, sample([{ vm_id: VM, state: "running" }])]]),
  });
  panel.render(view, new Map());
  host.querySelector(".agent-task-toggle").dispatchEvent({ type: "click" });
  const box = host.querySelector(".agent-prompt");
  box.value = "half-typed prompt";
  box.dispatchEvent({ type: "input" });

  // The poll re-renders while the reader is typing.
  panel.render(agentGroups({
    nodes: [NODE_A],
    samples: new Map([[NODE_A.node_id, sample([{ vm_id: VM, state: "running", cpu_util_pct: 9.5 }])]]),
  }), new Map());

  const again = host.querySelector(".agent-prompt");
  assert.ok(again, "the open box survives the poll");
  assert.equal(again.value, "half-typed prompt", "and so does the half-typed prompt (drafts live on the panel)");
});

test("the refusal the DEPLOYED controller answers today renders, without re-leaking instance names", () => {
  // Verbatim body captured from the live controller on 2026-10-18
  // (POST /v1/projects/…/agents/00000000000000000000000000/tasks → 404). It is
  // a controller that predates t_a1fa0567: it still sends `known_vm_ids`
  // (internal instance NAMES). The panel renders the safe id and DROPS the
  // superseded field rather than echoing what the contract stopped sending.
  const live = {
    status: 404,
    code: "AGENT_NOT_FOUND",
    message: "no agent 00000000000000000000000000 on this project's nodes",
    request_id: "01M56V8NBS2R1W1TZKRFN09Y34",
    details: [
      {
        field: "vm_id",
        reason: "unknown",
        known_vm_ids: ["termini-01m55jewvas5whw0qjnckpvj-0-0"],
        known_agent_ids: ["T6K2AZ0PQ08AGKMZTKGRM7QGB0"],
      },
    ],
  };
  const view = refusalView(live);
  assert.equal(view.kind, "not_on_project");
  const byName = Object.fromEntries(view.fields.map((f) => [f.name, f.value]));
  assert.equal(byName.reason, "unknown");
  assert.equal(byName.known_agent_ids, "T6K2AZ0PQ08AGKMZTKGRM7QGB0");
  assert.equal(byName.known_vm_ids, undefined, "internal instance names are not rendered any more");
  assert.doesNotMatch(view.text, /termini-01m55jewvas5whw0qjnckpvj/);
  assert.equal(view.requestId, "01M56V8NBS2R1W1TZKRFN09Y34");
});
