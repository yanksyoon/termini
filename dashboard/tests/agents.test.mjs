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
 *   - the §4.6 id derivation in JS matches the controller/node literal.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  INVENTORY,
  agentFromInstance,
  agentGroups,
  agentsSummary,
  inferenceLabel,
  taskView,
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

test("synthetic (seeder) nodes are never treated as hardware", () => {
  const fake = { ...NODE_A, synthetic: true };
  const samples = new Map([[NODE_A.node_id, sample([{ vm_id: VM, state: "running" }])]]);
  const view = agentGroups({ nodes: [fake], samples });
  assert.equal(view.nodes, 0);
  assert.equal(view.total, 0);
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
