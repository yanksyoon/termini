/*
 * Served-page checks for the assistant chat (t_d1aa35c8 work item 6).
 *
 * The other chat tests drive the real panel objects against the dependency-free
 * DOM double. This one goes through the HTTP boundary instead: it boots the
 * contract-faithful mock controller, fetches the page and the modules THE
 * SERVER ACTUALLY RETURNS, and asserts
 *
 *   1. the served page carries the docked chat panel and every Ask AI surface
 *      the panel is seeded from;
 *   2. the served modules are byte-identical to the ones the unit tests import
 *      (a stale bundle can never pass this while the source tests are green);
 *   3. the served assistant route answers an incident context with the §4.10
 *      shape, citing the incident's own id — the request the seeded chip sends.
 *
 * Run: node --test dashboard/tests/served-page.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const DASHBOARD = join(HERE, "..");
const PROJECT_ID = "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B";
const PROJECT_TOKEN = "demo-project-token-000000000000000000000000000000000000000000";

/** An OS-assigned free port (so parallel CI jobs never collide on 8099). */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function waitForServer(base, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${base}/`);
      if (res.ok) return;
      last = `status ${res.status}`;
    } catch (err) {
      last = err.message;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`mock server did not become ready: ${last}`);
}

test("served page + served assistant route match the panels the tests drive", async (t) => {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const child = spawn(process.env.PYTHON || "python3", [join(DASHBOARD, "mock", "server.py"), "--port", String(port)], {
    cwd: DASHBOARD,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (b) => {
    stderr += b.toString();
  });
  t.after(() => {
    child.kill("SIGTERM");
  });

  await waitForServer(base);

  /* 1) the served page carries the panel and every seeding surface --------- */
  const page = await (await fetch(`${base}/index.html`)).text();
  for (const id of ["btn-chat", "chat-panel", "chat-messages", "chat-context-chip", "chat-suggestions", "banner-host", "decision-list", "overview", "charts", "pending-list"]) {
    assert.ok(page.includes(`id="${id}"`), `the served page still carries #${id}`);
  }
  assert.match(page, /js\/main\.js/, "the served page boots the module bundle");
  assert.match(page, /malformed|fixture data|grounded in controller records/, "the served page names the chat's grounding");

  /* 2) the served modules are the ones the unit tests import -------------- */
  // A stale or half-copied bundle would otherwise let the component tests pass
  // while the projector demo loads different code.
  for (const rel of ["js/core/assistant.js", "js/ui/chat.js", "js/ui/decisions.js", "js/offline.js", "js/main.js"]) {
    const served = await (await fetch(`${base}/${rel}`)).text();
    const local = await readFile(join(DASHBOARD, rel), "utf8");
    assert.equal(served, local, `${rel} served by the mock is byte-identical to the source the tests import`);
  }

  /* 3) the Ask AI affordances are wired in the SERVED panel module -------- */
  const decisions = await (await fetch(`${base}/js/ui/decisions.js`)).text();
  assert.match(decisions, /askAiKind:\s*kind/, "decision rows and incident cards seed the chat with a context kind");
  assert.match(decisions, /decisionId:\s*r\.id/, "the seeded context also carries the decision the card was rendered from");

  /* 4) the served route answers an incident context with §4.10 shape ------ */
  const events = await (
    await fetch(`${base}/v1/operator/events`, { headers: { Authorization: `Bearer ${PROJECT_TOKEN}` } })
  ).json();
  const event = (events.events || [])[0];
  assert.ok(event && event.id, "the mock serves an incident to seed from");

  const res = await fetch(`${base}/v1/projects/${PROJECT_ID}/assistant/ask`, {
    method: "POST",
    headers: { Authorization: `Bearer ${PROJECT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: [{ role: "user", content: "What did Termini do about this incident?" }],
      context: { kind: "incident", id: event.id },
    }),
  });
  assert.equal(res.status, 200, `the served assistant route answered: ${stderr || "no server error"}`);
  const body = await res.json();
  for (const key of ["answer", "citations", "executed", "proposed_actions", "decided_by", "model", "elapsed_ms", "truncated", "errors"]) {
    assert.ok(key in body, `the served answer carries ${key}`);
  }
  assert.ok(body.answer.length > 0, "the served answer is not empty");
  assert.ok(
    body.citations.some((c) => c.kind === "incident" && c.id === event.id),
    "the answer cites the incident the chip attached",
  );
});
