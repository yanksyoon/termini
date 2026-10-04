// terminal-client.js — a DOM-free terminal session state machine that speaks
// the Termini terminal WebSocket protocol (docs/contracts/dashboard.md §3.1).
//
// Responsibilities:
//   * open the socket and complete the ready handshake
//   * relay input/resize/ping out, output/exit/error/pong in
//   * surface a clean connection state (connecting/connected/reconnecting/…)
//   * reconnect with exponential backoff + jitter on an unexpected drop
//   * refuse to auto-reconnect on an explicit server error/exit or a 4401 auth
//     rejection (those are terminal, not transient)
//
// It is transport-agnostic: pass any `socketFactory(url) -> WebSocketLike`.
// That makes the whole thing unit-testable in Node with a mock socket, while
// the browser passes the real `WebSocket` constructor.

import {
  CLOSE_UNAUTHORIZED,
  FRAME,
  base64ToBytes,
  decodeFrame,
  encodeInput,
  encodePing,
  encodeResize,
} from './framing.js';

export const SessionState = Object.freeze({
  IDLE: 'idle',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  RECONNECTING: 'reconnecting',
  CLOSED: 'closed',
  ERROR: 'error',
});

/** Minimal event emitter (browser EventTarget is not available in all test envs). */
class Emitter {
  constructor() {
    this._handlers = new Map();
  }
  on(event, cb) {
    if (!this._handlers.has(event)) this._handlers.set(event, []);
    this._handlers.get(event).push(cb);
    return () => this.off(event, cb);
  }
  off(event, cb) {
    const list = this._handlers.get(event);
    if (!list) return;
    const i = list.indexOf(cb);
    if (i >= 0) list.splice(i, 1);
  }
  emit(event, ...args) {
    const list = this._handlers.get(event);
    if (!list) return;
    for (const cb of list.slice()) {
      try {
        cb(...args);
      } catch (e) {
        // A listener throwing must not break the state machine.
        // eslint-disable-next-line no-console
        if (typeof console !== 'undefined') console.error('terminal listener error', e);
      }
    }
  }
}

export class TerminalSession {
  /**
   * @param {object} opts
   * @param {string} opts.url             terminal WebSocket URL
   * @param {Function} [opts.socketFactory] (url) => WebSocketLike; defaults to WebSocket
   * @param {{cols:number,rows:number}} [opts.initialSize]
   * @param {number} [opts.pingIntervalMs=20000]
   * @param {number} [opts.maxReconnectAttempts=8]
   * @param {number} [opts.backoffBaseMs=500]
   * @param {number} [opts.backoffMaxMs=10000]
   * @param {Function} [opts.random]     injectable RNG for deterministic jitter
   * @param {Function} [opts.setTimeout]
   * @param {Function} [opts.clearTimeout]
   * @param {Function} [opts.setInterval]
   * @param {Function} [opts.clearInterval]
   */
  constructor(opts = {}) {
    if (!opts.url) throw new Error('TerminalSession requires a url');
    this.url = opts.url;
    this.socketFactory = opts.socketFactory || ((url) => new WebSocket(url));
    // Wrap the timer globals: calling `setTimeout` detached from `window`
    // (e.g. `this._setTimeout(...)`) throws "Illegal invocation" in browsers.
    // Tests inject a fake clock through the same options.
    this._setTimeout = opts.setTimeout || ((fn, ms) => setTimeout(fn, ms));
    this._clearTimeout = opts.clearTimeout || ((id) => clearTimeout(id));
    this._setInterval = opts.setInterval || ((fn, ms) => setInterval(fn, ms));
    this._clearInterval = opts.clearInterval || ((id) => clearInterval(id));
    this.initialSize = opts.initialSize || null;
    this.pingIntervalMs = opts.pingIntervalMs ?? 20000;
    this.maxReconnectAttempts = opts.maxReconnectAttempts ?? 8;
    this.backoffBaseMs = opts.backoffBaseMs ?? 500;
    this.backoffMaxMs = opts.backoffMaxMs ?? 10000;
    this._random = opts.random || Math.random;

    this._state = SessionState.IDLE;
    this._ws = null;
    this._ready = false;
    this._size = this.initialSize;
    this._attempt = 0;
    this._manualClose = false;
    this._serverEnded = false;
    this._pingTimer = null;
    this._reconnectTimer = null;
    this._emitter = new Emitter();
  }

  get state() {
    return this._state;
  }

  get attempt() {
    return this._attempt;
  }

  on(event, cb) {
    return this._emitter.on(event, cb);
  }

  _setState(state) {
    if (this._state === state) return;
    this._state = state;
    this._emitter.emit('statechange', state);
  }

  /** Start (or restart) the session. Resets any prior terminal condition. */
  connect() {
    this._manualClose = false;
    this._serverEnded = false;
    this._attempt = 0;
    this._open();
  }

  _open() {
    this._clearReconnectTimer();
    this._ready = false;
    this._setState(SessionState.CONNECTING);
    let ws;
    try {
      ws = this.socketFactory(this.url);
    } catch (e) {
      this._failTransient(e);
      return;
    }
    this._ws = ws;
    ws.onopen = () => {
      // Await the server's `ready` frame before declaring the session usable.
      this._emitter.emit('open');
    };
    ws.onmessage = (ev) => this._onMessage(ev && 'data' in ev ? ev.data : ev);
    ws.onclose = (ev) => this._onClose(ev || {});
    ws.onerror = (ev) => {
      this._emitter.emit('socketError', ev);
    };
  }

  _onMessage(raw) {
    let frame;
    try {
      frame = decodeFrame(typeof raw === 'string' ? raw : new TextDecoder().decode(raw));
    } catch (e) {
      this._emitter.emit('protocolError', e);
      return;
    }
    switch (frame.type) {
      case FRAME.READY:
        this._ready = true;
        this._attempt = 0;
        this._setState(SessionState.CONNECTED);
        this._emitter.emit('ready', frame);
        if (this._size) this.resize(this._size.cols, this._size.rows);
        this._startPing();
        break;
      case FRAME.OUTPUT: {
        let bytes;
        try {
          bytes = base64ToBytes(frame.data || '');
        } catch (e) {
          this._emitter.emit('protocolError', e);
          return;
        }
        this._emitter.emit('data', bytes);
        break;
      }
      case FRAME.EXIT:
        // Server ended the session (normal, idle_timeout, replaced). Do not
        // auto-reconnect; surface the reason so the UI can offer a manual retry.
        this._serverEnded = true;
        this._stopPing();
        this._emitter.emit('exit', frame);
        this._setState(SessionState.CLOSED);
        this._closeSocket();
        break;
      case FRAME.ERROR:
        // Definite failure (NODE_OFFLINE, SESSION_REPLACED, INTERNAL…).
        this._serverEnded = true;
        this._stopPing();
        this._setState(SessionState.ERROR);
        this._emitter.emit('error', frame);
        this._closeSocket();
        break;
      case FRAME.PONG:
        this._emitter.emit('pong');
        break;
      default:
        this._emitter.emit('unknown', frame);
    }
  }

  _onClose(ev) {
    this._stopPing();
    const code = ev && typeof ev.code === 'number' ? ev.code : undefined;
    this._ws = null;
    this._ready = false;

    if (code === CLOSE_UNAUTHORIZED) {
      this._setState(SessionState.ERROR);
      this._emitter.emit('error', { type: 'error', code: 'UNAUTHORIZED', message: 'unauthorized' });
      this._emitter.emit('closed', ev);
      return;
    }
    if (this._manualClose) {
      this._setState(SessionState.CLOSED);
      this._emitter.emit('closed', ev);
      return;
    }
    if (this._serverEnded) {
      // State already moved to CLOSED/ERROR by the frame handler.
      this._emitter.emit('closed', ev);
      return;
    }

    // Unexpected drop -> reconnect with exponential backoff.
    if (this._attempt >= this.maxReconnectAttempts) {
      this._setState(SessionState.ERROR);
      this._emitter.emit('error', {
        type: 'error',
        code: 'RECONNECT_EXHAUSTED',
        message: `gave up after ${this._attempt} reconnect attempts`,
      });
      this._emitter.emit('closed', ev);
      return;
    }
    this._attempt += 1;
    const base = Math.min(this.backoffMaxMs, this.backoffBaseMs * 2 ** (this._attempt - 1));
    const delay = base + Math.floor(base * 0.2 * this._random());
    this._setState(SessionState.RECONNECTING);
    this._emitter.emit('reconnecting', { attempt: this._attempt, delayMs: delay });
    this._reconnectTimer = this._setTimeout(() => this._open(), delay);
  }

  _failTransient(err) {
    this._attempt += 1;
    if (this._attempt > this.maxReconnectAttempts) {
      this._setState(SessionState.ERROR);
      this._emitter.emit('error', { type: 'error', code: 'CONNECT_FAILED', message: String(err) });
      return;
    }
    const base = Math.min(this.backoffMaxMs, this.backoffBaseMs * 2 ** (this._attempt - 1));
    const delay = base + Math.floor(base * 0.2 * this._random());
    this._setState(SessionState.RECONNECTING);
    this._emitter.emit('reconnecting', { attempt: this._attempt, delayMs: delay });
    this._reconnectTimer = this._setTimeout(() => this._open(), delay);
  }

  /** Send terminal input (Uint8Array or string). No-op unless connected. */
  send(data) {
    if (!this._canSend()) return false;
    this._ws.send(encodeInput(data));
    return true;
  }

  /** Send a resize frame. Size is remembered and replayed on (re)connect. */
  resize(cols, rows) {
    this._size = { cols: Math.max(1, Math.floor(cols)), rows: Math.max(1, Math.floor(rows)) };
    if (!this._canSend()) return false;
    this._ws.send(encodeResize(this._size.cols, this._size.rows));
    return true;
  }

  /** User-initiated disconnect: closes cleanly and suppresses auto-reconnect. */
  disconnect() {
    this._manualClose = true;
    this._stopPing();
    this._clearReconnectTimer();
    this._closeSocket(1000, 'client close');
    this._setState(SessionState.CLOSED);
  }

  _closeSocket(code, reason) {
    const ws = this._ws;
    if (!ws) return;
    try {
      ws.close(code, reason);
    } catch {
      /* already closing */
    }
  }

  _canSend() {
    return this._state === SessionState.CONNECTED && this._ready && !!this._ws;
  }

  _startPing() {
    this._stopPing();
    if (this.pingIntervalMs > 0) {
      this._pingTimer = this._setInterval(() => {
        if (this._canSend()) this._ws.send(encodePing());
      }, this.pingIntervalMs);
    }
  }

  _stopPing() {
    if (this._pingTimer != null) {
      this._clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
  }

  _clearReconnectTimer() {
    if (this._reconnectTimer != null) {
      this._clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }
}
