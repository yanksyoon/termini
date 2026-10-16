/*
 * Node health derivation — implements docs/contracts/dashboard.md §1.2/§1.3
 * verbatim, and is used in two situations:
 *
 *   * the controller already sent a `health` object + `status` (contract §1.1);
 *     we render them as-is, only filling gaps;
 *   * the controller is the as-built core that does NOT yet send `health`
 *     (server.py `_node_view`), so the dashboard derives status/flags here from
 *     the latest telemetry sample so the UI is never blank.
 *
 * Pure functions only — no DOM / no network, unit-testable under node.
 */

export const SERVICE_ALLOWLIST = ["termini-bootstrap", "lxd", "sshd", "llama-cpp"];

/**
 * `processes[]` (node-telemetry.md §4.10) — metrics only, bounded top-10 by RSS.
 *
 * The dashboard renders what the contract admits and nothing more: `pid`,
 * `name`, `state`, `cpu_util_pct`, `ram_used_bytes`, `threads`, `age_sec`. Any
 * other key on an entry is dropped here, so a node (or a compromised node) that
 * sends `cmdline`/`argv`/`env` cannot have it displayed even though ingest
 * already rejects it — the panel is not a second egress path for C2 content.
 */
export const PROCESSES_MAX = 10;
export const PROCESS_STATES = [
  "running",
  "sleeping",
  "disk_sleep",
  "zombie",
  "stopped",
  "tracing",
  "dead",
];
const PROCESS_NAME_RE = /^[\x20-\x7e]{1,15}$/;

export function processes(sample) {
  const rows = arr(sample && sample.processes);
  const out = [];
  for (const p of rows) {
    if (!p || typeof p !== "object") continue;
    if (!Number.isInteger(p.pid) || p.pid < 1) continue;
    const row = { pid: p.pid };
    if (typeof p.name === "string" && PROCESS_NAME_RE.test(p.name)) row.name = p.name;
    if (PROCESS_STATES.includes(p.state)) row.state = p.state;
    if (isNum(p.cpu_util_pct)) row.cpu_util_pct = p.cpu_util_pct;
    if (isNum(p.ram_used_bytes)) row.ram_used_bytes = Math.max(0, p.ram_used_bytes);
    if (Number.isInteger(p.threads) && p.threads >= 1) row.threads = p.threads;
    if (Number.isInteger(p.age_sec) && p.age_sec >= 0) row.age_sec = p.age_sec;
    out.push(row);
    if (out.length >= PROCESSES_MAX) break;
  }
  return out;
}

/*
 * ---------------------------------------------------------------------------
 * Metric expansion (§4.12–§4.14) — bounded, allowlisted selectors.
 * ---------------------------------------------------------------------------
 *
 * Same rule as `processes()` above: the panel renders what the contract admits
 * and NOTHING else. Every selector drops a key it does not know, so a node (or a
 * compromised node) that adds `cmdline`, `env`, or an instance NAME to one of
 * these groups cannot have it displayed even though ingest already rejects it —
 * this panel is not a second egress path for C2 content. The one string admitted
 * is `vm_id`, and only in the 26-character ULID form the contract defines: never
 * the LXD instance name, which may carry customer hostname data (§4.6).
 *
 * Absent ≠ zero: a field that is not on the wire is simply not in the result —
 * never a `0` standing in for it — and a *measured* `0` is kept as `0`.
 *
 * Bounds: `disk[]` at DISK_MAX and `net[]` at NET_MAX (the collector's own caps,
 * §4.13), `lxd_instances[]` at INSTANCES_MAX (the contract deliberately does not
 * cap it — the guardrail resolves targets through it — so the panel bounds
 * itself, exactly as `processes()` does). The caller reports what it cut.
 */
export const DISK_MAX = 8;
export const NET_MAX = 1;
export const INSTANCES_MAX = 10;

export const PRESSURE_FIELDS = ["cpu_some_avg10", "io_some_avg10", "memory_some_avg10", "memory_full_avg10"];
export const MEMORY_PRESSURE_FIELDS = ["oom_kill_count", "pgmajfault_count", "mem_available_bytes"];
export const DISK_IO_FIELDS = ["io_ticks_ms", "io_in_flight", "inode_used", "inode_total"];
export const NET_ERROR_FIELDS = ["rx_errors", "tx_errors", "rx_dropped", "tx_dropped"];
export const TRANSPORT_FIELDS = ["tcp_retransmit_segs", "conntrack_count", "conntrack_max"];
export const INSTANCE_FAULT_FIELDS = ["oom_kill_count", "pgmajfault_count"];

/** Minimum each transport/conntrack scalar may take (conntrack_max is a capacity). */
const TRANSPORT_MIN = { tcp_retransmit_segs: 0, conntrack_count: 0, conntrack_max: 1 };
/** A vm_id is a ULID or it is not rendered at all (never an instance name, §4.6). */
const VM_ID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

/** A counter: a JSON integer at or above `min`. Anything else is not a value. */
function counter(v, min = 0) {
  return Number.isInteger(v) && v >= min ? v : undefined;
}

/** A percentage (§4.12 PSI): a finite 0–100 number. */
function pctField(v) {
  return isNum(v) && v >= 0 && v <= 100 ? v : undefined;
}

/** Keep only the named fields that carry a readable value; null when none do. */
function pick(source, fields, read) {
  if (!source || typeof source !== "object") return null;
  const out = {};
  for (const f of fields) {
    const v = read(source[f], f);
    if (v !== undefined) out[f] = v;
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** §4.12 `pressure{}` — the four PSI scalars, or null when not reported. */
export function pressureFacts(sample) {
  return pick(sample && sample.pressure, PRESSURE_FIELDS, pctField);
}

/** §4.12 `oom_kill_count` / `pgmajfault_count` / `mem_available_bytes`. */
export function memoryFacts(sample) {
  return pick(sample, MEMORY_PRESSURE_FIELDS, (v) => counter(v));
}

/** §4.13 flat transport scalars (`conntrack_max` is a capacity, so ≥ 1). */
export function transportFacts(sample) {
  return pick(sample, TRANSPORT_FIELDS, (v, f) => counter(v, TRANSPORT_MIN[f] ?? 0));
}

/**
 * §4.13 `disk[]` extension — one row per disk entry, in sample order, capped at
 * DISK_MAX. The row keeps its `index` so the panel can label it with the entry
 * it really came from, and a disk whose extension fields are all unreadable is
 * still a row (reported as "not reported") rather than a missing entry.
 */
export function diskFacts(sample) {
  return arrayFacts(sample, "disk", DISK_IO_FIELDS, (v, f) => counter(v, f === "inode_total" ? 1 : 0), DISK_MAX);
}

/** §4.13 `net[]` extension — the four NIC error/drop counters. */
export function netFacts(sample) {
  return arrayFacts(sample, "net", NET_ERROR_FIELDS, (v) => counter(v), NET_MAX);
}

/**
 * §4.14 `lxd_instances[]` — per-instance OOM/fault counts, keyed by the ULID
 * `vm_id`. An entry whose `vm_id` is not a ULID is refused outright (it would be
 * an instance name on the wire), and the counts are read from the allowlist.
 */
export function instanceFacts(sample) {
  const rows = [];
  const list = arr(sample && sample.lxd_instances);
  for (let i = 0; i < list.length && rows.length < INSTANCES_MAX; i++) {
    const e = list[i];
    if (!e || typeof e !== "object" || typeof e.vm_id !== "string" || !VM_ID_RE.test(e.vm_id)) continue;
    const row = { vm_id: e.vm_id, index: i };
    for (const f of INSTANCE_FAULT_FIELDS) {
      const v = counter(e[f]);
      if (v !== undefined) row[f] = v;
    }
    rows.push(row);
  }
  return rows;
}

function arrayFacts(sample, key, fields, read, max) {
  const list = arr(sample && sample[key]);
  const rows = [];
  for (let i = 0; i < list.length && i < max; i++) {
    const e = list[i];
    const row = { index: i, ...(pick(e, fields, read) || {}) };
    rows.push(row);
  }
  return rows;
}

export const DEGRADED_REASONS = [
  "heartbeat_late",
  "disk_read_only",
  "disk_degrading",
  "gpu_unavailable",
  "gpu_overtemp",
  "inference_down",
  "service_failed",
];

const HF_OFFLINE_MS = 45_000;
const HF_DEGRADED_MS = 15_000;

function arr(v) {
  return Array.isArray(v) ? v : [];
}

function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

/** Allowlisted service rows only (contract §1.3). */
export function services(sample) {
  return arr(sample && sample.services).filter(
    (s) => s && SERVICE_ALLOWLIST.includes(s.name) && typeof s.status === "string",
  );
}

/** Derive the `health` object (contract §1.3) from a raw Sample. */
export function deriveHealth(node, sample) {
  const role = node && node.role;
  // A missing sample (never reported) leaves every flag at its default — do not
  // invent failures from absent data (contract §1.3).
  if (!sample || typeof sample !== "object") {
    return {
      cpu_util_pct: null,
      ram_used_pct: null,
      disk_ok: null,
      gpu_ok: null,
      inference_ok: null,
      services: [],
      processes: [],
      degraded_reasons: [],
    };
  }
  const s = sample;
  const disks = arr(s.disk);
  const gpus = arr(s.gpu);

  const cpuUtil = isNum(s.cpu_util_pct) ? s.cpu_util_pct : null;
  const ramPct =
    isNum(s.ram_total_bytes) && s.ram_total_bytes > 0 && isNum(s.ram_used_bytes)
      ? (100 * s.ram_used_bytes) / s.ram_total_bytes
      : 0;

  const diskRo = disks.some((d) => d && d.mount_ro === true);
  const diskSlow = disks.some((d) => isNum(d && d.write_latency_ms) && d.write_latency_ms >= 50);

  let gpuOk = null;
  let gpuUnavailable = false;
  let gpuOvertemp = false;
  if (role === "inference") {
    gpuUnavailable = gpus.length === 0;
    gpuOvertemp = gpus.length > 0 && isNum(gpus[0].temp_celsius) && gpus[0].temp_celsius >= 85;
    gpuOk = !(gpuUnavailable || gpuOvertemp);
  }

  let inferenceOk = null;
  if (role === "inference") {
    inferenceOk = s.inference && typeof s.inference.server_up === "boolean" ? s.inference.server_up : null;
  }

  const svcs = services(s);

  const degradedReasons = [];
  if (diskRo) degradedReasons.push("disk_read_only");
  if (diskSlow) degradedReasons.push("disk_degrading");
  if (gpuUnavailable) degradedReasons.push("gpu_unavailable");
  if (gpuOvertemp) degradedReasons.push("gpu_overtemp");
  if (inferenceOk === false) degradedReasons.push("inference_down");
  if (svcs.some((x) => x.status === "failed")) degradedReasons.push("service_failed");

  return {
    cpu_util_pct: cpuUtil,
    ram_used_pct: ramPct,
    disk_ok: !diskRo,
    gpu_ok: gpuOk,
    inference_ok: inferenceOk,
    services: svcs,
    processes: processes(s),
    degraded_reasons: degradedReasons,
  };
}

/**
 * Full node view for the overview grid: merges a controller-provided
 * `health`/`status` with locally derived values so a partially-implemented
 * backend still renders. Returns:
 *   {status, health, staleMs, staleness, reasons, flags}
 */
export function nodeView(node, sample, nowMs = Date.now()) {
  const seen = node && node.last_seen_at ? Date.parse(node.last_seen_at) : NaN;
  const staleMs = Number.isFinite(seen) ? Math.max(0, nowMs - seen) : null;

  const derived = deriveHealth(node, sample);
  const serverHealth = node && node.health && typeof node.health === "object" ? node.health : null;
  const health = serverHealth ? mergeHealth(serverHealth, derived) : derived;

  // Locally derived status (contract §1.1) — used when the controller omits it
  // or leaves it at its default.
  let derivedStatus = "online";
  if (staleMs === null || staleMs > HF_OFFLINE_MS) derivedStatus = staleMs === null ? "offline" : "offline";
  else if (staleMs > HF_DEGRADED_MS) derivedStatus = "degraded";
  else if (healthHasFalse(health) || serviceNotRunning(health)) derivedStatus = "degraded";

  const status = node && typeof node.status === "string" && node.status ? node.status : derivedStatus;

  const reasons = Array.isArray(health.degraded_reasons) ? [...health.degraded_reasons] : [];
  if (status !== "offline" && staleMs !== null && staleMs > HF_DEGRADED_MS && !reasons.includes("heartbeat_late")) {
    reasons.unshift("heartbeat_late");
  }

  return {
    status,
    derivedStatus,
    staleMs,
    staleness: staleMs === null ? "unknown" : staleMs > HF_OFFLINE_MS ? "offline" : staleMs > HF_DEGRADED_MS ? "late" : "fresh",
    health,
    reasons,
    roles: node && node.role,
  };
}

function mergeHealth(server, derived) {
  const out = { ...derived, ...server };
  // Never let a server value erase a flag the client can compute, but keep the
  // server's `degraded_reasons` verbatim — an empty list is a real answer.
  if (!Array.isArray(out.services) || out.services.length === 0) out.services = derived.services;
  if (!Array.isArray(out.processes) || out.processes.length === 0) out.processes = derived.processes;
  if (!Array.isArray(out.degraded_reasons)) out.degraded_reasons = derived.degraded_reasons;
  return out;
}

export function healthHasFalse(health) {
  return [health.disk_ok, health.gpu_ok, health.inference_ok].some((v) => v === false);
}

export function serviceNotRunning(health) {
  return arr(health.services).some((s) => s && s.status && s.status !== "running");
}

export const REASON_LABELS = {
  heartbeat_late: "heartbeat late",
  disk_read_only: "disk read-only",
  disk_degrading: "disk degrading",
  gpu_unavailable: "GPU unavailable",
  gpu_overtemp: "GPU over-temp",
  inference_down: "inference down",
  service_failed: "service failed",
};
