/*
 * Chart model — the controller's flat `/metrics` series (dashboard.md §1.4) and
 * the metric-expansion sample groups (node-telemetry.md §4.12–§4.14).
 *
 * TWO SOURCES, ONE PANEL
 *
 *   * `source: "series"` — charted from `GET /nodes/{id}/metrics`, whose
 *     allowlist is FROZEN at the 19 names of dashboard.md §1.4. Only these names
 *     may appear in the `?metrics=` query: the controller answers HTTP 400
 *     `unknown metric(s)` for anything else, which fails the whole request and
 *     would take the entire panel down. `requestedMetrics()` is the only thing
 *     that builds that query, and it never names a sample-sourced metric.
 *
 *   * `source: "sample"` — read from the LATEST RAW TELEMETRY SAMPLE
 *     (node-telemetry.md §4.12–§4.14, carried in `metrics_latest` and in the
 *     `node.telemetry` push). These are per-sample facts, not time series, so
 *     `buildCharts()` never renders them and `buildSampleView()` does. Every
 *     field read goes through the bounded, allowlisted selectors in `health.js`,
 *     so an unknown key on the wire is dropped before it can be displayed —
 *     this panel is not a second egress path for C2 content.
 *
 * DERIVED vs RAW (visible in the UI, never implied)
 *
 *   A value the controller aggregated is DERIVED and is labelled as such in the
 *   chart title/legend: the `disk[0]`/`net[0]`/`gpu[0]` first-element mapping of
 *   §1.4, and the mean-per-bucket downsample to a resolution window. A raw
 *   sample is labelled raw. A cumulative counter is labelled `cumulative`,
 *   because it is not a rate and the panel never presents one as a rate.
 *
 * ABSENT ≠ ZERO
 *
 *   A field the node did not report is `reported: false` and renders as
 *   "not reported" — never as a zero. A *measured* zero (the collector reports a
 *   real `0`) stays `0`. Same rule the charts already follow: a group with no
 *   series produces no chart card at all.
 */

import { scaleForUnit, num, GIB } from "./format.js";
import {
  pressureFacts,
  memoryFacts,
  diskFacts,
  netFacts,
  transportFacts,
  instanceFacts,
  DISK_MAX,
  NET_MAX,
  INSTANCES_MAX,
} from "./health.js";

function arr(v) {
  return Array.isArray(v) ? v : [];
}

/**
 * The controller-served flat allowlist — dashboard.md §1.4, mirrored by
 * `controller/app/store.py METRICS` and pinned by `tests/test_dashboard_contract.py`.
 * This is the ONLY set of names that may be requested from `/metrics`.
 */
export const CONTROLLER_METRICS = [
  "cpu_util_pct",
  "cpu_core_count",
  "ram_total_bytes",
  "ram_used_bytes",
  "swap_used_bytes",
  "load_avg_1m",
  "cpu_temp_celsius",
  "gpu_util_pct",
  "gpu_vram_used_bytes",
  "gpu_vram_total_bytes",
  "net_rx_bytes",
  "net_tx_bytes",
  "net_latency_ms",
  "disk_total_bytes",
  "disk_free_bytes",
  "disk_write_latency_ms",
  "inference_tokens_per_s",
  "inference_active_requests",
  "inference_vram_used_bytes",
];

/**
 * Unit registry — name (or contract field path) -> unit. Two families: the 19
 * controller metrics above, and the §4.12–§4.14 sample fields, keyed by the
 * contract's own path notation so a unit can be checked against
 * `node-telemetry.md` line by line.
 */
export const METRIC_UNITS = {
  cpu_util_pct: "percent",
  cpu_core_count: "count",
  ram_total_bytes: "bytes",
  ram_used_bytes: "bytes",
  swap_used_bytes: "bytes",
  load_avg_1m: "loadavg",
  cpu_temp_celsius: "celsius",
  gpu_util_pct: "percent",
  gpu_vram_used_bytes: "bytes",
  gpu_vram_total_bytes: "bytes",
  net_rx_bytes: "bytes",
  net_tx_bytes: "bytes",
  net_latency_ms: "ms",
  disk_total_bytes: "bytes",
  disk_free_bytes: "bytes",
  disk_write_latency_ms: "ms",
  inference_tokens_per_s: "tok/s",
  inference_active_requests: "count",
  inference_vram_used_bytes: "bytes",
  // --- §4.12 PSI pressure + memory-pressure scalars
  "pressure.cpu_some_avg10": "percent",
  "pressure.io_some_avg10": "percent",
  "pressure.memory_some_avg10": "percent",
  "pressure.memory_full_avg10": "percent",
  oom_kill_count: "count",
  pgmajfault_count: "count",
  mem_available_bytes: "bytes",
  // --- §4.13 disk I/O + inode extension
  "disk[].io_ticks_ms": "ms",
  "disk[].io_in_flight": "count",
  "disk[].inode_used": "count",
  "disk[].inode_total": "count",
  // --- §4.13 network error/drop counters + transport scalars
  "net[].rx_errors": "count",
  "net[].tx_errors": "count",
  "net[].rx_dropped": "count",
  "net[].tx_dropped": "count",
  tcp_retransmit_segs: "count",
  conntrack_count: "count",
  conntrack_max: "count",
  // --- §4.14 per-instance OOM / fault attribution
  "lxd_instances[].oom_kill_count": "count",
  "lxd_instances[].pgmajfault_count": "count",
};

/** Flat controller series, aggregated by the controller per §1.4's mapping. */
export const SERIES_SOURCE = "series";
/** Sample groups, read from the latest raw telemetry sample (§4.12–§4.14). */
export const SAMPLE_SOURCE = "sample";

/*
 * Chart groups. `primary` groups are shown by default; the rest appear behind
 * the "all metrics" toggle so the projector view stays uncluttered.
 *
 * `source: "series"` groups come from `/metrics`; `source: "sample"` groups are
 * the §4.12–§4.14 additions, read from the latest raw sample. Sample groups are
 * ranked per the research card (`docs/research/infra-metric-inventory.md` §2) and
 * carry the frozen rule they are a leading indicator for, so the panel says WHY
 * an operator should look — without inventing a client-side threshold.
 */
export const CHART_GROUPS = [
  { id: "cpu", title: "CPU utilization", unit: "percent", metrics: ["cpu_util_pct"], primary: true, yMax: 100, source: SERIES_SOURCE },
  { id: "mem", title: "Memory", unit: "bytes", metrics: ["ram_used_bytes", "swap_used_bytes"], primary: true, source: SERIES_SOURCE },
  { id: "disk_io", title: "Disk write latency", unit: "ms", metrics: ["disk_write_latency_ms"], primary: true, source: SERIES_SOURCE },
  { id: "net", title: "Network throughput", unit: "bytes", metrics: ["net_rx_bytes", "net_tx_bytes"], primary: true, source: SERIES_SOURCE },
  { id: "gpu", title: "GPU utilization", unit: "percent", metrics: ["gpu_util_pct"], primary: true, yMax: 100, source: SERIES_SOURCE },
  { id: "gpu_mem", title: "GPU VRAM", unit: "bytes", metrics: ["gpu_vram_used_bytes", "gpu_vram_total_bytes"], primary: false, source: SERIES_SOURCE },
  { id: "disk_cap", title: "Disk capacity", unit: "bytes", metrics: ["disk_total_bytes", "disk_free_bytes"], primary: false, source: SERIES_SOURCE },
  { id: "net_lat", title: "Network latency", unit: "ms", metrics: ["net_latency_ms"], primary: false, source: SERIES_SOURCE },
  { id: "load", title: "Load average (1m)", unit: "loadavg", metrics: ["load_avg_1m"], primary: false, source: SERIES_SOURCE },
  { id: "cpu_temp", title: "CPU temperature", unit: "celsius", metrics: ["cpu_temp_celsius"], primary: false, source: SERIES_SOURCE },
  { id: "infer", title: "Inference throughput", unit: "tok/s", metrics: ["inference_tokens_per_s"], primary: false, source: SERIES_SOURCE },
  { id: "infer_req", title: "Inference requests", unit: "count", metrics: ["inference_active_requests"], primary: false, source: SERIES_SOURCE },
  { id: "infer_vram", title: "Inference VRAM", unit: "bytes", metrics: ["inference_vram_used_bytes"], primary: false, source: SERIES_SOURCE },
  { id: "cpu_cores", title: "CPU cores", unit: "count", metrics: ["cpu_core_count"], primary: false, source: SERIES_SOURCE },

  // ---------------------------------------------------------------- §4.12–§4.14
  // Ranked per the research card: a group ships when the metric changes the
  // remediation the operator would choose, or makes the incident explainable.
  // Shipped: research rows 1–9 and 11. Skipped: row 12 (`pressure[].avg60`) —
  // the contract froze `pressure{}` at the four avg10 scalars, so there is
  // nothing to chart; rows 10 and 13–16 are SHIP-LATER in the research card and
  // have no contract field yet.
  {
    id: "pressure", title: "PSI pressure (latest sample)", unit: "percent", primary: true, yMax: 100,
    source: SAMPLE_SOURCE, rank: 1, rule: "oom-guard-01 · disk-degrade-01 · latency-01",
    metrics: [
      "pressure.cpu_some_avg10",
      "pressure.io_some_avg10",
      "pressure.memory_some_avg10",
      "pressure.memory_full_avg10",
    ],
  },
  {
    id: "mem_pressure", title: "Memory pressure (latest sample)", unit: "count", primary: true,
    source: SAMPLE_SOURCE, rank: 2, rule: "oom-guard-01",
    metrics: ["oom_kill_count", "pgmajfault_count", "mem_available_bytes"],
  },
  {
    id: "disk_io_sat", title: "Disk saturation / inodes (latest sample)", unit: "count", primary: true,
    source: SAMPLE_SOURCE, rank: 4, rule: "disk-degrade-01",
    metrics: [
      "disk[].io_ticks_ms",
      "disk[].io_in_flight",
      "disk[].inode_used",
      "disk[].inode_total",
    ],
  },
  {
    id: "net_errors", title: "Network errors / drops (latest sample)", unit: "count", primary: true,
    source: SAMPLE_SOURCE, rank: 6, rule: "latency-01",
    metrics: [
      "net[].rx_errors",
      "net[].tx_errors",
      "net[].rx_dropped",
      "net[].tx_dropped",
    ],
  },
  {
    id: "transport", title: "Transport / conntrack (latest sample)", unit: "count", primary: true,
    source: SAMPLE_SOURCE, rank: 7, rule: "latency-01",
    metrics: ["tcp_retransmit_segs", "conntrack_count", "conntrack_max"],
  },
  {
    id: "instance_faults", title: "Per-instance OOM / faults (latest sample)", unit: "count", primary: true,
    source: SAMPLE_SOURCE, rank: 9, rule: "oom-guard-01 (instance attribution)",
    metrics: ["lxd_instances[].oom_kill_count", "lxd_instances[].pgmajfault_count"],
  },
];

/** The §4.12–§4.14 groups the panel renders from the latest raw sample. */
export const SAMPLE_GROUPS = CHART_GROUPS.filter((g) => g.source === SAMPLE_SOURCE);

/**
 * The flat series the controller derives per §1.4's nested→flat mapping. A flat
 * name listed here is an AGGREGATE (first element of a device array), and the
 * chart must say so: presenting `disk[0]` as "the node's disk" is a mislabel.
 * `disk_write_latency_ms` is additionally node-derived — it is the collector's
 * own write probe, not a kernel-reported latency.
 */
const DERIVED_FROM = {
  gpu_util_pct: "gpu[0]",
  gpu_vram_used_bytes: "gpu[0]",
  gpu_vram_total_bytes: "gpu[0]",
  net_rx_bytes: "net[0]",
  net_tx_bytes: "net[0]",
  net_latency_ms: "net[0]",
  disk_total_bytes: "disk[0]",
  disk_free_bytes: "disk[0]",
  disk_write_latency_ms: "disk[0] · collector write probe, not a kernel latency",
};

/** Resolution windows are bucket means (dashboard.md §1.4) — an aggregate. */
const BUCKET_LABEL = { "5s": "5 s", "1m": "1 min", "5m": "5 min" };

/**
 * Names a threshold needs as a DENOMINATOR, charted by no group.
 *
 * `oom-guard-01` fires on `ram_used_bytes` with a threshold of
 * ">= 95% of ram_total_bytes". `thresholdMarker()` (core/decisions.js) resolves
 * that percentage from the node's own `/metrics` payload, so the denominator has
 * to travel with the request — otherwise the series is absent, the threshold is
 * null, and the dashed line (plus the `threshold: …` caption) can never be
 * drawn. Requesting the extra name is cheap and harmless: no chart group carries
 * it, so `buildCharts()` simply ignores it.
 *
 * Every entry MUST be a §1.4 controller metric — a sample-sourced name here
 * would make the controller reject the whole request (HTTP 400).
 */
export const RESOLVER_METRICS = [
  "ram_total_bytes", // oom-guard-01: ">= 95% of ram_total_bytes"
  "disk_total_bytes", // disk-degrade-01 percentage-of-total forms
  "gpu_vram_total_bytes", // gpu rules expressed as a share of VRAM
];

/** Names the panel may put in the `?metrics=` query. NEVER a sample metric. */
export function requestedMetrics(includeAll = false) {
  const groups = includeAll ? CHART_GROUPS : CHART_GROUPS.filter((g) => g.primary);
  const names = new Set();
  for (const g of groups) {
    if (g.source !== SERIES_SOURCE) continue;
    for (const m of g.metrics) names.add(m);
  }
  // Threshold denominators, keyed by the rules' own vocabulary. Guarded against
  // the frozen allowlist because one bad name fails the entire request.
  for (const m of RESOLVER_METRICS) {
    if (CONTROLLER_METRICS.includes(m)) names.add(m);
  }
  return Array.from(names);
}

/**
 * How a flat group's points were produced, from the payload's own `resolution`:
 * `raw` -> the stored samples verbatim; anything else -> a mean per bucket.
 */
function seriesBasis(payload) {
  const resolution = payload && typeof payload.resolution === "string" ? payload.resolution : "raw";
  if (resolution === "raw") {
    return { basis: "raw", basisLabel: "raw stored samples (not aggregated)" };
  }
  const label = BUCKET_LABEL[resolution] || resolution;
  return { basis: "derived", basisLabel: `mean over ${label} buckets (derived by the controller)` };
}

/**
 * Build chart-ready groups from a `/metrics` response.
 *
 * `annotations` (optional) are raw-API decision/threshold marks:
 *   [{metric, t, kind, label, value, threshold:{value,basis,label}}]
 * Each is attached to the group that actually carries its metric and scaled with
 * that group's unit, so the chart and the decisions panel share one axis.
 *
 * Sample-sourced groups (§4.12–§4.14) are NOT rendered here: they are not time
 * series, and a `/metrics` response can never carry them.
 *
 * @returns [{id,title,unit,source,basis,basisLabel,aggregateLabel,derived,series:[{name,label,derived,derivedFrom,points}],markers[],thresholds[],hasData}]
 *   points are display-scaled (bytes -> GiB) and `t` is epoch ms.
 */
export function buildCharts(payload, { includeAll = false, annotations = [] } = {}) {
  const byName = new Map();
  for (const s of (payload && payload.series) || []) {
    if (s && typeof s.name === "string") byName.set(s.name, s);
  }
  const { basis, basisLabel } = seriesBasis(payload);
  const groups = includeAll ? CHART_GROUPS : CHART_GROUPS.filter((g) => g.primary);
  const out = [];
  for (const g of groups) {
    // A sample group has no flat series behind it: never chart one, even if a
    // payload (or a stub) named it. This is the guard that keeps a fabricated
    // series from being drawn as if the controller had served it.
    if (g.source !== SERIES_SOURCE) continue;
    const series = [];
    for (const name of g.metrics) {
      const raw = byName.get(name);
      if (!raw || !Array.isArray(raw.points) || raw.points.length === 0) continue;
      const scale = scaleForUnit(raw.unit || g.unit);
      const derivedFrom = DERIVED_FROM[name];
      series.push({
        name,
        label: metricLabel(name),
        unit: raw.unit || g.unit,
        derived: Boolean(derivedFrom) || basis === "derived",
        derivedFrom: derivedFrom || (basis === "derived" ? basisLabel : null),
        points: raw.points
          .map((p) => ({ t: Date.parse(p.t), v: Number(p.v) / scale.divisor }))
          .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v)),
      });
    }
    const groupScale = scaleForUnit(g.unit);
    const markers = [];
    const thresholds = [];
    const seenThresholds = new Set();
    for (const a of annotations || []) {
      if (!a || !g.metrics.includes(a.metric)) continue;
      if (Number.isFinite(a.t)) {
        markers.push({ t: a.t, kind: a.kind || "det", label: a.label || a.metric });
      }
      if (a.threshold && Number.isFinite(a.threshold.value)) {
        const label = a.threshold.label || `threshold ${a.threshold.basis || ""}`.trim();
        const value = Number(a.threshold.value) / groupScale.divisor;
        // The same rule firing twice in one window crosses the SAME line: draw
        // it once (the markers still show both firings) instead of stacking an
        // identical label on top of itself.
        const key = `${value}|${label}`;
        if (seenThresholds.has(key)) continue;
        seenThresholds.add(key);
        thresholds.push({ value, label });
      }
    }
    if (series.length === 0 && markers.length === 0) continue;
    const aggregates = series.filter((s) => s.derivedFrom).map((s) => `${s.label}: ${s.derivedFrom}`);
    out.push({
      id: g.id,
      title: g.title,
      unit: g.unit,
      yMax: g.yMax,
      source: SERIES_SOURCE,
      basis,
      basisLabel,
      aggregateLabel: aggregates.length ? `${basisLabel} · ${aggregates.join(" · ")}` : basisLabel,
      derived: aggregates.length > 0 || basis === "derived",
      series,
      markers,
      thresholds,
      hasData: series.some((s) => s.points.length > 0),
      fault: markers.length > 0 || thresholds.length > 0,
    });
  }
  return out;
}

const LABELS = {
  cpu_util_pct: "CPU %",
  cpu_core_count: "cores",
  ram_total_bytes: "RAM total",
  ram_used_bytes: "RAM used",
  swap_used_bytes: "swap used",
  load_avg_1m: "load 1m",
  cpu_temp_celsius: "CPU temp",
  gpu_util_pct: "GPU %",
  gpu_vram_used_bytes: "VRAM used",
  gpu_vram_total_bytes: "VRAM total",
  net_rx_bytes: "RX",
  net_tx_bytes: "TX",
  net_latency_ms: "latency",
  disk_total_bytes: "disk total",
  disk_free_bytes: "disk free",
  disk_write_latency_ms: "write latency",
  inference_tokens_per_s: "tokens/s",
  inference_active_requests: "active req",
  inference_vram_used_bytes: "infer VRAM",
  // --- §4.12–§4.14 (keyed by contract path; the label is the leaf's wording)
  "pressure.cpu_some_avg10": "CPU stall (some)",
  "pressure.io_some_avg10": "I/O stall (some)",
  "pressure.memory_some_avg10": "mem stall (some)",
  "pressure.memory_full_avg10": "mem stall (full)",
  oom_kill_count: "OOM kills",
  pgmajfault_count: "major faults",
  mem_available_bytes: "mem available",
  "disk[].io_ticks_ms": "I/O busy",
  "disk[].io_in_flight": "I/O in flight",
  "disk[].inode_used": "inodes used",
  "disk[].inode_total": "inodes total",
  "net[].rx_errors": "RX errors",
  "net[].tx_errors": "TX errors",
  "net[].rx_dropped": "RX dropped",
  "net[].tx_dropped": "TX dropped",
  tcp_retransmit_segs: "TCP retransmits",
  conntrack_count: "conntrack entries",
  conntrack_max: "conntrack max",
  "lxd_instances[].oom_kill_count": "OOM kills",
  "lxd_instances[].pgmajfault_count": "major faults",
};

export function metricLabel(name) {
  return LABELS[name] || name;
}

/**
 * Cumulative counters (node-telemetry.md §4.12–§4.14) — counts since boot, never
 * a rate. The panel marks them `cumulative` so a rising line is not read as a
 * per-second signal.
 */
const CUMULATIVE = new Set([
  "oom_kill_count",
  "pgmajfault_count",
  "disk[].io_ticks_ms",
  "net[].rx_errors",
  "net[].tx_errors",
  "net[].rx_dropped",
  "net[].tx_dropped",
  "tcp_retransmit_segs",
  "lxd_instances[].oom_kill_count",
  "lxd_instances[].pgmajfault_count",
]);

/** Rows the panel computes itself. Computed => derived, and labelled so. */
const COMPUTED_ROWS = [
  {
    group: "disk_io_sat", scope: "entry", metric: "inode_used_pct", label: "inodes used",
    unit: "percent", derivedFrom: "inode_used / inode_total", left: "inode_used", right: "inode_total",
  },
  {
    group: "transport", scope: "sample", metric: "conntrack_used_pct", label: "conntrack table used",
    unit: "percent", derivedFrom: "conntrack_count / conntrack_max", left: "conntrack_count", right: "conntrack_max",
  },
];

/**
 * Read one contract field path out of a raw sample: `pressure.cpu_some_avg10`
 * (dotted), `disk[].inode_used` (array member) or `oom_kill_count` (flat).
 * Returns `undefined` when the field is absent — never `0` in its place.
 */
export function sampleField(sample, path) {
  if (!sample || typeof sample !== "object" || typeof path !== "string") return undefined;
  const m = /^([A-Za-z0-9_]+)\[\]\.([A-Za-z0-9_]+)$/.exec(path);
  if (m) {
    return arr(sample[m[1]]).map((e) => (e && typeof e === "object" ? e[m[2]] : undefined));
  }
  const parts = path.split(".");
  let cur = sample;
  for (const part of parts) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = cur[part];
  }
  return cur;
}

function formatSampleValue(v, unit) {
  // Value ONLY: the unit rides beside it (`unitLabel`), so a row renders as
  // "8.7 %" / "2.25 GiB" / "297,295 #" and never "8.7 % %".
  if (unit === "bytes") return num(v / GIB, 2);
  if (unit === "percent") return num(v, 1);
  return num(v, 0);
}

function rowFrom(path, value, extra = {}) {
  const unit = METRIC_UNITS[path] || "count";
  const reported = value !== undefined && value !== null;
  return {
    metric: path,
    name: path.split(".").pop(),
    label: metricLabel(path),
    unit,
    unitLabel: scaleForUnit(unit).label,
    cumulative: CUMULATIVE.has(path),
    derived: false,
    derivedFrom: null,
    reported,
    value: reported ? value : null,
    display: reported ? formatSampleValue(value, unit) : "not reported",
    ...extra,
  };
}

function ratioPct(left, right) {
  if (!Number.isFinite(left) || !Number.isFinite(right) || right <= 0) return null;
  return Math.round(((100 * left) / right) * 10) / 10;
}

function computedRow(spec, left, right, extra = {}) {
  const value = ratioPct(left, right);
  return {
    metric: spec.metric,
    name: spec.metric,
    label: spec.label,
    unit: spec.unit,
    unitLabel: scaleForUnit(spec.unit).label,
    cumulative: false,
    derived: true,
    derivedFrom: spec.derivedFrom,
    reported: value !== null,
    value,
    display: value === null ? "not reported" : num(value, 1),
    ...extra,
  };
}

function shortInstanceId(vm_id) {
  if (typeof vm_id !== "string" || !vm_id) return "—";
  return vm_id.length > 10 ? `${vm_id.slice(0, 6)}…${vm_id.slice(-4)}` : vm_id;
}

/**
 * The §4.12–§4.14 panel model for ONE raw sample: a list of groups, each a list
 * of rows, each row either reported (value + unit) or explicitly NOT reported.
 *
 * A group whose fields are all absent still renders — as "not reported" — so a
 * role that does not have the metric, or a source the node could not read, is
 * never drawn as a flat zero.
 *
 * @returns [{id,title,unit,source,rank,rule,ts,seq,basis,basisLabel,rows[],reportedCount,missingCount,reported,entryCount,maxEntries,truncated}]
 */
export function buildSampleView(sample, { includeAll = true } = {}) {
  const groups = includeAll ? SAMPLE_GROUPS : SAMPLE_GROUPS.filter((g) => g.primary);
  const out = [];
  for (const g of groups) {
    const rows = [];
    let arrays = null;
    if (g.id === "pressure") {
      const facts = pressureFacts(sample) || {};
      for (const path of g.metrics) rows.push(rowFrom(path, facts[path.split(".").pop()]));
    } else if (g.id === "mem_pressure" || g.id === "transport") {
      const facts = (g.id === "mem_pressure" ? memoryFacts : transportFacts)(sample) || {};
      for (const path of g.metrics) rows.push(rowFrom(path, facts[path]));
      for (const c of COMPUTED_ROWS.filter((x) => x.group === g.id && x.scope === "sample")) {
        rows.push(computedRow(c, sampleField(sample, c.left), sampleField(sample, c.right)));
      }
    } else if (g.id === "disk_io_sat") {
      arrays = { key: "disk", label: "disk", rows: diskFacts(sample), max: DISK_MAX };
    } else if (g.id === "net_errors") {
      arrays = { key: "net", label: "net", rows: netFacts(sample), max: NET_MAX };
    } else if (g.id === "instance_faults") {
      arrays = { key: "lxd_instances", label: "instance", rows: instanceFacts(sample), max: INSTANCES_MAX };
    }

    let entryCount = null;
    let truncated = 0;
    if (arrays) {
      // The contract distinguishes an OMITTED array ("this node did not report an
      // inventory") from an EMPTY one ("it reports nothing") — so null and 0 are
      // different answers here too.
      const list = sample && sample[arrays.key];
      entryCount = Array.isArray(list) ? list.length : null;
      truncated = entryCount === null ? 0 : Math.max(0, entryCount - arrays.rows.length);
      arrays.rows.forEach((entry) => {
        // The label carries the entry's REAL index in the sample, so a disk whose
        // extension fields were unreadable can never make the next disk's numbers
        // look like disk 0's.
        const idx = Number.isInteger(entry.index) ? entry.index : 0;
        const entryLabel = arrays.label === "instance"
          ? `instance ${shortInstanceId(entry.vm_id)}`
          : `${arrays.label} ${idx}`;
        for (const path of g.metrics) {
          const rel = path.split(".").pop();
          rows.push(rowFrom(path, entry[rel], { entry: entryLabel, entryIndex: idx }));
        }
        for (const c of COMPUTED_ROWS.filter((x) => x.group === g.id && x.scope === "entry")) {
          rows.push(computedRow(c, entry[c.left], entry[c.right], { entry: entryLabel, entryIndex: idx }));
        }
      });
    }

    const reported = rows.filter((r) => r.reported);
    out.push({
      id: g.id,
      title: g.title,
      unit: g.unit,
      source: SAMPLE_SOURCE,
      rank: g.rank || null,
      rule: g.rule || null,
      ts: sample && typeof sample.ts === "string" ? sample.ts : null,
      seq: Number.isInteger(sample && sample.seq) ? sample.seq : null,
      basis: "raw",
      basisLabel: g.id === "pressure"
        ? "raw sample: kernel 10 s average per task class, not an aggregate of samples"
        : "raw sample: cumulative counters where marked, instant values otherwise",
      entryCount,
      maxEntries: arrays ? arrays.max : null,
      truncated,
      rows,
      reportedCount: reported.length,
      missingCount: rows.length - reported.length,
      reported: reported.length > 0,
    });
  }
  return out;
}

/** Resolve a window label + seconds to a `{start,end,resolution}` query. */
export function windowQuery(label, seconds, nowMs = Date.now()) {
  const end = nowMs;
  const start = nowMs - seconds * 1000;
  let resolution = "5s";
  if (seconds > 3600) resolution = "5m";
  else if (seconds > 900) resolution = "1m";
  return {
    start: new Date(start).toISOString(),
    end: new Date(end).toISOString(),
    resolution,
  };
}

/** Build the ?metrics= query list for a set of metric names. */
export function metricsQuery(names) {
  return Array.from(new Set(names)).join(",");
}
