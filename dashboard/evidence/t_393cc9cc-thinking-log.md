# Expanded thinking log survives the decision poll (t_393cc9cc)

Bug: on the AI operator's incident view, a `<details class="trace-details">` ("AI
THINKING LOG · thinking log · N steps") closed itself at most one decision poll
after the user expanded it, and the element they had opened no longer existed —
so the reading position, the open log and any focus inside the list were thrown
away every 3 s (`cfg.decisionMs`, `dashboard/js/core/config.js`).

Root cause: `DecisionsPanel.render()` called `clear(host)` and appended freshly
built rows on every update, and `_thinkingLog()` built its `<details>` with no
open state. A rebuilt trace is therefore always collapsed.

Fix (`dashboard/js/ui/decisions.js`):

1. **Keyed reconciliation** — a row whose decision id and rendered content are
   unchanged is left in the DOM untouched (same `<li>` object), and only rows
   whose content actually changed are rebuilt, in place. This is what keeps the
   scroll anchor, the reading position and focus alive across a poll.
2. **Keyed disclosure state** — `_details()` records `open`/closed on `toggle`
   keyed by `decision id + kind` (`<id>::trace`) on the panel instance and
   re-applies it to the rebuilt element. Both directions are recorded, so a log
   the user closed stays closed. Every `<details>` in the decision list is built
   through `_details()`, so the next one cannot inherit the bug.
3. **Scroll anchor + focus fallback** — where a row must be replaced, the row at
   the top of `#decision-viewport` is re-anchored and focus is restored to the
   equivalent element by path.
4. The "say it out loud" rules are untouched: `_traceAlerts()` lines stay
   outside the disclosure, `present: false` still renders the trace-none block
   and a `decided_by: fallback` trace is still visually distinct.

## Unit regression test

`dashboard/tests/decisions-panel.test.mjs` (run by `npm test` and by CI), with a
dependency-free mini-DOM in `dashboard/tests/helpers/minidom.mjs` — the bundle
has no runtime deps and the test adds none.

The test drives the panel through consecutive `update()` cycles with the same
decisions, opening/collapsing a trace between them, and asserts that it is still
open (and the same node), that a user-collapsed one stays collapsed, that row
identity survives, that every `<details>` in the list is keyed, that the
out-loud lines stayed outside the collapse, and that focus inside a rebuilt row
is restored.

Pre-fix run (`git stash` of the fix, then `node --test tests/decisions-panel.test.mjs`):

```
✖ an expanded thinking log stays open across the next poll
    AssertionError: the expanded thinking log closed itself on the poll  (false !== true)
✔ a trace the user collapsed stays collapsed across polls
✖ a row whose content changed is rebuilt but keeps its open log
✖ row identity survives an unchanged poll, per decision
✖ filtering the list and coming back keeps the expanded log
    AssertionError: state was keyed to the element, not the decision
✖ every disclosure in the decision list is keyed, so the next one cannot regress
    AssertionError: a <details> in the list must carry a disclosure key
✔ the say-it-out-loud lines stay outside the collapse
✔ a row with no trace still says so, and renders no empty disclosure
✖ focus inside the list survives a poll that rebuilds the row
ℹ tests 9   pass 3   fail 6
```

Post-fix: `cd dashboard && npm test` → `tests 86 / pass 86 / fail 0`.

## Served page — local

`python3 -m http.server 8099 -d dashboard`, then
`http://127.0.0.1:8099/?offline=1`.

A trace was expanded on a real decision card (a click on the `<summary>`, not a
scripted `open = true`) and observed across >2 poll intervals
(`decisionMs = 3000`):

```
BEFORE  open=true   #decisions-updated "updated 03:09:09"  steps visible 10
AFTER   open=true   #decisions-updated "updated 03:09:15"  steps visible 10
        same <li>: true   same <details>: true   marker attribute still set: true
```

Screenshots (viewport frames, clock in the top bar shows the elapsed polls):

- `screenshots/t_393cc9cc-thinking-log-local-before.png` — expanded, 03:09:09
- `screenshots/t_393cc9cc-thinking-log-local-after.png` — still expanded, 03:09:17

## Served page — deployed

`firebase deploy --only hosting --project termini-510609`, then
`https://termini-510609.web.app/?offline=1` (the same offline fixture the bug
was reported against; the deployed `js/ui/decisions.js` is byte-identical to the
committed one, checked with sha256 against the fetched file).

```
BEFORE  open=true   "updated 03:09:52"
AFTER   open=true   "updated 03:10:01"   same <li>: true   same <details>: true
COLLAPSE  open=false "updated 03:10:04"  →  still open=false "updated 03:10:13"
```

Screenshots:

- `screenshots/t_393cc9cc-thinking-log-live-before.png` — expanded, 03:09:52
- `screenshots/t_393cc9cc-thinking-log-live-after.png` — still expanded, 03:10:01

Note: the dashboard's decision poll is skipped while `document.visibilityState`
is `hidden` (by design, `js/main.js`), so the browser tab driving these checks
was brought to the front before the wait — the `updated` clock advancing between
the two frames is the proof that polls actually ran.
