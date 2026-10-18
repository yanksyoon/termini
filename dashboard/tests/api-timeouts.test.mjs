/*
 * Per-call client deadlines on the dashboard's REST client (task t_1cd79fba).
 *
 * The bug these pin: `_request()` gave EVERY call one 20 s
 * `AbortSignal.timeout(20000)`. `/intents` runs the on-prem model extraction
 * synchronously (controller bound: `intent_model_timeout_s` = 120 s) and
 * `/vms/apply` creates and starts LXD instances before answering
 * (`lxd_create_timeout_s` + `lxd_start_timeout_s` = 120 s each). Both routinely
 * outlive 20 s, so the dashboard aborted its own requests while the controller
 * was still working — the live controller answered `200` onto a socket the
 * browser had closed (`BrokenPipeError`) and the reader was told the controller
 * was "unreachable", which is both false and a bad instruction (retrying
 * `/vms/apply` creates a second set of instances).
 *
 * Two properties are asserted:
 *   1. the deadline each method asks for is the one its server-side work needs,
 *      and reads keep the short one;
 *   2. an expired client deadline is reported as `CLIENT_TIMEOUT` ("may still be
 *      working"), never as `NETWORK`/"controller unreachable".
 *
 * Run: node --test dashboard/tests/api-timeouts.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ApiClient, ApiError, TIMEOUT_MS } from "../js/api.js";

const CFG = {
  apiBase: "https://controller.example/v1",
  projectId: "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B",
  token: "project-token-000000000000000000000000000000000000000000",
};

/** Record every deadline `api.js` asks for without actually waiting for one. */
function captureTimeout() {
  const seen = [];
  const original = Object.getOwnPropertyDescriptor(AbortSignal, "timeout");
  const fake = (ms) => {
    seen.push(ms);
    return undefined; // no real timer: the stub fetch answers immediately
  };
  try {
    Object.defineProperty(AbortSignal, "timeout", { value: fake, configurable: true, writable: true });
  } catch {
    AbortSignal.timeout = fake;
  }
  return {
    seen,
    restore() {
      try {
        Object.defineProperty(AbortSignal, "timeout", { value: original.value, configurable: true, writable: true });
      } catch {
        AbortSignal.timeout = original.value;
      }
    },
  };
}

function okBody(payload = { ok: true }) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

test("the long-running calls declare the deadlines their server-side work needs", async (t) => {
  const cap = captureTimeout();
  const urls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return okBody();
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
    cap.restore();
  });

  const api = new ApiClient(CFG);
  await api.submitIntent({ prompt: "Deploy 2 worker agents" });
  await api.applyPlan("01H4KZ9Q2R3T5V7W9X1Y3Z5A7C");
  await api.assistantAsk({ messages: [] });
  await api.listNodes();

  assert.deepEqual(cap.seen, [
    TIMEOUT_MS.model,
    TIMEOUT_MS.provision,
    TIMEOUT_MS.assistant,
    TIMEOUT_MS.read,
  ]);
  // The whole point: model and provisioning calls must outlive the 20 s read
  // deadline, and provisioning must outlive the model call.
  assert.ok(TIMEOUT_MS.model > TIMEOUT_MS.read);
  assert.ok(TIMEOUT_MS.provision > TIMEOUT_MS.model);
  assert.equal(TIMEOUT_MS.read, 20000);
  assert.match(urls[0], /\/intents$/);
  assert.match(urls[1], /\/vms\/apply$/);
});

test("an expired deadline is reported as a client timeout, not 'unreachable'", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    const err = new Error("The operation was aborted due to timeout");
    err.name = "TimeoutError";
    throw err;
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const api = new ApiClient(CFG);
  const err = await api.submitIntent({ prompt: "x" }).then(
    () => null,
    (e) => e,
  );
  assert.ok(err instanceof ApiError);
  assert.equal(err.status, 0);
  assert.equal(err.code, "CLIENT_TIMEOUT");
  assert.doesNotMatch(err.message, /unreachable/i);
  // It says how long it waited and that the work may have landed.
  assert.match(err.message, /180s/);
  assert.match(err.message, /may still be working/);
});

test("a genuine transport failure is still 'controller unreachable'", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new TypeError("fetch failed");
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const api = new ApiClient(CFG);
  const err = await api.listNodes().then(
    () => null,
    (e) => e,
  );
  assert.equal(err.code, "NETWORK");
  assert.match(err.message, /controller unreachable/);
});
