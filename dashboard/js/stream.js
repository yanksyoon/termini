/*
 * AI-operator log stream client — SSE read with `fetch` (dashboard.md §2.2/§2.3).
 *
 * Why not `EventSource`: EventSource cannot set request headers, so the only way
 * to authenticate it is to put the credential in the query string — and a
 * credential in a URL is shareable by construction (address bar, history,
 * Referer, proxy logs, a copied link). The published dashboard URL carried
 * `?token=<project_token>` for exactly this reason; that pattern is now
 * forbidden (task `t_5649d117`).
 *
 * So the same wire format (`text/event-stream`) is consumed with `fetch()` and a
 * stream reader: the credential rides in the `Authorization` header and NEVER in
 * the URL. `apiUrl()` throws if a caller ever tries to build a URL with a
 * forbidden key, so this cannot silently regress.
 *
 * Reconnect/backfill keeps the `?since=<log_id>` cursor (contract §2.3) — the
 * browser's automatic EventSource reconnect (and its Last-Event-ID) is replaced
 * by the explicit backoff loop below, which preserves every other property of
 * the previous client: the cold-start cursor, the control line, and the fact
 * that a control line is a real cursor but not a real event.
 */

import { apiUrl } from "./core/config.js";
import { normalizeLine } from "./core/logs.js";

/**
 * Cold-start cursor (contract §2.3). The controller's SSE replays only when it
 * is given a cursor, so a fresh page load with no cursor renders NOTHING until
 * the next live event — which is how the published panel came to read
 * "0 lines — Waiting for operator events…" while the same stream was delivering
 * events to anyone who asked for the tail.
 *
 * A ULID of all zeros is a well-formed cursor older than anything the ring
 * buffer can hold, so §2.3's documented out-of-buffer path applies: the server
 * emits one control line and then replays every retained line, live-tail
 * included. After that first connection every reconnect uses the real last id.
 */
export const FIRST_CURSOR = "00000000000000000000000000";

/** Split a growing SSE buffer into complete event blocks + the remainder. */
export function splitSseBlocks(buffer) {
  const blocks = [];
  let rest = String(buffer ?? "");
  let idx = rest.indexOf("\n\n");
  while (idx !== -1) {
    blocks.push(rest.slice(0, idx));
    rest = rest.slice(idx + 2);
    idx = rest.indexOf("\n\n");
  }
  return { blocks, rest };
}

/**
 * Parse one SSE event block into `{event, data}` (data is the raw text, with
 * multi-line `data:` joined by "\n" per the SSE grammar). Exported for tests.
 */
export function parseSseBlock(block) {
  let event = "";
  const data = [];
  for (const rawLine of String(block ?? "").split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (!line || line.startsWith(":")) continue; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  return { event, data: data.join("\n") };
}

export class LogStream {
  /**
   * @param cfg resolved config
   * @param handlers {onLine(line), onStatus({state, detail}), onError(err)}
   */
  constructor(cfg, handlers = {}) {
    this.cfg = cfg;
    this.handlers = handlers;
    this.lastId = null;
    this.state = "idle";
    this._retry = 0;
    this._closed = false;
    this._timer = null;
    this._ac = null;
    this._opening = false;
  }

  setStatus(state, detail) {
    this.state = state;
    if (this.handlers.onStatus) this.handlers.onStatus({ state, detail });
  }

  start() {
    this._closed = false;
    this._open();
  }

  /** The exact URL this client will request — always credential-free. */
  url() {
    return apiUrl(this.cfg, `/projects/${encodeURIComponent(this.cfg.projectId)}/logs`, {
      // First connect asks for the retained tail (§2.3 out-of-buffer cursor);
      // every later reconnect resumes from the last line we actually saw.
      since: this.lastId || FIRST_CURSOR,
    });
  }

  _headers() {
    const h = { Accept: "text/event-stream", "Cache-Control": "no-cache", "X-Termini-Version": "1" };
    // The credential travels here — never in `url()`.
    if (this.cfg.token) h.Authorization = `Bearer ${this.cfg.token}`;
    return h;
  }

  _handleData(raw) {
    if (!raw) return;
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // Tolerate a plain-text line from a partially-migrated server.
      parsed = { message: raw, severity: "info", ts: new Date().toISOString() };
    }
    const line = normalizeLine(parsed);
    if (!line) return;
    // A control line (§2.3 truncation marker) is a real cursor but not a real
    // event; the panel decides how to show it.
    if (line.log_id && !line.control) this.lastId = line.log_id;
    else if (line.log_id && !this.lastId) this.lastId = line.log_id;
    if (this.handlers.onLine) this.handlers.onLine(line);
  }

  async _open() {
    if (this._closed || this._opening) return;
    this._opening = true;
    let url;
    try {
      url = this.url();
    } catch (err) {
      this._opening = false;
      this.setStatus("error", String(err && err.message));
      return;
    }
    this.setStatus("connecting");
    this._ac = typeof AbortController !== "undefined" ? new AbortController() : null;
    let res;
    try {
      res = await fetch(url, {
        method: "GET",
        headers: this._headers(),
        signal: this._ac ? this._ac.signal : undefined,
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch (err) {
      this._opening = false;
      if (this._closed) return;
      this.setStatus("reconnecting", `stream interrupted (${err && err.message})`);
      if (this.handlers.onError) this.handlers.onError(err);
      this._scheduleReconnect();
      return;
    }
    if (this._closed) {
      this._opening = false;
      return;
    }
    if (!res.ok || !res.body) {
      this._opening = false;
      const detail = res.status === 401 || res.status === 403
        ? `${res.status} — not signed in (open Settings)`
        : `HTTP ${res.status} from ${url}`;
      this.setStatus("error", detail);
      if (this.handlers.onError) this.handlers.onError(new Error(detail));
      this._scheduleReconnect();
      return;
    }

    this._retry = 0;
    this.setStatus("open");
    this._opening = false;
    try {
      await this._read(res.body);
    } catch (err) {
      if (this._closed) return;
      this.setStatus("reconnecting", `stream interrupted (${err && err.message})`);
      if (this.handlers.onError) this.handlers.onError(err);
      this._scheduleReconnect();
      return;
    }
    if (this._closed) return;
    this.setStatus("closed", "stream closed by server");
    this._scheduleReconnect();
  }

  async _read(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { blocks, rest } = splitSseBlocks(buffer);
      buffer = rest;
      for (const block of blocks) {
        const ev = parseSseBlock(block);
        if (!ev) continue;
        // `event: log` is the documented frame; bare `message` frames and any
        // future type carrying data are tolerated, as the EventSource client
        // tolerated them.
        this._handleData(ev.data);
      }
    }
    if (buffer.trim()) {
      const ev = parseSseBlock(buffer);
      if (ev) this._handleData(ev.data);
    }
  }

  _scheduleReconnect() {
    if (this._closed) return;
    this._retry = Math.min(this._retry + 1, 6);
    const delay = Math.min(500 * 2 ** this._retry, 15000);
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._open(), delay);
  }

  stop() {
    this._closed = true;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    if (this._ac) {
      try {
        this._ac.abort();
      } catch {
        /* ignore */
      }
      this._ac = null;
    }
    this.setStatus("stopped");
  }
}
