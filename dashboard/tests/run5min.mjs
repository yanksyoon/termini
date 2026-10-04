/*
 * Continuous-run verifier for the Termini dashboard (acceptance evidence).
 *
 * Launches headless Chromium, drives it over the DevTools Protocol (Node's
 * built-in WebSocket — no npm packages), loads the dashboard, and for
 * `durationSeconds`:
 *   - collects every console error/warning, uncaught exception and browser log
 *     entry (Runtime.consoleAPICalled / Runtime.exceptionThrown / Log.entryAdded)
 *   - samples the rendered DOM every 15 s (node cards, statuses, log rows,
 *     charts, pending approvals, degraded banner, pills)
 *   - captures a final screenshot and writes summary.json
 *
 * Exit code is non-zero if any console error, uncaught exception or browser log
 * error was observed.
 *
 * Usage:  node dashboard/tests/run5min.mjs [url] [durationSeconds]
 *         CHROME=/path/to/chrome  overrides browser discovery.
 */
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import { mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const URL = process.argv[2] || 'http://127.0.0.1:8099/';
const DURATION = Number(process.argv[3] || 300);

function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  for (const name of ['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable']) {
    const r = spawnSync('sh', ['-c', `command -v ${name}`], { encoding: 'utf8' });
    if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  }
  const hermes = `${process.env.HOME}/.hermes/tools`;
  const r = spawnSync('sh', ['-c', `ls -d ${hermes}/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1`], { encoding: 'utf8' });
  if (r.stdout && r.stdout.trim()) return r.stdout.trim();
  console.error('no chrome/chromium found; set CHROME=/path/to/chrome');
  process.exit(2);
}

const CHROME = findChrome();
const PORT = Number(process.env.CDP_PORT || 9333);
const OUT = process.env.OUT || join(tmpdir(), `termini-dashboard-run-${Date.now()}`);
mkdirSync(OUT, { recursive: true });

const chrome = spawn(
  CHROME,
  [
    '--headless=new',
    '--no-sandbox',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--hide-scrollbars',
    '--window-size=1680,1050',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${join(OUT, 'chrome-profile')}`,
    'about:blank',
  ],
  { stdio: 'ignore' },
);

const killChrome = () => {
  try {
    chrome.kill('SIGKILL');
  } catch {
    /* ignore */
  }
};
process.on('exit', killChrome);

let target = null;
for (let i = 0; i < 80 && !target; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    target = list.find((t) => t.type === 'page');
  } catch {
    /* not up yet */
  }
  if (!target) await sleep(250);
}
if (!target) {
  console.error('FAIL: chrome devtools never came up');
  killChrome();
  process.exit(2);
}

const ws = new WebSocket(target.webSocketDebuggerUrl);
let nextId = 1;
const pending = new Map();
const ev = { consoleErrors: [], consoleWarns: [], exceptions: [], logErrors: [], logWarns: [] };

const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

ws.addEventListener('message', (m) => {
  let msg;
  try {
    msg = JSON.parse(m.data);
  } catch {
    return;
  }
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
    return;
  }
  const { method, params } = msg;
  if (method === 'Runtime.consoleAPICalled') {
    const text = (params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' ');
    if (params.type === 'error' || params.type === 'assert') ev.consoleErrors.push(text);
    else if (params.type === 'warning') ev.consoleWarns.push(text);
  } else if (method === 'Runtime.exceptionThrown') {
    const d = params.exceptionDetails || {};
    ev.exceptions.push(`${d.text || ''} ${(d.exception && d.exception.description) || ''} @${d.url || ''}:${d.lineNumber || ''}`);
  } else if (method === 'Log.entryAdded') {
    const e = params.entry || {};
    if (e.level === 'error') ev.logErrors.push(`${e.source || ''}: ${e.text || ''} ${e.url || ''}`);
    else if (e.level === 'warning') ev.logWarns.push(`${e.source || ''}: ${e.text || ''}`);
  }
});

await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true });
  ws.addEventListener('error', reject, { once: true });
});
await send('Runtime.enable');
await send('Log.enable');
await send('Page.enable');
await send('Page.navigate', { url: URL });

const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'eval failed');
  return r.result ? r.result.value : undefined;
};

const COUNTERS = `(() => ({
  nodes: document.querySelectorAll('.node-card').length,
  statuses: Array.from(document.querySelectorAll('.node-card .badge[class*=status-]')).map(e=>e.textContent).join(','),
  logRows: document.querySelectorAll('.log-row').length,
  charts: document.querySelectorAll('.chart-canvas').length,
  pending: document.querySelectorAll('#pending-list .pending-item').length,
  banner: (document.querySelector('#banner-host .banner')||{}).textContent || null,
  backendPill: (document.getElementById('pill-backend-text')||{}).textContent || null,
  streamPill: (document.getElementById('pill-stream-text')||{}).textContent || null
}))()`;

await sleep(6000);
console.log('[   0s]', JSON.stringify(await evaluate(COUNTERS)));

const statuses = new Set();
const t0 = Date.now();
while (Date.now() - t0 < DURATION * 1000) {
  await sleep(15000);
  let c;
  try {
    c = await evaluate(COUNTERS);
  } catch (e) {
    console.log(`[err] ${e.message}`);
    continue;
  }
  String(c.statuses || '').split(',').filter(Boolean).forEach((s) => statuses.add(s));
  console.log(`[${String(Math.round((Date.now() - t0) / 1000)).padStart(4)}s]`, JSON.stringify(c));
}

try {
  writeFileSync(join(OUT, 'final.png'), Buffer.from((await send('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  console.log('screenshot:', join(OUT, 'final.png'));
} catch (e) {
  console.log('screenshot failed:', e.message);
}

const summary = {
  url: URL,
  duration_s: Math.round((Date.now() - t0) / 1000),
  statuses_observed: [...statuses],
  console_errors: ev.consoleErrors,
  console_warnings: ev.consoleWarns,
  uncaught_exceptions: ev.exceptions,
  log_errors: ev.logErrors,
  log_warnings: ev.logWarns,
  counts: {
    console_errors: ev.consoleErrors.length,
    uncaught_exceptions: ev.exceptions.length,
    log_errors: ev.logErrors.length,
    console_warnings: ev.consoleWarns.length,
    log_warnings: ev.logWarns.length,
  },
};
writeFileSync(join(OUT, 'summary.json'), JSON.stringify(summary, null, 2));
console.log('SUMMARY', JSON.stringify(summary));

ws.close();
killChrome();
process.exit(summary.counts.console_errors + summary.counts.uncaught_exceptions + summary.counts.log_errors === 0 ? 0 : 1);
