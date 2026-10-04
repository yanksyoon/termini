// Unit tests for the terminal session state machine (terminal-client.js),
// driven by a mock WebSocket and a deterministic fake clock.
// Run: node --test frontend/test/terminal-client.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { TerminalSession, SessionState } from '../terminal/js/terminal-client.js';
import { encodeInput, encodeResize, bytesToBase64, utf8ToBytes, bytesToUtf8 } from '../terminal/js/framing.js';

// --------------------------------------------------------------------------
// test doubles
// --------------------------------------------------------------------------
class MockWebSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }
  send(data) {
    this.sent.push(data);
  }
  close(code, reason) {
    this.closedWith = { code, reason };
    if (this.onclose) this.onclose({ code: code ?? 1000, reason, wasClean: true });
  }
  // test drivers
  fireOpen() {
    this.readyState = 1;
    if (this.onopen) this.onopen({});
  }
  fireMessage(obj) {
    const data = typeof obj === 'string' ? obj : JSON.stringify(obj);
    if (this.onmessage) this.onmessage({ data });
  }
  fireClose(code = 1006, reason = '') {
    this.readyState = 3;
    if (this.onclose) this.onclose({ code, reason, wasClean: false });
  }
  fireError() {
    if (this.onerror) this.onerror({});
  }
}

class FakeClock {
  constructor() {
    this.now = 0;
    this.seq = 0;
    this.timers = new Map();
  }
  setTimeout(fn, ms) {
    const id = ++this.seq;
    this.timers.set(id, { at: this.now + ms, fn, interval: null });
    return id;
  }
  setInterval(fn, ms) {
    const id = ++this.seq;
    this.timers.set(id, { at: this.now + ms, fn, interval: ms });
    return id;
  }
  clearTimeout(id) {
    this.timers.delete(id);
  }
  clearInterval(id) {
    this.timers.delete(id);
  }
  advance(ms) {
    const target = this.now + ms;
    for (;;) {
      let nextId = null;
      let nextAt = Infinity;
      for (const [id, t] of this.timers) {
        if (t.at <= target && t.at < nextAt) {
          nextAt = t.at;
          nextId = id;
        }
      }
      if (nextId === null) break;
      this.now = nextAt;
      const t = this.timers.get(nextId);
      if (t.interval) t.at = this.now + t.interval;
      else this.timers.delete(nextId);
      t.fn();
    }
    this.now = target;
  }
}

function setup(overrides = {}) {
  const clock = new FakeClock();
  const sockets = [];
  const events = [];
  const session = new TerminalSession({
    url: 'ws://test/terminal/v1/nodes/node-a?token=t',
    socketFactory: (url) => {
      const ws = new MockWebSocket(url);
      sockets.push(ws);
      return ws;
    },
    initialSize: { cols: 100, rows: 30 },
    backoffBaseMs: 500,
    backoffMaxMs: 5000,
    random: () => 0,
    setTimeout: clock.setTimeout.bind(clock),
    clearTimeout: clock.clearTimeout.bind(clock),
    setInterval: clock.setInterval.bind(clock),
    clearInterval: clock.clearInterval.bind(clock),
    ...overrides,
  });
  for (const ev of ['statechange', 'ready', 'data', 'exit', 'error', 'reconnecting', 'closed', 'protocolError']) {
    session.on(ev, (payload) => events.push([ev, payload]));
  }
  return { session, sockets, events, clock };
}

const states = (events) => events.filter(([e]) => e === 'statechange').map(([, s]) => s);
const emitted = (events, name) => events.filter(([e]) => e === name);

// --------------------------------------------------------------------------
test('connect -> ready reaches CONNECTED and replays the initial size', () => {
  const { session, sockets, events } = setup();
  session.connect();
  assert.equal(session.state, SessionState.CONNECTING);
  assert.equal(sockets.length, 1);

  sockets[0].fireOpen();
  assert.equal(session.state, SessionState.CONNECTING, 'still connecting until ready');

  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu', shell: '/bin/bash' });
  assert.equal(session.state, SessionState.CONNECTED);
  assert.equal(emitted(events, 'ready').length, 1);
  assert.deepEqual(sockets[0].sent, [encodeResize(100, 30)]);
  assert.deepEqual(states(events), [SessionState.CONNECTING, SessionState.CONNECTED]);
});

test('resize stores size and only sends once connected', () => {
  const { session, sockets } = setup();
  session.connect();
  assert.equal(session.resize(90, 20), false, 'cannot send before ready');
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  // ready replays the latest stored size (90x20), not the constructor default
  assert.deepEqual(sockets[0].sent, [encodeResize(90, 20)]);
  session.resize(120, 40);
  assert.equal(sockets[0].sent.at(-1), encodeResize(120, 40));
});

test('send relays input only when connected', () => {
  const { session, sockets } = setup();
  session.connect();
  assert.equal(session.send('ls\n'), false);
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  assert.equal(session.send('whoami\n'), true);
  assert.ok(sockets[0].sent.includes(encodeInput('whoami\n')));
});

test('output frames are decoded to bytes and emitted as data', () => {
  const { session, sockets, events } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  sockets[0].fireMessage({ type: 'output', data: bytesToBase64(utf8ToBytes('ubuntu\n')) });
  const data = emitted(events, 'data');
  assert.equal(data.length, 1);
  assert.equal(bytesToUtf8(data[0][1]), 'ubuntu\n');
});

test('an unexpected drop reconnects with backoff and re-establishes', () => {
  const { session, sockets, events, clock } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });

  sockets[0].fireClose(1006, 'lost'); // no exit/error frame -> transient
  assert.equal(session.state, SessionState.RECONNECTING);
  assert.equal(emitted(events, 'reconnecting').length, 1);
  assert.equal(sockets.length, 1);
  assert.equal(emitted(events, 'error').length, 0, 'a transient drop is not an error');

  clock.advance(500); // backoff (jitter 0)
  assert.equal(sockets.length, 2, 'a new socket is opened');
  sockets[1].fireOpen();
  sockets[1].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  assert.equal(session.state, SessionState.CONNECTED);
});

test('NODE_OFFLINE error frame is terminal (no reconnect)', () => {
  const { session, sockets, events, clock } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'error', code: 'NODE_OFFLINE', message: 'node not reachable' });

  assert.equal(session.state, SessionState.ERROR);
  const errs = emitted(events, 'error');
  assert.equal(errs.length, 1);
  assert.equal(errs[0][1].code, 'NODE_OFFLINE');

  clock.advance(60000);
  assert.equal(sockets.length, 1, 'must not reconnect an offline node');
  assert.equal(states(events).includes(SessionState.RECONNECTING), false);
});

test('auth close 4401 is terminal and reported as UNAUTHORIZED', () => {
  const { session, sockets, events, clock } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireClose(4401, 'unauthorized');
  assert.equal(session.state, SessionState.ERROR);
  assert.equal(emitted(events, 'error')[0][1].code, 'UNAUTHORIZED');
  clock.advance(60000);
  assert.equal(sockets.length, 1);
});

test('exit frame ends the session cleanly without reconnect', () => {
  const { session, sockets, events, clock } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  sockets[0].fireMessage({ type: 'exit', code: -1, reason: 'idle_timeout' });
  assert.equal(session.state, SessionState.CLOSED);
  assert.equal(emitted(events, 'exit')[0][1].reason, 'idle_timeout');
  clock.advance(60000);
  assert.equal(sockets.length, 1);
});

test('user disconnect suppresses reconnect', () => {
  const { session, sockets, events, clock } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  session.disconnect();
  assert.equal(session.state, SessionState.CLOSED);
  clock.advance(60000);
  assert.equal(sockets.length, 1);
  assert.equal(states(events).includes(SessionState.RECONNECTING), false);
});

test('reconnect gives up after maxReconnectAttempts', () => {
  const { session, sockets, events, clock } = setup({ maxReconnectAttempts: 1 });
  session.connect();
  // drop 1 -> reconnect allowed
  sockets[0].fireClose(1006);
  clock.advance(1000);
  assert.equal(sockets.length, 2);
  // drop 2 -> exhausted
  sockets[1].fireClose(1006);
  clock.advance(1000);
  assert.equal(session.state, SessionState.ERROR);
  assert.equal(emitted(events, 'error').at(-1)[1].code, 'RECONNECT_EXHAUSTED');
  assert.equal(sockets.length, 2);
});

test('malformed server frames surface protocolError without crashing', () => {
  const { session, sockets, events } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage('}{ not json');
  assert.equal(emitted(events, 'protocolError').length, 1);
  assert.equal(session.state, SessionState.CONNECTING, 'session survives a bad frame');
});

test('pong frames are handled silently', () => {
  const { session, sockets, events } = setup();
  session.connect();
  sockets[0].fireOpen();
  sockets[0].fireMessage({ type: 'ready', node_id: 'node-a', user: 'ubuntu' });
  sockets[0].fireMessage({ type: 'pong' });
  assert.equal(emitted(events, 'protocolError').length, 0);
  assert.equal(session.state, SessionState.CONNECTED);
});

test('default timers are invoked with a safe `this` (browser Illegal-invocation guard)', () => {
  // In browsers, `setTimeout` detached from `window` (called as `this._setTimeout`)
  // throws "Illegal invocation" — which stalled reconnect. The default wrappers
  // must call the globals bound to nothing, not to the session object.
  const orig = globalThis.setTimeout;
  let badThis = false;
  globalThis.setTimeout = function (fn, ms) {
    // strict-mode module: a bare call has this === undefined; a method call on
    // the session would pass the session object here.
    if (this !== undefined && this !== globalThis) badThis = true;
    return orig(fn, ms);
  };
  try {
    const s = new TerminalSession({
      url: 'ws://host/terminal/v1/nodes/n?token=t',
      socketFactory: (u) => new MockWebSocket(u),
      random: () => 0,
      backoffBaseMs: 1,
      backoffMaxMs: 2,
    });
    s.connect();
    s._ws.fireClose(1006); // schedules a reconnect via the default _setTimeout
    assert.equal(badThis, false, 'global setTimeout must not receive the session as `this`');
    assert.equal(s.state, SessionState.RECONNECTING);
    s.disconnect();
  } finally {
    globalThis.setTimeout = orig;
  }
});
