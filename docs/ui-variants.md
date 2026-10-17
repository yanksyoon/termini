# Dashboard UI — three directions, served behind a picker

- Owner: `frontend` · task `t_4458a089` · 2026-10-17
- Surface: <https://termini-510609.web.app/proto/> (picker; `?v=1|2|3`)
- Method: [emilkowalski/skills](https://github.com/emilkowalski/skills) at commit
  **`2d228630ba1446365f614a108cb8252f1feda202`** (2026-10-11)
- Root dashboard: **unchanged** by this PR. The winner is promoted separately, after the
  owner picks.

## 1. How to look at it

Open <https://termini-510609.web.app/proto/> and flip:

| key | does |
| --- | --- |
| `1` `2` `3` | switch variant (also `←` / `→`, or click the pill) |
| `R` | replay the variant's entrance animation |
| `?v=N` | deep link to a variant; the picker keeps it in the URL as you flip |

The prototype starts in the bundled fixture (`?offline=1`) when it is opened without
connection parameters, so it renders with no controller behind it; the caption under the
pill says so. Point it at a real controller with `?apiBase=…&projectId=…` (the credential
still comes from the Settings dialog — never from a URL).

The picker is the `prototype` skill's `PICKER.md` copied verbatim: dark floating pill,
bottom-centre, sliding highlight, `data-ready` after first paint so the load does not
animate. It is chrome, not a contestant, and no variant can restyle it — `harness.css`
loads after the variant sheets and every variant rule is scoped to its own
`html[data-variant="…"]`.

## 2. How the prototype is mounted (and why "nothing lost" is structural)

The dashboard is a dependency-free static bundle whose modules bind to ids in the markup
at load, then poll and stream. Re-creating that markup per variant would boot a second app
per switch (second poll timer, second SSE stream) and would turn "no behaviour lost" into a
claim about copy-paste.

So `dashboard/proto/index.html` carries **the shipped page's markup, id for id**, and boots
the **shipped modules unmodified** (`../js/main.js` and its imports). A variant is a
**design layer**: one stylesheet, plus the harness's `data-variant` attribute. Switching
swaps the sheet — no re-boot, no second stream, no state to migrate.

Consequences, stated plainly:

- Every behaviour in the shipped dashboard is present in every variant *by construction*,
  because it is the same running app. The test suite proves the markup parity mechanically
  (`dashboard/tests/proto-variants.test.mjs`), so a control cannot quietly disappear from a
  variant.
- A variant may re-order, re-weight, re-type and re-colour. It may not remove: the test
  fails if any variant stylesheet sets `display: none` on a panel, a control, a decision
  surface, the log, the chat, the banner, badges or the charts.
- The prototype layer may not modify shipped modules, so two things are honoured rather
  than changed: the canvas plot renderer's light-on-dark ink (see §6) and the exact
  behaviour of the polls, filters and disclosures.

The only additive chrome beyond the picker is the caption above the pill, which names the
axis being looked at (and the fixture state) — without it, three pills named "Console",
"Briefing" and "Instrument" do not tell the owner what each direction is *for*.

## 3. The three directions

Each one is a direction I would defend shipping on its own. They are not three tints: they
differ in information architecture, density, type, material, palette and motion budget.

| # | Variant | Axis | When it is the right choice | What it costs |
| --- | --- | --- | --- | --- |
| 1 | **Console** | density | The dashboard lives on a second monitor, all day | Reads as instrumentation, not as a welcome; 12.5px type |
| 2 | **Briefing** | layout & personality | You check in for thirty seconds and want a calm answer | Fewer things on screen; you scroll to act |
| 3 | **Instrument** | personality & motion | You want the tool to feel made, and you read it in daylight | A light dashboard is loud across a room; motion is real |

### 3.1 Console — density

**Axis:** maximum signal per screen. Fleet rail on the left, the incident console taking the
remaining width, instruments beneath the fleet. Everything on one screen at 1240px+.

- **Changes:** 12.5px base type; 10px uppercase monospace section labels instead of panel
  titles; 2–3px radii; hairline rules; no shadows; node cards become dense blocks with
  inline metric figures; the nested incident card is flattened into its decision row; the
  decision viewport loses its own scroll (the page is the scroll); the raw log sits
  full-width at the bottom; charts are 118px tall in a single column.
- **Keeps:** every panel, filter, control, disclosure and honesty surface; the log stays a
  collapsed `<details>`; the thinking log stays a `<details>` with its alerts *outside* it;
  provenance keeps its colour rail; the enrolment panel keeps all of its first-run steps —
  it is clamped to 240px of scroll so it cannot set the height of the row.
- **Motion:** near-static, by decision. No entrance, no row animation — an operator console
  is watched for hours, and at that frequency motion is latency. Press feedback (100ms
  transform) and colour-only hover, gated to a fine pointer. `R` is a deliberate no-op here.
- **For:** the person who watches the fleet rather than visits it.

### 3.2 Briefing — layout & personality

**Axis:** one calm reading column. 1040px, centred; large type; real leading; panels lifted
by a semi-transparent shadow instead of outlined by a border.

- **Changes:** single column in reading order (state → what the operator did → instruments
  → levers → raw log → enrolment) via `order`, without touching DOM order; 16.5px/1.55 body
  type, 21px section headings; 18px radii; soft material surfaces (translucent shadow +
  1px inset highlight); node cards two-up with 21px figures; charts two-up at 168px;
  incident cards flattened inside their decision row; the topbar is a translucent material
  the page scrolls under.
- **Keeps:** all of it — including the explanatory paragraph that says what a decision card
  contains, and every honesty surface (it is re-typed, never softened).
- **Motion:** considered entrances only — 8px rise + fade, 240ms strong ease-out, 40ms
  stagger across the six panels, once per mount. Press feedback 120ms. Nothing continuous.
- **For:** the owner opening the URL for thirty seconds, or reading on a laptop in a bright
  room.

### 3.3 Instrument — personality & motion

**Axis:** a light, precise instrument. Paper-white surfaces, ink type, hairline rules, one
electric accent that only ever marks state or action; plots stay dark on purpose (§6).

- **Changes:** light theme (the only one of the three); two panes — the incident console
  reads on the left, the instruments answer on the right; 14px type with monospace
  micro-labels; 8px radii; a 2px status rail on node cards and decision rows; buttons as
  white/flat with an accent primary; the whole shipped colour vocabulary re-stated for a
  light surface (§"light ink" in `variants/instrument.css`) so no badge, chip, trace alert
  or chat state goes invisible.
- **Keeps:** every panel and every honesty surface, and keeps the *distinction* between a
  model decision, a controller-generated fallback and an unrecorded one — the amber rail,
  the dashed "no record" line and the demo label are all still there, in light inks.
- **Motion:** expressive but inside the budget — panels rise 6px at 200ms with a 30ms
  stagger, plots wipe in at 260ms (`clip-path`), a newly arrived decision fades in at 180ms
  (opacity only: a list that polls every 3s must never move under the cursor), node cards
  lift 1px on hover behind `(hover: hover) and (pointer: fine)`, press feedback at 120ms.
- **For:** someone who wants the tool to feel made, and reads in daylight.

## 4. Behaviour checklist (per variant)

Same app, same markup, same modules — so every row is true in all three. The tests below
the table are what makes this checkable rather than asserted.

| Behaviour | Console | Briefing | Instrument |
| --- | --- | --- | --- |
| Node health cards (status, health flags, services, processes, seen/uptime) | ✅ | ✅ | ✅ |
| Per-node Restart / Drain / Charts / Ask AI | ✅ | ✅ | ✅ |
| Resource-utilisation charts + node selector + window buttons + demo/all toggles | ✅ | ✅ | ✅ |
| Chart annotations, fault frames, sample cards, absent-not-zero rows | ✅ | ✅ | ✅ |
| Live AI-operator log: severity/actor/node filters, pause-scroll, clear, count | ✅ | ✅ | ✅ |
| Incident view: incident card (title / explanation / evidence / decided / executed+audit) | ✅ | ✅ | ✅ |
| Thinking log disclosure incl. alerts rendered outside it, stays open across polls | ✅ | ✅ | ✅ |
| Manual intervention: validate intent → apply plan; pending remediation approve/deny | ✅ | ✅ | ✅ |
| Web terminal link, Settings dialog, connection pills, clock | ✅ | ✅ | ✅ |
| AI chat + per-element Ask AI seeding, context chip, citations, executed[] | ✅ | ✅ | ✅ |
| Offline mode (`?offline=1`) with fixture labels | ✅ | ✅ | ✅ |
| Degraded-backend banner, "record absent, not zero", trace provenance, synthetic labels | ✅ | ✅ | ✅ |

What is verified, and how:

- `cd dashboard && npm test` → **108 tests, 108 pass** (was 102; +5 prototype checks and a
  positive control).
- `node --test dashboard/tests/proto-variants.test.mjs` →
  1. every id in `dashboard/index.html` exists in `proto/index.html`, with an in-memory
     positive control proving the check can fail;
  2. no variant hides a panel/control/decision/log/chat/chart/banner element;
  3. craft lint per variant: no `transition: all`, no `scale(0)`, no `ease-in` (only
     `ease-in-out`), the strong `cubic-bezier(0.23, 1, 0.32, 1)` curve present,
     `prefers-reduced-motion` present, hover gated, `:active` present, every duration under
     the UI budget, all rules scoped to the variant;
  4. the picker's markup, styles and key wiring match PICKER.md verbatim;
  5. over HTTP: `/proto/` serves the page, all three sheets and the harness are
     byte-identical to disk, and the app modules it boots are the shipped ones.
- Browser walkthrough at 1440×900 and 390×844 on each variant: chat seeded from a decision
  row (chip attached), thinking log expanded, controls exercised, degraded controller
  (`?apiBase=` at a dead port) and the offline fixture. Screenshots in
  `dashboard/evidence/screenshots/t_4458a089-*.png`.

## 5. Honesty surfaces — where they live in each variant

Removing or softening these to look cleaner would have failed this task, so here is where
they are in every variant:

- degraded-backend banner: the shipped `.banner` in `#banner-host`, re-typed per variant,
  never hidden; in Instrument it is a light amber/red strip with dark ink.
- "record absent, not zero": sample cards keep the `not reported` line (Instrument keeps the
  dark plot surface, which is where the sample rows live); the incident "no record" state
  keeps its dashed border.
- trace provenance warnings: `.trace-alert*` stays outside the disclosure and keeps its
  colour; the raw model reply stays a dashed block, never the solid "executed" box.
- synthetic/demo labels: `.badge.synthetic` keeps a distinct violet ink on light.
- fixture labelling: the chat's `FIXTURE DATA · OFFLINE DEMO` badge is untouched, and the
  prototype caption additionally states "bundled fixture data · no controller".

## 6. One deliberate exception, and the dependency question

**The plots stay dark in Instrument.** `js/charts.js` draws its grid in
`rgba(255,255,255,.08)` and its series in `#5ac8fa`/`#ff9f0a`/… — light-on-dark by
construction — and the prototype layer may not modify shipped modules. A light plot area
would be an unreadable inversion, so the plot is treated as a screen inset in the paper.
That is a design decision made inside a real constraint, and it is documented in
`variants/instrument.css`.

**No dependency was added, and no build step.** `pick-ui-library` only justifies a library
when a task needs a *component* (a toast stack, a dialog primitive, a virtualised list) or a
capability the platform lacks. Here the platform covers it: CSS transitions, `@starting-style`
free, WAAPI not needed, `clip-path` for the plot wipe, `ResizeObserver` already in the
shipped chart module. The dashboard is a dependency-free static bundle served straight from
`dashboard/` by Firebase hosting, and a build step would be a permanent cost paid by every
future change for a one-PR comparison. The picker itself is vanilla and about 150 lines.

## 7. Motion method (summary)

The `animate` / `emil-design-eng` decision order was applied per surface, and the
`find-animation-opportunities` gate rejected more than it accepted. In short:

- **Animated:** panel entrance (occasional, "preventing a jarring change"), a new decision
  row fading in (occasional, opacity only), press feedback on every pressable, plot wipe in
  Instrument (occasional, `clip-path`), hover lift in Instrument (gated to fine pointers).
- **Rejected:** animating the 3–5s poll (100+/session — no animation), animating the log
  rows (high-frequency), animating the thinking-log open/close (keyboard-adjacent, native
  `<details>` behaviour, and the parent task exists because that state must stay put), a
  page-level scroll reveal (functional UI a user visits daily), and number count-ups on the
  metric figures (decoration on data the user is reading).

The `review-animations` self-review, with every finding and its fix and the verdict, is in
`dashboard/evidence/t_4458a089-animation-review.md`.

## 7. Known shipped artefacts (present before this PR, unchanged here)

Not introduced by the variants, and not fixed here because the prototype layer may not modify
shipped modules — recorded so nobody mistakes them for a variant's fault:

- `js/charts.js` draws the last x-axis label left-aligned at the right edge, so it can clip to
  "13:3" at some card widths. Same on the root dashboard.
- With no controller reachable, the node-health panel shows skeleton placeholders while the
  banner says "Controller unreachable… Showing last known data". The banner (the honesty
  surface) is truthful; the skeletons are the shipped loading state.

## 8. Running it locally

```bash
cd dashboard
python3 mock/server.py --port 8099     # fixture controller + static server
# open http://127.0.0.1:8099/proto/?v=1
```

`/proto/` resolves to `/proto/index.html` in the dev server the same way Firebase Hosting
serves a directory (the dev server gained that four-line fallback in this PR — it used to
404 a path that works in production).

## 9. After the owner picks

1. Promote the winning variant: move its design layer onto the root dashboard (not a
   copy-paste of the prototype page — the promoted sheet becomes the shipped one, with the
   variant scope removed), through `scripts/land.sh`.
2. Delete the prototype surface (`dashboard/proto/`) per the `prototype` skill's clean-up
   rule, unless the owner asks to keep the runners-up.
3. Re-run the full suite and the served-page checks against the promoted root, and
   re-capture the evidence screenshots for the root URL.

## 10. Evidence index

| file | what |
| --- | --- |
| `dashboard/evidence/screenshots/t_4458a089-{console,briefing,instrument}-01-node-health.png` | fleet, node cards |
| `…-02-charts.png` | charts, selectors, sample cards |
| `…-03-incident-thinking.png` | incident view with the thinking log expanded |
| `…-04-chat-context.png` | chat open with an Ask AI context chip attached |
| `…-05-manual-intervention.png` | actions, scale intent, pending remediation |
| `…-06-degraded-absent.png` | controller unreachable: banner + explicit "feed unavailable" |
| `dashboard/evidence/t_4458a089-animation-review.md` | `review-animations` findings, fixes, verdict |
