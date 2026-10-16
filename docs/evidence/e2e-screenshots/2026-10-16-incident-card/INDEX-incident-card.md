# Incident-card evidence — the AI panel rendering a real incident (title / explanation / evidence / actions + audit result)

Card **t_ee3f18df** ("Dashboard: render an incident card … in the AI panel", REQUIRED WORK 2.4 of
`t_957956e5`). Run id **2026-10-16-incident-card**. Parent contract: `docs/contracts/operator-decision.md`
§4.2 (the typed incident record) and §8.1 (the labelled fallback record).

## What is real here — and the one thing that is not

Everything below was captured from a **live controller process on this host**, not from the dashboard's
offline fixtures and not from a redrawn diagram:

| Layer | Real? |
| --- | --- |
| `python3 -m app.main` controller | **real** — its own temp DB, migrations 0001…**0016_incident_record** applied |
| Node registration + a real first telemetry sample | **real** (`POST /v1/nodes/register`, `POST /v1/nodes/{nid}/telemetry`) |
| Fault injection | **real** — `POST /v1/projects/{pid}/demo/faults` (`oom`, the scenario this host's `oom-guard-01` rule fires on), which writes samples through the same store path a node's telemetry uses |
| Detection → decision → guardrail G1–G9 → SQLite row → `GET /v1/operator/decisions` | **real** — including guardrail G9, which is what validated the model-shaped record before it was persisted |
| Execution + audit | **real** — the LXD REST call goes to a **stub LXD** (`chaos/lxd_stub.py`, its own JSONL log is here as `live/lxd-commands.jsonl`), so the audit row is a real audit row with a real (failing) result |
| Dashboard bundle + browser | **real** — the committed `dashboard/` bundle served by `python3 -m http.server`, rendered in Chrome driven over CDP |
| **The model endpoint** | **STUB** — `tools/stub_model.py` answers `POST /v1/chat/completions`. This host has no fleet, no GPU and no managed-model credential in this window, so the single thing that could not be real is the model. Its answer is **derived from the request the controller sent** (the rendered §3 `DecisionInput`: the fired rule, `state`, `target_resource.instances[]`), and every request it received is logged verbatim in `live/stub-model-requests.jsonl`, so the prompt/answer pair is part of the evidence rather than a claim. Everything downstream of the stub — adapter parse, G9, persistence, API, UI — is the production path. |

No credential is in any URL, any sidecar or any JSON here: the project token is a run-local fixture that
lives only in the work directory (`0600`, never printed) and, in the browser, in that tab's
`sessionStorage` — the app's own contract (`dashboard/js/main.js`, `TOKEN_KEY`). The dashboard URL in the
sidecars carries `apiBase` + `projectId` only.

## The three states, all from one scripted pass

`tools/run-live-incident.sh` drives three phases against one DB and one node, then starts the dashboard
and parks:

| # | Phase | Decision source | `incident_generated` | Where |
| --- | --- | --- | --- | --- |
| 2 | stub model answers | `model` | `"model"` | `live/01-decisions-model-mode.json` |
| 3 | model endpoint killed, fault re-injected | `fallback` (`inference_unavailable`) | `"fallback"` (§8.1 controller-built record) | `live/02-decisions-fallback.json` |
| 4 | controller restarted with `TERMINI_OPERATOR_DECISION_MODE=deterministic` | `deterministic` | `null` — **no record at all**, all five incident columns `NULL` | `live/03-decisions-deterministic.json` |

`live/decisions-all.json`, `live/audits-all.json` and `live/events-all.json` are the raw
`GET /v1/operator/{decisions,audits,events}?limit=50` bodies the panel joins client-side — i.e. **the
underlying API payload** behind every pixel below, unedited.

Run facts: node `01M53499XAB360H6XQ325RJ9T2`; decisions `01M5349C2X88AW4GCNAH9QDF7J` (model),
`01M534DP8XZD6792QQEWVP9A0D` (fallback), `01M534HZYMRRJBZD6YSAS6R8S4` (deterministic); the panel's own
count line read `3 shown / 3 decisions · model 1 · fallback 1 · incident records 2 (AI 1 · controller-generated 1)`.
The model decision's audit row is a **real failure** (`error`, `operation still pending after 120.0s`,
`idempotency_key 01M53499XAB360H6XQ325RJ9T2:oom-guard-01:instance.restart:agent-c1:1`,
`audit 01M534D1PEPZP9QZZ1TFSRXE8B`, `lxd_endpoint http://127.0.0.1:39659`) because the stub LXD never
completes the operation — deliberately kept: it is the case where the record must *not* imply an outcome,
and the card renders "decided" and "executed" as two distinct blocks.

## Captures

Chrome driven over CDP against the live stack (window 1500×1600, `deviceScaleFactor` 1.25; the panel
captures are CDP `Page.captureScreenshot` clips of the panel's own scroll box at `scale: 2`, so the
image is legible pixel-for-pixel). Each PNG has a `.txt` sidecar with the DOM text read back at capture
time. No crop, no annotation, no compositing, no hand-made images.

| # | Step (acceptance) | What it proves | File | sha256 | Sidecar |
| --- | --- | --- | --- | --- | --- |
| 01 | Model incident card, in full (acceptance: title / explanation / evidence / action + audit result) | `AI-GENERATED INCIDENT · incident_generated: "model" · AI model`; title `Memory exhaustion on node 01M53499XAB360H6XQ325RJ9T2`; the model's own explanation; **EVIDENCE · 4 telemetry fields quoted** (`ram_used_bytes = 62.0 GiB seq 3 19:51:08`, `ram_total_bytes = 64.0 GiB`, `cpu_util_pct = 92.0%`, `lxd_instances.ram_used_bytes = 3.40 GiB`); **DECIDED · 1 action named in the record (a decision, not an outcome)**; **EXECUTED · what the fleet answered** — `ERROR … PUT /1.0/instances/agent-c1/state`, `idempotency_key …`, `operation still pending after 120.0s`, `executed 19:51:09 · audit 01M534D1PE…`; and below the card the row's metric/threshold/rationale with links to the audit record and the firing telemetry sample | `live/capture-05-model-card-full.png` | `58d3d6bfa2011ef43257ae1c141378b06b8666aa07763e9d0da95ea3df30aca0` | `live/capture-05-model-card-full.txt` |
| 02 | Fallback record is **labelled**, never rendered as model output (acceptance 3) | the amber `CONTROLLER-GENERATED · MODEL UNAVAILABLE` badge with `incident_generated: "fallback"` and the raw column value beside it, `oom on node 01M53499XAB360H6XQ325RJ9T2`, `model unavailable (inference_unavailable); …`, its own (smaller) evidence list built from the fired event, `DECIDED 1 action`, and the joined audit row for that decision | `live/capture-04-fallback-card-full.png` | `a988dddf737faa6b00da25320048847f547c179dff72b9d8fd3cabcabefcc6fe` | `live/capture-04-fallback-card-full.txt` |
| 03 | No record ⇒ say so, never a blank card (acceptance 4) | the deterministic-mode row: `INCIDENT RECORD · no incident record (deterministic mode)`, above the labelled fallback card and the model card — one list, three provably different states | `live/capture-01-panel-top.png` | `4771494393f544050c4c9f87289d28c6bc2709d317d8c2a89d7374d8a693e0f1` | `live/capture-01-panel-top.txt` |
| 04 | The log stream is the **secondary** view (acceptance 2) | `#log-panel` is a native `<details>` with `open=false` on load, summarised `Live AI-operator log — SECONDARY — RAW LOG LINES / collapsed by default; expand for the unparsed operator stream`; the incident cards are the primary surface above it | `live/capture-06-log-secondary-collapsed.png` | `f57fb1b385058960efeb4deb861c87fc90b5a61587b01acf99d8363553288b00` | `live/capture-06-log-secondary-collapsed.txt` |

## Two harness defects found while producing this evidence (fixed here, in scope)

Both were in the evidence harness, not in the dashboard, and both **silently produced a wrong record with
a green check** — which is exactly why they are written down rather than quietly re-run:

1. **Interleaved injections.** Phases 2–4 share one node and the injector writes that node's telemetry
   while an injection is open. Phase 3's injection started 3 s after phase 2's, so the stored series
   alternated between the two scenarios and `latency-01` (needs 3 consecutive samples) never fired —
   twice. Fixed by waiting for the previous injection to clear *and* for the fired rule to re-arm
   (`tools/wait_fault_clear.py` + `settle_for_rearm`), and by using the scenario this host's rule
   actually fires on.
2. **`$!` captured a wrapper, not the controller.** `( cd … && env … python3 -m app.main & echo $! )`
   backgrounds the whole `cd && …` chain, so `stop_controller` killed nothing, the old controller kept
   the port, the replacement died of `EADDRINUSE`, the readiness probe was answered by the **old**
   controller, and phase 4 recorded a `fallback` row while claiming to be deterministic. Fixed with
   `( cd … && exec env … python3 -m app.main ) &` plus a port-free wait and a probe that also requires
   the process it started to still be alive. The deterministic row in this evidence is from the
   controller that actually ran in deterministic mode (`controller-deterministic.log`,
   `listening on 127.0.0.1:42199`).

## Re-running

```bash
# three phases + dashboard on a second origin, then park (prints the dashboard URL; KEEP=1 disarms teardown)
KEEP=1 bash docs/evidence/e2e-screenshots/2026-10-16-incident-card/tools/run-live-incident.sh \
  docs/evidence/e2e-screenshots/2026-10-16-incident-card /tmp/termini-incident-run

# the browser half: load the printed URL (no credential in it), put the run's project token into
# sessionStorage under "termini.dashboard.token", and screenshot the panel's scroll box over CDP
```

`tools/` holds the whole capture path: `run-live-incident.sh` (stack + three phases + dumps + dashboard),
`node_setup.py` (register one node and give it a real sample), `stub_model.py` (the one stub — with
`_safe_number()`, added here because a raw float like `96.99999999866122` trips the C2 phone/card
signature and turns a perfectly good answer into a `schema_violation` fallback), `wait_decision.py`
(wait for the row and keep the raw API body), `wait_fault_clear.py`.
