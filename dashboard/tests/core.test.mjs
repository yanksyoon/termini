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
import { normalizeLine, appendLines, SEVERITIES, severityVisible, isControlLine } from "../js/core/logs.js";
import { resolveConfig, apiUrl, DEFAULTS } from "../js/core/config.js";
import {
  sourceMeta,
  parseEgressLine,
  deciderMeta,
  buildDecisionRows,
  thresholdMarker,
  annotationsForNode,
  formatMetricValue,
  breachValueText,
  actionTarget,
} from "../js/core/decisions.js";
import { FIRST_CURSOR } from "../js/stream.js";

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
  const u = apiUrl({ apiBase: "/v1" }, "/p/1/logs", { since: "A" });
  assert.match(u, /since=A/);
  // A credential in a URL is refused at the builder (task t_5649d117).
  assert.throws(() => apiUrl({ apiBase: "/v1" }, "/p/1/logs", { since: "A", token: "tok" }), /credential/);
});

test("config: offline flag and clamping", () => {
  assert.equal(resolveConfig({ query: { offline: "1" } }).offline, true);
  assert.equal(resolveConfig({ query: { pollMs: "10" } }).pollMs, 1000);
  assert.equal(resolveConfig({ query: { pollMs: "999999" } }).pollMs, 60000);
});

/* --------------------------------------------------------------- log stream */

test("logs: the §2.3 truncation marker is a control line, not an event", () => {
  const control = normalizeLine({
    log_id: "01M4NZKDT5EF05BCSK1SEG1RNN",
    ts: "2026-10-15T15:36:12Z",
    node_id: null,
    severity: "warn",
    actor: "ai_operator",
    message: "log tail truncated; resuming from oldest retained line",
    remediation: null,
    truncated: true,
  });
  assert.equal(control.control, true);
  assert.equal(isControlLine(control), true);
  const real = normalizeLine({ log_id: "X", message: "hello", severity: "info" });
  assert.equal(real.control, false);
  assert.equal(isControlLine(real), false);
});

test("stream: a cold start asks for the retained tail (§2.3 out-of-buffer cursor)", () => {
  // A ULID of all zeros is older than anything the ring buffer can hold, so the
  // server takes its documented out-of-buffer path and replays the tail instead
  // of delivering nothing until the next event.
  assert.equal(FIRST_CURSOR, "00000000000000000000000000");
  assert.equal(FIRST_CURSOR.length, 26);
  assert.match(FIRST_CURSOR, /^[0-9A-HJKMNP-TV-Z]{26}$/);
  const url = apiUrl({ apiBase: "https://c/v1" }, "/projects/P/logs", { since: FIRST_CURSOR });
  assert.match(url, /since=0{26}/);
  assert.ok(!url.includes("token"), "the stream URL carries no credential (header only)");
});

/* --------------------------------------------------------------- decisions */

const EGRESS_FORWARDED =
  "externalModelCall after endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external " +
  "model=gemini-2.5-flash destination=aiplatform.googleapis.com answered_by=forwarded-managed-model " +
  "outcome=returned request_id=01M4ZJRGAW86QHPDHTN3JY76FG";
const EGRESS_LOCAL =
  "externalModelCall after endpoint=http://10.10.0.13:8080 backend=local egress=in-vpc model=gemma " +
  "answered_by=local-weights outcome=returned request_id=01M4ZJRGAW86QHPDHTN3JY76FG";

test("decisions: the egress record is parsed off the operator log line", () => {
  const rec = parseEgressLine(EGRESS_FORWARDED);
  assert.equal(rec.phase, "after");
  assert.equal(rec.backend, "gemini-proxy");
  assert.equal(rec.model, "gemini-2.5-flash");
  assert.equal(rec.destination, "aiplatform.googleapis.com");
  assert.equal(rec.egress, "external");
  assert.equal(rec.answered_by, "forwarded-managed-model");
  assert.equal(rec.outcome, "returned");
  assert.equal(rec.endpoint, "http://10.10.0.13:8080");
  assert.equal(parseEgressLine("oom-guard-01: instance.restart x -> ok"), null);
  assert.equal(parseEgressLine(null), null);
});

test("decisions: the decider label never claims local inference for a forwarded model", () => {
  const fwd = deciderMeta(parseEgressLine(EGRESS_FORWARDED));
  assert.equal(fwd.kind, "forwarded");
  assert.match(fwd.label, /forwarded managed model/);
  assert.equal(fwd.label.includes("gemini-2.5-flash"), true);
  assert.equal(fwd.detail.includes("local weights"), true); // only to deny it explicitly
  const fields = Object.fromEntries(fwd.fields);
  assert.equal(fields.backend, "gemini-proxy");
  assert.equal(fields.destination, "aiplatform.googleapis.com");

  const loc = deciderMeta(parseEgressLine(EGRESS_LOCAL));
  assert.equal(loc.kind, "local");
  assert.match(loc.label, /local weights/);

  const unknown = deciderMeta(null);
  assert.equal(unknown.known, false);
  assert.equal(unknown.kind, "unknown");
  assert.match(unknown.label, /unknown/);
});

test("decisions: model and fallback are never presented the same way", () => {
  const model = sourceMeta("model");
  const fallback = sourceMeta("fallback");
  const det = sourceMeta("deterministic");
  assert.equal(model.byModel, true);
  assert.equal(fallback.byModel, false);
  assert.equal(det.byModel, false);
  assert.notEqual(model.label, fallback.label);
  assert.notEqual(model.tone, fallback.tone);
  assert.match(fallback.note, /did NOT decide/);
});

const DECISION = {
  id: "01M53A",
  event_id: "01M53E",
  node_id: "N1",
  rule_id: "oom-guard-01",
  playbook: "oom-guard-01",
  action_kind: "instance.restart",
  action_params: { instance_name: "agent-01", force: true, timeout_s: 30 },
  reason: "RAM is at 96% of total; restarting the heaviest instance reclaims it.",
  decided_at: "2026-10-15T15:45:11.242Z",
  decision_source: "model",
  confidence: 0.9,
  fallback_reason: null,
};
const EVENT = {
  id: "01M53E",
  node_id: "N1",
  rule_id: "oom-guard-01",
  type: "oom",
  metric: "ram_used_bytes",
  value: 33114165248,
  threshold: ">= 95% of ram_total_bytes for 2 consecutive samples",
  evidence_seq_start: 1,
  evidence_seq_end: 3,
  fired_at: "2026-10-15T15:45:11.000Z",
  breach_started_at: "2026-10-15T15:44:59.000Z",
  evidence: [],
};
const AUDIT = {
  id: "01M53B",
  decision_id: "01M53A",
  event_id: "01M53E",
  node_id: "N1",
  rule_id: "oom-guard-01",
  action_kind: "instance.restart",
  result: "error",
  detail: "HTTP 404: Instance not found",
  lxd_method: "PUT",
  lxd_path: "/1.0/instances/agent-01/state",
  lxd_body: "{}",
  idempotency_key: "N1:oom-guard-01:instance.restart:agent-01:3",
  dry_run: 0,
  executed_at: "2026-10-15T15:45:11.300Z",
  lxd_endpoint: "https://10.10.0.11:8443",
};

test("decisions: one row per decision joins event + audit by their own keys", () => {
  const rows = buildDecisionRows({ decisions: [DECISION], events: [EVENT], audits: [AUDIT] }, { auditWindow: 50 });
  assert.equal(rows.length, 1);
  const r = rows[0];
  assert.equal(r.nodeId, "N1");
  assert.equal(r.ruleId, "oom-guard-01");
  assert.equal(r.source, "model");
  assert.equal(r.confidence, 0.9);
  assert.equal(r.target, "agent-01");
  assert.equal(r.breach.metric, "ram_used_bytes");
  assert.equal(r.breach.valueText, "30.84 GiB");
  assert.match(r.breach.threshold, /95% of ram_total_bytes/);
  assert.equal(r.breach.sampleSeq, 3);
  assert.equal(r.auditId, "01M53B");
  assert.equal(r.execution.result, "error");
  assert.equal(r.execution.ok, false);
  assert.equal(r.execution.endpoint, "https://10.10.0.11:8443");
});

test("decisions: an audit outside the fetched window is reported, never hidden", () => {
  const rows = buildDecisionRows({ decisions: [DECISION], events: [EVENT], audits: [] }, { auditWindow: 50 });
  assert.equal(rows[0].audit, null);
  assert.equal(rows[0].auditId, null);
  assert.equal(rows[0].auditMissing, true);
  assert.equal(rows[0].execution, null);
});

test("decisions: a fallback row carries its reason and no confidence", () => {
  const fb = { ...DECISION, id: "01M53F", decision_source: "fallback", confidence: null, fallback_reason: "inference_unavailable" };
  const rows = buildDecisionRows({ decisions: [fb], events: [EVENT], audits: [] });
  assert.equal(rows[0].source, "fallback");
  assert.equal(rows[0].confidence, null);
  assert.equal(rows[0].fallbackReason, "inference_unavailable");
  assert.equal(rows[0].sourceMeta.byModel, false);
});

test("decisions: values are formatted with the frozen unit registry", () => {
  assert.equal(formatMetricValue("ram_used_bytes", 34359738368), "32.00 GiB");
  assert.equal(formatMetricValue("cpu_util_pct", 31.24), "31.2%");
  assert.equal(formatMetricValue("disk_write_latency_ms", 120), "120.0 ms");
  assert.equal(formatMetricValue("ram_used_bytes", null), "—");
});

test("decisions: the trigger value carries a unit only when the API justifies one", () => {
  // The controller sends ram_used_bytes = 96.04 for an OOM breach: it is a
  // percentage, and stamping "bytes" on it would print "96 B".
  assert.equal(breachValueText({ ...EVENT, value: 96.04 }), "96.04% of ram_total_bytes");
  // the same rule with the absolute byte value keeps the byte unit
  assert.equal(breachValueText(EVENT), "30.84 GiB");
  // a threshold that names the event metric resolves to that metric's unit
  assert.equal(
    breachValueText({ metric: "net_latency_ms", value: 240, threshold: "net_latency_ms >= 200 for 3 consecutive samples" }),
    "240.0 ms",
  );
  // an ambiguous OR-list names neither -> raw, never a fabricated unit
  assert.equal(
    breachValueText({ metric: "disk_free_bytes", value: 120, threshold: "free <= 5% OR write_latency_ms >= 50 OR free slope <= -5000000 bytes/s" }),
    "120.00 (raw)",
  );
  assert.equal(breachValueText({ metric: "ram_used_bytes", value: null }), "—");
});

test("decisions: the action target is read from action_params", () => {
  assert.equal(actionTarget(DECISION).name, "agent-01");
  assert.equal(actionTarget({ action_params: {} }).name, null);
  assert.equal(actionTarget(null).name, null);
});

test("decisions: a threshold line is drawn only when it resolves unambiguously", () => {
  const series = { ram_total_bytes: { name: "ram_total_bytes", unit: "bytes", points: [{ t: "T", v: 68719476736 }] } };
  const rel = thresholdMarker(EVENT, series);
  assert.equal(rel.basis, "95% of ram_total_bytes");
  assert.equal(Math.round(rel.value), Math.round(68719476736 * 0.95));
  // no total series -> no line, but the threshold TEXT is still shown in the row
  assert.equal(thresholdMarker(EVENT, {}), null);
  // an ambiguous "free <= 5%" style threshold resolves to nothing rather than a guess
  assert.equal(thresholdMarker({ metric: "disk_free_bytes", threshold: "free <= 5% OR write_latency_ms >= 50" }, {}), null);
  // absolute threshold on the event's own metric does resolve
  const abs = thresholdMarker(
    { metric: "disk_write_latency_ms", threshold: "disk_write_latency_ms >= 50 OR free <= 5%" },
    { disk_write_latency_ms: { unit: "ms", points: [] } },
  );
  assert.equal(abs.value, 50);
});

test("decisions: chart annotations are per node and scaled by their group", () => {
  // The dashboard resolves the threshold from the node's own /metrics series, so
  // pass one in exactly as the metrics poll does.
  const series = { ram_total_bytes: { name: "ram_total_bytes", unit: "bytes", points: [{ t: "T", v: 68719476736 }] } };
  const rows = buildDecisionRows({ decisions: [DECISION], events: [EVENT], audits: [AUDIT], seriesByNode: { N1: series } });
  const ann = annotationsForNode(rows, "N1");
  assert.equal(ann.length, 1);
  assert.equal(ann[0].metric, "ram_used_bytes");
  assert.equal(ann[0].kind, "model");
  assert.equal(annotationsForNode(rows, "OTHER").length, 0);

  const payload = {
    series: [
      { name: "ram_used_bytes", unit: "bytes", points: [{ t: "2026-10-15T15:44:00.000Z", v: 68719476736 }] },
    ],
  };
  const groups = buildCharts(payload, { annotations: ann });
  const mem = groups.find((g) => g.id === "mem");
  assert.ok(mem, "memory group present");
  assert.equal(mem.markers.length, 1);
  assert.equal(mem.thresholds.length, 1);
  // bytes -> GiB: the threshold is 95% of the 64 GiB total, drawn at 60.8
  assert.equal(Math.round(mem.thresholds[0].value), 61);
  assert.equal(mem.fault, true);
  // a group without the annotated metric gets no marks
  const cpu = groups.find((g) => g.id === "cpu");
  assert.equal((cpu && cpu.markers.length) || 0, 0);
});
