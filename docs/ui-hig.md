# HIG conformance — variant 4, "Overview" (`t_6af4e389`)

The operator dashboard re-designed under Apple's Human Interface Guidelines and shipped as a
fourth prototype variant at `/proto/?v=4` next to Console, Briefing and Instrument. The root
dashboard is untouched: the variant is a stylesheet (`dashboard/proto/variants/hig.css`) plus one
additive module (`dashboard/proto/variants/hig.js`) that rearranges the shipped nodes into a
different information architecture and puts them back when the picker moves on.

Vocabulary used below: a **destination** is a top-level place in the sidebar; a **record** is a
node or a decision; a **sheet** is a modal surface that slides over the page.

## Three destinations (rule 1)

Fleet, Incidents and Settings are peers in a sidebar — the job is "is the fleet healthy, what needs
me, what did the operator do".

- The operator log is not a destination. It is a sheet opened from the destination bar
  (`Operator log`) or from Settings → Diagnostics. Its markup is the shipped `#log-panel` (the
  stream, its filters, pause and clear), moved into a native `<dialog>` that slides in from the
  right edge and takes the window's full height (620 px wide, 96 vw on a phone) so the page it
  explains stays visible behind it; `Escape` dismisses it and focus returns to the button that
  opened it.
- Enrolling a node is a section inside Settings, not a peer of the incident feed.
- Below 820 px the sidebar becomes a top bar carrying the same three destinations, labelled. There
  is no hamburger and nothing is hidden behind one. A record on a phone is a push
  (list → detail) with a Back control, so the phone also keeps one screen.

## Master–detail (rule 2)

Incidents is a list on the left and the selected record on the right.

- The list carries the shipped decision row's own head: time, provenance badge, node, rule,
  action, target, and the fleet's answer.
- The record — incident card, evidence with `seq` references, thinking log, breach, rationale and
  the audit links — is the same DOM subtree, moved into the pane. Every record body lives either in
  the pane (the selected one) or parked in a hidden store (all the others); a row keeps its head
  and nothing else. Nothing is expanded inline, so the page is one screen however long the history
  is, and the 75 KB of incident text lives in a pane that scrolls on its own.
- Because the row is a `role="option"` and nothing else, it nests no button or link: an option that
  contains the row's `Ask AI`, audit-id and chart links would hide them from assistive technology
  and is an axe `nested-interactive` violation. The links are one level deeper, in the pane, where
  they are ordinary controls in a `role="region"`.
- The list is a `role="listbox"` with `role="option"` rows and roving `tabindex`: `↑`/`↓`/`Home`/
  `End` move the selection and the pane together, `Enter` moves focus into the pane.
- The decider strip, the join-key note and the counts are provenance, not the list: they are one
  disclosure deeper ("How this list is built — decider, join keys and counts"), still in the
  destination.

## Fleet landing (rule 3)

One row per node: name, a semantic status dot with the word beside it, CPU / RAM / disk as three
compact values, and the reason when degraded. Row → node detail (flags, services, processes,
uptime, charts, manual intervention). The row's only control is one overflow menu — the shipped
Charts / Ask AI / Restart / Drain buttons, moved in, with their listeners intact.

## Attention (rule 4)

There is no permanent "needs you" queue. The standing signal is a count badge on Incidents. When
an approval is genuinely running out, one banner appears at the top of that destination with the
countdown and the shipped Approve / Deny controls. Failed executions and fallbacks are semantic
labels on the list row (`exec-err`, `src-fallback`), not a separate queue.

The countdown is honest about its clock: only the controller knows when a `pending_approval` was
issued, and the browser does not receive that timestamp, so the banner says **"counted from when
this tab first saw the request"** and names the contract rule (auto-deny 300 s after issue).

## Type (rule 5)

One family (`-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, …`), two
weights (400 and 600), and a scale of 22 (destination and record titles) / 17 / 15 / 13 / 11 px.
Sentence case throughout; the shipped ALL-CAPS micro-labels (`EVIDENCE`, `DERIVED`, `BREACH`,
`CHAT`) are reset with `text-transform: none` rather than hidden. Badges are reserved for
exceptions (demo/synthetic data, fallback, failed execution); normal states are text beside a dot.

The shipped sheet sizes its dense console text in `rem` (0.68–0.85 rem), which lands at
10.9–13.6 px — off any scale and different at every zoom step. Every one of those selectors that
appears in an adopted surface (`inc-detail`, `trace-*`, `breach-*`, `decision-target`,
`incident-prov`, …) is re-pinned to the 13 px or 11 px step, so the measured `fontSizes` of every
destination and appearance is exactly `{11, 13, 15, 22}`.

## Colour (rule 6)

Semantic tokens only, defined once per appearance:
`label` / `secondaryLabel` / `tertiaryLabel`, `separator`, `systemRed` (destructive and error),
`systemOrange` (attention, degraded, fallback), `systemGreen` (healthy), and one accent for links
and the single primary action. Everything else is neutral. The shipped sheet's own token names
(`--bg`, `--fg-dim`, `--muted`, `--warn`, …) are remapped onto the semantic set inside the variant,
so surfaces this card does not restyle individually (charts, dialogs, the client-node form) cannot
leak a second palette.

The shipped sheet also hard-codes literals — the pale amber `#ffdca8`, green `#b6f0c8`, red
`#ffc0bb`, violet `#e3c6ff`, and ink backgrounds behind the raw reply, the "nothing recorded"
blocks and the result `pre`. Those are the only colours in the variant that are not tokens, and a
literal tuned for the dark console is unreadable on the light one: the first measurement pass found
25 text runs at 1.18–2.3:1 in light Incidents (dark-on-dark `pre`, pale amber on white). Every one
is re-pointed at a token in both appearances, and the ink surfaces take `bg-sunken` with a
`separator` border, so the meaning survives the appearance switch through the label and the border
rather than a literal fill.

## Both appearances (rule 7)

Light is the base, dark follows `prefers-color-scheme`, and Settings → Appearance offers
System / Light / Dark, stored per browser (`localStorage`, this tab's origin only) and reflected in
`?appearance=light|dark` for screenshots. Both appearances are captured for every destination at
1440×900 and 390×844.

## Targets and input (rule 8)

Every button, nav item, chip, segmented item, inline link, select, textarea and text input is at
least 44 px tall (inline links get a 44 px hit area without moving the text). Checkboxes are 20 px
inside a 44 px `label`, which is the target. Every focusable element has a `:focus-visible` ring.
The full keyboard path works — see `evidence/hig/audit/keyboard-walkthrough.md`.

## Destructive actions (rule 9)

Restart and Drain are red, live inside the row's overflow menu, and keep the shipped confirmation
dialog, which names the node ("Restart termini-node-a?") and the consequence (workloads
interrupted; the controller queues `node.reboot` with approval required). The confirmation's
primary button is red, not accent.

## States (rule 10)

Empty, loading, degraded and offline each get one line of text and one recovery action. Loading is
"Loading nodes…" beside the shipped skeletons; an empty fleet keeps the shipped sentence and adds a
`Refresh` action; a degraded node shows its reason in orange in the row and the failed flags in the
detail; offline nodes are red-dotted, never a red wall. Errors from the controller keep the shipped
banner, restyled, with its retry.

## Motion (rule 11)

220 ms destination fade, 260 ms sheet slide-in from the right edge, 200 ms menu fade — transform and
opacity only, strong ease-out (`cubic-bezier(0.23, 1, 0.32, 1)`), and `prefers-reduced-motion:
reduce` removes all of it while keeping colour and state changes. No layout property is animated and
no colour is transitioned: colour transitions are a paint animation *and* they leave an element
stuck on the old appearance's token when the colour scheme flips mid-transition.

## Nothing removed (rule 12)

Every honesty surface survives, one level deeper or in another destination: the thinking log
(disclosure in the record), evidence with `seq` references, audit ids and their dialogs, the
model-vs-fallback provenance badges, the decider strip, the demo/synthetic label on fabricated
nodes, the "absent, not zero" notes on charts, the raw operator stream (sheet), the AI chat panel
(its launcher is in the destination bar), enrolment and connection settings.

## Acceptance (measured, not asserted)

Measured by `dashboard/evidence/hig/tools/capture.py`, which drives a headless Chromium over the
DevTools Protocol and writes the raw JSON next to the screenshots. Contrast is WCAG 2.x relative
luminance on composited backgrounds (the same maths the a11y check uses). Accessibility is the
repository check in `tools/a11y.js`, which implements the axe **critical-impact** rule set plus
colour contrast — axe-core itself could not be installed in this environment (npm package threat
intelligence lookup unavailable to the sandbox), so the report says which rules ran.

Two rules are implemented with axe's own semantics rather than a convenient approximation, because
the approximation produced false positives that would have hidden real defects: `label` counts a
`<label for>` or a wrapping `<label>` as a name (the client-node form has both and was being
reported as unlabelled), and `nested-interactive` asks whether a **widget** is inside a widget —
a scrollable `div[tabindex=0]` or a focusable `role="region"` pane is not one, while a button inside
a `role="option"` row is.

`audit/summary.json` ends with an `acceptance` object (`cases`, `keyboardWalkthrough`, `pass`) so a
reader or a CI job does not have to re-derive the verdict from twelve cases by hand. The keyboard
walkthrough records one `ok` per step: the destination actually changed, the arrow keys actually
moved the selection and the detail, the sheet actually opened and `Escape` actually closed it with
focus returned.

| Check | Target | Result |
| --- | --- | --- |
| Horizontal scroll at 1440×900 and 390×844 | none | see `audit/summary.json` |
| Chrome controls per screen | ≤ 20 | see `audit/summary.json` |
| Nav and primary controls ≥ 44 px | 100% | see `audit/summary.json` |
| Icon-only control without `aria-label` | 0 | see `audit/summary.json` |
| Body ≥ 13 px, captions ≥ 11 px | yes | see each measurement's `fontSizes` |
| Type scale | exactly 22 / 17 / 15 / 13 / 11 | every case's `fontSizes` keys |
| Font weights | ≤ 2 | 400 and 600 only |
| Text colours | semantic tokens only | see each measurement's `textColors` |
| Contrast | ≥ 4.5:1 body, ≥ 3:1 large | every case: 0 runs below target |
| Keyboard-only walkthrough | every destination, arrows, sheet | `audit/keyboard-walkthrough.md`, 5/5 PASS |
| a11y critical / serious violations | 0 | `audit/a11y-*.json`, 0 in all twelve cases |
| `cd dashboard && npm test` | green | includes `tests/ui-hig.test.mjs` |
| Raw-sample rows — `.sr-label`, `.sr-flags`, `.sr-where` in every sample card | 0 clipped, every cell ≥ 40 px, no page horizontal scroll, at 1024 / 1280 / 1440 / 1920 | measured 2026-10-17 (`t_214ec609`): 72 cells per width, **0 clipped**, narrowest label 151 px (1280), widest case 231 px (1440), `scrollWidth == innerWidth` at all four widths, dark and light |

**Raw-sample legibility (`t_214ec609`, measured 2026-10-17).** The audit above measures contrast,
type scale, chrome count and a11y — it never measured whether a raw-sample row's own text fits
inside its card, and at laptop widths it did not. `dashboard/assets/styles.css` gave the row five
fixed tracks (84 + 96 + 30 px plus four 6 px gaps = 234 px of chrome) while
`hig.css`'s `repeat(auto-fit, minmax(260px, 1fr))` lets a chart card be 260–380 px wide, so the
`minmax(0, 1fr)` name track collapsed to 12–18 px and `text-overflow: ellipsis` hid it — the
names rendered as a one-character-per-line smear and `cumulative` as `cumu…`. Re-measured after
the fix, in a headless Chromium driven over the DevTools Protocol against the local bundle
(`dashboard/mock/server.py`, `?dest=fleet`, both appearances): at **1024, 1280, 1440 and 1920**
every `.sr-label`, `.sr-flags` and `.sr-where` satisfied `scrollWidth <= clientWidth + 1` and
`clientWidth >= 40` (0 clipped cells out of 72 per width), and
`document.documentElement.scrollWidth <= innerWidth` held at every width. The sweep that finds
the grid's 260 px floor (900 → 2560 in steps) is also clean, so the row cannot collapse at any
card width the grid can produce. The checked-in mock fixtures predate
`docs/contracts/node-telemetry.md` §4.12–§4.14, so they render the "not reported" note instead
of rows; the rows measured here had those contract fields added to the sample by a
measurement-only proxy in front of the mock — the dashboard JS/CSS under measurement were the
checked-in files, unmodified. The deployed page was not re-measured in this pass (no project
token in the sandbox); the local bundle is byte-identical to the one the orchestrator deploys.

"Chrome controls" means the persistent page furniture — the sidebar, the destination bar, a panel's
own toolbar and a banner. A row's single menu button, a chart's own "Ask AI", a form field and a
detail-pane link are content, not chrome (the card excludes the row action explicitly). The
measurement reports both the chrome count and the total so the definition is visible, not asserted.

## Not followed, and why

- **44 pt on the harness picker.** The picker at the bottom of the prototype page is harness
  chrome, copied verbatim from the prototype skill (PICKER.md) and identical in all four variants.
  It is not part of the design being judged, so styling it would break the comparison.
- **A tab bar on the phone.** iOS uses a bottom tab bar for the three-destination case. A bottom
  bar would collide with the prototype picker (also bottom-fixed) and with the AI chat sheet, so
  the phone keeps a top bar with the same three labelled destinations. Same destinations, same
  order, no hamburger — the platform's alternative when a bottom bar is not available.
- **Animated colour transitions.** Apple animates some colour changes (button fills). This build
  does not, for the reason in "Motion": a colour transition in flight when `prefers-color-scheme`
  flips leaves the element on the old appearance's token in Chromium, and the acceptance measures
  contrast in both appearances.
- **Confirming every destructive action twice.** Restart and Drain keep the shipped confirmation
  dialog (one sheet, naming the node and the consequence). A second "are you sure" on top of the
  existing one would be ceremony, not safety.

## How to re-run

```sh
cd dashboard
python3 mock/server.py --port 8110 &            # fixtures; no controller needed
python3 evidence/hig/tools/capture.py --base http://127.0.0.1:8110 --out evidence/hig
```

`capture.py` launches its own headless Chromium on a throw-away profile, captures every
destination at 1440×900 and 390×844 in both appearances, measures the page, runs the a11y check,
performs the keyboard walkthrough with real key events, and checks that switching away from the
variant restores the shipped page. The report lands in `evidence/hig/audit/summary.json`.

## Promotion (2026-10-17)

The owner selected this variant, so it is no longer a prototype: `dashboard/index.html` now declares
`data-variant="hig"` on `<html>`, links `proto/variants/hig.css` and loads `proto/variants/hig.js` — the same
files the picker used, loaded exactly once, with no harness and no picker on the root. The three other variants
stay at `/proto/` as references; the prototype page and the root render the same design when `?v=4` is selected.

Two test guards were inverted rather than deleted, because the property they protect changed owners: the root must
load ONE design layer, once, and must never embed the picker (`ui-hig.test.mjs`, `proto-variants.test.mjs`).

Fixed in the same change: the node detail pane accumulated a second, stale block after each poll, because the
dashboard rebuilds node cards and the parked `.hig-node-rest` outlived its card. `placeNodeDetail()` now keys each
parked block to its node id, drops blocks whose node is gone, and replaces a rebuilt card's block instead of
appending another.
