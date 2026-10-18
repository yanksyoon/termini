# UI demo walkthrough — Incidents

What this is: the per-element companion to `docs/ui-demo-walkthrough.md` (the Fleet half)
and `docs/demo-script.md` (the timed narration). It answers, for every element on the
**Incidents** destination: what is it, what do I click, why is the number there, and where
else does the same fact show up. Bullets, not prose; written to skim backstage, not to read
aloud. The Fleet destination's elements (node rows, charts, agents, actions) are covered in
`docs/ui-demo-walkthrough.md` and are not repeated here.

Captured against `origin/main` @ `b156932` (includes the row-click fix `6de7a52` and its
hit-test follow-up `90617b0`), served locally at **1440×900**, dark, pointed at the live
controller (`https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB`,
`?synthetic=1&dest=incidents&decisionLimit=200`). Screenshots in
`dashboard/evidence/ui-demo/` (numbered below). The click is **proved live** on the merged
tree (§6).

**Deployed since (2026-10-18, orchestrator):** Firebase Hosting was redeployed from
`origin/main` @ `e523f62` after this capture, so the served `proto/variants/hig.js`
(`e04a7ba6…`) now carries `6de7a52` + `90617b0`. The click was re-proved **on the deployed
page** (Chromium, cache disabled, `?synthetic=1&dest=incidents`): clicking a row that was not
the newest — `01M56PJBF65WS2DHCYGSBT278K` — moved `aria-selected` to that row and rendered its
own record in the pane ("Host memory exhaustion due to runaway instance memory usage", six
blocks), and the pane still held the same record 10 s later across three 3 s polls (no
duplicate parts, no blank pane). The controller half of this capture is live as well: the
deployed controller ref `4bca67b` contains the fallback-evidence fix `4928eea`, and the
installed `/opt/termini/controller/app/operator/decision.py` carries
`_fallback_observed_value` (unit restarted 22:22 UTC) — see the corrected contradiction
below.

Each element below: **Shows** / **Do** / **Why** / **From → reappears**.

---

## 1. The list row

**Shows** — one row per operator decision, newest first: the time, a source badge, the node
label (with `(demo)` on synthetic rows), the rule id, `→ action target`, and the result
badge. The source badge is the decider, and its three shapes are deliberately distinct:

- `AI MODEL 0.90` / `0.95` — a **model** decision, carrying the model's `confidence`
  (`dashboard/js/ui/decisions.js` renders `confidence` only when `decision_source === "model"`).
- `FALLBACK` (uppercase, no number) — the model did **not** decide; the deterministic playbook
  did (`operator-decision.md` §8.1). Never styled like a model decision.
- `DETERMINISTIC` — playbook-only, no model call at all.

Case note: the HIG sheet renders every badge in lowercase (a `text-transform: lowercase` design
rule in `proto/variants/hig.css`), so on screen these read `ai model 0.90`, `fallback`,
`deterministic` — the words are the same, the case is the HIG style, not a different value.

The row's `→ action target` names the resolved LXD instance (e.g. `instance.stop demo-workload`),
and the result badge is `ok · 10.10.0.11:8443` (or `error · <endpoint>`): the transport outcome
from the joined audit row.

**Do** — read the source badge out loud first, then the node label, then the rule → action →
result. Point out the `(demo)` flag on `demo-node-a`/`demo-node-c` (synthetic seeder rows) versus
a hash-named real node (e.g. `ba1f1faeacb4501aed12cf8ea1e5713b`), which carries no `(demo)`.

**Why** — a row is the answer to "what did the operator do, and did the fleet accept it". Each
field proves one fact and only one:

- **`ok` proves the node's LXD accepted the call** — `PUT /1.0/instances/<name>/state` returned
  an operation that reached Success *and* the instance state was read back to confirm it
  (`controller/app/operator/executor.py` `_await_write` / `_confirm_state`). It does **not**
  prove the model did anything: a `FALLBACK` row is `ok` exactly as often, because the playbook
  runs through the same executor.
- **`error` names the endpoint that refused** — the result badge prints `error · <lxd_endpoint>`
  so a refused node is attributable, not just "failed".
- The `confidence` only appears on a model row; a fallback has no confidence because the model
  produced nothing to be confident in.

**From → reappears** — one row is a client-side join of three endpoints
(`dashboard/js/core/decisions.js` `buildDecisionRows`): `GET /v1/operator/decisions` (the
decision + its incident record + its thinking log), `GET /v1/operator/events` (the breach), and
`GET /v1/operator/audits` (the executed result). A decision has **no audit id of its own**; the
audit row points back with `decision_id`, and the breach lives on the health event the decision
was made for.

---

## 2. The filters and the header count

**Shows** — four chips `All · AI model · fallback · deterministic`, and a header count
(`200 decisions · 10 by fallback` in the bar; the full breakdown is inside the disclosure, §4).

**Do** — click each filter and say what the resulting list proves:

- `AI model` — the decisions the model actually made. On the live fleet this is the dominant
  source; a `model` row is the "detect → decide → execute" story with the AI in the loop.
- `fallback` — the moments the model was in the loop but **did not answer** (`parse_error`,
  `schema_violation`, `inference_unavailable`…). This is the honesty surface: a fallback is a
  decision made by the deterministic playbook, labelled, never passed off as the AI.
- `deterministic` — rows recorded while the controller ran playbook-only (no model call). They
  carry no incident record and no thinking log (§8.1).
- `All` — the unfiltered stream, which is the honest picture of a mixed decider.

**Why** — the filter is the "which decider" answer without opening a record. The header count
`N decisions · M by fallback` is computed from `decision_source` on the fetched window, so the
presenter can state the model/fallback mix without counting rows by hand.

**From → reappears** — the same `decision_source` field that drives the badge drives the filter
and the count; all three are one column (`remediation_decisions.decision_source`,
`operator-decision.md` §11).

---

## 3. The record pane, block by block

Clicking a row (or the auto-selected newest row) loads that decision's full record into the
right-hand pane. The blocks, top to bottom:

**Incident card head.** The provenance badge plus the literal column value:
`AI-GENERATED INCIDENT` / `incident_generated: "model"` for a model record, or
`CONTROLLER-GENERATED · MODEL UNAVAILABLE` / `incident_generated: "fallback"` for the §8.1
fallback record. Then the title and the explanation — the model's own words, with byte-magnitude
integers thousands-grouped (`"8,070,137,774 bytes"`), which is the C2-safe grouped form the
prompt authored (`operator-decision.md` §4.2/§6.1). `generated` is `"model"` only when the text
is the model's validated output; a fallback record is the controller's build from the fired
event, and it says so on the badge.

**Thinking log** (collapsed disclosure). `decided_by "model" | "fallback"`, `trace_version 1`,
the step count, and the honest marker about the raw reply: `raw reply recorded — not executed`,
or `raw reply withheld (C2) — not executed`. Expand for the ordered §4.3 steps
(`threshold_fired → evidence_window → prompt_built → model_call → reply_received → gates →
action_decided → executed → audit_recorded`), each with a state chip. The raw model reply is
shown in its own block labelled **`model output — not executed`** — it is what the model said,
never the action that ran (the executed block is the separate truth).

**Evidence.** `N telemetry fields quoted` — the C0 field names and values the record is checkable
against. Model records quote dotted paths (`ram_used_bytes`, `lxd_instances.ram_used_bytes`,
`disk.write_latency_ms`, `pressure.io_some_avg10`, …) with their sample `seq`; the fallback
record quotes only the fired event's own C0 fields (`operator-decision.md` §4.2/§8.1).

**Decided.** `N action(s) named in the record (a decision, not an outcome)` — the `action_kind`
and `target` the record names. On a model record the target is a `vm_id`
(`NNB9287WRHGFDPEZ995TKCXDNW`); the executed block is where the outcome lives.

**Executed.** `what the fleet answered (remediation_audits, joined by decision id)`: the result
(`ok`/`error`), the LXD endpoint, the exact call `PUT /1.0/instances/demo-workload/state`, the
idempotency key (`<node_id>:<rule_id>:<action_kind>:<target>:<episode>`), the terminal operation
state (`operation Success …; action=restart; state Running -> Running`), the audit id, and an
`Ask AI about this audit row` link.

**Breach line.** `metric = value`, the threshold rule, and `fired <time>`. For OOM this reads
`ram_used_bytes RAM used = 97.00% of ram_total_bytes` against `>= 95% of ram_total_bytes for 2
consecutive samples`. The threshold text and the consecutive-sample count are the frozen rules
(`controller/app/operator/detect.py`, `node-telemetry.md` §5).

**Rationale.** `model rationale` (the model's one-line reason) or `playbook rationale` /
`fallback reason <code> — …` (the deterministic playbook's, with the fallback code when the
model did not answer).

**Why** — the card's contract is "answers the four questions the log stream cannot": what
happened (title), why (explanation), on what evidence (the quoted fields), what was decided
(decided) versus what the fleet answered (executed). The provenance badge and `decided_by` make
it impossible to read controller text as model output.

---

## 4. The cross-links and the "How this list is built" disclosure

- **`audit <id>`** — opens a dialog for that audit row (decision id, health event, node, rule,
  action, result, executed at, endpoint, the LXD call, idempotency key, dry-run, detail, and an
  `Ask AI about this audit row` action). Join key: `decision_id`.
- **`telemetry sample`** — opens a dialog for the health event the decision was made on: the
  metric, value at trigger, threshold, fired-at, breach-started, sample `seq` range, and the
  firing sample JSON (the actual telemetry the decision read). Join key: `event_id`.
- **`show on chart`** — runs the shipped chart handler: reveals `?include=synthetic`, sets the
  `15m` window, selects the decision's node, and scrolls to that metric's chart card, where the
  decision marker (coloured by source) is drawn and, where the threshold resolves against a
  plotted series, the threshold line (`thresholdMarker`). Join key: `node_id` + `metric`
  (`annotationsForNode`). See "Contradictions found" for what it does **not** do in this build.
- **`Ask AI`** — seeds the assistant with `{kind, id}` and the decision id; the controller
  resolves the id (`incident` → health-event id, `decision` → decision id, `audit` → audit id).
  The chip in the chat shows the attached context, and the reply's citations jump back to the row.
- **`How this list is built — decider, join keys and counts`** — a disclosure holding the decider
  strip, the join note, and the count. The decider strip is parsed from the operator log's
  `externalModelCall` line and states **who answered** (`decider: forwarded managed model ·
  gemini-2.5-flash`, `backend=gemini-proxy`, `answered_by=forwarded-managed-model`,
  `destination=aiplatform.googleapis.com`) — never "local GPU". The count is the full breakdown
  (`200 shown / 200 decisions · model 139 · fallback 10 · incident records 149 (AI 139 ·
  controller-generated 10) · thinking logs 149 (model 139 · fallback 10)`).

---

## 5. The story to say out loud

One sentence per step, each pointing at the block on screen:

1. **A threshold fired** — the breach line (`ram_used_bytes = 97.00% of ram_total_bytes`,
   `fired …`). The detector (`detect.py`) raised it after `2 consecutive samples`.
2. **The model decided** — the `AI MODEL 0.95` badge on the row, and the thinking log's
   `decided_by "model"` / `model_call` step (`endpoint=external model=gemini-2.5-flash
   answered_by=forwarded-managed-model latency_ms=…`).
3. **The controller gated it** — the `gates` steps (`G7 rationale`, `G9 incident record`
   `decision=accept`); when the model fails, the `fallback` alert names the reason and the gate.
4. **The executor acted** — the executed block: `PUT /1.0/instances/demo-workload/state`, the
   idempotency key, `ok · 10.10.0.11:8443`, the terminal operation state.
5. **It was audited** — the audit id in the executed block and the `audit_recorded` step
   (`audit_id=… decision_id=…`).

The contrast worth one sentence: the same `ok` appears on a `FALLBACK` row, because the playbook
and the model share one executor — the badge, not the result, is what tells you who decided.

---

## 6. The interaction (the demo moment)

Clicking a decision row loads **that** record in the pane. Verified live: the auto-selected row
was `01M55WARSE…` (`Disk I/O degradation on /dev/sda`); a real mouse click on row 2 moved the
selection to `01M55W9J9J…` and the pane to `Host memory exhaustion from a single runaway
instance` — `aria-selected` moved to exactly one row, and the pane rendered the clicked record's
card, thinking log, evidence, decided, executed, breach, rationale and links (the same
`selection_check.py` assertions the parent card ran also cover focus-follows and no-duplicate).
Fixed by `6de7a52` (the row-click handler `onDecisionClick` in
`dashboard/proto/variants/hig.js`); hit-test hardening in `90617b0`. Screenshot `inc-08`.

---

## Screenshots (all 1440×900, dark)

1. `inc-01-incidents-list.png` — the destination: sidebar, the list rows (source badge, node
   `(demo)` flag, rule → action → `ok · 10.10.0.11:8443`), the four filter chips, and the pane.
   Look at: the `AI MODEL 0.90` badge versus the hash-named real node row.
2. `inc-02-how-list-is-built.png` — the `How this list is built` disclosure open: the decider
   strip, the join-key note, the full count line.
3. `inc-03-record-card.png` — the model record's card: `AI-GENERATED INCIDENT` /
   `incident_generated: "model"`, title, and the grouped-byte explanation.
4. `inc-04-evidence-decided.png` — the `evidence · 4 telemetry fields quoted` block and the
   `decided · 1 action` block.
5. `inc-05-executed.png` — the executed block: `ok · 10.10.0.11:8443`, `PUT /1.0/instances/
   demo-workload/state`, the idempotency key, the audit id.
6. `inc-06-breach-rationale-links.png` — the breach line, the model rationale, and the
   cross-links (`audit`, `telemetry sample`, `show on chart`, `Ask AI`, `decision`).
7. `inc-07-thinking-log.png` — the thinking log expanded: the ordered §4.3 steps, and the
   `model output — not executed` block holding the raw reply.
8. `inc-08-clicked-record.png` — the demo moment: a non-newest row selected, its record in the
   pane (a different decision than the auto-selected one).
9. `inc-09-fallback-record.png` — a `CONTROLLER-GENERATED · MODEL UNAVAILABLE` record: the
   fallback alert, `raw reply withheld (C2) — not executed`, and the evidence defect (§7).
10. `inc-10-deterministic-record.png` — a deterministic row's pane: `no incident record
    (deterministic mode)` and `no decision trace (deterministic mode writes none — §8.1)`.

## State at the time of capture

- `decisionLimit=200`; header `200 decisions · 10 by fallback`; count line
  `200 shown / 200 decisions · model 139 · fallback 10 · incident records 149 (AI 139 ·
  controller-generated 10) · thinking logs 149 (model 139 · fallback 10)` → 51 deterministic
  rows in the window.
- Decider strip: `forwarded managed model · gemini-2.5-flash`, `backend=gemini-proxy`,
  `answered_by=forwarded-managed-model`, `destination=aiplatform.googleapis.com`,
  `endpoint=http://127.0.0.1:8085`.
- Transport to the node was **`ok`** across the fetched window (audits: `ok`/`already_applied`,
  endpoint `https://10.10.0.11:8443`); no `error` result was in the window. Model decisions
  carried `confidence` 0.90–0.95 and `incident_generated: "model"`; the newest rows were
  **model** decisions (the model path was healthy during capture).
- The counts shift continuously (the list re-polls every 3 s and new decisions land), so treat
  the numbers above as point-in-time.

## Not verified

- **The screenshots predate the hosting deploy.** `https://termini-510609.web.app/
  proto/variants/hig.js` hashed to `a4867c3e…` (the pre-fix build, no `onDecisionClick`) at
  capture time, so these images come from a local `origin/main` bundle served against the
  live controller, exactly as the Fleet half's do. Hosting has since been redeployed from
  `e523f62`: the served `hig.js` now hashes to `e04a7ba6…` and the click was re-proved on the
  deployed page (see the note at the top).
- **The audit-row dialog** was verified from the shipped `showAuditDetail` handler and the
  sibling `telemetry sample` dialog (seen live, §4); the audit dialog itself was not pinned in a
  screenshot because the 3 s poll re-selected a different record between the probe and the click.
- **The `Fault view` toast** fired by `show on chart` was not captured (it is transient); the
  state it asserts (node selected, 15 m window, synthetic revealed) was verified after switching
  to Fleet.

## Contradictions found

- **`show on chart` does not jump to Fleet in this build.** The shipped `showOnChart` handler
  reveals `?include=synthetic`, sets the `15m` window, selects the node and scrolls to the
  metric's chart card — but it does **not** switch destinations, and in the HIG layout the charts
  live in the (hidden) Fleet destination. From the Incidents pane the visible effect is the
  state change (node/window/synthetic) that the viewer sees only after switching to Fleet by
  hand. The card's phrase "jumps to the Fleet chart" overstates the actual behaviour.
- **The fallback evidence unit defect is fixed in the deployed code, but not observed on a
  rendered fallback row.** At capture time the controller still ran a tree without the fix, and
  a fallback OOM record quoted `ram_used_bytes = 96 B` in the evidence block (the value
  `96.04…` is a *percent*, rendered with the `_bytes` unit because the field name is
  `ram_used_bytes`), while the breach line beside it correctly read
  `ram_used_bytes RAM used = 96.04% of ram_total_bytes`. Exact strings, from
  `inc-09-fallback-record.png`: evidence `ram_used_bytes = 96 B seq 1792270042`; breach
  `ram_used_bytes RAM used = 96.04% of ram_total_bytes`. Model records do **not** show this —
  they quote the observed byte count (`ram_used_bytes = 8,070,137,774`) under the same field
  name. The fix (`4928eea`) is now in the deployed controller tree (`4bca67b`; the installed
  `decision.py` carries `_fallback_observed_value`, unit restarted 22:22 UTC), but **no
  fallback decision occurred in the last 200 fetched** (all `incident_generated: "model"`), so
  the corrected rendering has not been seen on screen — `inc-09` remains pre-fix evidence.
  Re-check the first fallback record of the demo run before quoting it on stage.
- **Model records use dotted field paths; the fallback uses a bare metric name.** Model evidence
  quotes `ram_used_bytes`, `ram_total_bytes`, `lxd_instances.ram_used_bytes`,
  `disk.write_latency_ms`, `pressure.io_some_avg10` (dotted); the fallback evidence quotes the
  bare `ram_used_bytes` (the event's own metric). Both were observed live.
- **One model record showed a dropped trace step, another did not.** The auto-selected
  `Disk I/O degradation` record carried `gates (dropped): G9 trace detail` (its
  `evidence_window` step detail tripped the C2 phone-number scanner and was dropped); the
  clicked `Host memory exhaustion` record had all 10 steps `ok`. Both are honest §4.3
  behaviour — a C2 hit in a step's detail drops that step and says so.
