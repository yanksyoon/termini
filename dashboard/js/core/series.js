/*
 * Resource-utilization chart model (contract §1.4). Pure transforms from the
 * `/nodes/{id}/metrics` response into chart-ready groups with display units.
 *
 * The 19-metric allowlist is frozen in dashboard.md §1.4; groups below only
 * reference those names and only render when the node actually reports data.
 */

import { scaleForUnit } from "./format.js";

/** Frozen metric allowlist (dashboard.md §1.4) — name -> unit. */
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
};

/*
 * Chart groups. `primary` groups are shown by default; the rest appear behind
 * the "all metrics" toggle so the projector view stays uncluttered.
 */
export const CHART_GROUPS = [
  { id: "cpu", title: "CPU utilization", unit: "percent", metrics: ["cpu_util_pct"], primary: true, yMax: 100 },
  { id: "mem", title: "Memory", unit: "bytes", metrics: ["ram_used_bytes", "swap_used_bytes"], primary: true },
  { id: "disk_io", title: "Disk write latency", unit: "ms", metrics: ["disk_write_latency_ms"], primary: true },
  { id: "net", title: "Network throughput", unit: "bytes", metrics: ["net_rx_bytes", "net_tx_bytes"], primary: true },
  { id: "gpu", title: "GPU utilization", unit: "percent", metrics: ["gpu_util_pct"], primary: true, yMax: 100 },
  { id: "gpu_mem", title: "GPU VRAM", unit: "bytes", metrics: ["gpu_vram_used_bytes", "gpu_vram_total_bytes"], primary: false },
  { id: "disk_cap", title: "Disk capacity", unit: "bytes", metrics: ["disk_total_bytes", "disk_free_bytes"], primary: false },
  { id: "net_lat", title: "Network latency", unit: "ms", metrics: ["net_latency_ms"], primary: false },
  { id: "load", title: "Load average (1m)", unit: "loadavg", metrics: ["load_avg_1m"], primary: false },
  { id: "cpu_temp", title: "CPU temperature", unit: "celsius", metrics: ["cpu_temp_celsius"], primary: false },
  { id: "infer", title: "Inference throughput", unit: "tok/s", metrics: ["inference_tokens_per_s"], primary: false },
  { id: "infer_req", title: "Inference requests", unit: "count", metrics: ["inference_active_requests"], primary: false },
  { id: "infer_vram", title: "Inference VRAM", unit: "bytes", metrics: ["inference_vram_used_bytes"], primary: false },
  { id: "cpu_cores", title: "CPU cores", unit: "count", metrics: ["cpu_core_count"], primary: false },
];

/**
 * Build chart-ready groups from a `/metrics` response.
 * @returns [{id,title,unit,labels[],series:[{name,label,points:[{t,v}]}],hasData}]
 *   points are display-scaled (bytes -> GiB) and `t` is epoch ms.
 */
export function buildCharts(payload, { includeAll = false } = {}) {
  const byName = new Map();
  for (const s of (payload && payload.series) || []) {
    if (s && typeof s.name === "string") byName.set(s.name, s);
  }
  const groups = includeAll ? CHART_GROUPS : CHART_GROUPS.filter((g) => g.primary);
  const out = [];
  for (const g of groups) {
    const series = [];
    for (const name of g.metrics) {
      const raw = byName.get(name);
      if (!raw || !Array.isArray(raw.points) || raw.points.length === 0) continue;
      const scale = scaleForUnit(raw.unit || g.unit);
      series.push({
        name,
        label: metricLabel(name),
        unit: raw.unit || g.unit,
        points: raw.points
          .map((p) => ({ t: Date.parse(p.t), v: Number(p.v) / scale.divisor }))
          .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v)),
      });
    }
    if (series.length === 0) continue;
    out.push({
      id: g.id,
      title: g.title,
      unit: g.unit,
      yMax: g.yMax,
      series,
      hasData: series.some((s) => s.points.length > 0),
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
};

export function metricLabel(name) {
  return LABELS[name] || name;
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
