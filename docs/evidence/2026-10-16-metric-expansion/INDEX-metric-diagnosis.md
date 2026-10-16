# Evidence — metric expansion in the AI diagnosis (`t_922b3268`)

The diagnosis card's offline proof: a real controller process builds the §3
`DecisionInput` from telemetry that carries the §4.12–§4.14 groups, the §7.2
`externalModelCall()` guard asserts the C0 classes, the (stubbed) model answers
and quotes a new metric, and the guardrail G9 persists an incident record whose
`evidence[]` leads with `pressure.*`, alongside the §4.3 decision trace that
names the new fields.

## What is real here — and the one thing that is not

| Layer | Real? |
| --- | --- |
| `python3 -m app.main` controller | **real** — own temp DB, migrations 0001…0017 applied |
| Node registration + 3 telemetry samples carrying the new groups AND breaching `oom-guard-01` | **real** — `POST /v1/nodes/register`, `POST /v1/nodes/{nid}/telemetry` (the store path a node uses) |
| Detection → `build_decision_input` (incl. the §3.2 `derived` deltas) → `externalModelCall()` C0 assertion → stub model → guardrail G1–G9 → SQLite row → `GET /v1/operator/decisions` | **real** |
| LXD REST call | **real** — `chaos/lxd_stub.py` (its `lxd-commands.jsonl` is here) |
| **The model endpoint** | **STUB** — `docs/evidence/e2e-screenshots/2026-10-16-incident-card/tools/stub_model.py` answers `POST /v1/chat/completions`. Its answer is derived from the request the controller sent, and every request is logged verbatim in `live/stub-model-requests.jsonl`, so the prompt/answer pair is part of the evidence, not a claim. |

The stub model was extended (same file, backward-compatible) to quote the PSI
group first: `_pressure_evidence()` emits `pressure.<name>` for the two highest
of the four PSI scalars when the request's `state` carries them, then the
existing ram evidence. A request without `pressure{}` still gets exactly the old
answer.

## Reproduce

```bash
python3 docs/evidence/2026-10-16-metric-expansion/tools/run_metric_diagnosis.py \
  docs/evidence/2026-10-16-metric-expansion
```

The tool boots the stack, registers a node, posts three breaching samples (seq
1–3) that carry every §4.12–§4.14 group, waits for the operator loop's model
decision, and writes `live/` beside this file. The controller's own DB/logs are
kept in a temp dir, not committed.

## Result (captured 2026-10-16 on this host)

The decision is `source=model`, `incident_generated="model"` (decision
`01M53BRD6KXHMB0KAY69925KVG`, node `01M53BRAZKYXAZ6WXJ4EYECXHK`).

* **The new metric is quoted first in `evidence[]`.** `live/incident-record.json`:
  ```json
  "evidence": [
    { "field": "pressure.memory_some_avg10", "value": 92.1, "seq": 2, "ts": "…" },
    { "field": "pressure.cpu_some_avg10",    "value": 12.3, "seq": 2, "ts": "…" },
    { "field": "ram_used_bytes",             "value": 66200000000, "seq": 2, "ts": "…" },
    { "field": "lxd_instances.ram_used_bytes", "value": 10000000000, "seq": 2 }
  ]
  ```
* **The new fields appear in the thinking-log trace.** `live/decision-trace.json`,
  step `evidence_window`: `C0 fields: conntrack_count, conntrack_max, …,
  mem_available_bytes, …, oom_kill_count, …, pgmajfault_count, pressure, …,
  tcp_retransmit_segs, …` — and `prompt_built` reads
  `template=operator-decision v2 … C0_assertion=ok` (the §7.2 assertion passed
  with the new fields + the `derived` object present).
* **The `derived` deltas reach the model** (from `live/stub-model-requests.jsonl`,
  the assembled request): `"derived": {"window_samples": 2, "oom_kill_count_delta": 2,
  "pgmajfault_count_delta": 306, "tcp_retransmit_segs_delta": 18}` — computed by
  the controller over the window (seq 1→2), never raw samples.

## One pre-existing quirk, stated not hidden

The trace's `reply_received` step is `[dropped: C2] state=rejected` because the
raw model reply carries 11-digit byte counts (`ram_used_bytes = 66200000000`),
which `governance.scan_c2`'s phone-number heuristic flags. This is the same
behaviour the trace card (`t_e07abffc`) recorded and is the guardrail failing
closed on a reply it cannot prove clean — the *parsed* record is unaffected, and
the incident record above persisted intact. The panel renders that state
explicitly (not shown here).

## Token delta and daily cost (measured, not estimated)

Assumption (the research card's, restated): **4 chars/token** (Gemini-class
tokenizer, conservative), **100 model diagnoses/day** (the deterministic
fallback never bills the model). Price is not asserted — the token figure is the
invariant.

| Part | delta |
| --- | --- |
| static (system prompt + few-shot, v1 → v2) | +1,782 chars ≈ **+445 tokens** |
| dynamic (`derived` + new fields, 2-sample window) | +1,435 chars ≈ **+359 tokens** |
| **total per diagnosis (captured 2-sample window)** | +3,217 chars ≈ **+804 tokens** |
| **daily, at 100 diagnoses/day** | ≈ **+80,000 tokens/day** |

The static +445 tokens is constant per call. The dynamic part scales with the
evidence window and instance count exactly as the research card's linear model
predicted — its §2 worst case (21 sample-equivalents, 6 running instances) was
≈4,600 tokens/diagnosis, which this captured 2-sample / 1-instance window
measures at ≈804. `derived` itself is 3–4 scalars (~50 chars ≈ 13 tokens),
negligible next to the per-sample field cost.
