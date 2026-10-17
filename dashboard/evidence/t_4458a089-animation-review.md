# `review-animations` self-review — dashboard UI variants (`t_4458a089`)

Reviewed: `dashboard/proto/variants/{console,briefing,instrument}.css`, `dashboard/proto/harness.css`,
`dashboard/proto/harness.js`, against [review-animations](https://github.com/emilkowalski/skills)
and its `STANDARDS.md`, at skills commit `2d228630ba1446365f614a108cb8252f1feda202`.

I wrote these files and then reviewed them as if someone else had. Every finding below was a
real defect in a draft of the prototype layer, and every one is fixed in the delivered files —
the "After" column is what is in the CSS now, not a suggestion.

## Part 1 — Findings, with the fix

| Before | After | Why |
| --- | --- | --- |
| `variant="briefing"`: `.decision-row:hover { box-shadow: … }` — the hover shadow **replaced** the row's `inset 3px 0 0` provenance rail | decision rows dropped from the hover-shadow group; only node and chart cards lift | feel-breaking: the model/fallback/deterministic rail is a *honesty surface*. A hover state must not delete it |
| `variant="instrument"`: `.decision-row` entered with `translateY(6px)` + `opacity: 0` | entrance is `opacity` only, 180ms | frequency: the decision list re-polls every 3s. A list that moves under the cursor while its reader is aiming at "Ask AI" is a regression, not a nicety |
| `variant="briefing"`: `.panel:nth-of-type(n)` stagger delays on a parent mixing `section.panel` and `details.panel` | `.panel:nth-child(n)` | `nth-of-type` counted each element type separately, so the log panel got delay 0 and the cascade ran out of order |
| `variant="instrument"`: `.node-card { transition: transform …, box-shadow … }` | lift transitions `transform` only; the shadow lives on a `::after` whose `opacity` fades | performance: `box-shadow` is a paint property. Opacity is composited; the lift is the thing the eye reads |
| `variant="instrument"`: chart entrance wipe applied to `.chart-card` | wipe applied to `.chart-canvas` (260ms, `clip-path`) | the card wipe clipped its own title, basis caption and shadow; the plot is the element that should draw itself |
| `variant="console"`: first draft reused the 8px / 240ms panel entrance from Briefing | entrance deleted; the variant animates nothing on mount | frequency gate: an operator console is watched for hours — the remedial hierarchy's first move is *delete the animation*, not shorten it |
| `harness.css`: picker caption at `rgba(10,10,10,0.82)` | `rgba(10,10,10,0.94)`; one line with ellipsis ≤620px | chrome must not depend on the page for contrast: over the light variant, and over dense text, the axis line was unreadable |
| `variant="instrument"`: topbar `rgba(255,255,255,0.86)` + 14px blur | `0.95` + 20px blur | while scrolling, the page smeared behind the status pills — material conveys hierarchy, it must not garble content |
| all three: variant `.layout`/`.topbar` padding beat the shipped `body.chat-open` shift (same specificity, later sheet) | `body.chat-open .layout/.topbar` restated at higher specificity, ≥901px only | feel-breaking: the chat panel covered the incident view it was seeded from. Below 901px the shipped bottom-sheet rule still wins |
| `variant="console"`: 24px desktop buttons at phone width | `.btn`/`.chip`/card buttons get touch padding at ≤720px | feedback that is physically unhittable is not feedback; density gives way where it stops being usable |
| `.linklike` inside a flex column stretched and centred ("Ask AI about this thinking log" floated mid-row) | `text-align: left` | spatial correctness: a link-shaped control must read as a link, anchored to its block |
| `.chat-intro` clipped by `max-height: 4.4em; overflow: hidden`, ending mid-word | clamped to 3 lines with `-webkit-line-clamp` | a sentence that stops mid-word reads as a bug, not as brevity |

## Part 2 — Verdict

**Feel-breaking regressions.** Two were found and fixed: the provenance rail that vanished on
hover (Briefing) and the chat panel that covered the incident view in every variant. Both are
the kind of defect that only shows up under the pointer, which is why they are in this list
rather than in the docs.

**Missed simplifications.** One: the Console variant had motion it did not earn. Deleted rather
than shortened — at 100+/session frequency the correct number of entrance animations is zero.

**Performance.** One: `box-shadow` in a transition (Instrument node cards). Moved to an
opacity-faded pseudo-element. Everything else animates `transform`, `opacity`, or `clip-path`
(the sanctioned fourth), and the one `width` transition in the codebase is PICKER.md's own
highlight, which is 28px tall, absolutely positioned, and has no layout dependents.

**Interruptibility & timing.** No keyframes on anything added or triggered rapidly: toasts are
the shipped sheet's (its `rise` keyframe fires on a toast, which is acceptable because a toast
is added once and not re-fired mid-flight), decision rows fade with a transition-backed
animation on creation only, and the picker's highlight is a transition so flips retarget.
Every UI duration is ≤300ms; the longest single motion is the 260ms plot wipe.

**Origin, physicality & cohesion.** Modals stay centred (shipped `.dialog`); no popover in the
prototype surface is trigger-anchored, so no `transform-origin` override was needed. Nothing
enters from `scale(0)`. Each variant's motion is tuned to its own personality: Console
near-static, Briefing a considered 240ms rise, Instrument an expressive 200ms rise with a plot
wipe — and the picker's chrome is identical in all three, because it is not a contestant.

**Accessibility.** All three sheets ship `prefers-reduced-motion: reduce` blocks that keep
colour/opacity and drop movement (Briefing and Instrument redefine their own keyframes so the
`animation-name` still resolves); all hover styling is behind `(hover: hover) and (pointer:
fine)`; press feedback is on `:active`, which works on touch; `scale(0.97)` on press is
consistent across variants; the picker's own reduced-motion path is PICKER.md's. Phone
verification was at 390×844 with touch emulation (`hover: false` confirmed), and the tap
targets were re-checked after the density fix.

**Decision: approve.** No feel-breaking regression remains, no motion is left that should be
deleted, every duration and curve comes from the standards table, interruptibility is handled
where it matters, and reduced motion and pointer gating ship with the animations rather than
after them.

## `find-animation-opportunities` — what was proposed and what was refused

Surviving opportunities (all implemented): panel entrance (occasional → standard, 8px/240ms or
6px/200ms); new decision row fade (occasional, opacity only); press feedback on every pressable
(100–120ms); hover lift in Instrument (tens/day → near-imperceptible, gated); plot wipe in
Instrument (occasional, `clip-path`, 260ms).

Refused, with the gate question that killed it:

- **Animating the 3s decision poll / 5s node poll** — 100+/session. "No animation. Ever."
- **Animated number count-ups on the metric figures** — function: numbers the operator is
  reading; decoration on data hinders, and the poll would restart the count every 3s.
- **Opening/closing the thinking log with a height animation** — it is a native `<details>`
  whose open state the product must preserve across polls; animating it adds a state machine
  for a control the parent task (`t_393cc9cc`) exists to keep stable.
- **A page-level scroll reveal through the panels** — a daily-use operational surface; scroll
  reveal fights the reader.
- **Log rows sliding in** — high frequency (a busy fleet can add several a second); motion on
  a stream is latency.
- **A shared-element transition when the picker flips variants** — flipping is a 100+/session
  action by design; the spec says the swap stays instant, and only the picker's own highlight
  animates.
