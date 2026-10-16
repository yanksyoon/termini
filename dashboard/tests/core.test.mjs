/**
 * Unit tests for the dashboard's pure core modules (no DOM, no network).
 *
 *   node --test dashboard/tests/
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { pct, num, bytes, scaleForUnit, ago, uptime, shortId, hhmm, clock } from "../js/core/format.js";
import { deriveHealth, nodeView, SERVICE_ALLOWLIST, PROCESSES_MAX } from "../js/core/health.js";
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
  incidentSourceMeta,
  incidentView,
  incidentEvidence,
  incidentActions,
  evidenceValueText,
  noIncidentReason,
  traceView,
  traceStepView,
  traceDeciderMeta,
  noTraceReason,
  TRACE_MAX_STEPS,
  TRACE_STEP_LABEL_MAX_CHARS,
  TRACE_STEP_DETAIL_MAX_CHARS,
  TRACE_REPLY_MAX_CHARS,
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

test("health: processes are allowlisted to the contract fields (§4.10)", () => {
  // A node that sends C2 or junk alongside the metrics must not get it rendered:
  // the panel drops anything outside the contract instead of displaying it.
  const h = deriveHealth(COMPUTE, sample({
    processes: [
      { pid: 4187, name: "llama-server", state: "running", cpu_util_pct: 41.5,
        ram_used_bytes: 1024, threads: 9, age_sec: 900 },
      { pid: 921, name: "cmdline", cmdline: "/bin/sh -c secret", argv: ["x"] },
      { pid: 0, name: "bad-pid" },
      { pid: 12, name: "a very long process name", state: "sleepy" },
      "not-an-object",
    ],
  }));
  assert.equal(h.processes.length, 3);   // 4187, 921 (junk keys dropped) and 12
  assert.deepEqual(Object.keys(h.processes[0]).sort(),
    ["age_sec", "cpu_util_pct", "name", "pid", "ram_used_bytes", "state", "threads"]);
  // the second entry keeps only the contract keys — `cmdline`/`argv` are gone
  assert.equal(h.processes[1].pid, 921);
  assert.deepEqual(Object.keys(h.processes[1]).sort(), ["name", "pid"]);
  assert.equal(h.processes[1].cmdline, undefined);
  assert.equal(h.processes[1].argv, undefined);
  // pid 12 survives on its pid alone: the over-long name and the unknown state
  // are dropped rather than displayed
  assert.deepEqual(h.processes[2], { pid: 12 });
});

test("health: processes are bounded to the top-10 (RSS-ranked, §4.10)", () => {
  assert.equal(PROCESSES_MAX, 10);
  const many = Array.from({ length: 30 }, (_, i) => ({ pid: 100 + i, name: `p${i}` }));
  const h = deriveHealth(COMPUTE, sample({ processes: many }));
  assert.equal(h.processes.length, PROCESSES_MAX);
});

test("health: a missing sample yields no processes rather than inventing some", () => {
  assert.deepEqual(deriveHealth(COMPUTE, null).processes, []);
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

/* --------------------------------------------- incident card (§4.2 / §8.1) */

// The record as the controller persists it (migration 0016): five columns on the
// decision row, `incident_evidence`/`incident_actions` already parsed back into
// arrays by GET /v1/operator/decisions (operator-decision.md §11).
const INCIDENT = {
  incident_generated: "model",
  incident_title: "Memory exhaustion on node 01M4349RPTT29RNTC6VBCXQ0RC",
  incident_explanation:
    "ram_used_bytes reached 96.0% of ram_total_bytes for two consecutive samples and one instance holds 29% of the node's RAM by itself.",
  incident_evidence: [
    { field: "ram_used_bytes", value: 32641751449, seq: 1050, ts: "2026-10-04T07:49:05.000Z" },
    { field: "ram_total_bytes", value: 34359738368, seq: 1050, ts: "2026-10-04T07:49:05.000Z" },
    { field: "lxd_instances.ram_used_bytes", value: 10000000000, seq: 1050 },
  ],
  incident_actions: [
    { action_kind: "instance.restart", target: "01M4349RPXWDC52ZDAVMPCMJSH",
      note: "restart the heaviest instance to reclaim its resident memory" },
  ],
};

test("decisions: the §4.2 incident record lands in the row view model", () => {
  const d = { ...DECISION, ...INCIDENT };
  const rows = buildDecisionRows({ decisions: [d], events: [EVENT], audits: [AUDIT] }, { auditWindow: 50 });
  const r = rows[0];
  assert.equal(r.incidentSource, "model");
  assert.equal(r.incident.generated, "model");
  assert.equal(r.incidentSource, r.incident.generated);
  assert.equal(r.incident.title, INCIDENT.incident_title);
  assert.equal(r.incident.explanation, INCIDENT.incident_explanation);
  assert.equal(r.noIncidentReason, null);
  // evidence: the contractual keys only, in order, with the value's own text
  assert.equal(r.incident.evidence.length, 3);
  assert.deepEqual(Object.keys(r.incident.evidence[0]).sort(), ["field", "seq", "ts", "value", "valueText"]);
  assert.equal(r.incident.evidence[0].field, "ram_used_bytes");
  assert.equal(r.incident.evidence[0].valueText, "30.40 GiB");
  assert.equal(r.incident.evidence[0].seq, 1050);
  assert.equal(r.incident.evidence[0].ts, "2026-10-04T07:49:05.000Z");
  // a dotted field name is still a field name — the suffix rule gives it bytes
  assert.equal(r.incident.evidence[2].valueText, "9.31 GiB");
  assert.equal(r.incident.evidence[2].seq, 1050);
  assert.equal("ts" in r.incident.evidence[2], false);
  // actions: what the model DECIDED, never an outcome
  assert.deepEqual(r.incident.actions, INCIDENT.incident_actions);
  assert.equal("result" in r.incident.actions[0], false);
  // ...and the executed half is the audit join, kept separate from the record
  assert.equal(r.execution.result, "error");
  assert.equal(r.execution.detail, "HTTP 404: Instance not found");
  assert.equal(r.execution.idempotencyKey, AUDIT.idempotency_key);
  assert.equal(r.auditId, "01M53B");
});

test("decisions: a controller-generated fallback record is labelled, never shown as model output", () => {
  const model = incidentSourceMeta("model");
  const fallback = incidentSourceMeta("fallback");
  assert.equal(model.byModel, true);
  assert.equal(fallback.byModel, false);
  assert.notEqual(model.label, fallback.label);
  assert.notEqual(model.badge, fallback.badge);
  assert.notEqual(model.tone, fallback.tone);
  assert.match(fallback.label, /controller-generated, model unavailable/);
  assert.match(fallback.badge, /MODEL UNAVAILABLE/);
  assert.match(fallback.note, /did NOT write this/);

  // The same distinction must survive the row: a fallback row whose record was
  // built by the controller (§8.1) is NOT a model record, whatever the row's
  // decision_source says about who chose the action.
  const fb = {
    ...DECISION, id: "01M53F", decision_source: "fallback", confidence: null,
    fallback_reason: "inference_unavailable",
    incident_generated: "fallback",
    incident_title: "oom on node N1",
    incident_explanation: "model unavailable (inference_unavailable); the deterministic playbook chose instance.restart on agent-01 for rule oom-guard-01",
    incident_evidence: [{ field: "ram_used_bytes", value: 33000000000, seq: 1122 }],
    incident_actions: [{ action_kind: "instance.restart", target: "agent-01" }],
  };
  const row = buildDecisionRows({ decisions: [fb], events: [EVENT], audits: [] }, { auditWindow: 50 })[0];
  assert.equal(row.incidentSource, "fallback");
  assert.equal(row.incident.modelSourced, false);
  assert.equal(row.incident.meta.tone, "fallback");
  assert.equal(row.incident.evidence.length, 1);
  assert.match(row.incident.explanation, /^model unavailable \(inference_unavailable\)/);
});

test("decisions: a deterministic-mode row has NO incident record — never an empty card", () => {
  const det = { ...DECISION, id: "01M53D", decision_source: "deterministic", confidence: null };
  // The controller writes no incident columns at all in that mode (§8.1), so the
  // row arrives with the five keys present-but-NULL (or absent in a JSON reply).
  const withNulls = buildDecisionRows(
    { decisions: [{ ...det, incident_title: null, incident_explanation: null, incident_evidence: null, incident_actions: null, incident_generated: null }], events: [EVENT], audits: [] },
  )[0];
  assert.equal(withNulls.incident, null);
  assert.equal(withNulls.incidentSource, null);
  assert.equal(withNulls.incidentMeta, null);
  assert.equal(withNulls.noIncidentReason, "no incident record (deterministic mode)");

  // ...and identically when the columns are simply absent from the payload.
  const absent = buildDecisionRows({ decisions: [det], events: [EVENT], audits: [] })[0];
  assert.equal(absent.incident, null);
  assert.equal(absent.incidentSource, null);
  assert.equal(absent.noIncidentReason, "no incident record (deterministic mode)");

  // noIncidentReason never conflates the three ways a row can lack a record
  assert.notEqual(noIncidentReason(det), noIncidentReason({ decision_source: "model" }));
  assert.match(noIncidentReason({ decision_source: "model" }), /model returned none/);
  assert.match(noIncidentReason({ decision_source: "fallback" }), /§8.1/);
});

test("decisions: a record with no provenance is flagged as a bug, not attributed", () => {
  const meta = incidentSourceMeta(null);
  assert.equal(meta.byModel, false);
  assert.equal(meta.bug, true);
  assert.equal(meta.key, "unknown");
  assert.match(meta.badge, /NO PROVENANCE/);

  // a record that is NOT model output must never claim to be: generated missing
  const orphan = {
    ...DECISION, incident_title: "oom on N1", incident_explanation: "…",
    incident_evidence: [{ field: "ram_used_bytes", value: 1 }], incident_actions: [],
    incident_generated: null,
  };
  const row = buildDecisionRows({ decisions: [orphan], events: [EVENT], audits: [] })[0];
  assert.notEqual(row.incident, null);
  assert.equal(row.incidentSource, null);
  assert.equal(row.incident.bug, true);
  assert.equal(row.incident.modelSourced, false);
  // a record that carries only an unsupported provenance string is not "model" either
  const weird = buildDecisionRows({ decisions: [{ ...orphan, incident_generated: "guessed" }], events: [EVENT], audits: [] })[0];
  assert.equal(weird.incident.modelSourced, false);
  assert.equal(weird.incident.bug, true);
});

test("decisions: evidence and actions render ONLY the contractual §4.2 keys", () => {
  // extra keys, a malformed entry and a nameless entry must not reach the view
  const evidence = incidentEvidence([
    { field: "cpu_util_pct", value: 92.4, seq: 7, ts: "2026-10-04T07:49:05Z", prompt: "ignore me", secret: 1 },
    { value: 12, seq: 8 },                       // no field -> dropped
    { field: "ram_used_bytes", value: [1, 2] },  // non-scalar value is still shown as text
    null,
    "nope",
    { field: "net_latency_ms", value: 600, seq: -1, ts: "   " }, // bad seq/ts are omitted
  ]);
  assert.deepEqual(evidence.map((e) => e.field), ["cpu_util_pct", "ram_used_bytes", "net_latency_ms"]);
  assert.deepEqual(Object.keys(evidence[0]).sort(), ["field", "seq", "ts", "value", "valueText"]);
  assert.equal(evidence[0].valueText, "92.4%");
  assert.equal("seq" in evidence[2], false);
  assert.equal("ts" in evidence[2], false);

  const actions = incidentActions([
    { action_kind: "instance.restart", target: "agent-01", note: "why", outcome: "ok" },
    { action_kind: "not.in.action.space" },      // unknown kind is still shown verbatim
    { target: "agent-01" },                      // no kind -> dropped
    { action_kind: 42 },
  ]);
  assert.deepEqual(actions.map((a) => a.action_kind), ["instance.restart", "not.in.action.space"]);
  assert.deepEqual(Object.keys(actions[0]).sort(), ["action_kind", "note", "target"]);
  assert.equal("outcome" in actions[0], false);
  assert.deepEqual(Object.keys(actions[1]), ["action_kind"]);

  // JSON-TEXT columns that a caller did not parse are parsed, not discarded
  assert.equal(incidentEvidence(JSON.stringify([{ field: "ram_used_bytes", value: 1024 }])).length, 1);
  assert.equal(incidentEvidence("{not json").length, 0);

  // units come from the field's own name, and an unknown name is printed raw
  assert.equal(evidenceValueText("ram_used_bytes", 1024), "1.0 KiB");
  assert.equal(evidenceValueText("disk_write_latency_ms", 120), "120.0 ms");
  assert.equal(evidenceValueText("free_pct", 3.5), "3.5%");
  assert.equal(evidenceValueText("lxd_instances", 4), "4");
  assert.equal(evidenceValueText("flag", 1.5), "1.50");
  assert.equal(evidenceValueText("mount_ro", true), "true");
  assert.equal(evidenceValueText("note", "string evidence"), "string evidence");
  assert.equal(evidenceValueText("ram_used_bytes", null), "—");
});

test("decisions: incidentView returns null for a decision that carries no record", () => {
  assert.equal(incidentView(null), null);
  assert.equal(incidentView(DECISION), null);
  assert.equal(incidentView({ incident_generated: null, incident_title: null }), null);
  // a record with only an evidence list and no title/explanation is still a record
  const partial = incidentView({ incident_generated: "model", incident_evidence: [{ field: "cpu_util_pct", value: 10 }] });
  assert.notEqual(partial, null);
  assert.equal(partial.title, "");
  assert.equal(partial.explanation, "");
  assert.equal(partial.evidence.length, 1);
});

/* ------------------------------------- decision trace / thinking log (§4.3) */

// A step as the controller writes it. `extra` lets a test inject keys the
// contract does not define (they must not survive the mapper).
const step = (name, ts, over = {}) => ({
  step: name,
  ts,
  label: `${name} label`,
  detail: `${name}=ok`,
  state: "ok",
  ...(over.extra || {}),
  ...over,
});

// A model trace in the controller's canonical order (§4.3 step table).
const TRACE = {
  trace_version: 1,
  decided_by: "model",
  steps: [
    step("threshold_fired", "2026-10-15T15:45:11.000Z", {
      label: "rule oom-guard-01 fired",
      detail: "rule_id=oom-guard-01 event_id=01M53E fired_at=2026-10-15T15:45:11.000Z detection_latency_ms=2000",
    }),
    step("evidence_window", "2026-10-15T15:45:11.100Z", {
      detail: "seq 1..3 (3 samples) C0 fields: cpu_util_pct, ram_total_bytes, ram_used_bytes",
    }),
    step("prompt_built", "2026-10-15T15:45:11.200Z", {
      detail: "template=operator-decision v1 sha256=9f2c1e77 substituted_fields=14 C0_assertion=ok",
    }),
    step("model_call", "2026-10-15T15:45:12.000Z", {
      label: "model answered",
      detail: "endpoint=external model=deepseek-flash answered_by=forwarded-managed-model latency_ms=812",
    }),
    step("reply_received", "2026-10-15T15:45:12.010Z", {
      label: "model reply received",
      detail: '{"schema_version":1,"action_id":"instance.restart","confidence":0.9}',
    }),
    step("gates", "2026-10-15T15:45:12.020Z", {
      label: "G7 rationale",
      detail: "rule=G7 field=rationale decision=accept (C0-safe, within bounds)",
    }),
    step("action_decided", "2026-10-15T15:45:12.030Z", {
      label: "action decided: instance.restart",
      detail: "action_kind=instance.restart target=agent-01",
    }),
    step("executed", "2026-10-15T15:45:12.300Z", {
      label: "action executed: error",
      detail: "result=error latency_ms=45",
    }),
    step("audit_recorded", "2026-10-15T15:45:12.310Z", {
      label: "audit row written",
      detail: "audit_id=01M53B decision_id=01M53A",
    }),
  ],
};

const rowWithTrace = (trace, decision = DECISION) =>
  buildDecisionRows({ decisions: [{ ...decision, decision_trace: trace }], events: [EVENT], audits: [AUDIT] })[0];

test("decisions: the §4.3 trace lands in the row view model, in order, with only its own keys", () => {
  const r = rowWithTrace(TRACE);
  assert.equal(r.trace.present, true);
  assert.equal(r.traceSource, "model");
  assert.equal(r.traceMeta.byModel, true);
  assert.equal(r.trace.version, 1);
  assert.equal(r.trace.truncated, false);
  assert.equal(r.trace.shownSteps, 9);
  assert.equal(r.noTraceReason, null);
  assert.equal(r.traceBug, false);
  assert.deepEqual(r.trace.steps.map((s) => s.step), TRACE.steps.map((s) => s.step));
  // the raw reply is exposed as its own thing, not joined into the steps
  assert.equal(r.trace.reply.text, TRACE.steps[4].detail);
  assert.equal(r.trace.reply.state, "ok");
  assert.equal(r.trace.reply.withheld, false);
  assert.equal(r.trace.reply.available, true);
  // ONLY the contract's step keys survive — the panel is not a second egress path
  assert.deepEqual(Object.keys(r.trace.steps[0]).sort(), ["detail", "label", "state", "step", "ts"]);
});

test("decisions: unknown keys in the trace and in a step are dropped, never rendered", () => {
  const dirty = traceView({
    trace_version: 1,
    decided_by: "model",
    prompt_text: "the rendered prompt (never client-visible)",
    tokens: { total: 42 },
    steps: [
      step("threshold_fired", "2026-10-15T15:45:11.000Z", {
        extra: { prompt: "SECRET PROMPT", raw_reply: "not the reply step", nested: { a: 1 } },
      }),
      // a step name the §4.3 table does not define: withheld, and counted
      { step: "telemetry_read", ts: "2026-10-15T15:45:11.500Z", label: "read telemetry", detail: "seq=3", state: "ok" },
      "not an object",
      null,
    ],
  });
  assert.equal(dirty.present, true);
  assert.deepEqual(Object.keys(dirty.steps[0]).sort(), ["detail", "label", "state", "step", "ts"]);
  assert.equal("prompt" in dirty.steps[0], false);
  assert.equal("prompt_text" in dirty, false);
  // the refused steps are gone from the timeline but NOT hidden from the reader
  assert.deepEqual(dirty.steps.map((s) => s.step), ["threshold_fired"]);
  assert.equal(dirty.droppedSteps, 3);
});

test("decisions: the mapper re-enforces the §4.3 caps client-side and reports what it cut", () => {
  const many = {
    trace_version: 1,
    decided_by: "model",
    steps: Array.from({ length: 30 }, (_, i) =>
      step("gates", `2026-10-15T15:45:${String(10 + i).padStart(2, "0")}.000Z`, { label: `gate ${i}` }),
    ),
  };
  const t = traceView(many);
  assert.equal(t.cap, TRACE_MAX_STEPS);
  assert.equal(t.totalSteps, 30);
  assert.equal(t.shownSteps, TRACE_MAX_STEPS);
  assert.equal(t.steps.length, TRACE_MAX_STEPS);
  assert.equal(t.truncated, true); // the panel says "showing first 24 of 30 steps"

  const long = traceView({
    trace_version: 1,
    decided_by: "model",
    steps: [
      step("evidence_window", "2026-10-15T15:45:11.100Z", {
        label: "L".repeat(TRACE_STEP_LABEL_MAX_CHARS + 80),
        detail: "D".repeat(TRACE_STEP_DETAIL_MAX_CHARS + 300),
      }),
      {
        step: "reply_received",
        ts: "2026-10-15T15:45:12.010Z",
        label: "model reply received",
        detail: "R".repeat(TRACE_REPLY_MAX_CHARS + 500),
        state: "ok",
        truncated: true,
      },
    ],
  });
  const [ev, rep] = long.steps;
  assert.equal(ev.label.length, TRACE_STEP_LABEL_MAX_CHARS);
  assert.equal(ev.detail.length, TRACE_STEP_DETAIL_MAX_CHARS);
  assert.equal(ev.clamped, true);
  // the reply step keeps the reply bound, not the 512-char detail bound
  assert.equal(rep.detail.length, TRACE_REPLY_MAX_CHARS);
  assert.equal(rep.clamped, true); // cut HERE, at the reply bound — and it says so
  assert.equal(rep.truncated, true);
  assert.equal(long.reply.truncated, true);
  assert.equal(long.clampedSteps, 2); // the over-long label/detail step AND the over-long reply
});

test("decisions: an absent or NULL trace stays absent — never an empty timeline", () => {
  for (const missing of [null, undefined, "", "   ", "{not json", 42, [1, 2], {}]) {
    const t = traceView(missing);
    assert.equal(t.present, false, `${JSON.stringify(missing)} must not render a timeline`);
    assert.deepEqual(t.steps, []);
    assert.equal(t.decidedBy, null);
    assert.equal(t.version, null);
    assert.equal(t.truncated, false);
  }
  assert.match(traceView("{not json").problem, /not JSON/);
  assert.match(traceView({}).problem, /none of the §4.3 keys/);
  // a deterministic-mode row carries no trace columns at all (§8.1)
  const det = { id: "01M53D", event_id: "01M53E", node_id: "N1", rule_id: "latency-01", decision_source: "deterministic", action_kind: "instance.restart", action_params: {}, decided_at: "2026-10-15T15:45:11.242Z" };
  const r = buildDecisionRows({ decisions: [det], events: [EVENT], audits: [] })[0];
  assert.equal(r.trace.present, false);
  assert.equal(r.traceSource, null);
  assert.equal(r.traceMeta, null);
  assert.equal(r.traceBug, false);
  assert.equal(r.trace.steps.length, 0);
  assert.match(r.noTraceReason, /deterministic mode writes none/);
  // ...and the incident half of the same rule still holds
  assert.equal(r.incident, null);
  assert.equal(r.noIncidentReason, "no incident record (deterministic mode)");
  // a fallback row with no trace is the contradiction §4.3 says it is
  assert.match(noTraceReason({ decision_source: "fallback" }), /a fallback row records one/);
});

test("decisions: a rejected step survives the mapper and is never silently hidden", () => {
  // What the controller persists when the raw reply tripped the C2 scanner:
  // the step stays, its text is replaced with the marker, state becomes rejected.
  const c2 = {
    trace_version: 1,
    decided_by: "model",
    steps: [
      step("threshold_fired", "2026-10-15T15:45:11.000Z"),
      {
        step: "reply_received",
        ts: "2026-10-15T15:45:12.010Z",
        label: "model reply received",
        detail: "[dropped: C2]",
        state: "rejected",
        truncated: false,
      },
      step("gates", "2026-10-15T15:45:12.020Z", {
        label: "G9 trace detail",
        detail: "rule=G9 field=reply_received decision=drop reason=C2_signature",
        state: "dropped",
      }),
      step("action_decided", "2026-10-15T15:45:12.030Z", { state: "ok" }),
    ],
  };
  const t = traceView(c2);
  assert.equal(t.present, true);
  // the step AND the guardrail step that recorded the drop are both in the log
  assert.deepEqual(t.steps.map((s) => s.state), ["ok", "rejected", "dropped", "ok"]);
  assert.equal(t.steps[1].detail, "[dropped: C2]");
  // ...and both are listed as warnings, so a collapsed card can say so out loud
  assert.deepEqual(t.warnings, [
    { step: "reply_received", state: "rejected", label: "model reply received" },
    { step: "gates", state: "dropped", label: "G9 trace detail" },
  ]);
  // the reply is still shown, but marked as the controller's marker rather than
  // the model's words — the panel must not paint it as model output text
  assert.equal(t.reply.available, true);
  assert.equal(t.reply.withheld, true);
});

test("decisions: a fallback trace renders as controller text, with its reason and rejecting gate", () => {
  const fb = {
    trace_version: 1,
    decided_by: "fallback",
    steps: [
      step("threshold_fired", "2026-10-15T15:45:11.000Z"),
      step("evidence_window", "2026-10-15T15:45:11.100Z"),
      step("reply_received", "2026-10-15T15:45:12.010Z", {
        label: "model reply (parse=step)",
        detail: "no raw reply text retained",
        state: "skipped",
      }),
      step("action_decided", "2026-10-15T15:45:12.030Z", {
        label: "fallback to deterministic action",
        detail: "decided_by=fallback reason=low_confidence action_kind=instance.restart rejecting_gate=G6",
        state: "fallback",
      }),
    ],
  };
  const r = rowWithTrace(fb, { ...DECISION, decision_source: "fallback", confidence: null, fallback_reason: "low_confidence" });
  assert.equal(r.trace.present, true);
  assert.equal(r.traceSource, "fallback");
  // NEVER attributed to the model
  assert.equal(r.traceMeta.byModel, false);
  assert.equal(r.traceMeta.tone, "fallback");
  assert.match(r.traceMeta.note, /did NOT answer/);
  assert.equal(r.traceBug, false);
  assert.notEqual(r.traceMeta.tone, sourceMeta("model").tone);
  // the reason and the gate that rejected the model's answer come from the trace
  assert.equal(r.trace.fallback.reason, "low_confidence");
  assert.equal(r.trace.fallback.rejectingGate, "G6");
  // ...and the model's reply, when the trace has none, is not invented
  assert.equal(r.trace.reply.available, false);
  assert.equal(r.trace.reply.text, "no raw reply text retained");

  // the metadata table itself: model is the only label that claims model output
  assert.equal(traceDeciderMeta("model").byModel, true);
  assert.equal(traceDeciderMeta("fallback").byModel, false);
  assert.equal(traceDeciderMeta("deterministic").byModel, false);
  assert.equal(traceDeciderMeta(null).byModel, false);
  // a trace claiming deterministic, or carrying no decided_by, is a violation
  assert.equal(traceDeciderMeta("deterministic").bug, true);
  assert.equal(traceDeciderMeta(null).bug, true);
  assert.equal(traceDeciderMeta(undefined).bug, true);
  assert.equal(traceDeciderMeta("fallback").bug, undefined);
});

test("decisions: a step with no valid state is shown unclassified, never as ok", () => {
  const t = traceView({
    trace_version: 1,
    decided_by: "model",
    steps: [
      step("threshold_fired", "2026-10-15T15:45:11.000Z"),
      { step: "gates", ts: "2026-10-15T15:45:12.020Z", label: "gate", detail: "rule=G7 decision=accept", state: "succeeded" },
    ],
  });
  assert.equal(t.steps[1].state, null);
  assert.equal(t.steps[1].unclassified, true);
  assert.equal(t.unclassifiedSteps, 1);
  // and it is in `warnings`, so the collapsed card still reports it
  assert.equal(t.warnings.length, 1);
  assert.equal(t.warnings[0].state, null);
});

test("decisions: steps are ordered by ts, and a reorder is reported rather than silent", () => {
  const t = traceView({
    trace_version: 1,
    decided_by: "model",
    steps: [
      step("action_decided", "2026-10-15T15:45:12.030Z"),
      step("threshold_fired", "2026-10-15T15:45:11.000Z"),
      step("evidence_window", "2026-10-15T15:45:11.100Z"),
    ],
  });
  assert.deepEqual(t.steps.map((s) => s.step), ["threshold_fired", "evidence_window", "action_decided"]);
  assert.equal(t.reordered, true);
  // a step with no parseable ts keeps the array order the contract wrote
  const partly = traceView({
    trace_version: 1,
    decided_by: "model",
    steps: [step("threshold_fired", ""), step("evidence_window", "2026-10-15T15:45:11.100Z")],
  });
  assert.deepEqual(partly.steps.map((s) => s.step), ["threshold_fired", "evidence_window"]);
  assert.equal(partly.reordered, false);
});

test("decisions: a trace that contradicts the row is flagged, not smoothed over", () => {
  // version drift
  const v2 = rowWithTrace({ ...TRACE, trace_version: 2 });
  assert.equal(v2.trace.versionMismatch, true);
  assert.equal(v2.traceBug, true);
  // the row says the controller decided, the trace says the model did
  const lying = rowWithTrace({ ...TRACE, decided_by: "model" }, { ...DECISION, decision_source: "fallback", fallback_reason: "low_confidence" });
  assert.equal(lying.trace.sourceMismatch, true);
  assert.equal(lying.traceBug, true);
  // ...and a trace with no decided_by cannot be attributed to anyone
  const unlabelled = rowWithTrace({ trace_version: 1, steps: [step("executed", "2026-10-15T15:45:12.300Z")] });
  assert.equal(unlabelled.trace.decidedBy, null);
  assert.equal(unlabelled.traceMeta.byModel, false);
  assert.equal(unlabelled.traceBug, true);
});

test("decisions: traceStepView refuses anything outside the §4.3 step table", () => {
  assert.equal(traceStepView(null), null);
  assert.equal(traceStepView("threshold_fired"), null);
  assert.equal(traceStepView({ ts: "T", label: "l", detail: "d", state: "ok" }), null);
  assert.equal(traceStepView(step("made_up_step", "T")), null);
  const ok = traceStepView(step("gates", "T"));
  assert.deepEqual(Object.keys(ok).sort(), ["detail", "label", "state", "step", "ts"]);
});
