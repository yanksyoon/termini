/*
 * Runtime configuration resolution (pure; no network/DOM beyond injected
 * inputs) so it can be unit-tested under node.
 *
 * Precedence: query params > localStorage > window.TERMINI_CONFIG > defaults.
 */
export const DEFAULTS = {
  apiBase: "",
  projectId: "",
  token: "",
  pollMs: 5000,
  windows: { "5m": 300, "15m": 900, "1h": 3600, "6h": 21600 },
  defaultWindow: "15m",
  logBufferMax: 2000,
  offline: false,
};

export function resolveConfig({ query = {}, stored = {}, file = {}, defaults = DEFAULTS } = {}) {
  const pick = (key) => {
    const q = query[key];
    if (q !== undefined && q !== null && q !== "") return q;
    const s = stored[key];
    if (s !== undefined && s !== null && s !== "") return s;
    const f = file[key];
    if (f !== undefined && f !== null && f !== "") return f;
    return defaults[key];
  };
  const bool = (v, fallback) => {
    if (v === true || v === "1" || v === "true") return true;
    if (v === false || v === "0" || v === "false") return false;
    return fallback;
  };
  return {
    apiBase: trimSlash(String(pick("apiBase") ?? "")),
    projectId: String(pick("projectId") ?? ""),
    token: String(pick("token") ?? ""),
    pollMs: clampInt(pick("pollMs"), 1000, 60000, defaults.pollMs),
    windows: file.windows && typeof file.windows === "object" ? file.windows : defaults.windows,
    defaultWindow: String(pick("window") ?? file.defaultWindow ?? defaults.defaultWindow),
    logBufferMax: clampInt(pick("logBufferMax"), 50, 20000, defaults.logBufferMax),
    offline: bool(offlineFlag(query, stored, file), defaults.offline),
  };
}

function offlineFlag(query, stored, file) {
  if (query.mock === "1" || query.offline === "1") return true;
  if (stored.offline !== undefined) return stored.offline;
  return file.offline;
}

function trimSlash(s) {
  return s.replace(/\/+$/, "") ;
}

function clampInt(v, lo, hi, fallback) {
  const n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(hi, Math.max(lo, Math.trunc(n)));
}

/** Build a REST URL under the configured base, preserving a leading path. */
export function apiUrl(cfg, path, extraQuery = {}) {
  const base = cfg.apiBase || "/v1";
  const resolved = base.endsWith("/v1") || base.includes("/v1")
    ? base
    : `${base}/v1`;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(extraQuery)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const q = qs.toString();
  return `${resolved}${path}${q ? `?${q}` : ""}`;
}
