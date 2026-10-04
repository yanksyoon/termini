/**
 * Unit tests for the dashboard's pure core modules (no DOM, no network).
 *
 *   node --test dashboard/tests/
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { pct, num, bytes, scaleForUnit, ago, uptime, shortId, hhmm, clock } from "../js/core/format.js";
import { deriveHealth, nodeView, SERVICE_ALLOWLIST } from "../js/core/health.js";
import { buildCharts, windowQuery, metricsQuery, METRIC_UNITS, CHART_GROUPS } from "../js/core/series.js";
import { normalizeLine, appendLines, SEVERITIES, severityVisible } from "../js/core/logs.js";
import { resolveConfig, apiUrl, DEFAULTS } from "../js/core/config.js";

/* --------------------------------------------------------------- format */

test("format: numbers, percents and unknown values", () => {
  assert.equal(pct(31.2), "31.2%");
  assert.equal(pct(null), "—");
  assert.equal(pct(undefined), "—");
  assert.equal(num(1234.5, 1), "1,234.5");
  assert.equal(num("x", 1), "—");
});

test("format: bytes selects the right binary unit", () => {
  assert.match(bytes(19756849561), /^18\.40 GiB$/);
  assert.match(bytes(5 * 1024 * 1024), /MiB$/);
  assert.equal(scaleForUnit("bytes").label, "GiB");
  assert.equal(scaleForUnit("percent").label, "%");
});

test("format: relative time and uptime", () => {
  const now = Date.parse("2026-10-04T07:49:00Z");
  assert.equal(ago("2026-10-04T07:48:57Z", now), "3s ago");
  assert.equal(ago(null, now), "never");
  assert.equal(uptime(3661), "1h 1m");
  assert.equal(uptime(59), "59s");
  assert.equal(uptime(null), "—");
});

test("format: identifiers and clocks", () => {
  assert.equal(shortId("01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E").includes("…"), true);
  assert.equal(shortId("short"), "short");
  assert.match(clock("2026-10-04T07:49:00Z"), /^\d{2}:\d{2}:\d{2}$/);
  assert.match(hhmm("2026-10-04T07:49:00Z"), /^\d{2}:\d{2}$/);
});

/* --------------------------------------------------------------- health */

const COMPUTE = { node_id: "N1", role: "compute" };
const INFERENCE = { node_id: "N2", role: "inference" };

function sample(over = {}) {
  return {
    ts: "2026-10-04T07:49:00.000Z",
    cpu_util_pct: 31.2,
    ram_total_bytes: 34359738368,
    ram_used_bytes: 19756849561,
    disk: [{ path: "/dev/sda", total_bytes: 100, free_bytes: 50, write_latency_ms: 3, mount_ro: false }],
    net: [{ iface: "eth0", rx_bytes: 1, tx_bytes: 1, latency_ms: 0.5 }],
    gpu: [],
    services: [
      { name: "termini-bootstrap", status: "running" },
      { name: "lxd", status: "running" },
    ],
    ...over,
  };
}

test("health: compute node has null gpu/inference flags", () => {
  const h = deriveHealth(COMPUTE, sample());
  assert.equal(h.disk_ok, true);
  assert.equal(h.gpu_ok, null);
  assert.equal(h.inference_ok, null);
  assert.deepEqual(h.degraded_reasons, []);
  assert.equal(h.services.length, 2);
});

test("health: read-only disk and slow writes degrade", () => {
  const h = deriveHealth(COMPUTE, sample({ disk: [{ mount_ro: true, write_latency_ms: 80 }] }));
  assert.equal(h.disk_ok, false);
  assert.ok(h.degraded_reasons.includes("disk_read_only"));
  assert.ok(h.degraded_reasons.includes("disk_degrading"));
});

test("health: inference node GPU over-temp and missing GPU", () => {
  const hot = deriveHealth(INFERENCE, sample({ gpu: [{ temp_celsius: 90 }], inference: { server_up: true } }));
  assert.equal(hot.gpu_ok, false);
  assert.ok(hot.degraded_reasons.includes("gpu_overtemp"));

  const none = deriveHealth(INFERENCE, sample({ gpu: [] }));
  assert.equal(none.gpu_ok, false);
  assert.ok(none.degraded_reasons.includes("gpu_unavailable"));

  const ok = deriveHealth(INFERENCE, sample({ gpu: [{ temp_celsius: 60 }], inference: { server_up: true } }));
  assert.equal(ok.gpu_ok, true);
  assert.equal(ok.inference_ok, true);

  const down = deriveHealth(INFERENCE, sample({ gpu: [{ temp_celsius: 60 }], inference: { server_up: false } }));
  assert.equal(down.inference_ok, false);
  assert.ok(down.degraded_reasons.includes("inference_down"));
});

test("health: a missing sample leaves flags at their defaults (no invented failure)", () => {
  const h = deriveHealth(INFERENCE, null);
  assert.equal(h.gpu_ok, null);
  assert.equal(h.disk_ok, null);
  assert.deepEqual(h.degraded_reasons, []);
});

test("health: service failures are detected on the allowlist only", () => {
  const h = deriveHealth(COMPUTE, sample({ services: [{ name: "lxd", status: "failed" }, { name: "not-allowed", status: "failed" }] }));
  assert.ok(h.degraded_reasons.includes("service_failed"));
  assert.equal(h.services.length, 1);
  assert.deepEqual(SERVICE_ALLOWLIST.includes("llama-cpp"), true);
});

test("health: status derivation from heartbeat age (contract §1.1)", () => {
  const now = Date.parse("2026-10-04T07:49:00Z");
  const fresh = nodeView({ ...COMPUTE, last_seen_at: "2026-10-04T07:48:58Z" }, sample(), now);
  assert.equal(fresh.status, "online");
  const late = nodeView({ ...COMPUTE, last_seen_at: "2026-10-04T07:48:40Z" }, sample(), now);
  assert.equal(late.status, "degraded");
  assert.ok(late.reasons.includes("heartbeat_late"));
  const gone = nodeView({ ...COMPUTE, last_seen_at: "2026-10-04T07:47:00Z" }, sample(), now);
  assert.equal(gone.status, "offline");
});

test("health: server-provided status/health is respected", () => {
  const now = Date.parse("2026-10-04T07:49:00Z");
  const v = nodeView(
    { ...COMPUTE, status: "degraded", last_seen_at: "2026-10-04T07:48:59Z", health: { disk_ok: false, degraded_reasons: ["disk_read_only"] } },
    sample(),
    now,
  );
  assert.equal(v.status, "degraded");
  assert.deepEqual(v.reasons, ["disk_read_only"]);
});

/* --------------------------------------------------------------- series */

test("series: chart groups only reference allowlisted metrics", () => {
  for (const g of CHART_GROUPS) {
    for (const m of g.metrics) assert.ok(METRIC_UNITS[m], `unknown metric ${m}`);
  }
  assert.equal(Object.keys(METRIC_UNITS).length, 19);
});

test("series: buildCharts scales bytes to GiB and skips empty series", () => {
  const payload = {
    series: [
      { name: "cpu_util_pct", unit: "percent", points: [{ t: "2026-10-04T07:49:00Z", v: 31.2 }] },
      { name: "ram_used_bytes", unit: "bytes", points: [{ t: "2026-10-04T07:49:00Z", v: 1073741824 }] },
      { name: "gpu_util_pct", unit: "percent", points: [] },
    ],
  };
  const charts = buildCharts(payload);
  const cpu = charts.find((c) => c.id === "cpu");
  assert.equal(cpu.series[0].points[0].v, 31.2);
  const mem = charts.find((c) => c.id === "mem");
  assert.equal(mem.series[0].points[0].v, 1); // 1 GiB
  assert.equal(charts.find((c) => c.id === "gpu"), undefined); // no points -> dropped
});

test("series: window query maps to contract resolutions", () => {
  assert.equal(windowQuery("5m", 300, 0).resolution, "5s");
  assert.equal(windowQuery("15m", 900, 0).resolution, "5s");
  assert.equal(windowQuery("1h", 3600, 0).resolution, "1m");
  assert.equal(windowQuery("6h", 21600, 0).resolution, "5m");
  const q = windowQuery("15m", 900, 1000);
  assert.ok(q.start && q.end && q.resolution);
});

test("series: metrics query is de-duplicated", () => {
  assert.equal(metricsQuery(["a", "a", "b"]), "a,b");
});

/* --------------------------------------------------------------- logs */

test("logs: normalizes the frozen envelope and the legacy thin shape", () => {
  const full = normalizeLine({
    log_id: "L1",
    ts: "t",
    node_id: "N1",
    severity: "warn",
    actor: "ai_operator",
    message: "hello",
    remediation: { command_id: "C1", type: "node.drain", status: "pending_approval" },
  });
  assert.equal(full.severity, "warn");
  assert.equal(full.remediation.type, "node.drain");

  const legacy = normalizeLine({ ts: "t", level: "error", text: "legacy", node_id: null });
  assert.equal(legacy.severity, "error");
  assert.equal(legacy.message, "legacy");
  assert.equal(legacy.actor, "ai_operator");

  assert.equal(normalizeLine({ severity: "info" }), null); // no message
  assert.equal(normalizeLine(null), null);
});

test("logs: severity filter and ring-buffer cap", () => {
  assert.equal(severityVisible("warn", new Set(["warn", "error"])), true);
  assert.equal(severityVisible("debug", new Set(["warn"])), false);
  const buf = appendLines([], [1, 2, 3, 4, 5], 3);
  assert.deepEqual(buf, [3, 4, 5]);
  assert.deepEqual(SEVERITIES, ["debug", "info", "warn", "error"]);
});

/* --------------------------------------------------------------- config */

test("config: precedence query > stored > file > defaults", () => {
  const cfg = resolveConfig({
    query: { projectId: "Q" },
    stored: { projectId: "S", token: "T" },
    file: { projectId: "F", apiBase: "https://c/v1" },
    defaults: DEFAULTS,
  });
  assert.equal(cfg.projectId, "Q");
  assert.equal(cfg.token, "T");
  assert.equal(cfg.apiBase, "https://c/v1");
  assert.equal(cfg.pollMs, 5000);
});

test("config: apiBase normalisation and url building", () => {
  assert.equal(apiUrl({ apiBase: "" }, "/projects/1/nodes"), "/v1/projects/1/nodes");
  assert.equal(apiUrl({ apiBase: "http://h" }, "/projects/1/nodes"), "http://h/v1/projects/1/nodes");
  assert.equal(apiUrl({ apiBase: "http://h/v1" }, "/projects/1/nodes"), "http://h/v1/projects/1/nodes");
  const u = apiUrl({ apiBase: "/v1" }, "/p/1/logs", { since: "A", token: "tok" });
  assert.match(u, /since=A/);
  assert.match(u, /token=tok/);
});

test("config: offline flag and clamping", () => {
  assert.equal(resolveConfig({ query: { offline: "1" } }).offline, true);
  assert.equal(resolveConfig({ query: { pollMs: "10" } }).pollMs, 1000);
  assert.equal(resolveConfig({ query: { pollMs: "999999" } }).pollMs, 60000);
});
