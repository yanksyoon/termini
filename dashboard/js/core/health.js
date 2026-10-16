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
