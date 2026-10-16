/**
 * The client's own node view (task `t_5649d117`, frozen contract on `t_074b1a14`
 * option (a)): registration state, first telemetry, and the instructive empty
 * state — all pure, so the contract is asserted in tests instead of eyeballed.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  FRESH_S,
  NODE_IDENTITY_PATH,
  OFFLINE_S,
  STATE,
  emptyState,
  healthCards,
  healthSummary,
  newestSample,
  oneLiner,
  registrationView,
  sampleAgeS,
} from "../js/core/client-node.js";

test("empty state instructs the documented one-liner and what the client will see", () => {
  const es = emptyState({ controller: "https://controller.example" });
  const text = JSON.stringify(es);
  assert.match(text, /install\/termini-node\.sh/);
  assert.match(text, /--controller https:\/\/controller\.example/);
  assert.match(text, /--token <CREDENTIAL>/);
  assert.match(text, /YOUR NODE IS REGISTERED/);
  assert.match(text, /termini-node-status --watch/);
  assert.match(text, /node_token/);
  assert.match(text, /single-use/);
  // the empty state never hands the client a provider credential
  assert.ok(!text.includes("demo-project-token"));
  assert.ok(!text.includes("project_token"));
  assert.equal(es.promise.length, 3);
});

test("oneLiner builds the frozen contract's command from the controller origin", () => {
  assert.equal(
    oneLiner("https://c.example/"),
    "curl -fsSL https://c.example/install/termini-node.sh | sudo sh -s -- --controller https://c.example --token <CREDENTIAL>",
  );
  assert.match(oneLiner(""), /https:\/\/<controller>\/install\/termini-node\.sh/);
});

test("no credential -> the instructive empty state, never an error", () => {
  const v = registrationView({});
  assert.equal(v.state, STATE.NO_CREDENTIAL);
  assert.equal(v.tone, "muted");
  assert.equal(v.sample, null);
  assert.equal(registrationView({ nodeId: "01M4", nodeToken: "" }).state, STATE.NO_CREDENTIAL);
});

test("registration states derive from the samples the controller returned", () => {
  const now = Date.parse("2026-10-15T16:00:30Z");
  const cred = { nodeId: "01M4TGD31W9VC52ZDAVMPCMJSH", nodeToken: "node-credential" };

  const fresh = { seq: 12, ts: "2026-10-15T16:00:25Z", cpu_util_pct: 31.2 };
  const r = registrationView({ ...cred, samples: [fresh], now });
  assert.equal(r.state, STATE.REGISTERED);
  assert.equal(r.tone, "ok");
  assert.equal(Math.round(r.ageS), 5);

  const late = { seq: 11, ts: "2026-10-15T16:00:00Z" }; // 30 s old
  assert.equal(registrationView({ ...cred, samples: [late], now }).state, STATE.DEGRADED);

  const gone = { seq: 10, ts: "2026-10-15T15:50:00Z" };
  const u = registrationView({ ...cred, samples: [gone], now });
  assert.equal(u.state, STATE.UNREACHABLE);
  assert.equal(u.tone, "err");

  // enrolled but the controller has no sample for it yet
  assert.equal(registrationView({ ...cred, samples: [], now }).state, STATE.PENDING);
});

test("thresholds are the frozen contract's 15 s / 45 s", () => {
  assert.equal(FRESH_S, 15);
  assert.equal(OFFLINE_S, 45);
});

test("controller refusals are distinct and actionable", () => {
  const cred = { nodeId: "01M4", nodeToken: "node-credential" };
  const rejected = registrationView({ ...cred, error: { status: 403, message: "nope" } });
  const down = registrationView({ ...cred, error: { status: 0, message: "Failed to fetch" } });
  assert.equal(rejected.state, STATE.CREDENTIAL_REJECTED);
  assert.equal(down.state, STATE.CONTROLLER_UNREACHABLE);
  assert.match(rejected.detail, /403/);
  assert.match(rejected.detail, new RegExp(NODE_IDENTITY_PATH.replace(/\//g, "\\/")));
  assert.notEqual(rejected.detail, down.detail);
  assert.equal(rejected.sample, null);
});

test("the newest sample wins regardless of the order the controller returned", () => {
  const a = { seq: 1, ts: "2026-10-15T16:00:00Z" };
  const b = { seq: 2, ts: "2026-10-15T16:00:05Z" };
  assert.equal(newestSample([b, a]).seq, 2);
  assert.equal(newestSample([a, b]).seq, 2);
  assert.equal(newestSample([]), null);
  assert.equal(sampleAgeS({ ts: "2026-10-15T16:00:00Z" }, Date.parse("2026-10-15T16:00:10Z")), 10);
  assert.equal(sampleAgeS({}), null);
});

test("first telemetry: heartbeat, CPU, memory and runtime facts become cards", () => {
  const sample = {
    seq: 42,
    ts: "2026-10-15T16:00:00Z",
    uptime_sec: 3600,
    cpu_util_pct: 31.2,
    cpu_core_count: 16,
    ram_total_bytes: 34359738368,
    ram_used_bytes: 19756849561,
    load_avg_1m: 1.42,
    os_name: "Ubuntu",
    os_version: "24.04",
    kernel_version: "6.8.0-142-generic",
    disk: [{ path: "/dev/sda", total_bytes: 274877906944, free_bytes: 214748364800 }],
    services: [
      { name: "termini-bootstrap", status: "running" },
      { name: "lxd", status: "running" },
    ],
  };
  const s = healthSummary(sample);
  assert.equal(s.heartbeat.seq, 42);
  assert.equal(s.heartbeat.uptimeS, 3600);
  assert.equal(s.cpu.pct, 31.2);
  assert.equal(s.cpu.cores, 16);
  assert.ok(Math.abs(s.mem.pct - 57.5) < 0.05, `memory percent ${s.mem.pct}`);
  assert.equal(s.runtime.kernel, "6.8.0-142-generic");

  const keys = healthCards(s).map((c) => c.key);
  for (const k of ["heartbeat", "cpu", "mem", "uptime", "load", "runtime", "services"]) {
    assert.ok(keys.includes(k), `cards carry ${k}`);
  }
  const heartbeat = healthCards(s).find((c) => c.key === "heartbeat");
  assert.equal(heartbeat.value, "seq 42");

  assert.equal(healthSummary(null), null);
  assert.deepEqual(healthCards(null), []);
});

test("missing telemetry fields degrade to dashes, never to NaN or a crash", () => {
  const s = healthSummary({ ts: "2026-10-15T16:00:00Z" });
  assert.equal(s.cpu.pct, null);
  assert.equal(s.mem.pct, null);
  const cards = healthCards(s);
  assert.equal(cards.find((c) => c.key === "cpu").value, "—");
  assert.equal(cards.find((c) => c.key === "mem").value, "—");
});
