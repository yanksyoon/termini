// app.js — wires xterm.js + TerminalSession + SshPanel into the demo page.
// Config comes from the URL query so the same static bundle points at the dev
// mock bridge or the real controller:
//   ?api=http://127.0.0.1:8091 &project=<pid> &token=<project_token> &key=<path>

import { TerminalSession, SessionState } from './terminal-client.js';
import { terminalUrl } from './framing.js';
import { SshPanel } from './ssh-panel.js';

const params = new URLSearchParams(location.search);
const apiBase = (params.get('api') || location.origin).replace(/\/$/, '');
const projectId = params.get('project') || 'demo';
const token = params.get('token') || 'demo-project-token';
const keyPath = params.get('key') || '~/.ssh/termini_breakglass';

const $ = (id) => document.getElementById(id);
const nodesUrl = `${apiBase}/v1/projects/${projectId}/nodes`;
const sshUrl = (nodeId) => `${apiBase}/v1/projects/${projectId}/nodes/${nodeId}/ssh`;

const els = {
  select: $('node-select'),
  connect: $('connect-btn'),
  reconnect: $('reconnect-btn'),
  state: $('conn-state'),
  banner: $('banner'),
  term: $('terminal'),
};

// --- terminal ------------------------------------------------------------
const term = new window.Terminal({
  convertEol: true,
  cursorBlink: true,
  fontFamily: '"DejaVu Sans Mono", Menlo, Consolas, monospace',
  fontSize: 13,
  theme: { background: '#0b0f14', foreground: '#d7e0ea', cursor: '#7dd3fc' },
  scrollback: 5000,
});
const fitAddon = new window.FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(els.term);
fitAddon.fit();

// --- ssh panel -----------------------------------------------------------
const sshPanel = new SshPanel($('ssh-panel-root'), {
  endpoint: sshUrl,
  keyPath,
});

// --- session -------------------------------------------------------------
let session = null;
let currentNode = null;

const STATE_LABEL = {
  [SessionState.IDLE]: 'idle',
  [SessionState.CONNECTING]: 'connecting…',
  [SessionState.CONNECTED]: 'connected',
  [SessionState.RECONNECTING]: 'reconnecting…',
  [SessionState.CLOSED]: 'closed',
  [SessionState.ERROR]: 'error',
};
const STATE_CLASS = {
  [SessionState.CONNECTING]: 'state-pending',
  [SessionState.RECONNECTING]: 'state-pending',
  [SessionState.CONNECTED]: 'state-ok',
  [SessionState.CLOSED]: 'state-idle',
  [SessionState.ERROR]: 'state-err',
};

function setState(state) {
  els.state.textContent = STATE_LABEL[state] || state;
  els.state.className = `conn-state ${STATE_CLASS[state] || ''}`;
}

function showBanner(msg, kind = 'info') {
  els.banner.textContent = msg;
  els.banner.className = `banner banner-${kind}`;
  els.banner.hidden = !msg;
}

function termLine(text, color) {
  const esc = color ? `\x1b[${color}m` : '';
  const reset = color ? '\x1b[0m' : '';
  term.write(`\r\n${esc}${text}${reset}\r\n`);
}

function disconnect(manual = true) {
  if (session) {
    if (manual) session.disconnect();
    session = null;
  }
}

function connect(nodeId) {
  if (!nodeId) return;
  disconnect();
  currentNode = nodeId;
  term.reset();
  showBanner('');

  const url = terminalUrl(apiBase, nodeId, token);
  termLine(`Connecting to ${nodeId} …`, '90');

  session = new TerminalSession({
    url,
    initialSize: { cols: term.cols, rows: term.rows },
    socketFactory: (u) => new WebSocket(u),
  });

  session.on('statechange', setState);
  session.on('ready', (f) => {
    showBanner('');
    termLine(`Connected to ${f.node_id || nodeId} as ${f.user || 'user'} (${f.shell || 'shell'})`, '32');
    // Re-assert the current geometry once the server shell is live.
    session.resize(term.cols, term.rows);
  });
  session.on('data', (bytes) => term.write(bytes));
  session.on('exit', (f) => {
    termLine(`Session ended (${f.reason || 'exit'}${typeof f.code === 'number' ? `, code ${f.code}` : ''}).`, '33');
    showBanner(`Session ended: ${f.reason || 'exit'}. Click Connect to start a new session.`, 'warn');
  });
  session.on('error', (f) => {
    const msg = `${f.code || 'ERROR'}: ${f.message || 'connection failed'}`;
    termLine(msg, '31');
    showBanner(
      f.code === 'NODE_OFFLINE'
        ? `Node ${nodeId} is unreachable (${msg}). The terminal is not hung — pick another node or retry once it is online.`
        : `Terminal error — ${msg}`,
      'error',
    );
  });
  session.on('reconnecting', ({ attempt, delayMs }) => {
    showBanner(`Connection lost — reconnecting (attempt ${attempt}, in ${Math.round(delayMs)} ms)…`, 'warn');
    termLine(`Connection lost; reconnecting (attempt ${attempt})…`, '33');
  });
  session.on('closed', () => {
    els.connect.textContent = 'Connect';
  });
  session.on('protocolError', (e) => termLine(`Protocol error: ${e.message}`, '31'));

  session.connect();
}

// --- node selector -------------------------------------------------------
async function loadNodes() {
  setState(SessionState.IDLE);
  try {
    const res = await fetch(nodesUrl, { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const nodes = Array.isArray(data.nodes) ? data.nodes : [];
    els.select.innerHTML = '';
    if (!nodes.length) {
      els.select.appendChild(new Option('(no nodes)', ''));
      showBanner('No nodes found for this project.', 'warn');
      return;
    }
    for (const n of nodes) {
      const label = `${n.node_id}${n.role ? ` · ${n.role}` : ''}${n.state ? ` · ${n.state}` : ''}`;
      els.select.appendChild(new Option(label, n.node_id));
    }
    els.select.value = nodes[0].node_id;
    await selectNode(nodes[0].node_id);
  } catch (e) {
    els.select.innerHTML = '';
    els.select.appendChild(new Option('(failed to load nodes)', ''));
    showBanner(`Could not load nodes: ${e.message}`, 'error');
  }
}

async function selectNode(nodeId) {
  if (!nodeId) return;
  disconnect();
  setState(SessionState.IDLE);
  await sshPanel.load(nodeId);
}

// --- events --------------------------------------------------------------
els.select.addEventListener('change', () => selectNode(els.select.value));

els.connect.addEventListener('click', () => {
  const state = session ? session.state : SessionState.IDLE;
  if (state === SessionState.CONNECTED || state === SessionState.CONNECTING || state === SessionState.RECONNECTING) {
    disconnect();
    termLine('Disconnected.', '90');
    setState(SessionState.CLOSED);
    els.connect.textContent = 'Connect';
  } else {
    els.connect.textContent = 'Disconnect';
    connect(els.select.value);
  }
});

els.reconnect.addEventListener('click', () => {
  els.connect.textContent = 'Disconnect';
  connect(els.select.value);
});

// terminal input -> session
term.onData((data) => {
  if (session && !session.send(data)) {
    /* not connected — ignore */
  }
});
term.onResize(({ cols, rows }) => {
  if (session) session.resize(cols, rows);
});

// viewport resize -> refit terminal -> session resize (via term.onResize)
let fitTimer = null;
function refit() {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => {
    try {
      fitAddon.fit();
    } catch (e) {
      if (window.__termini) window.__termini.lastFitError = String(e && e.message ? e.message : e);
    }
  }, 80);
}
window.addEventListener('resize', refit);
if (typeof ResizeObserver !== 'undefined') new ResizeObserver(refit).observe(els.term);

// --- boot ----------------------------------------------------------------
setState(SessionState.IDLE);
loadNodes();
window.__termini = {
  connect,
  disconnect,
  get session() {
    return session;
  },
  term,
  fitAddon,
  refit,
  lastFitError: null,
};
