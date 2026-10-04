/* Pure formatting helpers. No DOM / no network — unit-testable under node. */

export const KIB = 1024;
export const MIB = 1024 * 1024;
export const GIB = 1024 * 1024 * 1024;

/** Fixed-precision number with thousands separators, tolerant of null. */
export function num(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Percent value, e.g. 31.2 -> "31.2%". */
export function pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${num(v, digits)}%`;
}

/** Bytes -> a human string with the right binary unit (GiB default for charts). */
export function bytes(v, opts = {}) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const abs = Math.abs(n);
  if (abs >= GIB || opts.forceUnit === "GiB") return `${num(n / GIB, opts.digits ?? 2)} GiB`;
  if (abs >= MIB || opts.forceUnit === "MiB") return `${num(n / MIB, opts.digits ?? 1)} MiB`;
  if (abs >= KIB || opts.forceUnit === "KiB") return `${num(n / KIB, opts.digits ?? 1)} KiB`;
  return `${num(n, 0)} B`;
}

/** Pick a display unit + scale factor for a raw series unit. */
export function scaleForUnit(unit) {
  switch (unit) {
    case "bytes":
      return { divisor: GIB, label: "GiB", encode: "binary", digits: 2 };
    case "percent":
      return { divisor: 1, label: "%", encode: "linear", digits: 1 };
    case "ms":
      return { divisor: 1, label: "ms", encode: "linear", digits: 1 };
    case "celsius":
      return { divisor: 1, label: "°C", encode: "linear", digits: 1 };
    case "count":
      return { divisor: 1, label: "#", encode: "linear", digits: 0 };
    case "loadavg":
      return { divisor: 1, label: "load", encode: "linear", digits: 2 };
    case "tok/s":
      return { divisor: 1, label: "tok/s", encode: "linear", digits: 1 };
    default:
      return { divisor: 1, label: unit || "", encode: "linear", digits: 2 };
  }
}

/** Format a scaled value with the group's label. */
export function scaled(v, unit) {
  const s = scaleForUnit(unit);
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${num(Number(v) / s.divisor, s.digits)} ${s.label}`;
}

/** Epoch ms (or RFC3339) -> "HH:MM:SS" in local time. */
export function clock(ts) {
  const d = toDate(ts);
  if (!d) return "—";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

/** Epoch ms (or RFC3339) -> "HH:MM" for axis ticks. */
export function hhmm(ts) {
  const d = toDate(ts);
  if (!d) return "";
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

/** Compact relative age, e.g. "3s ago", "2m ago". */
export function ago(ts, nowMs = Date.now()) {
  const d = toDate(ts);
  if (!d) return "never";
  const s = Math.max(0, Math.round((nowMs - d.getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

/** Human uptime from seconds. */
export function uptime(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return "—";
  let s = Math.max(0, Math.floor(Number(sec)));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function toDate(ts) {
  if (ts === null || ts === undefined) return null;
  if (ts instanceof Date) return Number.isNaN(ts.getTime()) ? null : ts;
  if (typeof ts === "number") return new Date(ts);
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Shorten a ULID for badges without losing identifiability. */
export function shortId(id, head = 6, tail = 4) {
  if (!id || typeof id !== "string") return "—";
  if (id.length <= head + tail + 1) return id;
  return `${id.slice(0, head)}…${id.slice(-tail)}`;
}
