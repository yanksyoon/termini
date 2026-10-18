/*
 * Runtime configuration resolution (pure; no network/DOM beyond injected
 * inputs) so it can be unit-tested under node.
 *
 * Precedence: query params > sessionStorage > window.TERMINI_CONFIG > defaults.
 *
 * The credential follows the same precedence, with one rule on top: a token
 * that arrives in a share link (`?token=…`) is honoured, flagged as
 * `tokenFromUrl: true`, and then moved out of the URL by the app — it is written
 * to this tab's sessionStorage and stripped from the address bar and history
 * entry immediately (`main.js` `adoptUrlToken`). That keeps a demo link that
 * works in one click without leaving the credential in every copy of the link
 * the viewer keeps afterwards.
 *
 * The credential still travels in the `Authorization` header on every request,
 * including the SSE log stream (`js/stream.js` does not use EventSource,
 * precisely because EventSource cannot set a header), and `apiUrl()` still
 * refuses to build a request URL that carries one.
 */
export const DEFAULTS = {
  apiBase: "",
  projectId: "",
  token: "",
  pollMs: 5000,
  decisionMs: 3000,
  decisionLimit: 50,
  windows: { "5m": 300, "15m": 900, "1h": 3600, "6h": 21600 },
  defaultWindow: "15m",
  logBufferMax: 2000,
  offline: false,
  includeSynthetic: false,
};

/**
 * Query-string keys that are credentials and are therefore refused, everywhere
 * a URL is built or resolved (see `resolveConfig`, `apiUrl`, `apiUrlIsClean`).
 * `projectId`/`apiBase` are not credentials: they are non-secret identifiers
 * and may keep travelling as query parameters.
 */
export const FORBIDDEN_URL_KEYS = Object.freeze(["token"]);

/** First non-empty value, treating "" and nullish alike. */
function firstNonEmpty(values) {
  for (const v of values) {
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return "";
}

/**
 * Query keys in a URL that must never carry a credential. Pure so the rule can
 * be asserted in tests and reused by the URL builders below.
 *
 * @param {string} urlOrSearch absolute URL, path or bare `?a=b` string
 * @returns {string[]} the offending keys, in the order they were found
 */
export function forbiddenQueryKeys(urlOrSearch) {
  const raw = String(urlOrSearch ?? "");
  const qs = raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : raw.replace(/^\?/, "");
  if (!qs) return [];
  const search = qs.split("#")[0];
  const out = [];
  for (const [k] of new URLSearchParams(search).entries()) {
    if (FORBIDDEN_URL_KEYS.includes(k) && !out.includes(k)) out.push(k);
  }
  return out;
}

/** True when the URL is safe to request/log/share (no credential in it). */
export function apiUrlIsClean(urlOrSearch) {
  return forbiddenQueryKeys(urlOrSearch).length === 0;
}

/**
 * Remove credential keys from a query string / location search, keeping every
 * other parameter in place and in order. Used to scrub the address bar after a
 * legacy `?token=…` link is opened (so the credential cannot be re-shared from
 * the URL or replayed out of history).
 *
 * @param {string} search e.g. `?apiBase=x&token=secret&projectId=y`
 * @param {string[]} keys keys to drop (default: the forbidden ones)
 * @returns {string} `?apiBase=x&projectId=y` (or "" when nothing is left)
 */
export function stripKeysFromQuery(search, keys = FORBIDDEN_URL_KEYS) {
  const raw = String(search ?? "");
  const hashless = raw.split("#")[0];
  const qs = hashless.includes("?") ? hashless.slice(hashless.indexOf("?") + 1) : hashless.replace(/^\?/, "");
  const kept = [];
  for (const [k, v] of new URLSearchParams(qs).entries()) {
    if (keys.includes(k)) continue;
    kept.push([k, v]);
  }
  if (!kept.length) return "";
  const out = new URLSearchParams();
  for (const [k, v] of kept) out.append(k, v);
  return `?${out.toString()}`;
}

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
  // The credential IS picked from the query: a share link has to work in one
  // click. `tokenFromUrl` tells the app to keep it for this tab and scrub the
  // URL as soon as it has been read.
  const urlToken = String(query.token ?? "");
  const token = String(firstNonEmpty([urlToken, stored.token, file.token, defaults.token]) ?? "");
  return {
    apiBase: trimSlash(String(pick("apiBase") ?? "")),
    projectId: String(pick("projectId") ?? ""),
    token,
    pollMs: clampInt(pick("pollMs"), 1000, 60000, defaults.pollMs),
    decisionMs: clampInt(pick("decisionMs"), 1000, 30000, defaults.decisionMs),
    decisionLimit: clampInt(pick("decisionLimit"), 5, 500, defaults.decisionLimit),
    windows: file.windows && typeof file.windows === "object" ? file.windows : defaults.windows,
    defaultWindow: String(pick("window") ?? file.defaultWindow ?? defaults.defaultWindow),
    logBufferMax: clampInt(pick("logBufferMax"), 50, 20000, defaults.logBufferMax),
    offline: bool(offlineFlag(query, stored, file), defaults.offline),
    includeSynthetic: bool(pick("synthetic") ?? query.synthetic, defaults.includeSynthetic),
    // The credential came in on the URL. The app adopts it into this tab's
    // sessionStorage and strips it from the address bar (main.js adoptUrlToken).
    tokenFromUrl: urlToken !== "",
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

/**
 * Build a REST URL under the configured base, preserving a leading path.
 *
 * Every request in this dashboard — REST polls and the SSE log stream alike —
 * goes through here, and this is the chokepoint that makes the URL-credential
 * anti-pattern impossible rather than merely discouraged: it throws rather than
 * return a URL carrying a forbidden key, whether the key came from the caller's
 * `extraQuery`, from a `cfg.apiBase` someone pasted out of a tokenized link, or
 * from anywhere else.
 */
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
  const url = `${resolved}${path}${q ? `?${q}` : ""}`;
  const bad = forbiddenQueryKeys(url);
  if (bad.length) {
    throw new TypeError(
      `refusing to build a request URL carrying the credential(s) ${bad.join(", ")}: ` +
        "credentials travel in the Authorization header, never in a URL " +
        "(dashboard/README.md, task t_5649d117)",
    );
  }
  return url;
}
