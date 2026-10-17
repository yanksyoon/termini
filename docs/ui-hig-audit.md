# Apple-HIG conformance audit — Termini dashboard

- Owner: `verifier` · kanban task `t_7094134f` · 2026-10-17
- Audited commit: `e0ac661` (origin/main at audit time)
- Instrument: `scripts/ui-hig-audit.mjs` (committed, re-runnable — see §7)
- Browser: Google Chrome for Testing **145.0.7632.6**, headless, viewport **1440×900**
- Data sources measured: the contract-faithful mock controller (`dashboard/mock/server.py`,
  the repo's documented local instrument for the dashboard — README "Verify") and the
  bundled `?offline=1` fixtures. The dashboard is a static bundle: every measurement here
  except the empty/loading/degraded *state text* is independent of which backend serves it.

This document measures the dashboard **as shipped** against a fixed set of Apple-HIG rules,
produces a number for each, and records the artifact (screenshot or command) that backs every
verdict — so a redesign is gated on evidence, not opinion.

---

## 1. What "shipped" and "candidate" mean here

- **Shipped** = the root page `/` (the dashboard on origin/main), measured twice:
  with contract data (mock controller, 3 nodes / 4 decisions) and with `?offline=1`.
- **Candidate** = the redesign that landed as `t_4458a089` ("three dashboard UI directions
  behind a prototype picker", PR #218). It serves **three** design layers at `/proto/`,
  not one:

  | URL | Variant | Axis |
  | --- | --- | --- |
  | `/proto/?v=1` | **Console** | density (12.5px base, fleet rail + incident console on one screen) |
  | `/proto/?v=2` | **Briefing** | layout & personality (one calm column, large type, soft material) |
  | `/proto/?v=3` | **Instrument** | personality & motion (light theme, one electric accent) |

  **Correction to the card body:** the card anticipated a single candidate at `/proto/?v=4`.
  No such variant exists — the picker has exactly three (`?v=1|2|3`), and `?v=4` is out of
  range (the harness clamps to `v=1`). This audit measures all three actual candidates and
  puts them side by side with the shipped baseline. The prototype page boots the shipped
  modules unmodified (`docs/ui-variants.md` §2), so a variant is a design layer over the
  same markup id-for-id — which is exactly what an HIG comparison wants to isolate.

---

## 2. Rules and verdicts — shipped baseline

Verdict key: **PASS** (meets the rule, artifact present) · **FAIL** (violates the rule) ·
**CAUTION** (measured, not a hard HIG rule, but flagging it) · **NOT VERIFIED** (instrument
missing — none used here).

| # | Rule (Apple HIG) | Measured (shipped, contract data) | Verdict | Evidence |
|---|---|---|---|---|
| R1 | Control density: controls/screen, total, page height vs viewport | 102 visible controls (116 in DOM); **1694 words**; page **7.3×** viewport; no horizontal overflow | CAUTION | `report.json` §controls/§words/§docSize; `screenshot-dark.png` |
| R2 | Interactive target ≥ **44 px** (HIG minimum) | **98/102 (96.1%)** controls under 44 px; **median height 25 px**, min 13 px | **FAIL** | `report.json` §targetSizes; `screenshot-dark.png` |
| R3 | Type floor ≥ ~11 px (smallest legible); restrained scale | min **10.6 px**; **17** text runs < 11 px; **20 distinct sizes**, weights {400,600,700} | **FAIL** | `report.json` §typeScale |
| R4 | Semantic states use **one colour system** (ok/warn/err) | one system: ok `#b6f0c8`/`#2f8a4d`, warn `#ffdca8`/`#8a6a1f`, err `#ffc0bb`/`#8a2f28` across badge/flag/dot; 16 text tints, 9 backgrounds | **PASS** | `report.json` §semantic (table below, §4) |
| R5 | Light/dark follows `prefers-color-scheme` | palette identical under dark and light emulation (`#0b0e14` both) — **no light variant** | **FAIL** | `report.json` §scheme; `screenshot-dark.png` vs `screenshot-light.png` |
| R6a | Keyboard tab reaches every destination, focus never lost/invisible | **34 unique tab stops**, **0 lost**, **0 invisible** | **PASS** | `report.json` §keyboard.tab |
| R6b | Arrow navigation in the incident list | ArrowDown moves focus: **false** — no keydown handler, list is a tabbable scroll region | **FAIL** | `report.json` §keyboard.arrowNav; `grep -rn "keydown\|ArrowDown" dashboard/js/` → none |
| R6c | Escape closes dialogs | Settings dialog **closes on Escape** (native `<dialog>` cancel) | **PASS** | `report.json` §keyboard.escape |
| R6d | Visible focus indicator | **no `:focus-visible` rule** in shipped CSS — relies on UA default outline | **FAIL** | `report.json` §focusCss; `grep -n ":focus" dashboard/assets/styles.css` → none |
| R7 | Icon-only controls have an accessible name | **0** focusable controls without an accessible name (AX tree, 1643 nodes) | **PASS** | `report.json` §accessibility |
| R8 | Contrast: body ≥ 4.5:1, every coloured badge ≥ 3:1 | body **15.9:1** (`#e6e9ef`/`#0b0e14`); **0** badges < 3:1 (min badge **6.8:1**) | **PASS** | `report.json` §contrast (method stated) |
| R9 | Destructive actions red, confirmed, not adjacent to benign | **not red** (amber `#ffd9a3` on `#2a2010`); **confirmed** (confirm dialog); **adjacent** to each other + Ask AI | **FAIL (red)** / **PASS (confirmed)** / CAUTION (adjacency) | `report.json` §destructive; `js/ui/modal.js` `danger:true` |
| R10 | Empty / loading / degraded / offline = one line + one recovery | loading=skeleton; empty="No nodes registered yet."; degraded=banner + "Retry now"; offline=fixture labels | **PASS** | §5 below; `js/ui/overview.js:15,21`, `js/main.js:549-560` |

The same measurements for the offline run are in
`docs/evidence/ui-hig-audit/shipped-offline/report.json` (99 controls, 2086 words, 6.5×
viewport, same verdicts on every rule).

---

## 3. Shipped vs candidate — side by side

| # | Rule | Shipped | Console (`v=1`) | Briefing (`v=2`) | Instrument (`v=3`) |
|---|---|---|---|---|---|
| R1 | Controls / words / page×viewport | 102 / 1694 / **7.3×** | 107 / 1712 / **4.4×** | 117 / 1749 / **11.5×** | 107 / 1711 / **5.4×** |
| R2 | Controls < 44 px (median h) | 98 (**96.1%**, 25 px) | 104 (**97.2%**, **17 px**) | 114 (**97.4%**, **27 px**) | 104 (**97.2%**, 19 px) |
| R3 | Min font / runs < 11 px / distinct sizes | 10.6 / 17 / 20 | **8.0** / **676** / 18 | **10.9** / 17 / 24 | **8.3** / **616** / 24 |
| R4 | One semantic system | yes | yes | yes | yes (re-stated in light ink) |
| R5 | Follows `prefers-color-scheme` | no (dark-only) | no (dark-only) | no (dark-only) | **no** (fixed light; `#f3f3f0` under both schemes) |
| R6a | Tab stops / lost / invisible | 34 / 0 / 0 | 34 / 0 / 0 | 34 / 0 / 0 | 34 / 0 / 0 |
| R6b | Arrow nav in list | no | no | no | no |
| R6c | Escape closes dialog | yes | yes | yes | yes |
| R6d | `:focus-visible` rule | none | picker only | picker only | picker only |
| R7 | Nameless focusable controls | 0 | 0 | 0 | 0 |
| R8 | Body contrast / min badge | 15.9 / 6.8 | 15.3 / 7.7 | 16.8 / 6.4 | 16.0 / 5.4 |
| R9 | Destructive red / confirmed | no / yes | no / yes | no / yes | no / yes |
| R10 | One-line state + recovery | yes | yes | yes | yes |

Reading it honestly: **every variant fails the same three hard HIG rules as the shipped page
(R2 target size, R3 type floor, R5 light/dark), and none adds arrow-key list navigation or a
dashboard-control focus ring.** The variants trade these away for their axes — Console and
Instrument push *further* below the type floor (8 px micro-labels, 600+ sub-11 px runs) and
*slightly* below the target-size median (17–19 px), while Briefing is the closest to passing
type (10.9 px floor, 27 px median) and reads largest. On contrast (R8) all four surfaces are
strong — the light Instrument palette restates the semantic colours without losing legibility.
The winner, when picked, still has R2/R3/R5 to fix at promotion time.

Evidence: `docs/evidence/ui-hig-audit/{console,briefing,instrument}/report.json` and
`screenshot-{dark,light}.png` for each.

---

## 4. Colour system (R4 evidence)

One ok/warn/err system, one accent, applied consistently (shipped):

| Semantic state | text | border | fill |
|---|---|---|---|
| ok (online / disk ok / succeeded / `btn-ok`) | `#b6f0c8` | `#2f8a4d` | `#123a20` |
| warn (degraded / sev-warn / `btn-warn` / pending) | `#ffdca8` | `#8a6a1f` | `#2a2010` |
| err (offline / sev-error / `exec-err`) | `#ffc0bb` | `#8a2f28` | `#2a1311` |
| info (sev-info / model / `btn-primary`) | `#9fe0ff` | `#26678f` | `#17527a` |

The full 16-tint text histogram and per-selector semantic sample are in
`docs/evidence/ui-hig-audit/shipped-data/report.json` §colors/§semantic. Instrument keeps the
*same* three-way ok/warn/err distinction in light inks (verified: its `btn-warn` is still the
amber of the pair, §semantic in `instrument/report.json`).

---

## 5. Empty / loading / degraded / offline (R10 evidence)

| State | One line shown | Recovery | Where |
|---|---|---|---|
| loading | 3 × `.skeleton` shimmer cards | resolves on first poll | `js/ui/overview.js:15` |
| empty (no nodes) | "No nodes registered yet." | register a node (the "Your node" panel gives the one-liner) | `js/ui/overview.js:21` |
| empty (no decisions) | "Waiting for operator decisions…" / "No decisions recorded yet — …" | none needed (event-driven) | `js/ui/decisions.js:39,140` |
| empty (log) | "Waiting for operator events…" / "No lines match the current filters." | adjust filters / wait | `js/ui/logs.js:191,275` |
| degraded backend | "Controller unreachable for N attempts (CODE). Showing last known data." | "Retry now" button | `js/main.js:549-550` |
| offline | fixture labels ("bundled fixture data · no controller") | point at a real controller via Settings | `js/offline.js`, proto caption |

Every state is a one-line message plus a single recovery action — none is a blank or hung
panel. (This is the one set of strings that is backend-dependent in *content*; the mechanism —
banner + retry, skeleton, explicit empty — is in the shipped bundle and measured above.)

---

## 6. Destructive actions (R9 detail)

Shipped `Restart node` / `Drain node` and per-node `Restart` / `Drain` are `.btn-warn` —
amber (`#ffd9a3` on `#2a2010`), not the system red (`#ff453a`). They **are** confirmed
(`js/ui/modal.js` `confirmDialog(..., danger: true)`, and the confirm button is itself
`btn-warn`). They sit in the same `.btn-row` as each other (and, in the node cards, next to
"Charts" and "Ask AI"); the pending-remediation `Approve` (green `btn-ok`) and `Deny` (amber
`btn-warn`) are adjacent by design. Apple HIG calls for destructive actions to be visually
destructive (red) and separated from primary actions — the dashboard relies on amber +
confirmation instead. All three variants inherit this unchanged (same shipped modules);
Instrument re-inks it as dark amber `#7a4600` on white, still not red.

---

## 7. Reproducibility

The script is committed at `scripts/ui-hig-audit.mjs` (zero npm deps: Node ≥ 22 built-in
`WebSocket` + headless Chrome over CDP). From a clean checkout:

```bash
# 1) serve the dashboard + contract-faithful mock controller
python3 dashboard/mock/server.py --port 8099 &

# 2) measure any surface (url, label, outdir, settle-ms)
node scripts/ui-hig-audit.mjs "http://127.0.0.1:8099/" shipped-data docs/evidence/ui-hig-audit/shipped-data
node scripts/ui-hig-audit.mjs "http://127.0.0.1:8099/?offline=1" shipped-offline docs/evidence/ui-hig-audit/shipped-offline
node scripts/ui-hig-audit.mjs "http://127.0.0.1:8099/proto/?v=1&apiBase=/v1&projectId=01H4KZ9Q2R3T5V7W9X1Y3Z5A7B" console docs/evidence/ui-hig-audit/console
node scripts/ui-hig-audit.mjs "http://127.0.0.1:8099/proto/?v=2&apiBase=/v1&projectId=01H4KZ9Q2R3T5V7W9X1Y3Z5A7B" briefing docs/evidence/ui-hig-audit/briefing
node scripts/ui-hig-audit.mjs "http://127.0.0.1:8099/proto/?v=3&apiBase=/v1&projectId=01H4KZ9Q2R3T5V7W9X1Y3Z5A7B" instrument docs/evidence/ui-hig-audit/instrument
```

Each run writes `report.json` (every measured number), `screenshot-dark.png` /
`screenshot-light.png` (full page under each emulated scheme) and a `table.md` fragment.
`CHROME=/path/to/chrome` overrides browser discovery. To re-gate a future `/proto/?v=4` (or
the promoted winner), point the script at that URL — the comparison is identical by
construction.

What the numbers mean (stated so a re-run is self-interpreting):

- **Contrast** is WCAG 2.x relative luminance over `getComputedStyle`; a transparent
  background is resolved to the nearest painted ancestor, and semi-transparent fills are
  alpha-composited source-over before the ratio is computed (so an `rgba(69,209,122,.13)`
  badge is not misread as a solid fill). Gradients are sampled at their first colour stop.
- **Target size** is the rendered `getBoundingClientRect` of every visible interactive
  control (`button, a[href], input, select, textarea, [tabindex], summary, [role=…]`), counted
  in CSS px at DPR 1.
- **Accessible names** come from `Accessibility.getFullAXTree` (the accessibility tree), not
  the markup.

## 8. Limitations

- The live fleet controller (raw IP) was not reachable from this host (plain-HTTP probe to a
  raw IP is refused by the host's security policy). The mock serves the **identical static
  bundle** and contract-shaped data, which is the repo's documented verification instrument;
  no HIG number above depends on which backend fills the panels, so this does not change any
  verdict. State-text content (R10) was read from the shipped source rather than a live
  controller.
- `prefers-color-scheme` is probed by emulating the media feature and reading the computed
  body background in each scheme — a palette that does not change fails R5 regardless of how
  the author styles it.
