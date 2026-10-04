// ssh-panel.test.mjs — regression for the W5 console terminal auth gap.
//
// The composed console's terminal talks to the REAL controller, which gates the
// project endpoints on the project_token (contract §1.1). The terminal's REST
// calls (node list + SSH string) originally sent no Authorization header, so
// they 401'd against the controller (they only ever ran against the dev mock
// bridge, which did not enforce auth). requestHeaders() is the pure helper both
// call sites use; assert it carries the bearer token exactly when known.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { requestHeaders } from '../terminal/js/ssh-panel.js';

test('requestHeaders sends the project_token as a bearer', () => {
  assert.deepEqual(requestHeaders('demo-project-token'), {
    Accept: 'application/json',
    Authorization: 'Bearer demo-project-token',
  });
});

test('requestHeaders omits Authorization when no token is known', () => {
  assert.deepEqual(requestHeaders(''), { Accept: 'application/json' });
  assert.deepEqual(requestHeaders(undefined), { Accept: 'application/json' });
  assert.deepEqual(requestHeaders(null), { Accept: 'application/json' });
});
