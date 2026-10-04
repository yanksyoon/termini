// framing.js — wire codec for the Termini terminal WebSocket protocol.
//
// Frozen contract: docs/contracts/dashboard.md §3.1.
//   Client -> server: {"type":"input","data":"<base64>"} | {"type":"resize","cols":n,"rows":n} | {"type":"ping"}
//   Server -> client: {"type":"ready",...} | {"type":"output","data":"<base64>"} | {"type":"exit",...}
//                     | {"type":"error",...} | {"type":"pong"}
// Frames are UTF-8 text, one complete JSON object per frame. Terminal bytes
// travel base64-encoded inside `data` (binary-clean PTY relay).
//
// This module is intentionally dependency-free and usable from both the browser
// (ES module) and Node (used by the unit tests).

const B64_ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

const B64_LOOKUP = (() => {
  const m = new Int16Array(128).fill(-1);
  for (let i = 0; i < B64_ALPHA.length; i++) m[B64_ALPHA.charCodeAt(i)] = i;
  return m;
})();

/** Close code the server uses to reject an unauthenticated/invalid token. */
export const CLOSE_UNAUTHORIZED = 4401;

/** Terminal frame types, for exhaustive switch statements. */
export const FRAME = Object.freeze({
  INPUT: 'input',
  RESIZE: 'resize',
  PING: 'ping',
  READY: 'ready',
  OUTPUT: 'output',
  EXIT: 'exit',
  ERROR: 'error',
  PONG: 'pong',
});

/** base64-encode an ArrayBufferView / array of bytes. */
export function bytesToBase64(bytes) {
  const len = bytes.length;
  let out = '';
  for (let i = 0; i < len; i += 3) {
    const b0 = bytes[i];
    const b1 = i + 1 < len ? bytes[i + 1] : undefined;
    const b2 = i + 2 < len ? bytes[i + 2] : undefined;
    out += B64_ALPHA[b0 >> 2];
    out += B64_ALPHA[((b0 & 0x03) << 4) | ((b1 ?? 0) >> 4)];
    out += b1 === undefined ? '=' : B64_ALPHA[((b1 & 0x0f) << 2) | ((b2 ?? 0) >> 6)];
    out += b2 === undefined ? '=' : B64_ALPHA[b2 & 0x3f];
  }
  return out;
}

/** base64-decode a string to a Uint8Array. Throws on malformed input. */
export function base64ToBytes(b64) {
  if (typeof b64 !== 'string') throw new TypeError('base64 payload must be a string');
  const clean = b64.replace(/\s+/g, '');
  if (clean.length % 4 !== 0) throw new Error('invalid base64: length not a multiple of 4');
  let outLen = (clean.length / 4) * 3;
  if (clean.endsWith('==')) outLen -= 2;
  else if (clean.endsWith('=')) outLen -= 1;
  const out = new Uint8Array(outLen);
  let o = 0;
  for (let i = 0; i < clean.length; i += 4) {
    const c0 = decodeChar(clean.charAt(i), i);
    const c1 = decodeChar(clean.charAt(i + 1), i + 1);
    const c2 = clean.charAt(i + 2) === '=' ? -2 : decodeChar(clean.charAt(i + 2), i + 2);
    const c3 = clean.charAt(i + 3) === '=' ? -2 : decodeChar(clean.charAt(i + 3), i + 3);
    if (c0 < 0 || c1 < 0) throw new Error('invalid base64 character');
    if (o < outLen) out[o++] = (c0 << 2) | (c1 >> 4);
    if (c2 >= 0 && o < outLen) out[o++] = ((c1 & 0x0f) << 4) | (c2 >> 2);
    if (c3 >= 0 && o < outLen) out[o++] = ((c2 & 0x03) << 6) | c3;
  }
  return out;
}

function decodeChar(ch, idx) {
  const code = ch.charCodeAt(0);
  const v = code < 128 ? B64_LOOKUP[code] : -1;
  if (v === -1) throw new Error(`invalid base64 character at ${idx}`);
  return v;
}

/** UTF-8 encode a JS string to bytes (browser + Node). */
export function utf8ToBytes(str) {
  return new TextEncoder().encode(str);
}

/** UTF-8 decode bytes to a JS string (lossy on invalid sequences). */
export function bytesToUtf8(bytes) {
  return new TextDecoder('utf-8').decode(bytes);
}

/** Build a client "input" frame from raw bytes. */
export function encodeInput(data) {
  const bytes = typeof data === 'string' ? utf8ToBytes(data) : data;
  return JSON.stringify({ type: FRAME.INPUT, data: bytesToBase64(bytes) });
}

/** Build a client "resize" frame. */
export function encodeResize(cols, rows) {
  const c = Math.max(1, Math.floor(cols));
  const r = Math.max(1, Math.floor(rows));
  return JSON.stringify({ type: FRAME.RESIZE, cols: c, rows: r });
}

/** Build a client keepalive "ping" frame. */
export function encodePing() {
  return JSON.stringify({ type: FRAME.PING });
}

/**
 * Parse one server frame. Returns the decoded object; throws on malformed JSON
 * or a frame without a `type`. Never trusts the payload shape beyond that —
 * callers must validate `data` before decoding base64.
 */
export function decodeFrame(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    throw new Error(`malformed terminal frame: ${e.message}`);
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    throw new Error('terminal frame must be a JSON object');
  }
  if (typeof obj.type !== 'string' || obj.type === '') {
    throw new Error('terminal frame missing "type"');
  }
  return obj;
}

/**
 * Build the terminal WebSocket URL for a node:
 *   ws(s)://<host>/terminal/v1/nodes/{node_id}?token=<project_token>
 * (node-level shell; docs/contracts/dashboard.md §3.1, R-D1).
 * `origin` is the controller base (http:// or https://) or a full ws URL.
 */
export function terminalUrl(origin, nodeId, token) {
  if (!nodeId) throw new Error('nodeId is required');
  const u = new URL(origin);
  u.protocol = u.protocol === 'https:' || u.protocol === 'wss:' ? 'wss:' : 'ws:';
  u.pathname = `/terminal/v1/nodes/${encodeURIComponent(nodeId)}`;
  u.search = '';
  u.hash = '';
  if (token) u.search = `?${new URLSearchParams({ token }).toString()}`;
  return u.toString();
}
