// ssh-command.js — sanitize host/user/key values and build the frozen SSH
// access string: `ssh -i <private_key_path> [-p <port>] <user>@<public_ip>`
// (docs/contracts/dashboard.md §3.2).
//
// Security: the dashboard renders operator-supplied/inventory values into a
// command that a human copy-pastes into a real shell, so every part is
// validated against a strict allowlist before it is ever rendered. Anything
// that could carry shell metacharacters is rejected outright rather than
// escaped, and the server-computed `command` from GET /nodes/{id}/ssh is NOT
// trusted — we rebuild it from the validated parts.

// DNS hostname (labels of [A-Za-z0-9-], <=253 chars total, no leading/trailing -).
const DNS_RE = /^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
const IPV6_RE = /^[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}$|^::(ffff(:0{1,4})?:)?(\d{1,3}\.){3}\d{1,3}$|^([0-9A-Fa-f]{0,4}:){1,4}:(\d{1,3}\.){3}\d{1,3}$/;
const USER_RE = /^[A-Za-z0-9._][A-Za-z0-9._-]{0,31}$/; // no leading '-' or other metas
const KEYPATH_RE = /^[A-Za-z0-9._/~+-]{1,512}$/; // no spaces, quotes, $, ;, |, &, `, etc.

/** @returns {string|null} the cleaned user, or null if unsafe. */
export function sanitizeUser(user) {
  if (typeof user !== 'string') return null;
  const v = user.trim();
  return USER_RE.test(v) ? v : null;
}

/**
 * @returns {string|null} the cleaned host (IPv4 / IPv6 / DNS name), or null.
 * IPv6 may arrive bracketed (`[::1]`); brackets are stripped and re-added at
 * render time.
 */
export function sanitizeHost(host) {
  if (typeof host !== 'string') return null;
  let v = host.trim();
  if (v.startsWith('[') && v.endsWith(']')) v = v.slice(1, -1);
  if (v.length === 0 || v.length > 253) return null;
  if (IPV6_RE.test(v)) return v;
  if (DNS_RE.test(v)) return v;
  return null;
}

/** @returns {string|null} the cleaned private-key path, or null if unsafe. */
export function sanitizeKeyPath(path) {
  if (typeof path !== 'string') return null;
  const v = path.trim();
  if (v.startsWith('-')) return null; // never let it be parsed as an ssh flag
  return KEYPATH_RE.test(v) ? v : null;
}

/** @returns {number|null} a valid TCP port, or null. */
export function sanitizePort(port) {
  if (port === undefined || port === null || port === '') return 22;
  const n = typeof port === 'number' ? port : Number(port);
  if (!Number.isInteger(n) || n < 1 || n > 65535) return null;
  return n;
}

/** POSIX single-quote a value (used only where quoting is explicitly wanted). */
export function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

/** True when `cmd` is a well-formed rendered SSH command (post-sanitization). */
export function isSafeSshCommand(cmd) {
  if (typeof cmd !== 'string') return false;
  const m = /^ssh -i ([^\s]+)(?: -p (\d{1,5}))? ([A-Za-z0-9._-]+)@([A-Za-z0-9.:\[\]-]+)$/.exec(cmd);
  if (!m) return false;
  const host = m[4].replace(/^\[/, '').replace(/\]$/, ''); // sanitizeHost strips brackets
  return sanitizeKeyPath(m[1]) === m[1] && sanitizeUser(m[3]) === m[3] && sanitizeHost(host) === host;
}

/**
 * Build the exact frozen command string. Throws on any unsafe part so a
 * bad inventory value can never render as an injectable command.
 *
 * @param {{user:string, host:string, port?:number|string, keyPath:string}} parts
 * @returns {string}
 */
export function buildSshCommand({ user, host, port, keyPath }) {
  const u = sanitizeUser(user);
  const h = sanitizeHost(host);
  const k = sanitizeKeyPath(keyPath);
  const p = sanitizePort(port);
  if (!u) throw new Error(`unsafe SSH user: ${JSON.stringify(user)}`);
  if (!h) throw new Error(`unsafe SSH host: ${JSON.stringify(host)}`);
  if (!k) throw new Error(`unsafe SSH key path: ${JSON.stringify(keyPath)}`);
  if (p === null) throw new Error(`invalid SSH port: ${JSON.stringify(port)}`);
  const hostPart = h.includes(':') ? `[${h}]` : h; // bracket IPv6
  const portPart = p === 22 ? '' : ` -p ${p}`;
  return `ssh -i ${k}${portPart} ${u}@${hostPart}`;
}

/**
 * Turn a GET /v1/projects/{pid}/nodes/{nid}/ssh response into a rendered,
 * sanitized command. The server's own `command` field is ignored on purpose:
 * we re-derive from the individual fields so an unexpected/hostile value can
 * never reach the clipboard.
 *
 * @param {object} payload  contract §3.2 shape: {user, host, port, command, ...}
 * @param {{keyPath?:string}} [opts]  operator's local private-key path
 * @returns {{command:string, user:string, host:string, port:number,
 *            keyFingerprint:string|null, keyAlgorithm:string|null,
 *            keyComment:string|null}}
 */
export function sshCommandFromNode(payload, opts = {}) {
  if (!payload || typeof payload !== 'object') throw new Error('missing SSH payload');
  const keyPath = opts.keyPath || '~/.ssh/termini_breakglass';
  const user = sanitizeUser(payload.user);
  const host = sanitizeHost(payload.host);
  const port = sanitizePort(payload.port);
  const command = buildSshCommand({ user, host, port, keyPath });
  return {
    command,
    user,
    host,
    port,
    keyFingerprint: typeof payload.key_fingerprint === 'string' ? payload.key_fingerprint : null,
    keyAlgorithm: typeof payload.key_algorithm === 'string' ? payload.key_algorithm : null,
    keyComment: typeof payload.key_comment === 'string' ? payload.key_comment : null,
  };
}
