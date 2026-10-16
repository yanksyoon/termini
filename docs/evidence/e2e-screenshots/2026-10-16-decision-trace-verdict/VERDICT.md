# Verdict — AI panel diagnosis thinking log (§4.3 decision trace)

Independent adversarial verification, commissioned by parent card **t_e0d8077f** and
run by the **verifier** profile, on the **committed state only** (fresh detached
worktree at `origin/main` = `5e6bed9`, the panel PR #191 squash-merged on top of
`6f9defe`, the controller-trace PR #189).

Two implementation cards feed this verification; their outputs were treated as
claims, not evidence, and their own scripts/tests were not accepted as
verification. Everything below was re-run by the verifier against the committed
tree.

## Provenance

- Verification card: `t_e0d8077f` (this card).
- Owning implementation cards: `t_e07abffc` (controller trace, PR #189, merge
  `6f9defe`) and `t_dddf277d` (panel render, PR #191, merge `5e6bed9`).
- Commit verified: `5e6bed9a6b2917ae3d3f509ccf0e47030d8e7d75`
  `t_dddf277d feat: decision-trace thinking log in the AI panel (§4.3) (#191)`.
- Worktree: `git worktree add --detach <scratch>/thinklog-verify origin/main`
  (confirmed `git log -1` and `git status` clean; never the shared repo-root
  checkout, which is on worker branch `wt/t_a1b5e136-seeder-metric-expansion`).
- Files each PR added were read from the committed tree: migration
  `0017_decision_trace.sql`, `controller/app/operator/{decision,loop,store}.py`,
  `termini/decision/{types,adapter}.py`, `dashboard/js/{core,ui}/decisions.js`,
  `dashboard/tests/core.test.mjs`.

## Result table (one row per claim)

| # | Claim | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | Trace present on a **model** decision | PASS | Live E2E: row `decision_source=model`, `decision_trace.decided_by=model`, 11 steps (threshold_fired→…→executed→audit_recorded), `trace_version=1`. See §1. |
| 2 | Trace present and **labelled** on a **fallback** | PASS | Live E2E: row `decision_source=fallback`, `decided_by=fallback`, `action_decided` detail `decided_by=fallback reason=inference_unavailable action_kind=instance.restart`. See §2. |
| 3 | Deterministic row writes **no** trace and renders "no incident record" | PASS | Live E2E: `decision_trace=null`, `trace_version=null`, all incident columns null; panel DOM shows `data-trace-present=false` and text "no incident record (deterministic mode)". See §3. |
| 4 | `decided_by` names the decider; model trace carries **endpoint class + answered_by**; fallback trace carries **reason + rejecting gate** | PASS | Live model row: `model_call` detail `endpoint=local … answered_by=unknown` (field carried; honest "unknown" for a non-self-describing stub). Independent probe with a forwarded egress record: `endpoint=external model=deepseek-flash answered_by=forwarded-managed-model`. Fallback: live `reason=inference_unavailable` (transport failure → no gate rejection, correctly); gate-rejection case probed: `decided_by=fallback reason=low_confidence action_kind=none rejecting_gate=G6`. See §4. |
| 5 | Fallback path produced when the model fails deliberately | PASS | Live E2E phase 3: stub model killed, next fault → `decision_source=fallback`, `fallback_reason=inference_unavailable`. See §2. |
| 6 | Caps enforced — over-long reply truncated and says so | PASS | Controller: reply capped to 4096 + `truncated:true`; step detail capped 512; steps capped 24. Panel mapper: 30-step array → `shownSteps=24` + `truncated:true`; reply 4096 + `truncated`; label 128 / detail 512 with `clamped`. See §5. |
| 7 | No C2 in any trace field / panel render / committed capture | **PARTIAL — one FAIL** | Email + card-number planted in telemetry: **blocked** (externalModelCall fails closed before the model), appear in **no** trace/incident field (positive control: present in raw store; falsification: `add_reply`→`[dropped: C2]`, step detail→dropped). **`/proc/<pid>/cmdline`-shaped path: LEAKS** into the `reply_received` step verbatim (`state=ok`) because `scan_c2` has no path/cmdline value signature. See §6 and Finding F1. |
| 8 | Four reader questions (title, explanation, evidence, resolution) + reasoning steps answerable from the panel alone | PASS | Real-browser render against the live controller: model card shows title "Memory exhaustion…", explanation, 4 evidence rows (62.08 GiB / 64.00 GiB / 92.0% / 3.40 GiB), decided "instance.restart", executed "error … PUT /1.0/instances/agent-c1/state", and the 11-step thinking log with the withheld `[dropped: C2]` reply. Count line: "thinking logs 2 (model 1 · fallback 1)". See §7. |

COULD NOT VERIFY:

- **C2V-1 — a real model / live fleet.** The only stub in the chain is the model
  endpoint (`stub_model.py`); this host has no GPU, no fleet, and no managed-model
  credential in this window (cost guard, offline-only). The `model_call` step's
  `answered_by` was therefore verified only to `unknown` live and to
  `forwarded-managed-model` via an injected egress probe — a live
  forwarded-managed-model call is the demo card's window, not this one.

No other claim was left unverified; the two items above are the only ones that
need a live node.

## Findings

**F1 (new, reported on `t_e07abffc`) — path/`/proc/<pid>/cmdline`-shaped C2 values
leak into `reply_received`.** `governance.scan_c2` (§5.1 entity table) has no
value signature for file paths, `/proc/*/cmdline`, or `exe`/`cwd` path values,
yet `governance.md` §4.3 lists those as C2 and (line 123) promises "C2 material
never appear in [the decision_trace]". A path value in an allowed C0 field is not
caught by `external_call.assert_allowed_classes` (reuses scan_c2), so the payload
is sent, and a model that echoes it lands it verbatim in `reply_received`
(`state=ok`). Email/card values planted the same way are blocked fail-closed, so
the gap is specifically scan_c2's missing path/cmdline class. Pre-existing
(inherited by the "same G9 gate" design), does not block the demo (the committed
stub only quotes numeric fields), not fixed here (independence).

**F2 (confirmed residual, already reported by `t_dddf277d`) — `model_call` reads
"model answered" `state=ok` on a dead endpoint.** In the live transport-failure
fallback the `model_call` step carries no error field and reads ok; the truth is
recovered by `reply_received`(skipped) and `action_decided`(fallback), so nothing
is hidden, but the step label/state is misleading.

## Commands and raw output

### §1 — decisive suites (committed state)

```
$ python3 -m unittest discover -s tests -t .
... (log lines elided) ...
----------------------------------------------------------------------
Ran 937 tests in 374.341s
OK

$ node --test dashboard/tests/core.test.mjs
ℹ tests 52
ℹ pass 52
ℹ fail 0

$ python3 -m unittest tests.test_decision_trace
----------------------------------------------------------------------
Ran 14 tests in 14.235s
OK

$ shellcheck -S warning docs/evidence/e2e-screenshots/2026-10-16-decision-trace/tools/run-live-incident.sh
(no output = clean)
```

### §2 — end-to-end (committed harness, live controller + stub model + stub LXD)

```
$ KEEP=1 bash docs/evidence/e2e-screenshots/2026-10-16-decision-trace/tools/run-live-incident.sh \
    <scratch>/thinklog-e2e <scratch>/thinklog-e2e-work
```
Three phases, one controller run each, same DB. Final `GET /v1/operator/decisions`
(`decisions-all.json`), parsed by the verifier:

```
source=deterministic decision_trace=None trace_version=None
source=fallback      decided_by=fallback trace_version=1
   steps: ['threshold_fired','evidence_window','prompt_built','model_call','reply_received','action_decided','executed','audit_recorded']
   executed: rejected | audit_recorded: ok
source=model         decided_by=model trace_version=1
   steps: ['threshold_fired','evidence_window','prompt_built','model_call','reply_received','gates','gates','gates','action_decided','executed','audit_recorded']
   executed: rejected | audit_recorded: ok
```

Model row `decision_trace.steps` (the `model_call` and `reply_received` steps):

```
"model_call": "endpoint=local model=unknown answered_by=unknown latency_ms=27 request_id=01M53C0JEVTXWRW3YGCRP2QA3A"  (state ok)
"reply_received": "[dropped: C2]"  (state rejected, label "model reply received")
"gates": "rule=G9 field=trace.reply_received.detail decision=drop (C2 signature in model reply: : phone-number signature)"  (state dropped)
```
(`[dropped: C2]` is the guardrail marker — the stub's raw reply quoted an 11-digit
byte count, which `scan_c2` flags as a phone number; the panel renders this as a
withheld reply, demonstrated on real data.)

### §3 — C2 adversarial control (instrument: `tools/c2-control.py`)

Planting `adversary-plant-9f3c@example.com` + `4111111111111111` +
`/proc/31337/cmdline` into a telemetry sample's `kernel_version` via the real
store path, then running the committed `ModelDecisionProvider`:

```
POSITIVE CONTROL raw-store contains 'adversary-plant-9f3c@example.com': True
POSITIVE CONTROL raw-store contains '4111111111111111': True
POSITIVE CONTROL raw-store contains '/proc/31337/cmdline': True
decide_fn calls (must be 0 if C2 blocked pre-model): 0
result.source='fallback' fallback_reason='schema_violation'
NEGATIVE CONTROL 'adversary-plant-9f3c@example.com': in_trace=False in_incident=False
NEGATIVE CONTROL '4111111111111111': in_trace=False in_incident=False
NEGATIVE CONTROL '/proc/31337/cmdline': in_trace=False in_incident=False
scan_c2(trace) hits=[] (must be empty)
decided_by='fallback' (must be 'fallback')
--- falsification control (the scanner is NOT blind) ---
scan_c2 on the planted email literal: [': email signature']
scan_c2 on the planted card literal: [': card-number signature']
reply step carrying '...@example.com' -> detail='[dropped: C2]' state='rejected'
step detail carrying '...@example.com' -> add() returned None (dropped)
OVERALL: PASS
```

### §4 — decider field / answered_by / rejecting gate (probes)

`tools/answeredby-probe.py` (forwarded egress record injected):
```
model_call detail: endpoint=external model=deepseek-flash answered_by=forwarded-managed-model latency_ms=2 request_id=…
decided_by: model | source: model
OK: endpoint class + answered_by carried on the model trace
```

`tools/gate-probe.py` (low-confidence → G6):
```
source: fallback | fallback_reason: low_confidence
action_decided detail: decided_by=fallback reason=low_confidence action_kind=none rejecting_gate=G6
OK: fallback trace carries reason + rejecting gate
```

### §5 — caps (controller + panel)

`tools/caps-probe.py`:
```
reply len: 4096 truncated: True
detail len: 512
step count: 24
OK: controller caps enforced (detail<=512, reply<=4096+flag, steps<=24)
```
Panel mapper (`node`, committed `core/decisions.js` `traceView`):
```
array truncation -> truncated: true totalSteps: 30 shownSteps: 24
reply.truncated: true reply text len: 4096
label clamped: true label len: 128 detail len: 512
```

### §6 — the /proc-path leak (Finding F1, instrument `tools/proc-probe.py`)

Same plant but with **only** the `/proc/31337/cmdline` path (no email/card), stub
model echoes it:
```
result.source='model'
trace contains '/proc/31337/cmdline': True
reply step detail: '{"rationale": "restart; saw kernel /proc/31337/cmdline"}' state='ok'
```

### §7 — panel render against the live controller (real browser)

Browser loaded the parked dashboard (controller `127.0.0.1:34831`, dashboard
`127.0.0.1:52625`, project token in sessionStorage), polled, rendered. Extracted
DOM:

```
rowCount: 3
count: "3 shown / 3 decisions · model 1 · fallback 1 · incident records 2 (AI 1 · controller-generated 1) · thinking logs 2 (model 1 · fallback 1)"
DETERMINISTIC  tracePresent=false  "no incident record (deterministic mode)"
FALLBACK       tracePresent=true   8 steps; alert "the model did not answer — … fallback reason inference_unavailable · no guardrail gate rejected the model's answer (the model call itself failed)"
AI MODEL 0.90  tracePresent=true  11 steps; alert "reply withheld: … the trace holds the controller's marker, not the reply text, and it was NOT executed"
model card: title "Memory exhaustion on node …" · explanation "ram_used_bytes is at 97.0% …" · evidence [ram_used_bytes=62.08 GiB, ram_total_bytes=64.00 GiB, cpu_util_pct=92.0%, lxd_instances.ram_used_bytes=3.40 GiB] · decided instance.restart · executed "error 127.0.0.1:34347 PUT /1.0/instances/agent-c1/state"
```

Committed captures (panel PR) were additionally scanned with the controller's own
`scan_c2`: the only hits are false positives of applying a telemetry value-scanner
to raw files — the loopback LXD endpoint `127.0.0.1` ("raw IPv4"), 11-digit byte
counts ("phone"), and the 14-digit float percentage `96.99999999866122` ("card").
No email, credential, key-material, or real external IP signature anywhere in the
committed captures.

## Conclusion

GO with one finding. The decision trace is captured, sanitized, persisted, and
served as specified; the panel renders it against real incidents end-to-end; the
decider is labelled in-band; caps are enforced on both sides; and the C2 control
passes for every value class `scan_c2` can signature-match. The one gap is that
`scan_c2` has no path/`/proc/*/cmdline` value signature, so path-shaped C2 (the
card's third example) can reach the `reply_received` step — reported on the owning
card `t_e07abffc` and left unfixed here to preserve independence.
