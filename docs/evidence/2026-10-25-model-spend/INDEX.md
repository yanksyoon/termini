# Evidence — what the operator's Vertex spend was, and the two bounds added (`t_519d83a0`)

The operator loop calls a managed model (Gemini 2.5 Flash via Vertex AI) once per
decision. This pack is the measurement of what that cost, the two causes, and the
two bounds that stop it recurring. Every number below came from the provider's
own usage metrics or from Vertex's free `:countTokens` — none is an estimate from
character counts.

## 1. What was spent

`publisher/online_serving/token_count` and `model_invocation_count` for
`termini-510609`, 15 days to 2026-10-25, priced at the model's list price
($0.30 / 1M input, $2.50 / 1M output, global endpoint):

```
day                  input      output   calls   cost USD
2026-10-15       1,913,382      22,447     194       0.63
2026-10-17      33,102,506     871,301   1,908      12.11
2026-10-18      41,739,760   1,185,206   2,192      15.48
2026-10-20             523         156       1       0.00
2026-10-25               0           0       5       0.00   <- this pack's 5 :countTokens probes
TOTAL           76,756,171   2,079,110   4,300      28.22
response codes: {'200': 4288, '429': 12}
```

Two consecutive days carry 74.8M of the 76.8M tokens (97%). The day boundary is
whatever the 86400 s alignment anchor lands on — two pulls minutes apart attributed
the same block of usage to 10-18/10-19 and to 10-17/10-18 respectively — so read
the totals, not the individual dates.

Three things to note in that table:

* **Input is 82% of the cost.** 480 output tokens against a 20k-token prompt.
* **12 calls came back HTTP 429.** The loop was not just expensive, it was
  outrunning the provider's quota.
* **Usage stops dead after 10-20.** The loop only calls the model when a trigger
  fires, and the fleet has had no telemetry since, so nothing is currently being
  spent. The cost is a *recurrence* risk, not an open tap.

## 2. Why the call count was unbounded

Two config values, not a code bug — but nothing in the code could stop them.

* `/etc/systemd/system/termini-controller.service.d/zz-evidence-cooldown.conf`
  (created 2026-10-15 for an evidence run) pinned
  `TERMINI_OPERATOR_COOLDOWN_S=0`, overriding the unit's own `60`. Its comment
  says so: "cooldown 0 so each seeder fault episode fires".
* The loop ran `TERMINI_OPERATOR_DECISION_MODE=model` with
  `TERMINI_OPERATOR_POLL_INTERVAL_S=2`.

The hysteresis latch still stopped a *persistent* breach from re-firing on every
poll, so this was not literally 43,200 calls/day: it averaged **~2,200 calls/day
(one every ~39 s)** — a new episode on each oscillation, with bursts bad enough to
earn 12 HTTP 429s. That cadence alone is ~30× what a demo needs. The journal
carries **3,040 `externalModelCall` lines for 10-18 → 10-20**.

The contract's own cap (`operator-decision.md` §10.2) could not catch this: it
counts a `fallback_streak`, so calls that *succeed* are invisible to it. The
cooldown was the only rate limit, and it had been switched off.

## 3. Why each call was 20k tokens

Measured with the repo's shipped renderer (`termini.decision.prompt.build_messages`)
against Vertex `:countTokens`:

```text
evidence_samples=20  prompt_tokens= 20,521
evidence_samples= 6  prompt_tokens=  8,900
evidence_samples= 1  prompt_tokens=  4,754   <- the static prompt + few-shot floor
```

The prompt carries the evidence window as **full telemetry snapshots**, ~840
tokens each (each repeats `os_name`/`kernel_version`/`hostname`/`mac_address`/
`ip_local`/`project_id`, a `disk[]`, a `net[]` and two `lxd_instances[]` rows). At
the contract ceiling of 20 samples that is 16.8k of the 20.5k; the static system
prompt plus few-shot pairs are the remaining ~3.8k and are identical on every call.

## 4. What changed

| Change | Where | Effect (measured) |
| --- | --- | --- |
| The `cooldown=0` drop-in was removed from the live controller (window `t_519d83a0`, 20:18 UTC); effective `TERMINI_OPERATOR_COOLDOWN_S=60` verified via `systemctl show` | live fleet | worst case per `(node, rule)` falls from one call per poll (1,800/h) to 60/h |
| Global sliding-hour model-call budget, counted **before** the transport, over-budget ⇒ deterministic fast-path + one warn line | `controller/app/operator/budget.py`, `loop._decide` | hard ceiling of `TERMINI_OPERATOR_MODEL_CALLS_PER_HOUR` (default 120) regardless of every other knob |
| Evidence window configurable, default 6, clamped `1..20` | `loop._build_context`, `controller/app/config.py` | 20,521 → 8,900 prompt tokens per decision (−57%) |
| Contract amended (§10.3 budget, §10.4 window, §3.1 row) | `docs/contracts/operator-decision.md` | the invariant, the trade, and the in-process-window limitation are stated |

Combined, the arithmetic on the measured numbers (480 output tokens assumed):

| Setting | prompt tokens | cost/decision | ceiling with the bounds in place |
| --- | --- | --- | --- |
| before: 20 samples, cooldown `0` | 20,521 | $0.00736 | none (measured 2,192 calls / $15.48 in one day) |
| cooldown restored only | 20,521 | $0.00736 | 1,440 calls/day per `(node, rule)` ⇒ $10.60/day |
| this change: 6 samples + cooldown `60` | 8,900 | $0.00387 | 1,440 calls/day per `(node, rule)` ⇒ $5.57/day |
| this change, budget the only bound (`120/h`, global) | 8,900 | $0.00387 | 2,880 calls/day ⇒ $11.15/day |

So the per-decision bill halves, and the call count gains two independent
ceilings instead of one switchable one. Both bounds are tested:
`tests/test_operator_model_budget.py` (9 tests: the sliding window, the
attempt-counted-before-transport rule, the over-budget path still remediating, and
the window clamp).

## 5. What is NOT verified here — stated plainly

* **The deployed controller still runs the pre-change code.** The live fix is the
  config one (cooldown 60). The budget and the 6-sample window land on the next
  `deploy-termini-tree.sh` from a merge of this branch; until then the fleet has
  one bound (cooldown), not two.
* **No live fault was injected to watch the budget trip on the fleet.** The guard
  is proven by unit and wiring tests against the real `OperatorLoop._decide`
  (provider call counts), not by a production run.
* **The few-shot/static 3.8k prefix is not yet cached.** Vertex implicit caching
  should already discount a stable prefix, and explicit context caching
  ($0.03/1M cached input) would make it deterministic, but neither is measured
  here.
* **Dollar figures are list price × measured tokens**, not an invoice. The project
  has no BigQuery billing export configured, so the invoice was not read.

## Reproduce

```bash
# provider usage (needs gcloud auth with monitoring.timeSeries.list)
python3 docs/evidence/2026-10-25-model-spend/tools/vertex_usage.py termini-510609 --days 15

# prompt size of the shipped renderer (needs aiplatform generate-content access)
python3 docs/evidence/2026-10-25-model-spend/tools/measure_decision_prompt.py \
  --project termini-510609 --samples 20 6 1

# the bounds themselves
python3 -m unittest tests.test_operator_model_budget -v
```
