/*
 * Apply-plan provisioning placeholders (task t_6d63e8a2).
 *
 * After `Apply plan` the Agents panel must show the plan's instances IMMEDIATELY
 * as `provisioning` rows, computed from the instance names the controller
 * returned, and then let the node's own telemetry turn them into real rows. On
 * the live fleet (2026-10-17) the placeholder never appeared and the only
 * immediate feedback was a toast, so the first stage of REQUIRED WORK 3 of the
 * parent card was missing.
 *
 * These tests pin the whole chain, because the failure was a silent one:
 *
 *   1. `expectedRows()` reads BOTH response shapes the endpoint has answered with
 *      — per-allocation `allocations[].vm_ids` and the flat top-level `vm_ids` —
 *      and never invents a row for an allocation that materialised nothing;
 *   2. the id it derives is the §4.6 hash of the instance name, i.e. the id the
 *      node will report (checked against the running mock's own telemetry);
 *   3. `pruneExpected()` drops a placeholder only once telemetry reports it, and
 *      stops claiming "provisioning" when nothing ever does;
 *   4. the panel draws the placeholder row from that state (mini-DOM), so a
 *      "dropped before it rendered" regression cannot come back green.
 *
 * Run: node --test dashboard/tests/apply-placeholder.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const DASHBOARD = join(HERE, "..");
const PROJECT_ID = "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B";
const PROJECT_TOKEN = "demo-project-token-000000000000000000000000000000000000000000";
const NODE_A = "01H4KZ9Q2R3T5V7W9X1Y3Z5B1E";

const {
  EXPECTED_DROP_MS,
  EXPECTED_STALE_MS,
  EXPECTED_UNCONFIRMED,
  expectedRows,
  expectedSignature,
  materialisedNames,
  pruneExpected,
  vmIdForName,
} = await import("../js/core/agents.js");

/* --------------------------------------------------------------- fixtures */

/** The 202 body `controller/app/server.py _apply_plan` answers. */
function liveJob(overrides = {}) {
  return {
    job_id: "01M55J5TFQSYSG6W9YQAPY4E56",
    plan_id: "01M55JEWVA000000000000000A",
    state: "ready",
    vm_ids: ["termini-01m55jewva00000000000000a-0-0"],
    allocations: [
      {
        index: 0,
        status: "ready",
        node_id: NODE_A,
        realized_as: "vm",
        vm_ids: ["termini-01m55jewva00000000000000a-0-0"],
        created: 1,
        already_applied: 0,
      },
      {
        index: 1,
        status: "ready",
        node_id: NODE_A,
        realized_as: "host_service",
        vm_ids: [],
        detail: "host_service reference; no VM materialised",
      },
    ],
    ...overrides,
  };
}

/** Deterministic stand-in for the SHA-256 derivation inside the pure tests. */
async function fakeHash(name) {
  return `ID(${name})`;
}

/* ------------------------------------------------------- expectedRows shapes */

test("a live apply response becomes one provisioning row per instance, with the §4.6 id", async () => {
  const job = liveJob();
  const now = 1_700_000_000_000;
  const rows = await expectedRows(job, [], now);

  assert.equal(rows.length, 1, "one row per materialised instance (the host_service allocation adds none)");
  const [row] = rows;
  assert.equal(row.name, "termini-01m55jewva00000000000000a-0-0");
  assert.equal(row.nodeId, NODE_A, "the row points at the node the allocation resolved to");
  assert.equal(row.state, "provisioning");
  assert.equal(row.hashed, true);
  // The id the panel will match the node's report against.
  assert.equal(row.vmId, await vmIdForName(row.name));
  assert.equal(row.staleAt, now + EXPECTED_STALE_MS);
  assert.equal(row.dropAt, now + EXPECTED_DROP_MS);
});

test("a response with no usable allocations[] falls back to the flat vm_ids (the shape that broke live)", async () => {
  // 2026-10-17: the endpoint answered `{job_id, state, vm_ids:[…]}` with no
  // allocations and the panel silently rendered nothing. The names are still
  // instance names, so the rows can still be drawn.
  const job = { job_id: "J", state: "provisioning", vm_ids: ["termini-a-0-0", "termini-a-0-1"] };
  const rows = await expectedRows(job, [], 0, fakeHash);
  assert.deepEqual(rows.map((r) => r.name), ["termini-a-0-0", "termini-a-0-1"]);
  assert.deepEqual(rows.map((r) => r.vmId), ["ID(termini-a-0-0)", "ID(termini-a-0-1)"]);
  assert.deepEqual(rows.map((r) => r.nodeId), [null, null], "node stays unknown, never invented");
});

test("allocations[] win over the flat list when both are present", async () => {
  const job = liveJob({
    vm_ids: ["termini-flat-0-0"],
    allocations: [{ index: 0, status: "ready", node_id: NODE_A, vm_ids: ["termini-structured-0-0"] }],
  });
  const rows = await expectedRows(job, [], 0, fakeHash);
  assert.deepEqual(rows.map((r) => r.name), ["termini-structured-0-0"], "no duplicate row from the roll-up");
});

test("an allocation that materialised nothing never becomes a row", async () => {
  const job = {
    allocations: [
      { index: 0, status: "skipped", node_id: NODE_A, vm_ids: [], detail: "allocation status 'rejected'" },
      { index: 1, status: "failed", node_id: NODE_A, vm_ids: [], detail: "create failed; rolled back" },
      { index: 2, status: "ready", node_id: NODE_A, realized_as: "host_service", vm_ids: [] },
      { index: 3, status: "ready", node_id: NODE_A, vm_ids: ["termini-ok-3-0"] },
    ],
    vm_ids: ["termini-ok-3-0"],
  };
  const rows = await expectedRows(job, [], 0, fakeHash);
  assert.deepEqual(rows.map((r) => r.name), ["termini-ok-3-0"]);
  assert.deepEqual(materialisedNames(job), ["termini-ok-3-0"]);
});

test("an instance telemetry already reports is not drawn twice", async () => {
  const name = "termini-01m55jewva00000000000000a-0-0";
  const job = { allocations: [{ index: 0, status: "ready", node_id: NODE_A, vm_ids: [name, "termini-new-0-1"] }] };
  const reported = [{ vmId: await vmIdForName(name) }, { vmId: undefined }];
  const rows = await expectedRows(job, reported, 0);
  assert.deepEqual(rows.map((r) => r.name), ["termini-new-0-1"]);
  // A set of bare ids works too (what main.js passes from the loaded samples).
  const rows2 = await expectedRows(job, [await vmIdForName(name)], 0);
  assert.deepEqual(rows2.map((r) => r.name), ["termini-new-0-1"]);
});

test("without crypto.subtle the row still renders, marked as unmatchable", async () => {
  const job = { allocations: [{ index: 0, status: "ready", node_id: NODE_A, vm_ids: ["termini-nocrypto-0-0"] }] };
  const rows = await expectedRows(job, [], 0, async () => {
    throw new Error("crypto.subtle is unavailable (needs a secure context)");
  });
  assert.equal(rows.length, 1, "a non-secure context must not hide the plan's outcome");
  assert.equal(rows[0].vmId, null);
  assert.equal(rows[0].hashed, false);
});

test("an apply that materialised nothing yields no rows", async () => {
  assert.deepEqual(await expectedRows({ state: "ready", vm_ids: [], allocations: [] }, [], 0), []);
  assert.deepEqual(await expectedRows(null, [], 0), []);
  assert.deepEqual(materialisedNames(null), []);
});

/* --------------------------------------------------------------- pruning */

test("the placeholder is dropped exactly when telemetry reports its id", async () => {
  const name = "termini-01m55jewva00000000000000a-0-0";
  const now = 1_000_000;
  const rows = await expectedRows({ allocations: [{ index: 0, status: "ready", node_id: NODE_A, vm_ids: [name] }] }, [], now);

  // Not reported yet: the row stays, unchanged (this is the state that must be
  // on screen right after Apply plan).
  assert.equal(pruneExpected(rows, [], now + 500).length, 1);
  assert.equal(pruneExpected(rows, [], now + 500)[0].state, "provisioning");

  // Reported: the real row supersedes it.
  assert.deepEqual(pruneExpected(rows, [rows[0].vmId], now + 500), []);
  // A different id reported does not remove it.
  assert.equal(pruneExpected(rows, ["SOMEONEELSE0000000000000000"], now + 500).length, 1);
});

test("a row no node ever reports stops claiming 'provisioning', then goes away", async () => {
  const now = 0;
  const rows = await expectedRows(
    { allocations: [{ index: 0, status: "ready", node_id: NODE_A, vm_ids: ["termini-rolled-back-0-0"] }] },
    [],
    now,
    fakeHash,
  );
  const stale = pruneExpected(rows, [], now + EXPECTED_STALE_MS + 1);
  assert.equal(stale.length, 1, "still shown — the instance may exist on a node that cannot report it");
  assert.equal(stale[0].state, EXPECTED_UNCONFIRMED);
  assert.notEqual(expectedSignature(stale), expectedSignature(rows), "the state change is visible to the re-render check");
  assert.deepEqual(pruneExpected(rows, [], now + EXPECTED_DROP_MS + 1), [], "eventually dropped: telemetry is the only inventory");
});

/* ------------------------------------------------------- the panel's DOM */

test("the Agents panel draws the placeholder row, and drops it when the agent arrives", async () => {
  const { installDom } = await import("./helpers/minidom.mjs");
  const document = installDom();
  const { AgentsPanel } = await import("../js/ui/agents.js");
  const { agentGroups } = await import("../js/core/agents.js");

  const host = document.createElement("div");
  const empty = document.createElement("div");
  const panel = new AgentsPanel({ groups: host, empty, inference: null, status: null, refreshBtn: null }, {});
  const nodes = [{ node_id: NODE_A, registration_id: "termini-node-a", role: "compute", status: "online" }];

  const name = "termini-01m55jewva00000000000000a-0-0";
  const rows = await expectedRows({ allocations: [{ index: 0, status: "ready", node_id: NODE_A, vm_ids: [name] }] }, [], Date.now());
  const emptyView = agentGroups({ nodes, samples: new Map([[NODE_A, { seq: 1, ts: new Date().toISOString(), lxd_instances: [] }]]) });
  panel.render(emptyView, new Map());
  panel.setExpected(rows);

  const drawn = host.querySelectorAll(".agent-row-expected");
  assert.equal(drawn.length, 1, "the provisioning row is on screen after Apply plan");
  assert.match(drawn[0].textContent, /provisioning/);
  assert.match(drawn[0].textContent, new RegExp(name), "the row names the instance the controller created");
  // It is drawn inside its node's group, where the reader is already looking.
  let ancestor = drawn[0].parentElement;
  while (ancestor && !(ancestor.classList && ancestor.classList.contains("agent-group"))) {
    ancestor = ancestor.parentElement;
  }
  assert.ok(ancestor, "the placeholder sits inside a node group");
  assert.equal(ancestor.dataset.node, NODE_A, "and inside the group of the node the allocation resolved to");

  // The node's next sample lists it: prune, then render — the row is gone and a
  // real agent row is in its place.
  const reportedView = agentGroups({
    nodes,
    samples: new Map([[NODE_A, { seq: 2, ts: new Date().toISOString(), lxd_instances: [{ vm_id: rows[0].vmId, state: "running" }] }]]),
  });
  const remaining = pruneExpected(rows, reportedView.agents, Date.now());
  assert.deepEqual(remaining, []);
  panel.setExpected(remaining);
  panel.render(reportedView, new Map());
  assert.equal(host.querySelectorAll(".agent-row-expected").length, 0, "the placeholder does not outlive the report");
  assert.equal(reportedView.total, 1, "and the agent itself is listed once");
});

/* --------------------------------------- the mock must answer like the controller */

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function waitForServer(base, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${base}/`);
      if (res.ok) return;
      last = `status ${res.status}`;
    } catch (err) {
      last = err.message;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`mock server did not become ready: ${last}`);
}

test("mock: apply answers the controller's shape, and the placeholder id becomes the reported one", async (t) => {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  // `--agents none`: every node reports an empty inventory, so the only way an
  // instance can appear in telemetry is the apply that materialised it.
  const child = spawn(
    process.env.PYTHON || "python3",
    [join(DASHBOARD, "mock", "server.py"), "--port", String(port), "--agents", "none", "--materialise-delay", "0.5"],
    { cwd: DASHBOARD, stdio: ["ignore", "pipe", "pipe"] },
  );
  let stderr = "";
  child.stderr.on("data", (b) => {
    stderr += b.toString();
  });
  t.after(() => child.kill("SIGTERM"));
  await waitForServer(base);

  const headers = { Authorization: `Bearer ${PROJECT_TOKEN}`, "Content-Type": "application/json" };
  const plan = await (
    await fetch(`${base}/v1/projects/${PROJECT_ID}/intents`, {
      method: "POST",
      headers,
      body: JSON.stringify({ prompt: "deploy 1 worker agent for coding tasks" }),
    })
  ).json();
  assert.equal(plan.outcome, "accepted");

  const res = await fetch(`${base}/v1/projects/${PROJECT_ID}/vms/apply`, {
    method: "POST",
    headers,
    body: JSON.stringify({ plan_id: plan.plan_id }),
  });
  const job = await res.json();
  assert.equal(res.status, 202, `apply answered 202: ${stderr || "no server error"}`);
  assert.ok(Array.isArray(job.allocations) && job.allocations.length, "the mock answers the controller's per-allocation shape");
  const names = materialisedNames(job);
  assert.ok(names.length > 0, "the apply response carries instance names");
  assert.ok(
    job.allocations.some((a) => a.status === "ready" && a.vm_ids.length && a.vm_ids.every((n) => names.includes(n))),
    "allocations[].vm_ids are the instance names",
  );

  const rows = await expectedRows(job, [], Date.now());
  assert.equal(rows.length, names.length, "one placeholder per materialised instance");
  assert.ok(rows.every((r) => r.hashed), "every placeholder has the id the node will report");

  // Right after the apply the node has not noticed the instances yet — this is
  // the window the placeholder exists for.
  const before = await (await fetch(`${base}/v1/projects/${PROJECT_ID}/nodes/${rows[0].nodeId}`, { headers })).json();
  const reportedNow = (before.metrics_latest.lxd_instances || []).map((i) => i.vm_id);
  assert.deepEqual(reportedNow.filter((id) => rows.some((r) => r.vmId === id)), [], "the placeholder leads telemetry");

  // After the node's next sample they are reported under exactly the ids the
  // panel derived — so the comparison that prunes the placeholder cannot miss.
  const deadline = Date.now() + 10000;
  let reported = [];
  while (Date.now() < deadline) {
    const views = await Promise.all(
      [...new Set(rows.map((r) => r.nodeId))].map(async (nid) =>
        (await (await fetch(`${base}/v1/projects/${PROJECT_ID}/nodes/${nid}`, { headers })).json()),
      ),
    );
    reported = views.flatMap((v) => (v.metrics_latest.lxd_instances || []).map((i) => i.vm_id));
    if (rows.every((r) => reported.includes(r.vmId))) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  for (const row of rows) {
    assert.ok(reported.includes(row.vmId), `the node reports ${row.vmId} for ${row.name}`);
  }
  assert.deepEqual(pruneExpected(rows, reported, Date.now()), [], "every placeholder collapses into a reported row");
});
