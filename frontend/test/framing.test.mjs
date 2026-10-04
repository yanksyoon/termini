// Unit tests for the terminal wire codec (framing.js).
// Run: node --test frontend/test/framing.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  bytesToBase64,
  base64ToBytes,
  encodeInput,
  encodeResize,
  encodePing,
  decodeFrame,
  terminalUrl,
  utf8ToBytes,
  bytesToUtf8,
  CLOSE_UNAUTHORIZED,
} from '../terminal/js/framing.js';

test('base64 matches the frozen contract sample', () => {
  // docs/contracts/dashboard.md §3.1: {"type":"input","data":"bHMgLWxhCg=="}
  assert.equal(bytesToBase64(utf8ToBytes('ls -la\n')), 'bHMgLWxhCg==');
  assert.equal(bytesToUtf8(base64ToBytes('bHMgLWxhCg==')), 'ls -la\n');
});

test('encodeInput frames UTF-8 input exactly', () => {
  assert.equal(encodeInput('ls -la\n'), '{"type":"input","data":"bHMgLWxhCg=="}');
});

test('base64 round-trips all 256 byte values', () => {
  const all = Uint8Array.from({ length: 256 }, (_, i) => i);
  const rt = base64ToBytes(bytesToBase64(all));
  assert.deepEqual(Array.from(rt), Array.from(all));
});

test('base64 padding paths round-trip for lengths 0..16', () => {
  for (let n = 0; n <= 16; n++) {
    const b = Uint8Array.from({ length: n }, (_, i) => (i * 37 + 11) & 0xff);
    assert.deepEqual(Array.from(base64ToBytes(bytesToBase64(b))), Array.from(b), `len ${n}`);
  }
});

test('base64ToBytes rejects malformed input', () => {
  assert.throws(() => base64ToBytes('abc'), /length/);
  assert.throws(() => base64ToBytes('!!!!'), /invalid base64/);
});

test('encodeResize clamps and floors', () => {
  assert.equal(encodeResize(120, 40), '{"type":"resize","cols":120,"rows":40}');
  assert.equal(encodeResize(0, -3), '{"type":"resize","cols":1,"rows":1}');
  assert.equal(encodeResize(80.9, 24.2), '{"type":"resize","cols":80,"rows":24}');
});

test('encodePing frames a keepalive', () => {
  assert.equal(encodePing(), '{"type":"ping"}');
});

test('decodeFrame parses every server frame type', () => {
  assert.equal(decodeFrame('{"type":"pong"}').type, 'pong');
  assert.deepEqual(decodeFrame('{"type":"ready","user":"ubuntu","shell":"/bin/bash"}'), {
    type: 'ready',
    user: 'ubuntu',
    shell: '/bin/bash',
  });
  assert.equal(decodeFrame('{"type":"output","data":"dG90YWwgMjQK"}').data, 'dG90YWwgMjQK');
  assert.equal(decodeFrame('{"type":"exit","code":0,"reason":"normal"}').code, 0);
  assert.equal(decodeFrame('{"type":"error","code":"NODE_OFFLINE"}').code, 'NODE_OFFLINE');
});

test('decodeFrame rejects malformed frames', () => {
  assert.throws(() => decodeFrame('not json'), /malformed terminal frame/);
  assert.throws(() => decodeFrame('[1,2,3]'), /must be a JSON object/);
  assert.throws(() => decodeFrame('{"data":"x"}'), /missing "type"/);
  assert.throws(() => decodeFrame('{"type":42}'), /missing "type"/);
});

test('terminalUrl upgrades http->ws and https->wss with token', () => {
  assert.equal(
    terminalUrl('http://127.0.0.1:8091', 'node-a', 'tok en'),
    'ws://127.0.0.1:8091/terminal/v1/node-a?token=tok+en',
  );
  assert.equal(
    terminalUrl('https://ctrl.example.com', '01ABC', 't1'),
    'wss://ctrl.example.com/terminal/v1/01ABC?token=t1',
  );
});

test('terminalUrl percent-encodes the node id and omits empty token', () => {
  assert.equal(terminalUrl('http://h', 'a/b c', ''), 'ws://h/terminal/v1/a%2Fb%20c');
  assert.throws(() => terminalUrl('http://h', '', 'x'), /nodeId/);
});

test('CLOSE_UNAUTHORIZED is the contract close code', () => {
  assert.equal(CLOSE_UNAUTHORIZED, 4401);
});
