/*
 * AI-operator log stream client — SSE primary (dashboard.md §2.2/§2.3).
 *
 * EventSource cannot carry an Authorization header, so the project_token rides
 * in the query string (contract §2.2). Reconnect/backfill uses `?since=<log_id>`
 * kept from the last line we saw; the browser also auto-reconnects and would
 * send Last-Event-ID, which the server accepts as the equivalent cursor.
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

export class LogStream {
  /**
   * @param cfg resolved config
   * @param handlers {onLine(line), onStatus({state, detail}), onError(err)}
   */
  constructor(cfg, handlers = {}) {
    this.cfg = cfg;
    this.handlers = handlers;
    this.es = null;
    this.lastId = null;
    this.state = "idle";
    this._retry = 0;
    this._closed = false;
    this._timer = null;
  }

  setStatus(state, detail) {
    this.state = state;
    if (this.handlers.onStatus) this.handlers.onStatus({ state, detail });
  }

  start() {
    this._closed = false;
    this._open();
  }

  _open() {
    if (this._closed) return;
    if (typeof EventSource === "undefined") {
      this.setStatus("unsupported", "EventSource unavailable in this browser");
      return;
    }
    const url = apiUrl(this.cfg, `/projects/${encodeURIComponent(this.cfg.projectId)}/logs`, {
      // First connect asks for the retained tail (§2.3 out-of-buffer cursor);
      // every later reconnect resumes from the last line we actually saw.
      since: this.lastId || FIRST_CURSOR,
      // EventSource cannot set an Authorization header (contract §2.2).
      token: this.cfg.token || undefined,
    });
    this.setStatus("connecting");
    let es;
    try {
      es = new EventSource(url);
    } catch (err) {
      this.setStatus("error", String(err && err.message));
      this._scheduleReconnect();
      return;
    }
    this.es = es;

    es.onopen = () => {
      this._retry = 0;
      this.setStatus("open");
    };

    const handle = (ev) => {
      const raw = parseData(ev);
      if (!raw) return;
      const line = normalizeLine(raw);
      if (!line) return;
      // A control line (§2.3 truncation marker) is a real cursor but not a real
      // event; the panel decides how to show it.
      if (line.log_id && !line.control) this.lastId = line.log_id;
      else if (line.log_id && !this.lastId) this.lastId = line.log_id;
      if (this.handlers.onLine) this.handlers.onLine(line);
    };

    es.addEventListener("log", handle);
    // Some servers emit unnamed messages; accept those too.
    es.onmessage = handle;

    es.onerror = (err) => {
      // EventSource retries on its own; surface state and keep the cursor.
      const ready = es.readyState;
      if (ready === 2 /* CLOSED */) {
        this.setStatus("closed", "stream closed by server");
        this._scheduleReconnect();
      } else {
        this.setStatus("reconnecting", "stream interrupted");
      }
      if (this.handlers.onError) this.handlers.onError(err);
    };
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
    if (this.es) {
      try {
        this.es.close();
      } catch {
        /* ignore */
      }
      this.es = null;
    }
    this.setStatus("stopped");
  }
}

function parseData(ev) {
  if (!ev || typeof ev.data !== "string" || ev.data === "") return null;
  try {
    return JSON.parse(ev.data);
  } catch {
    // Tolerate a plain-text line from a partially-migrated server.
    return { message: ev.data, severity: "info", ts: new Date().toISOString() };
  }
}
