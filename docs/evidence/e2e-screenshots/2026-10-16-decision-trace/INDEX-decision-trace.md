# Decision-trace evidence — the AI panel rendering the reasoning behind a decision (the "thinking log")

Card **t_dddf277d** ("AI panel: render the diagnosis thinking log — ordered steps + model reply + evidence +
resolution inside the incident card"). Run id **2026-10-16-decision-trace**. Parent contract:
`docs/contracts/operator-decision.md` **§4.3** (the decision trace) and **§8.1** (the labelled fallback), landed by
card `t_e07abffc` (PR #189 → merge `6f9defe`).

## What is real here — and the one thing that is not

Everything below was captured from a **live controller process on this host**, not from a redrawn diagram:

| Layer | Real? |
| --- | --- |
| `python3 -m app.main` controller | **real** — its own temp DB, migrations 0001…**0017_decision_trace** applied |
| Node registration + real telemetry samples | **real** (`POST /v1/nodes/register`, `POST /v1/nodes/{nid}/telemetry`) |
| Fault injection | **real** — `POST /v1/projects/{pid}/demo/faults` (`oom`) |
| Detect → decide → guardrail G1–G9 → **trace capture** (`DecisionTrace`) → SQLite row → `GET /v1/operator/decisions` | **real** — including G9 (`governance.scan_c2`), which sanitized the trace before it was persisted |
| Execution + audit | **real** — the LXD REST call goes to a **stub LXD** (`chaos/lxd_stub.py`, JSONL log here as `live/lxd-commands.jsonl`), so the audit row is a real audit row with a real (failing) result |
| Dashboard bundle + browser | **real** — the committed `dashboard/` bundle served by `python3 -m http.server`, rendered in Chrome driven over CDP |
| **The model endpoint** | **STUB** — `tools/stub_model.py` answers `POST /v1/chat/completions`. This host has no fleet, no GPU and no managed-model credential in this window, so the model is the single thing that could not be real. Its answer is derived from the request the controller sent, and every request is logged verbatim in `live/stub-model-requests.jsonl`. Everything downstream of the stub is the production path. |
| **The two `capture-09/10` shots** | **offline fixture** — the dashboard's own zero-backend demo (`?offline=1`, `dashboard/js/offline.js`), labelled as such and used only for the two states the live run did not produce (a clean raw reply, and a guardrail-rejected fallback). Nothing else in this directory is offline. |

No credential is in any URL, sidecar or JSON here: the project token is a run-local fixture that lives only in the
work directory (`0600`, never printed, never written into this directory) and, in the browser, in that tab's
`sessionStorage` — the app's own contract (`dashboard/js/main.js`, `TOKEN_KEY`). The dashboard URL in the sidecars
carries `apiBase` + `projectId` only.

## The three live states, from one scripted pass

`tools/run-live-incident.sh` drives three phases against one DB and one node, then serves the dashboard and parks:

| # | Phase | `decision_source` | `incident_generated` | `decided_by` | trace | Raw API body |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | stub model answers | `model` | `"model"` | `"model"` | **11 steps**, reply step `rejected` (C2) | `live/01-decisions-model-mode.json` |
| 3 | model endpoint killed, fault re-injected | `fallback` | `"fallback"` (§8.1 controller-built record) | `"fallback"` | **8 steps** | `live/02-decisions-fallback.json` |
| 4 | controller restarted with `TERMINI_OPERATOR_DECISION_MODE=deterministic` | `deterministic` | `null` | — | **`NULL` — no trace column at all** (§8.1) | `live/03-decisions-deterministic.json` |

`live/decisions-all.json`, `live/audits-all.json` and `live/events-all.json` are the raw
`GET /v1/operator/{decisions,audits,events}?limit=50` bodies the panel joins client-side — i.e. **the payload the
captures were rendered from**, unedited, with `decision_trace` already parsed to an object by the API (§11).
The trace rides in on that one response: the panel makes no second request and hits no second endpoint for it.

Run facts: node `01M53A3CQ3PPC0QG0EEMEK48SJ`; decisions `01M53A3F4GGWMSC5HV78T2ATDR` (model),
`01M53A7RD5PXBKB7XMV72PHY9J` (fallback), `01M53AC3CFPHRM70DZCS1F2KTP` (deterministic); the panel's own count line
read `3 shown / 3 decisions · model 1 · fallback 1 · incident records 2 (AI 1 · controller-generated 1) ·
thinking logs 2 (model 1 · fallback 1)`. Both live audits are **real failures** (`error`,
`operation still pending after 120.0s`) because the stub LXD never completes the operation — kept deliberately: it is
the case where the record and the reasoning must *not* imply an outcome, and the card renders `decided` /
`executed` as two distinct blocks with the trace's own `executed` step carrying `state=rejected`.

## Captures

Chrome over CDP against the live stack (window 1500×1600). Each PNG is a CDP `Page.captureScreenshot` of the panel's
own scroll box (or of the card region inside it) at `scale: 2`, so the image is legible pixel-for-pixel. Each PNG has
a `.txt` sidecar (`innerText` — what a reader actually sees at that moment, so the collapsed and expanded sidecars
differ) and, for the two incident cards, a `.html` sidecar (the card's full `outerHTML`). No crop, no annotation, no
compositing, no hand-made images.

| # | Step (acceptance) | What it proves | File | sha256 | Sidecar |
| --- | --- | --- | --- | --- | --- |
| 01 | The panel as a whole | the panel head, the deterministic row (`INCIDENT RECORD · no incident record (deterministic mode)` and `THINKING LOG · no decision trace (deterministic mode writes none — §8.1)`) and the count line `… thinking logs 2 (model 1 · fallback 1)`. For this one shot the list's height was set to 240 px so the count line fits beside it. | `live/capture-01-panel.png` | `1ee86c78865352e02e083f0a87aba00e0d65eae7c9f367ae92fad8f08aef972e` | `live/capture-01-panel.txt` |
| 02 | **Model** incident card, log **COLLAPSED** (REQUIRED WORK 2/3: the log is secondary, the conclusion stays primary) | title / explanation / evidence / decided / executed intact, the `AI THINKING LOG` summary line reading `thinking log · 11 steps · decided_by "model" · trace_version 1 · raw reply withheld (C2) — not executed`, and — outside the disclosure, so closing the log cannot hide them — `NOT OK reply_received (rejected) · gates (dropped) · executed (rejected)` and `REPLY WITHHELD … was NOT executed` | `live/capture-02-model-card-collapsed.png` | `84fbda439e8e04ce5d3ac4b2c02fbe24b7bad5456e9cbcaed7c3168eab21b574` | `live/capture-02-model-card-collapsed.txt` |
| 03 | **Model** incident card, log **EXPANDED**, first steps | the ordered steps 1…7 with a state chip each (`threshold_fired`, `evidence_window`, `prompt_built`, `model_call`, `reply_received` **REJECTED**, `gates`, `gates`), the `[dropped: C2]` marker in its own box, and the two `gates` decisions recorded by the controller (G7 accept, G9 accept, then `G9 trace detail decision=drop`) | `live/capture-03-model-card-expanded.png` | `0b1a51e83486f976ca92c01f0047a646b1caadb92705e83c546cec21bf369557` | `live/capture-03-model-card-expanded.txt`, `live/capture-03-model-card.html` |
| 04 | **Model** card, log EXPANDED, last steps + the raw reply block | steps 8…11 (`gates DROPPED`, `action_decided`, `executed REJECTED`, `audit_recorded`) and the **`MODEL OUTPUT — NOT EXECUTED · TEXT WITHHELD BY THE C2 GUARDRAIL`** block — dashed, purple, chipped `NOT EXECUTED` — visibly *not* the solid `EXECUTED · what the fleet answered` block above it | `live/capture-04-model-card-reply.png` | `eaeb1a3fb7b00d0c51478a4fa4c6f423f8f328b886e528a6b9ae87129a01d767` | `live/capture-04-model-card-reply.txt` |
| 05 | **Fallback** card, log **COLLAPSED** | the amber `CONTROLLER-GENERATED · MODEL UNAVAILABLE` record, and outside the disclosure the `FALLBACK` line `the model did not answer — the controller decided: fallback reason inference_unavailable · no guardrail gate rejected the model's answer (the model call itself failed)` plus `NOT OK reply_received (skipped) · executed (rejected)` | `live/capture-05-fallback-card-collapsed.png` | `57de58d36f57714d73e6c55fcad36f09e677d27346963d7845de0f1b967159ea` | `live/capture-05-fallback-card-collapsed.txt` |
| 06 | **Fallback** card, log **EXPANDED** | the `CONTROLLER THINKING LOG · FALLBACK` badge (`decided_by "fallback"`, never the model's), the ordered 8 steps ending `action_decided … decided_by=fallback reason=inference_unavailable action_kind=instance.restart` (state `FALLBACK`) | `live/capture-06-fallback-card-expanded.png` | `1b056bb3d528b206e6e80efa6581c43ad4ce2b5eb377285395ef87d9e6d3b77d` | `live/capture-06-fallback-card-expanded.txt`, `live/capture-06-fallback-card.html` |
| 07 | **Fallback** card, the model-output block | `MODEL OUTPUT — NOT EXECUTED (THE TRACE RETAINED NO REPLY TEXT)`, chipped `SKIPPED`, plus the note `this reply did not become the decision — the controller's fallback did (decided_by=fallback)` — no reply text is invented for a trace that retained none | `live/capture-07-fallback-card-reply.png` | `edae73925aeb5520d380c1a55f8a0781ea4d3773f3c40052101eb34979eab570` | `live/capture-07-fallback-card-reply.txt` |
| 08 | **Deterministic** row | `no incident record (deterministic mode)` **and** `no decision trace (deterministic mode writes none — §8.1)` — two statements, never an empty timeline asserting a trace nobody wrote | `live/capture-08-deterministic-row.png` | `6101a8546fff5876d3b1885619a15eeafb749d3cd5bd484273276c15d56935ff` | `live/capture-08-deterministic-row.txt` |
| 09 | **Offline fixture** (labelled): a *clean* raw reply | `MODEL OUTPUT — NOT EXECUTED (RAW REPLY, `reply_received` STEP)` with the model's JSON reply rendered in full, chipped `OK` + `NOT EXECUTED`. The live model run's reply could not serve this: it was withheld by C2 (see below), so this shot comes from the dashboard's offline demo, not from the controller. | `live/capture-09-offline-model-reply-block.png` | `95407c2457faa3bfac074bc274ee07c39e29adfb74c84942f9e03504d2c9ba77` | `live/capture-09-offline-model-reply.txt`, `live/capture-09-offline-model-card.txt` |
| 10 | **Offline fixture** (labelled): a fallback whose gate rejected the model | `fallback reason low_confidence · the gate that rejected the model's answer: G6`, the step detail `decided_by=fallback reason=low_confidence action_kind=image.prune rejecting_gate=G6` (state `FALLBACK`), and the raw reply that was **not** executed in its own block | `live/capture-10-offline-fallback-gate.png` | `0c79ff02e86e6cc6e0e6661206fc5f1ad49da1f593a9198409c79c2ec63d7faf` | `live/capture-10-offline-fallback-card.txt` |

Sidecar note: `.txt` sidecars are `innerText`, i.e. the text as rendered — collapsed captures therefore contain the
summary line and the alerts but not the step list, and expanded captures contain the steps. `.html` sidecars carry
the card's whole DOM regardless of the disclosure state.

## What the run itself demonstrated (honest observations, not defects of this card)

1. **The live model reply was withheld by the C2 guardrail.** `live/01-decisions-model-mode.json` shows the
   `reply_received` step with `state: "rejected"` and `detail: "[dropped: C2]"`, plus a `gates` step recording
   `rule=G9 field=trace.reply_received.detail decision=drop (C2 signature in model reply: : phone-number signature)`.
   The cause is not a dashboard artefact: the stub's answer quotes the raw byte counts it was given
   (`ram_used_bytes: 66657892433` — 11 digits, as `live/01-decisions-model-mode.json` records it in the incident
   evidence), which `scan_c2` reads as a phone number, so the **reply text** is replaced by the marker — while the
   incident record itself passed G9 (its evidence values are numbers, not strings). The panel is built for exactly
   this: the step is rendered with a `REJECTED` chip, it is listed in the `NOT OK` line OUTSIDE the collapsed log, and
   the reply block says the text was withheld and was not executed. `live/stub-model-requests.jsonl` holds the
   request/answer pair.
2. **The fallback trace's `model_call` step says "model answered" with `state=ok`** even though the endpoint was dead
   (phase 3 killed the stub): `endpoint=local model=unknown answered_by=unknown latency_ms=19`. The trace's truth is
   still carried by the next two steps (`reply_received` `skipped`, "no raw reply text retained", then
   `action_decided` `fallback`), and the panel renders them faithfully; but the step label is imprecise for a failed
   call. This is the controller's trace text, not the panel's — reported on card `t_e07abffc` rather than changed here
   (this card must not touch the controller).
3. **Both live audits are `error`** (`operation still pending after 120.0s`, `rejected` on the `executed` step): the
   stub LXD never completes the operation. Kept on purpose — it is the case where the panel must show what the fleet
   answered rather than assume success, and where the trace's own `executed` step is not silent about it.

## Harness provenance

`tools/` here is a **byte-identical copy** of the harness committed with the incident-card evidence
(`docs/evidence/e2e-screenshots/2026-10-16-incident-card/tools/`), so this capture is reproducible offline:

| file | sha256 (both copies) |
| --- | --- |
| `tools/run-live-incident.sh` | `e1db0fe8f7f737d692aa2282d0e87c17770c7c7beba84dcbf61e409d20731fca` |
| `tools/stub_model.py` | `73f57a2b38eb600497994b6f18912e01a413131e821db021d1810e42b44ef784` |
| `tools/node_setup.py` | `fcd6548c719e54103ec78a4e42044b606450edf49040ef420369edf7695914f8` |
| `tools/wait_decision.py` | `b528c6b645b6ca96c0537033032965977152e4758bb340dab2dce2bfa799815c` |
| `tools/wait_fault_clear.py` | `ad01daa713ae9274c9ea70f495147e5b74fea2540998f56ce2d86244826fedff` |

The copy exists because the harness resolves the repo root from its own location
(`"$(dirname "${BASH_SOURCE[0]}")/../../../../.."`), so the run works from this directory unchanged as well.

## Re-running

```bash
# three phases + dashboard on a second origin, then park (prints the dashboard URL; KEEP=1 disarms teardown)
KEEP=1 bash docs/evidence/e2e-screenshots/2026-10-16-decision-trace/tools/run-live-incident.sh \
  docs/evidence/e2e-screenshots/2026-10-16-decision-trace /tmp/termini-trace-run

# the browser half: load the printed URL (no credential in it), put the run's project token into
# sessionStorage under "termini.dashboard.token", then screenshot the panel's scroll box over CDP.
# The panel polls every 5 s (config `pollMs`); the capture cancelled the tab's interval timers so the
# disclosure state set for a shot survives it (the data already rendered is untouched — SSE is not a timer).
```

Capture helpers used here, all over CDP from the browser session: `Page.captureScreenshot` with a clip in **page**
coordinates (element `getBoundingClientRect()` + `window.scrollY`, intersected with the scroll box), `scale: 2`, and
`Runtime.evaluate` for the `.txt` / `.html` sidecars.
