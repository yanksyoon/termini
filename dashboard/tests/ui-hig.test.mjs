/*
 * Mechanical checks for variant 4 ("Overview", task t_6af4e389).
 *
 * The browser audit (dashboard/evidence/hig/tools/capture.py) measures the LIVE
 * page: targets, type, contrast, axe-style a11y and the keyboard walkthrough.
 * These are the checks that can be made from the repository itself, so a
 * reviewer without a browser still gets a signal:
 *
 *   1. The fourth destination-set is wired into the picker at ?v=4, and the
 *      three earlier variants are untouched.
 *   2. The stylesheet is scoped to its own variant, is dependency-free, and
 *      carries the HIG contract: semantic tokens, both appearances, 44 px
 *      controls, a focus ring, reduced motion, hover gating, no colour
 *      transitions, and durations inside the 200–300 ms budget.
 *   3. The module is the additive design layer only: it moves shipped nodes,
 *      restores them on the way out, and owns no data — no fetch, no polling
 *      of an API, no re-implementation of the decision join.
 *   4. docs/ui-hig.md maps every rule the card lists.
 *
 * Run: node --test dashboard/tests/ui-hig.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const DASHBOARD = join(HERE, "..");
const REPO = join(DASHBOARD, "..");
const read = (rel) => readFile(join(DASHBOARD, rel), "utf8");

const css = () => read("proto/variants/hig.css");
const js = () => read("proto/variants/hig.js");
const strip = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "");

test("variant 4 is the fourth entry everywhere the harness looks", async () => {
  const html = await read("proto/index.html");
  const harness = await read("proto/harness.js");

  assert.match(
    html,
    /class="proto-variant" data-variant="hig" href="variants\/hig\.css" disabled/,
    "the HIG sheet is declared with the other variant sheets",
  );
  assert.match(html, /<script type="module" src="variants\/hig\.js"><\/script>/, "the additive module is loaded");
  assert.equal(
    (html.match(/class="proto-picker-item"/g) || []).length,
    4,
    "the picker offers four variants",
  );
  assert.match(html, /<button class="proto-picker-item">Overview<\/button>/, "variant 4 is named the way the card names it");

  const ids = [...harness.matchAll(/id: "([a-z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(ids, ["console", "briefing", "instrument", "hig"], "the HIG variant is last, so it is ?v=4");
  assert.match(harness, /name: "Overview"/);
  assert.match(harness, /proto:variant-change/, "the module is mounted and unmounted through one event");

  // PROMOTED 2026-10-17 (owner decision): the root IS the HIG surface now. The
  // guard inverts — it must load exactly one design layer, once, and must not
  // pull the picker in with it.
  const shipped = await read("index.html");
  assert.match(shipped, /<html lang="en" data-variant="hig">/, "the root declares the HIG variant");
  assert.equal((shipped.match(/variants\/hig\.css/g) || []).length, 1, "one HIG stylesheet, once");
  assert.equal((shipped.match(/variants\/hig\.js/g) || []).length, 1, "one HIG module, once");
  assert.doesNotMatch(shipped, /proto-picker|harness\.js|proto\/index\.html/, "the root does not embed the picker");
  assert.doesNotMatch(shipped, /variants\/(console|briefing|instrument)\.css/, "no other variant rides along");
});

test("the HIG sheet is scoped, dependency-free and meets the platform contract", async () => {
  const sheet = strip(await css());
  const rules = [...sheet.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({ selector: m[1].trim(), body: m[2].trim() }));

  // Scoped: every non-keyframe selector starts from the variant root.
  const unscoped = rules
    .filter((r) => !r.selector.startsWith("@"))
    .filter((r) => !/^(from|to|\d+%)$/.test(r.selector))
    .filter((r) => !r.selector.split(",").every((s) => s.trim().startsWith("html[data-variant=\"hig\"]")))
    .map((r) => r.selector);
  assert.deepEqual(unscoped, [], "every rule is scoped to this variant");

  assert.doesNotMatch(sheet, /@import|url\(\s*['"]?https?:/, "no external stylesheet, font or image");
  assert.doesNotMatch(sheet, /transition\s*:\s*all\b/, "never animate every property");
  assert.doesNotMatch(sheet, /ease-in\b(?!-out)/, "never ease-in on UI");
  assert.doesNotMatch(sheet, /scale\(0\)(?!\.)/, "nothing enters from scale(0)");

  // Motion budget: 200-300 ms, transform/opacity only.
  const durations = [...sheet.matchAll(/(\d+)ms/g)].map((m) => Number(m[1]));
  assert.ok(durations.length > 5, "the stylesheet states its motion");
  assert.deepEqual(durations.filter((d) => d < 120 || d > 300), [], "every duration is 120-300ms");
  assert.match(sheet, /cubic-bezier\(0\.23, 1, 0\.32, 1\)/, "the strong ease-out curve is used");
  assert.doesNotMatch(sheet, /transition:[^;]*(background|color)/, "colour is never transitioned");
  assert.match(sheet, /prefers-reduced-motion:\s*reduce/, "reduced motion is honoured");
  assert.match(sheet, /@media \(hover: hover\) and \(pointer: fine\)/, "hover styling is gated");
  assert.match(sheet, /:active/, "pressables have press feedback");

  // Semantic tokens: the three system colours plus neutrals, both appearances.
  for (const token of ["--hig-label:", "--hig-label-2:", "--hig-label-3:", "--hig-separator:", "--hig-red:", "--hig-orange:", "--hig-green:", "--hig-bg:"]) {
    assert.ok(sheet.includes(token), `token ${token} is defined`);
  }
  assert.match(sheet, /@media \(prefers-color-scheme: dark\)/, "dark follows the system setting");
  assert.match(sheet, /data-hig-appearance="dark"/, "the explicit override exists");
  assert.match(sheet, /data-hig-appearance="light"/, "the explicit override has both directions");
  assert.match(sheet, /color-scheme: light/, "the UA is told which appearance is in use");

  // 44 px controls: nav, buttons, chips, segmented items, inline links, inputs.
  for (const selector of ["\\.hig-nav-item", "\\.btn", "\\.hig-btn", "\\.chip", "\\.hig-seg-item", "\\.linklike", "input\\[type=\"text\"\\]"]) {
    const rule = rules.find((r) => new RegExp(selector + "[^{]*\\{").test(r.selector + "{") && /min-height:\s*44px/.test(r.body));
    assert.ok(rule, `${selector} declares a 44px target`);
  }
  assert.match(sheet, /:focus-visible/, "every focusable element has a focus ring");
  assert.match(sheet, /text-transform:\s*none/, "shipped ALL-CAPS micro-labels are reset");
});

test("the module moves the shipped nodes and puts them back, and owns no data", async () => {
  const source = strip(await js());

  // Reversible: snapshots of the original children + owned nodes removed again.
  assert.match(source, /snapshots\.set\(parent, \[\.\.\.parent\.childNodes\]\)/, "original child order is captured");
  assert.match(source, /function restore\(\)/, "a restore path exists");
  assert.match(source, /export function unmount\(\)/, "the harness can unmount it");
  assert.match(source, /export function mount\(\)/, "the harness can mount it");
  assert.match(source, /proto:variant-change/, "mount/unmount are driven by the picker");

  // Three destinations, not five.
  assert.match(source, /const DESTINATIONS = \["fleet", "incidents", "settings"\]/);
  assert.match(source, /const LABELS = \{ fleet: "Fleet", incidents: "Incidents", settings: "Settings" \}/);

  // Master-detail + keyboard.
  assert.match(source, /setAttribute\("role", "listbox"\)/);
  assert.match(source, /setAttribute\("role", "option"\)/);
  assert.match(source, /ev\.key === "ArrowDown"/);
  assert.match(source, /ev\.key === "ArrowUp"/);
  assert.match(source, /ev\.key === "Home"/);
  assert.match(source, /ev\.key === "End"/);
  assert.match(source, /ev\.key === "Escape"/);

  // 44 pt / naming obligations the module itself creates.
  assert.match(source, /aria-haspopup": "menu"/);
  assert.match(source, /aria-label": `Actions for \$\{label\}`/);
  assert.match(source, /setAttribute\("aria-label", text\.charAt\(0\)\.toUpperCase\(\) \+ text\.slice\(1\)\)/);

  // Not a second application: no data access of its own.
  assert.doesNotMatch(source, /\bfetch\(/, "the module never fetches");
  assert.doesNotMatch(source, /EventSource|WebSocket/, "the module never opens a stream");
  assert.doesNotMatch(source, /buildDecisionRows|nodeView\(|parseEgressLine/, "it does not re-implement the joins");

  // Approvals are the one interruption, and its clock is labelled honestly.
  assert.match(source, /300_000/);
  assert.match(source, /counted from when this tab first saw the request/);

  // It imports the shipped DOM helper and nothing else.
  const imports = [...source.matchAll(/^import .*from "([^"]+)";$/gm)].map((m) => m[1]);
  assert.deepEqual(imports, ["../../js/ui/dom.js"]);
});

test("docs/ui-hig.md maps every rule in the card", async () => {
  const doc = await readFile(join(REPO, "docs", "ui-hig.md"), "utf8");
  const required = [
    "Three destinations",
    "Master–detail",
    "Fleet landing",
    "Attention",
    "Type",
    "Colour",
    "Both appearances",
    "Targets and input",
    "Destructive actions",
    "States",
    "Motion",
    "Nothing removed",
  ];
  for (const heading of required) {
    assert.ok(doc.includes(heading), `docs/ui-hig.md covers "${heading}"`);
  }
  for (const section of ["Acceptance", "Not followed", "How to re-run"]) {
    assert.ok(doc.includes(section), `docs/ui-hig.md has a "${section}" section`);
  }
  assert.match(doc, /4\.5:1/, "the contrast method and thresholds are stated");
});
