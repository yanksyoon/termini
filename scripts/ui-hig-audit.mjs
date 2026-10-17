#!/usr/bin/env node
/*
 * Apple-HIG conformance audit for the Termini dashboard (repeatable gate).
 *
 * Measures a loaded dashboard URL (any variant, e.g. the shipped bundle or a
 * candidate redesign) and writes machine-readable evidence: a JSON report, full
 * page screenshots in dark + light, and a markdown table fragment. It is the
 * instrument behind docs/ui-hig-audit.md — every number in that document comes
 * from a run of this script, so the shipped baseline and the candidate redesign
 * (/proto/?v=4) are judged against the same measurements.
 *
 * Zero npm dependencies: Node >= 22 (built-in WebSocket) + a headless
 * Chromium/Chrome, driven over the DevTools Protocol.
 *
 * Usage:
 *   node scripts/ui-hig-audit.mjs <url> <label> <outdir> [--settle-ms N]
 *
 *   label  = slug for the report (e.g. "shipped" or "shipped-offline")
 *   outdir = directory for report.json / table.md / screenshots
 *
 * Example (shipped, contract data served by the mock controller):
 *   python3 dashboard/mock/server.py --port 8099 &
 *   node scripts/ui-hig-audit.mjs http://127.0.0.1:8099/ shipped audit/shipped
 *   node scripts/ui-hig-audit.mjs "http://127.0.0.1:8099/?offline=1" shipped-offline audit/offline
 *
 * CHROME=/path/to/chrome overrides browser discovery.
 */
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const URL = process.argv[2];
const LABEL = process.argv[3];
const OUT = resolve(process.argv[4] || `audit-${Date.now()}`);
const SETTLE_MS = Number(process.argv[5] || 6000);

if (!URL || !LABEL) {
  console.error("usage: node scripts/ui-hig-audit.mjs <url> <label> <outdir> [settleMs]");
  process.exit(2);
}

function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  for (const name of ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]) {
    const r = spawnSync("sh", ["-c", `command -v ${name}`], { encoding: "utf8" });
    if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  }
  const hermes = `${process.env.HOME}/.hermes/tools`;
  const r = spawnSync("sh", ["-c", `ls -d ${hermes}/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1`], { encoding: "utf8" });
  if (r.stdout && r.stdout.trim()) return r.stdout.trim();
  console.error("no chrome/chromium found; set CHROME=/path/to/chrome");
  process.exit(2);
}

const CHROME = findChrome();
const PORT = Number(process.env.CDP_PORT || 9444);
mkdirSync(OUT, { recursive: true });

const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--window-size=1440,900",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${join(OUT, "chrome-profile")}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

const killChrome = () => {
  try {
    chrome.kill("SIGKILL");
  } catch {
    /* ignore */
  }
};
process.on("exit", killChrome);
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(sig, () => {
    killChrome();
    process.exit(128 + (sig === "SIGINT" ? 2 : sig === "SIGTERM" ? 15 : 1));
  });
}

let target = null;
for (let i = 0; i < 80 && !target; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    target = list.find((t) => t.type === "page");
  } catch {
    /* not up yet */
  }
  if (!target) await sleep(250);
}
if (!target) {
  console.error("FAIL: chrome devtools never came up");
  killChrome();
  process.exit(2);
}

const ws = new WebSocket(target.webSocketDebuggerUrl);
let nextId = 1;
const pending = new Map();
const send = (method, params = {}) =>
  new Promise((resolveP, reject) => {
    const id = nextId++;
    pending.set(id, { resolve: resolveP, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
ws.addEventListener("message", (m) => {
  let msg;
  try {
    msg = JSON.parse(m.data);
  } catch {
    return;
  }
  if (msg.id && pending.has(msg.id)) {
    const { resolve: resolveP, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolveP(msg.result);
  }
});

await new Promise((resolveP, reject) => {
  ws.addEventListener("open", resolveP, { once: true });
  ws.addEventListener("error", reject, { once: true });
});
await send("Runtime.enable");
await send("Page.enable");
await send("Log.enable");
await send("Accessibility.enable");

const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || "eval failed");
  return r.result ? r.result.value : undefined;
};

await send("Page.navigate", { url: URL });
const log = (m) => process.stderr.write(`[audit:${LABEL}] ${m}\n`);

// ---- wait for first paint signal, then settle ---------------------------
let painted = false;
for (let i = 0; i < 120 && !painted; i++) {
  await sleep(500);
  try {
    painted = await evaluate(
      "Boolean(document.querySelector('.node-card, .skeleton, .empty, .mynode-empty, .chart-canvas, .decision-row, .log-row'))",
    );
  } catch {
    /* not ready */
  }
}
log("painted");
await sleep(SETTLE_MS);
log("settled");

// ---- dark / light detection ---------------------------------------------
async function screenshot(name, scheme) {
  await send("Emulation.setEmulatedMedia", {
    media: "screen",
    features: [{ name: "prefers-color-scheme", value: scheme }],
  });
  await sleep(200);
  // Full-page capture: expand the viewport to the document size instead of
  // `captureBeyondViewport`, which can hang on a page with a live stream +
  // continuous canvas redraw. The metrics override is cleared immediately after.
  const dims = await evaluate(
    "({ w: Math.max(1, document.documentElement.scrollWidth), h: Math.max(1, document.documentElement.scrollHeight) })",
  );
  await send("Emulation.setDeviceMetricsOverride", { width: dims.w, height: dims.h, deviceScaleFactor: 1, mobile: false });
  await sleep(150);
  let img;
  try {
    img = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  } finally {
    await send("Emulation.clearDeviceMetricsOverride");
  }
  writeFileSync(join(OUT, name), Buffer.from(img.data, "base64"));
  return join(OUT, name);
}

const darkPng = await screenshot("screenshot-dark.png", "dark");
log("shot-dark");
const lightPng = await screenshot("screenshot-light.png", "light");
log("shot-light");

// Read the effective appearance in each scheme: if the palette does not change
// with the emulated scheme, there is no light variant (evidence, not opinion).
const schemeProbe = await evaluate(`(() => {
  const read = () => {
    const b = getComputedStyle(document.body);
    const root = getComputedStyle(document.documentElement);
    const d = document.documentElement.style.colorScheme;
    return {
      bodyBg: b.backgroundColor,
      bodyFg: b.color,
      rootColorScheme: root.colorScheme || null,
      styleColorScheme: d || null,
      darkMatches: matchMedia('(prefers-color-scheme: dark)').matches,
      lightMatches: matchMedia('(prefers-color-scheme: light)').matches,
      prefersSchemePresent: Array.from(document.styleSheets).some((ss) => {
        try { return [...ss.cssRules].some((r) => r.media && /prefers-color-scheme|color-scheme/i.test(r.media.mediaText)); }
        catch (e) { return false; }
      }),
    };
  };
  return { dark: read() };
})()`);
await send("Emulation.setEmulatedMedia", { media: "screen", features: [{ name: "prefers-color-scheme", value: "light" }] });
await sleep(200);
const lightProbe = await evaluate(`(() => {
  const b = getComputedStyle(document.body);
  return { bodyBg: b.backgroundColor, bodyFg: b.color, darkMatches: matchMedia('(prefers-color-scheme: dark)').matches, lightMatches: matchMedia('(prefers-color-scheme: light)').matches };
})()`);
// reset to dark (the app's actual scheme) for the DOM measurements
await send("Emulation.setEmulatedMedia", { media: "screen", features: [{ name: "prefers-color-scheme", value: "dark" }] });

// ---- in-page measurement (the big one) -----------------------------------
const MEASURE = `(() => {
  // ---- helpers
  const parseColor = (s) => {
    const m = (s || '').match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x.trim()));
    if (p.length < 3) return null;
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const hex = (c) => '#' + [c.r, c.g, c.b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
  const luminance = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const contrast = (fg, bg) => {
    const l1 = luminance(fg), l2 = luminance(bg);
    const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  };
  const effectiveBg = (el) => {
    // Collect painted backgrounds from el upward; composite semi-transparent
    // layers (source-over) over the first opaque layer found below, so an
    // rgba(69,209,122,.13) badge does not get judged as a solid saturated fill.
    const layers = [];
    let n = el;
    while (n && n.nodeType === 1) {
      const cs = getComputedStyle(n);
      let c = parseColor(cs.backgroundColor);
      if (!c) {
        const gm = (cs.backgroundImage || '').match(/rgba?\\([^)]+\\)/);
        if (gm) c = parseColor(gm[0]);
      }
      if (c && c.a > 0) {
        layers.push(c);
        if (c.a > 0.99) break;
      }
      n = n.parentElement;
    }
    let acc = { r: 11, g: 14, b: 20, a: 1 }; // --bg fallback
    for (let i = layers.length - 1; i >= 0; i--) {
      const s = layers[i];
      const outA = s.a + acc.a * (1 - s.a);
      const blend = (sv, dv) => (outA === 0 ? 0 : (sv * s.a + dv * acc.a * (1 - s.a)) / outA);
      acc = { r: blend(s.r, acc.r), g: blend(s.g, acc.g), b: blend(s.b, acc.b), a: outA };
    }
    return acc;
  };
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const ownText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    return t.trim();
  };
  const round1 = (n) => Math.round(n * 10) / 10;

  const doc = document.documentElement;
  const out = {};

  // ---- viewport + document size
  out.viewport = { w: innerWidth, h: innerHeight, dpr: devicePixelRatio };
  out.docSize = {
    scrollHeight: doc.scrollHeight, scrollWidth: doc.scrollWidth,
    clientWidth: doc.clientWidth, clientHeight: doc.clientHeight,
  };
  out.horizontalOverflow = doc.scrollWidth > doc.clientWidth;
  out.pageHeightVsViewport = round1(doc.scrollHeight / innerHeight);
  out.meta = {
    lang: doc.lang || null,
    title: document.title,
    viewport: (document.querySelector('meta[name=viewport]') || {}).content || null,
    prefersColorSchemeRulePresent: schemeProbe.prefersSchemePresent,
  };

  // ---- interactive controls
  const INTERACTIVE = 'button, a[href], input, select, textarea, [tabindex], summary, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"]';
  const allControls = Array.from(document.querySelectorAll(INTERACTIVE));
  const controls = allControls.filter(isVisible);

  const panelName = (el) => {
    const panel = el.closest('.panel, .topbar, .dialog, .toasts, .banner-host');
    if (!panel) return 'other';
    const h = panel.querySelector('h2, h3');
    const labelled = panel.getAttribute('aria-labelledby');
    if (labelled) {
      const l = document.getElementById(labelled);
      if (l) return l.textContent.trim();
    }
    if (h) return h.textContent.trim();
    if (panel.classList.contains('topbar')) return 'topbar';
    if (panel.classList.contains('dialog')) return 'dialog';
    return panel.id || panel.className;
  };
  const perPanel = {};
  for (const el of controls) {
    const k = panelName(el);
    perPanel[k] = (perPanel[k] || 0) + 1;
  }
  const byTag = {};
  for (const el of controls) byTag[el.tagName.toLowerCase()] = (byTag[el.tagName.toLowerCase()] || 0) + 1;
  out.controls = {
    totalInDom: allControls.length,
    visible: controls.length,
    byTag,
    perPanel: Object.fromEntries(Object.entries(perPanel).sort((a, b) => b[1] - a[1])),
  };

  // ---- words
  const bodyText = document.body.innerText || '';
  out.words = { total: bodyText.trim().split(/\\s+/).filter(Boolean).length };

  // ---- target sizes (44px Apple minimum)
  const sizes = controls.map((el) => {
    const r = el.getBoundingClientRect();
    return { tag: el.tagName.toLowerCase(), text: (el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 24), w: Math.round(r.width), h: Math.round(r.height) };
  });
  const under44 = sizes.filter((s) => s.w < 44 || s.h < 44);
  const heights = sizes.map((s) => s.h).sort((a, b) => a - b);
  const widths = sizes.map((s) => s.w).sort((a, b) => a - b);
  const med = (arr) => (arr.length ? arr[Math.floor(arr.length / 2)] : null);
  out.targetSizes = {
    count: sizes.length,
    under44: under44.length,
    under44Pct: round1(100 * under44.length / (sizes.length || 1)),
    medianHeight: med(heights),
    medianWidth: med(widths),
    minHeight: heights[0] || null,
    smallest: under44.slice(0, 20),
  };

  // ---- type scale (own-text elements only, to avoid double counting)
  const fontSizes = [];
  const fontWeights = [];
  const sizesCount = {};
  const weightsCount = {};
  let below11 = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!ownText(el)) continue;
    const cs = getComputedStyle(el);
    const px = parseFloat(cs.fontSize);
    const wt = cs.fontWeight;
    if (!Number.isFinite(px)) continue;
    fontSizes.push(px);
    fontWeights.push(wt);
    sizesCount[px] = (sizesCount[px] || 0) + 1;
    weightsCount[wt] = (weightsCount[wt] || 0) + 1;
    if (px < 11) below11++;
  }
  const uniq = (arr) => Array.from(new Set(arr));
  const sizesSorted = fontSizes.slice().sort((a, b) => a - b);
  const distinct = uniq(fontSizes).sort((a, b) => a - b).map((x) => round1(x));
  out.typeScale = {
    textElements: fontSizes.length,
    minPx: round1(sizesSorted[0] || 0),
    maxPx: round1(sizesSorted[sizesSorted.length - 1] || 0),
    medianPx: round1(sizesSorted.length ? sizesSorted[Math.floor(sizesSorted.length / 2)] : 0),
    distinctSizes: distinct,
    distinctCount: distinct.length,
    weights: uniq(fontWeights).sort(),
    below11pxCount: below11,
  };

  // ---- distinct colours
  const textColors = {};
  const bgColors = {};
  for (const el of document.querySelectorAll('body *')) {
    if (!ownText(el)) continue;
    const cs = getComputedStyle(el);
    const fgc = parseColor(cs.color);
    if (fgc) textColors[hex(fgc)] = (textColors[hex(fgc)] || 0) + 1;
    const bgc = parseColor(cs.backgroundColor);
    if (bgc && bgc.a > 0.05) bgColors[hex(bgc)] = (bgColors[hex(bgc)] || 0) + 1;
  }
  out.colors = {
    distinctText: Object.keys(textColors).length,
    textColors: Object.fromEntries(Object.entries(textColors).sort((a, b) => b[1] - a[1])),
    distinctBackgrounds: Object.keys(bgColors).length,
    backgrounds: Object.fromEntries(Object.entries(bgColors).sort((a, b) => b[1] - a[1])),
  };

  // ---- semantic state colour system (is ok/warn/err one consistent system?)
  const sem = {};
  for (const sel of [
    '.badge.status-online', '.badge.status-degraded', '.badge.status-offline',
    '.flag.ok', '.flag.warn', '.flag.bad',
    '.dot-ok', '.dot-warn', '.dot-err',
    '.badge.sev-info', '.badge.sev-warn', '.badge.sev-error',
    '.btn-warn', '.btn-ok', '.btn-primary',
    '.result.ok', '.result.bad',
    '.incident-card.inc-model', '.incident-card.inc-fallback', '.incident-card.inc-bug',
  ]) {
    const el = document.querySelector(sel);
    if (!el) { sem[sel] = null; continue; }
    const cs = getComputedStyle(el);
    const c = parseColor(cs.color);
    sem[sel] = { color: c ? hex(c) : null, border: parseColor(cs.borderColor) ? hex(parseColor(cs.borderColor)) : null, background: parseColor(cs.backgroundColor) && parseColor(cs.backgroundColor).a > 0.05 ? hex(parseColor(cs.backgroundColor)) : (parseColor(cs.backgroundImage) ? 'gradient' : 'transparent') };
  }
  out.semantic = sem;

  // ---- contrast (WCAG 2.x relative luminance, computed styles; transparent
  //       backgrounds resolved by walking up to the nearest painted ancestor)
  const bodyCs = getComputedStyle(document.body);
  const bodyFg = parseColor(bodyCs.color);
  const bodyBg = effectiveBg(document.body);
  out.contrast = {
    method: 'WCAG 2.x relative luminance on getComputedStyle; transparent backgrounds resolved to nearest painted ancestor (gradients sampled at first colour stop)',
    body: { fg: bodyFg ? hex(bodyFg) : null, bg: hex(bodyBg), ratio: round1(contrast(bodyFg || { r: 0, g: 0, b: 0 }, bodyBg)) },
  };
  const badges = [];
  for (const sel of [
    '.badge.status-online', '.badge.status-degraded', '.badge.status-offline',
    '.badge.sev-info', '.badge.sev-warn', '.badge.sev-error',
    '.badge.src-model', '.badge.src-fallback', '.badge.src-det',
    '.badge.inc-model', '.badge.inc-fallback', '.badge.inc-bug',
    '.badge.exec-ok', '.badge.exec-err',
    '.rem-status.succeeded', '.rem-status.pending_approval', '.rem-status.failed',
    '.flag.ok', '.flag.warn', '.flag.bad',
    '.pill', '.chip', '.chip.on', '.btn', '.btn-warn', '.btn-ok', '.btn-primary',
  ]) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const cs = getComputedStyle(el);
    const fg = parseColor(cs.color);
    const bg = effectiveBg(el);
    if (!fg) continue;
    badges.push({ selector: sel, text: (el.textContent || '').trim().slice(0, 24), fg: hex(fg), bg: hex(bg), ratio: round1(contrast(fg, bg)) });
  }
  out.contrast.badges = badges;
  out.contrast.badgesBelow3 = badges.filter((b) => b.ratio < 3);
  out.contrast.badgesBelow45 = badges.filter((b) => b.ratio < 4.5);

  // ---- destructive actions
  const destructive = [];
  for (const b of document.querySelectorAll('button')) {
    const t = (b.textContent || '').trim();
    if (/restart|reboot|drain|destroy|delete|remove|deny|forget/i.test(t) && isVisible(b)) {
      const cs = getComputedStyle(b);
      const c = parseColor(cs.color);
      const row = b.closest('.btn-row');
      const siblings = row ? Array.from(row.querySelectorAll('button')).map((x) => (x.textContent || '').trim()).filter(Boolean) : [];
      destructive.push({ text: t, cls: b.className, color: c ? hex(c) : null, border: parseColor(cs.borderColor) ? hex(parseColor(cs.borderColor)) : null, background: parseColor(cs.backgroundColor) && parseColor(cs.backgroundColor).a > 0.05 ? hex(parseColor(cs.backgroundColor)) : 'transparent', siblings });
    }
  }
  out.destructive = destructive;

  // ---- empty / loading / degraded / offline states
  out.states = {
    emptyTexts: Array.from(document.querySelectorAll('.empty')).filter(isVisible).map((e) => e.textContent.trim()),
    skeletons: document.querySelectorAll('.skeleton').length,
    banner: (document.querySelector('#banner-host .banner') || {}).textContent || null,
    degradedBadges: document.querySelectorAll('.badge.status-degraded').length,
    offlineBadges: document.querySelectorAll('.badge.status-offline').length,
    offlineCards: document.querySelectorAll('.node-card.status-offline').length,
    nodeCards: document.querySelectorAll('.node-card').length,
    decisionRows: document.querySelectorAll('.decision-row').length,
    incidentCards: document.querySelectorAll('.incident-card').length,
    logRows: document.querySelectorAll('.log-row').length,
    chartCanvases: document.querySelectorAll('.chart-canvas').length,
  };

  // ---- focus affordances in CSS
  let focusRule = null;
  try {
    for (const ss of document.styleSheets) {
      for (const r of ss.cssRules) {
        if (r.selectorText && /:focus|:focus-visible/.test(r.selectorText)) focusRule = r.selectorText;
      }
    }
  } catch (e) { /* cross-origin */ }
  out.focusCss = { anyFocusVisibleRule: !!focusRule, selector: focusRule };

  return out;
})()`;

// --------------------------------------------------------------------------
// inject schemeProbe into page context for the measurement function
const schemeProbeJson = JSON.stringify(schemeProbe.dark.prefersSchemePresent);
await evaluate(`window.__prefersSchemePresent = ${schemeProbeJson};`);
const measureJs = MEASURE.replace("schemeProbe.prefersSchemePresent", "window.__prefersSchemePresent");
const report = await evaluate(measureJs);
log("measured");
report.url = URL;
report.label = LABEL;
report.scheme = { dark: schemeProbe.dark, light: lightProbe };
report.screenshots = { dark: darkPng, light: lightPng };
// "follows prefers-color-scheme" = the palette actually CHANGES when the
// system scheme flips. A dark-only app (shipped) or a fixed-light app
// (Instrument) both report false here; the doc distinguishes them via
// scheme.dark.bodyBg / scheme.light.bodyBg.
report.respondsToScheme = schemeProbe.dark.bodyBg !== lightProbe.bodyBg;
report.lightVariantPresent = report.respondsToScheme;

// ---- keyboard: tab order, arrow nav, escape -------------------------------
const FOCUS_DESC = `(() => {
  const a = document.activeElement;
  if (!a || a === document.body || a === document.documentElement) return { lost: true };
  const r = a.getBoundingClientRect();
  const cs = getComputedStyle(a);
  return {
    tag: a.tagName.toLowerCase(), id: a.id || null,
    cls: (typeof a.className === 'string') ? a.className : null,
    text: (a.textContent || '').trim().slice(0, 32),
    w: Math.round(r.width), h: Math.round(r.height),
    visible: r.width > 0 && r.height > 0 && r.bottom >= 0 && r.right >= 0 && r.top <= innerHeight && r.left <= innerWidth,
    outline: cs.outlineStyle + '/' + cs.outlineWidth,
  };
})()`;

const key = async (k, code, vk) => {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
};

await evaluate("document.activeElement && document.activeElement.blur ? document.activeElement.blur() : 0; document.body.focus();");
const tabSeq = [];
const seen = new Set();
let lostCount = 0;
let invisibleCount = 0;
for (let i = 0; i < 60; i++) {
  await key("Tab", "Tab", 9);
  const d = await evaluate(FOCUS_DESC);
  if (d.lost) { lostCount++; tabSeq.push({ step: i, lost: true }); continue; }
  if (!d.visible) invisibleCount++;
  const sig = d.tag + "|" + (d.id || "") + "|" + d.text;
  if (!seen.has(sig)) { seen.add(sig); tabSeq.push({ step: i, ...d }); }
}
log("tabbed");
report.keyboard = {
  tab: { uniqueTargets: tabSeq.length, lostFocusEvents: lostCount, invisibleFocusEvents: invisibleCount, sequence: tabSeq },
};

// arrow navigation inside the incident list (there is no keydown handler)
await evaluate("const v = document.getElementById('decision-viewport'); if (v) { v.focus(); }");
const beforeArrow = await evaluate(FOCUS_DESC);
await key("ArrowDown", "ArrowDown", 40);
const afterArrow = await evaluate(FOCUS_DESC);
report.keyboard.arrowNav = {
  before: beforeArrow,
  after: afterArrow,
  movedFocus: JSON.stringify(beforeArrow) !== JSON.stringify(afterArrow),
  note: "no keydown handler for ArrowDown/ArrowUp exists in the shipped bundle (grep), so the list is a plain tabbable scroll region",
};

// Escape closes the native <dialog> (form method=dialog)
const escResult = {};
await evaluate("document.getElementById('btn-settings').click();");
await sleep(150);
escResult.opened = await evaluate("document.getElementById('settings-dialog').open");
await key("Escape", "Escape", 27);
await sleep(150);
escResult.closedByEscape = !(await evaluate("document.getElementById('settings-dialog').open"));
report.keyboard.escape = escResult;

// ---- accessibility tree: nameless interactive controls --------------------
let axRoot = null;
try {
  axRoot = await send("Accessibility.getFullAXTree");
} catch (e) {
  axRoot = { nodes: [] };
}
log("axtree");
const INTERACTIVE_ROLES = new Set([
  "button", "link", "textbox", "searchbox", "combobox", "listbox", "checkbox",
  "radio", "switch", "slider", "tab", "menuitem", "menu", "spinbutton", "option",
  "gridcell", "cell", "listitem", "treeitem", "textField", "comboBox", "checkBox",
  "radioButton", "switch", "toggleButton", "disclosureTriangle",
]);
const nameless = [];
const focusableNameless = [];
for (const n of axRoot.nodes || []) {
  const role = (n.role && n.role.value) || "";
  const props = {};
  for (const p of n.properties || []) props[p.name] = p.value;
  const name = (n.name && n.name.value) || "";
  const isInteractive = INTERACTIVE_ROLES.has(role) || props.focusable === true;
  if (!isInteractive) continue;
  const hasName = Boolean(name && name.trim());
  if (!hasName) {
    (props.focusable === true ? focusableNameless : nameless).push({ role, name: name || null });
  }
}
report.accessibility = {
  method: "Accessibility.getFullAXTree (the accessibility tree, not markup)",
  totalNodes: (axRoot.nodes || []).length,
  interactiveWithoutName: nameless.length,
  focusableWithoutName: focusableNameless.length,
  focusableNamelessList: focusableNameless.slice(0, 20),
};

// ---- write outputs --------------------------------------------------------
writeFileSync(join(OUT, "report.json"), JSON.stringify(report, null, 2));

// mechanical verdicts (thresholds stated; the doc maps these to HIG rules)
const verdicts = [
  ["target size >= 44px (count of visible interactive controls under 44px)", report.targetSizes.under44, "pass if 0"],
  ["min text size >= 11px", report.typeScale.minPx, "pass if >= 11"],
  ["body text contrast >= 4.5:1", report.contrast.body.ratio, "pass if >= 4.5"],
  ["all badge/control contrast >= 3:1", report.contrast.badgesBelow3.length, "pass if 0"],
  ["light variant follows prefers-color-scheme", report.lightVariantPresent, "pass if true"],
  ["icon-only controls without accessible name", report.accessibility.focusableWithoutName, "pass if 0"],
];

const lines = [];
lines.push("| # | measurement | value | pass? |");
lines.push("|---|-------------|-------|-------|");
verdicts.forEach(([name, value, note], i) => {
  let pass = "—";
  if (note.startsWith("pass if 0")) pass = value === 0 ? "PASS" : "FAIL";
  else if (note.startsWith("pass if >= 11")) pass = value >= 11 ? "PASS" : "FAIL";
  else if (note.startsWith("pass if >= 4.5")) pass = value >= 4.5 ? "PASS" : "FAIL";
  else if (note.startsWith("pass if true")) pass = value === true ? "PASS" : "FAIL";
  lines.push(`| ${i + 1} | ${name} | ${JSON.stringify(value)} | ${pass} |`);
});
writeFileSync(join(OUT, "table.md"), lines.join("\n") + "\n");

console.log(JSON.stringify({ label: LABEL, out: OUT, key: {
  controls: report.controls,
  targetSizes: report.targetSizes,
  typeScale: report.typeScale,
  words: report.words,
  pageHeightVsViewport: report.pageHeightVsViewport,
  horizontalOverflow: report.horizontalOverflow,
  bodyContrast: report.contrast.body,
  badgesBelow3: report.contrast.badgesBelow3.length,
  lightVariantPresent: report.lightVariantPresent,
  focusableNameless: report.accessibility.focusableWithoutName,
  tab: { uniqueTargets: report.keyboard.tab.uniqueTargets, lost: report.keyboard.tab.lostFocusEvents, invisible: report.keyboard.tab.invisibleFocusEvents },
  arrowMovedFocus: report.keyboard.arrowNav.movedFocus,
  escapeCloses: report.keyboard.escape.closedByEscape,
  states: report.states,
  destructive: report.destructive.map((d) => d.text),
}}, null, 2));

ws.close();
killChrome();
process.exit(0);
