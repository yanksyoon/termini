/*
 * Prototype harness (task t_4458a089) — the variant picker's wiring.
 *
 * The behaviour contract is PICKER.md's, unchanged:
 *   - number keys 1–N and ←/→ switch; R replays; ignored inside form fields and
 *     with a modifier held;
 *   - exactly one item carries data-active + aria-current, and the highlight
 *     slides to it (250ms strong ease-out; it takes its first position without
 *     animating, hence data-ready after first paint);
 *   - the selection persists in `?v=`;
 *   - switching re-mounts the variant so its entrance animations re-run, and R
 *     re-mounts without switching.
 *
 * HOW THE VARIANTS ARE MOUNTED. The dashboard is one dependency-free
 * static bundle whose modules bind to the ids in the markup at load. Re-creating
 * the markup would boot a second app (second poll timer, second log stream) and
 * would make "no behaviour lost" a story instead of a fact. So the prototype
 * page carries the SAME markup and boots the SAME unmodified modules once, and a
 * variant is a design layer: its stylesheet plus — only where an axis cannot be
 * expressed in CSS — its own additive module. Console, Briefing and Instrument
 * are stylesheets alone. Variant 4 ("Overview") also needs an information
 * architecture, so its module rearranges the shipped nodes into three
 * destinations and puts them back on the way out; it is told to do so by the
 * `proto:variant-change` event fired below. Swap = toggle one stylesheet and one
 * `data-variant` attribute, so every behaviour is present in every variant by
 * construction. What the owner compares is the design, not the feature set —
 * which is what the brief asked for.
 */
const VARIANTS = [
  {
    id: "console",
    name: "Console",
    axis: "density — fleet rail, incident console and instruments on one screen; near-static motion",
    short: "dense operator console",
  },
  {
    id: "briefing",
    name: "Briefing",
    axis: "layout & personality — one calm reading column, soft materials, considered entrances",
    short: "one calm reading column",
  },
  {
    id: "instrument",
    name: "Instrument",
    axis: "personality & motion — light precision, one electric accent, expressive sub-300ms motion",
    short: "light precision + motion",
  },
  {
    id: "hig",
    name: "Overview",
    axis:
      "information architecture & platform convention — three destinations, master–detail records, semantic tokens, light and dark",
    short: "three destinations, HIG",
  },
];

/*
 * The prototype is for judging design, so it has to render something with no
 * controller behind it: with no connection parameters at all it starts in the
 * bundled fixture mode (`?offline=1`), and says so in the caption. Append
 * `apiBase`+`projectId` (or `?offline=0`) to point it at a real controller.
 * This runs before ../js/main.js is evaluated, because main.js resolves its
 * config from location.search at module init.
 */
const params = new URLSearchParams(location.search);
if (!params.has("offline") && !params.has("mock") && !params.has("apiBase") && !params.has("projectId")) {
  params.set("offline", "1");
  history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
}
const FIXTURE = params.get("offline") === "1" || params.get("mock") === "1";

const stage = document.documentElement;
const picker = document.querySelector(".proto-picker");
const caption = document.querySelector(".proto-caption");
const highlight = picker.querySelector(".proto-picker-highlight");
const items = [...picker.querySelectorAll(".proto-picker-item:not(.proto-picker-replay)")];
const replayBtn = picker.querySelector(".proto-picker-replay");
const sheets = [...document.querySelectorAll('link.proto-variant')];

let current = 0;

/** The picker's own highlight measurement — the flip, not the variant, animates. */
function moveHighlight() {
  const el = items[current];
  if (!el) return;
  highlight.style.width = el.offsetWidth + "px";
  highlight.style.transform = `translateX(${el.offsetLeft}px)`;
}

/**
 * Re-mount the variant's entrance. CSS-only variants cannot be re-created, so
 * the harness cycles the `data-enter` hook the variant stylesheets hang their
 * entrance animations on; the double rAF is what makes the browser see the
 * attribute leave and come back (a single class flip would coalesce).
 */
function replay() {
  stage.removeAttribute("data-enter");
  requestAnimationFrame(() => {
    requestAnimationFrame(() => stage.setAttribute("data-enter", ""));
  });
}

function setActive(i, { replayEntrance = true } = {}) {
  if (i < 0 || i >= VARIANTS.length) return;
  const previous = VARIANTS[current];
  current = i;
  const v = VARIANTS[i];

  for (const sheet of sheets) sheet.disabled = sheet.dataset.variant !== v.id;
  stage.setAttribute("data-variant", v.id);

  items.forEach((el, j) => {
    el.toggleAttribute("data-active", j === i);
    if (j === i) el.setAttribute("aria-current", "true");
    else el.removeAttribute("aria-current");
  });

  if (caption) {
    // On a phone the full axis sentence would cover a third of the screen; the
    // short form names the direction and the pill already carries the name.
    const text = window.innerWidth < 640 ? `${v.name} — ${v.short}` : `${v.name} — ${v.axis}`;
    caption.innerHTML =
      `<span class="proto-caption-axis"><b>${v.name}</b> — ${window.innerWidth < 640 ? v.short : v.axis}</span>` +
      (FIXTURE ? `<span class="proto-caption-fixture">bundled fixture data · no controller</span>` : "");
    caption.setAttribute("title", text);
  }
  // The prototype must never be mistaken for the product page: the tab title
  // always says so, so a screenshot cannot be filed as the root view.
  document.title = `Prototype ${i + 1}/${VARIANTS.length} — ${v.name} · Termini dashboard`;

  moveHighlight();

  const url = new URL(location.href);
  url.searchParams.set("v", String(i + 1));
  history.replaceState(null, "", url);

  if (replayEntrance) replay();

  // A variant is a design layer. Three are CSS only; the fourth is an additive
  // module that rearranges the shipped nodes (three destinations, master-detail)
  // and must be told when to mount and to put the page back. Announced after the
  // stylesheet is on, so the module never builds against a half-applied layer.
  window.dispatchEvent(
    new CustomEvent("proto:variant-change", { detail: { variant: v.id, previous: previous ? previous.id : null } }),
  );
}

items.forEach((el, i) => el.addEventListener("click", () => setActive(i)));
replayBtn?.addEventListener("click", () => replay());
window.addEventListener("resize", moveHighlight);

document.addEventListener("keydown", (e) => {
  const target = e.target;
  if (target && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const num = parseInt(e.key, 10);
  if (num >= 1 && num <= VARIANTS.length) setActive(num - 1);
  else if (e.key === "ArrowRight") setActive((current + 1) % VARIANTS.length);
  else if (e.key === "ArrowLeft") setActive((current - 1 + VARIANTS.length) % VARIANTS.length);
  else if (e.key === "r" || e.key === "R") replay();
});

// All three sheets arrive pre-declared (cascade order is fixed); only one is on.
for (const sheet of sheets) sheet.disabled = true;

const requested = parseInt(new URLSearchParams(location.search).get("v"), 10);
setActive(Number.isFinite(requested) && requested >= 1 && requested <= VARIANTS.length ? requested - 1 : 0, {
  replayEntrance: false,
});

// Enable the slide only after first paint, so load doesn't animate.
requestAnimationFrame(() => {
  requestAnimationFrame(() => {
    picker.setAttribute("data-ready", "");
    moveHighlight();
  });
});

// Fonts/caption width can settle after first paint; keep the highlight honest.
if (document.fonts?.ready) document.fonts.ready.then(moveHighlight);
