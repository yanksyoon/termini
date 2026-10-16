/**
 * The credential-in-URL rule (task `t_5649d117`) — enforced, not just documented.
 *
 * The published dashboard URL used to carry `?token=<project_token>`. That is
 * shareable by construction (address bar, browser history, Referer, proxy logs,
 * any link a viewer copies), so it is now refused at three depths:
 *
 *   1. `resolveConfig` never reads a credential from the query string,
 *   2. `apiUrl` (used by every REST call AND the SSE stream) throws rather than
 *      build a URL carrying one,
 *   3. the SSE reader sends the credential in the `Authorization` header, which
 *      is why the dashboard no longer uses `EventSource` at all.
 *
 * The last test is the behavioural one: it runs the real `LogStream` against a
 * real HTTP server and asserts on the bytes the server actually received.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import {
  DEFAULTS,
  apiUrl,
  apiUrlIsClean,
  forbiddenQueryKeys,
  resolveConfig,
  stripKeysFromQuery,
} from "../js/core/config.js";
import { FIRST_CURSOR, LogStream, parseSseBlock, splitSseBlocks } from "../js/stream.js";

test("config: ?token= in the URL is never resolved into a credential", () => {
  const cfg = resolveConfig({
    query: { apiBase: "https://c.example/v1", projectId: "01J0DEM0WARM000000000000AB", token: "leaked-in-url" },
    stored: {},
    file: {},
    defaults: DEFAULTS,
  });
  assert.equal(cfg.token, "");
  assert.equal(cfg.urlTokenIgnored, true);
  assert.equal(cfg.projectId, "01J0DEM0WARM000000000000AB");
  assert.equal(cfg.apiBase, "https://c.example/v1");
});

test("config: the credential still comes from this tab's storage (never the query)", () => {
  assert.equal(resolveConfig({ query: {}, stored: { token: "from-storage" }, file: { token: "from-file" } }).token, "from-storage");
  assert.equal(resolveConfig({ stored: {}, file: { token: "from-file" } }).token, "from-file");
  assert.equal(resolveConfig({ query: { token: "x" }, stored: {}, file: {} }).token, "");
  assert.equal(resolveConfig({ stored: {}, file: {} }).urlTokenIgnored, false);
});

test("stripKeysFromQuery scrubs the credential and keeps everything else", () => {
  assert.equal(
    stripKeysFromQuery("?apiBase=https://c/v1&token=secret&projectId=01J0DEM0WARM000000000000AB"),
    "?apiBase=https%3A%2F%2Fc%2Fv1&projectId=01J0DEM0WARM000000000000AB",
  );
  assert.equal(stripKeysFromQuery("?token=secret"), "");
  assert.equal(stripKeysFromQuery(""), "");
  assert.equal(stripKeysFromQuery("?projectId=01AB#frag"), "?projectId=01AB");
});

test("apiUrl refuses to build any URL that carries a credential", () => {
  const cfg = { apiBase: "https://c.example/v1" };
  assert.throws(() => apiUrl(cfg, "/projects/p/logs", { token: "secret" }), /credential/);
  assert.throws(() => apiUrl({ apiBase: "https://c.example/v1?token=secret" }, "/projects/p/nodes"), /credential/);
  // non-credential parameters are untouched
  assert.equal(apiUrl(cfg, "/projects/p/nodes"), "https://c.example/v1/projects/p/nodes");
  assert.equal(apiUrl(cfg, "/telemetry/recent", { node: "01M4", limit: 5 }), "https://c.example/v1/telemetry/recent?node=01M4&limit=5");
  assert.equal(forbiddenQueryKeys("https://c/v1/x?token=1&limit=2").join(","), "token");
  assert.equal(forbiddenQueryKeys("https://c/v1/x?limit=2").length, 0);
  assert.equal(apiUrlIsClean("https://c/v1/x?limit=2"), true);
});

test("sse: frames are parsed from the wire format", () => {
  const { blocks, rest } = splitSseBlocks('event: log\ndata: {"a":1}\n\nevent: log\ndata: {"a":2}\n\npartial');
  assert.equal(blocks.length, 2);
  assert.equal(rest, "partial");
  assert.deepEqual(parseSseBlock(blocks[0]), { event: "log", data: '{"a":1}' });
  assert.equal(parseSseBlock(": keep-alive"), null);
  // multi-line data is joined per the SSE grammar
  assert.deepEqual(parseSseBlock("event: log\ndata: one\ndata: two"), { event: "log", data: "one\ntwo" });
});

test("sse: the real client puts the credential in a header and nowhere in the URL", async () => {
  const CRED = "client-credential-value";
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push({ url: req.url, auth: req.headers.authorization || "", method: req.method });
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    res.write(`event: log\ndata: ${JSON.stringify({ log_id: "01M4TGD31W0000000000000B1", message: "first", severity: "info", ts: "2026-10-15T16:00:00Z" })}\n\n`);
    res.write(": keep-alive\n\n");
    res.write(`event: log\ndata: ${JSON.stringify({ log_id: "01M4TGD31W0000000000000B2", message: "second", severity: "info", ts: "2026-10-15T16:00:01Z" })}\n\n`);
    res.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;

  const lines = [];
  const states = [];
  const stream = new LogStream(
    { apiBase: `http://127.0.0.1:${port}/v1`, projectId: "01J0DEM0WARM000000000000AB", token: CRED },
    { onLine: (l) => lines.push(l), onStatus: (s) => states.push(s.state) },
  );
  stream.start();

  const deadline = Date.now() + 8000;
  while (lines.length < 2 && Date.now() < deadline) await new Promise((r) => setTimeout(r, 20));
  stream.stop();
  await new Promise((resolve) => server.close(resolve));

  assert.equal(lines.length, 2, "both SSE frames were parsed by the fetch-based reader");
  assert.equal(lines[1].message, "second");
  assert.equal(stream.lastId, "01M4TGD31W0000000000000B2", "cursor advances on real lines");
  assert.ok(states.includes("open"));

  assert.ok(seen.length >= 1, "the server saw a request");
  for (const s of seen) {
    assert.equal(s.method, "GET");
    assert.ok(!s.url.includes(CRED), `credential must not be in the URL: ${s.url}`);
    assert.ok(!/[?&]token=/.test(s.url), `no token query parameter: ${s.url}`);
    assert.equal(s.auth, `Bearer ${CRED}`, "credential travels in the Authorization header");
  }
  assert.ok(seen[0].url.includes(`since=${FIRST_CURSOR}`), "the documented cold-start cursor is still requested");
});
