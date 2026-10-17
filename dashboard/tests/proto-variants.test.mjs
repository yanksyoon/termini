/*
 * Prototype-surface checks for the three dashboard variants (task t_4458a089).
 *
 * These are the mechanical half of "no behaviour was lost" and of the craft bar,
 * so a reviewer does not have to trust the screenshots:
 *
 *   1. ID PARITY — every id in the shipped page (dashboard/index.html) exists in
 *      the prototype page. The prototype carries the same markup and boots the
 *      same unmodified modules, so the shipped behaviours are bound in all three
 *      variants by construction. A removed control fails here.
 *   2. NOTHING IS HIDDEN — no variant stylesheet hides a panel, a control or a
 *      decision surface with `display: none`; variants re-arrange and re-weight,
 *      they do not remove.
 *   3. CRAFT LINT — no `transition: all`, no `scale(0)`, no `ease-in` (only
 *      `ease-in-out`), every variant has a prefers-reduced-motion block and gates
 *      its hover styling, and the shared strong curves are the ones used.
 *   4. PICKER VERBATIM — the picker's markup, styles and key wiring match
 *      PICKER.md values (it is harness chrome, not a contestant).
 *   5. SERVED — over HTTP: /proto/ serves the page, all three variant sheets and
 *      the harness are reachable and byte-identical to disk, and the root page
 *      does not reference the prototype surface.
 *
 * Run: node --test dashboard/tests/proto-variants.test.mjs
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
const PROTO = join(DASHBOARD, "proto");
const VARIANTS = ["console", "briefing", "instrument", "hig"];

const readDashboard = (rel) => readFile(join(DASHBOARD, rel), "utf8");
const readProto = (rel) => readFile(join(PROTO, rel), "utf8");

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

/** `id="x"` tokens, in document order, deduped. */
function ids(html) {
  return [...new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]))];
}

/** Split a stylesheet into `{ selector, body }` pairs (flat, no nesting). */
function rules(css) {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out = [];
  for (const m of stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: m[1].trim(), body: m[2].trim() });
  }
  return out;
}

test("the prototype page carries every id the shipped page binds", async () => {
  const shipped = await readDashboard("index.html");
  const proto = await readProto("index.html");
  const shippedIds = ids(shipped);
  const protoIds = new Set(ids(proto));

  assert.ok(shippedIds.length > 40, `the shipped page has the expected id surface (${shippedIds.length})`);
  const missing = shippedIds.filter((id) => !protoIds.has(id));
  assert.deepEqual(missing, [], `the prototype page is missing ids: ${missing.join(", ")}`);

  /* Positive control: the comparison above must be able to fail. Break one id in
     the prototype markup in memory and prove the check notices it — otherwise a
     green result here would prove nothing. */
  const probe = proto.replace(' id="decision-list"', ' id="decision-list-probe"');
  const probeIds = new Set(ids(probe));
  assert.ok(
    shippedIds.filter((id) => !probeIds.has(id)).includes("decision-list"),
    "positive control: removing an id from the prototype markup is detected",
  );

  // It boots the same modules, unmodified, and the variants are pure design layers.
  assert.match(proto, /<script type="module" src="\.\.\/js\/main\.js"><\/script>/);
  assert.match(proto, /class="proto-picker"/);
  for (const v of VARIANTS) {
    assert.match(
      proto,
      new RegExp(`class="proto-variant"[^>]*data-variant="${v}"[^>]*href="variants/${v}\\.css"`),
      `${v} is declared as a variant sheet`,
    );
  }
  // The root page must not point at the prototype surface.
  assert.doesNotMatch(shipped, /proto\//, "the shipped dashboard does not reference /proto/");
});

test("no variant hides a panel, a control or a decision surface", async () => {
  const forbidden = [
    ".panel",
    ".overview",
    ".node-card",
    ".decision",
    ".incident",
    ".trace",
    ".chat",
    ".log-",
    ".btn",
    ".controls",
    ".metrics",
    ".chart",
    ".banner",
    ".badge",
    "#overview",
    "#charts",
    "#chat",
    "#log",
    "#decision",
  ];
  for (const v of VARIANTS) {
    const css = await readProto(`variants/${v}.css`);
    for (const { selector, body } of rules(css)) {
      if (!/display\s*:\s*none/.test(body)) continue;
      const hits = forbidden.filter((needle) => selector.includes(needle));
      assert.deepEqual(
        hits,
        [],
        `${v}.css hides "${selector}" which carries ${hits.join(", ")} — variants must not remove behaviour`,
      );
    }
  }
});

test("every variant meets the craft bar the skills set", async () => {
  for (const v of VARIANTS) {
    // Lint the declarations, not the prose: the files explain themselves in
    // comments, and a comment that says "never ease-in" is not a violation.
    const css = (await readProto(`variants/${v}.css`)).replace(/\/\*[\s\S]*?\*\//g, "");

    assert.doesNotMatch(css, /transition\s*:\s*all\b/, `${v}: never animate every property`);
    assert.doesNotMatch(css, /scale\(0\)(?!\.)/, `${v}: nothing enters from scale(0)`);
    assert.doesNotMatch(css, /\bease-in\b(?!-out)/, `${v}: never ease-in on UI`);
    assert.match(css, /cubic-bezier\(0\.23, 1, 0\.32, 1\)/, `${v}: uses the strong ease-out curve`);
    assert.match(css, /prefers-reduced-motion:\s*reduce/, `${v}: honours prefers-reduced-motion`);
    assert.match(css, /@media \(hover: hover\) and \(pointer: fine\)/, `${v}: gates hover styling`);
    assert.match(css, /:active/, `${v}: pressables have press feedback`);
    assert.match(css, /html\[data-variant="[a-z]+"\]/, `${v}: all rules are scoped to its own variant`);
    // Motion stays inside the UI budget except in documented, reduced-motion paths.
    const durations = [...css.matchAll(/(\d+)ms/g)].map((m) => Number(m[1]));
    const over = durations.filter((d) => d > 500);
    assert.deepEqual(over, [], `${v}: every duration is inside the UI budget (found ${over.join(", ")}ms)`);
  }
});

test("the picker is PICKER.md verbatim", async () => {
  const html = await readProto("index.html");
  const harnessCss = await readProto("harness.css");
  const harnessJs = await readProto("harness.js");

  // markup
  assert.match(html, /<nav class="proto-picker" aria-label="Prototype variants">/);
  assert.match(html, /<span class="proto-picker-highlight" aria-hidden="true"><\/span>/);
  assert.match(html, /<button class="proto-picker-item" data-active aria-current="true">Console<\/button>/);
  assert.match(html, /<span class="proto-picker-divider" aria-hidden="true"><\/span>/);
  assert.match(html, /<button class="proto-picker-item proto-picker-replay" aria-label="Replay animation \(R\)">/);
  // one button per variant
  assert.equal((html.match(/class="proto-picker-item"/g) || []).length, VARIANTS.length);

  // styles: the spec values, not project tokens
  for (const decl of [
    "border-radius: 999px",
    "background: rgba(10, 10, 10, 0.82)",
    "backdrop-filter: blur(12px) saturate(1.4)",
    "transform 250ms cubic-bezier(0.23, 1, 0.32, 1)",
    "width 250ms cubic-bezier(0.23, 1, 0.32, 1)",
    "height: 28px",
    "font-size: 13px",
  ]) {
    assert.ok(harnessCss.includes(decl), `picker style kept verbatim: ${decl}`);
  }
  assert.match(harnessCss, /\.proto-picker\[data-ready\] \.proto-picker-highlight\s*\{\s*transition: none;?\s*\}/);

  // behaviour: keys, persistence, aria, first-paint highlight
  for (const token of ["ArrowRight", "ArrowLeft", "searchParams.set(\"v\"", "aria-current", "data-ready", "isContentEditable"]) {
    assert.ok(harnessJs.includes(token), `picker wiring keeps ${token}`);
  }
  assert.match(harnessJs, /e\.metaKey \|\| e\.ctrlKey \|\| e\.altKey/, "modifier-held keys are ignored");
});

test("the prototype surface is served, and its sheets are the ones on disk", async (t) => {
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
  t.after(() => child.kill("SIGTERM"));
  await waitForServer(base);

  // /proto/ resolves to /proto/index.html, the way Firebase Hosting serves it.
  const page = await fetch(`${base}/proto/`);
  assert.equal(page.status, 200, `GET /proto/ -> ${page.status} ${stderr}`);
  const html = await page.text();
  assert.match(html, /Prototype — Termini dashboard/);
  for (const v of VARIANTS) {
    const sheet = await fetch(`${base}/proto/variants/${v}.css`);
    assert.equal(sheet.status, 200, `GET /proto/variants/${v}.css -> ${sheet.status}`);
    assert.equal(await sheet.text(), await readProto(`variants/${v}.css`), `${v}.css served byte-identical`);
  }
  const harness = await fetch(`${base}/proto/harness.js`);
  assert.equal(harness.status, 200);
  assert.equal(await harness.text(), await readProto("harness.js"), "harness.js served byte-identical");
  // ...and the app modules the page boots are the shipped ones.
  for (const rel of ["js/main.js", "js/ui/chat.js", "js/ui/decisions.js"]) {
    const served = await (await fetch(`${base}/${rel}`)).text();
    assert.equal(served, await readDashboard(rel), `${rel} served byte-identical to the shipped module`);
  }
});
