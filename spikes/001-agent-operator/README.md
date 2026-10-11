# Spike 001 — an AI operator with no playbook, grammar-constrained

**Question.** Can the deterministic `playbook.choose()` be replaced by a *dynamic AI
agent* — a local model deciding from live telemetry — without losing the safety
property that only pre-approved actions can ever execute?

Phase 1 of the Gemini/Gemma plan. No cloud, no contract changes, no tracked file
modified.

## Approach

```
telemetry ──► trigger engine ──► agent.choose() ──► executor ──► LXD stub
              (attention)        │  local model
                                 │  GBNF-constrained tool schema
                                 │  per-call enum of REAL vm_ids
                                 └─► RemediationAction | None
```

- **`gbnf.py`** generates a GBNF grammar per call from the tool schema. The
  decoder is constrained, so an out-of-set action has no token sequence — the
  safety property is structural, not a post-hoc validator that can fail open.
- **`agent.py`** exposes `choose(event, playbooks, instance_names, default_instance_name)`
  — byte-compatible with `playbook.choose()` — and **ignores `playbooks` entirely**.
  Evidence-backed by passing a decoy playbook (see Results).
- **`agent_operator.py`** monkeypatches `app.operator.playbook.choose` and then
  runs the *real* production CLI. Nothing tracked is edited.

## Results

### 1. Structural safety (6/6)

Same synthetic state, repeated runs, temperature 0:

| | runs | structurally valid | deterministic |
|---|---|---|---|
| Scenario A — OOM | 3 | **3/3** | byte-identical |
| Scenario B — GPU overtemp + inference down | 3 | **3/3** | byte-identical |

### 2. The honest control — the grammar was *not* what produced correctness

The same states were run through a **permissive** grammar (`action`/`target` =
any string). The model chose `instance.restart` **both times** — a legal action.

> So on these two states the constraint changed nothing. Gemma 3 4B is
> conservative and picks a known verb even when it fits poorly. What the grammar
> buys is a **guarantee that does not depend on the model behaving** — across
> model swaps, quantisations, prompt injection, or a future model that is more
> creative than this one. The control did not demonstrate an escape, and
> pretending otherwise would be dishonest.

### 3. No playbook is consulted

```
decoy playbook declares : image.prune
agent actually chose    : instance.restart
provenance field        : agent          (no playbook file was read)
```

### 4. The real chaos harness — PASS

```
$ TERMINI_OPERATOR_ENABLED=false bash run_chaos_with_agent.sh oom
  PASS  telemetry_series_emitted      {'count': 90}
  PASS  injection_crosses_rule        {'expected_rule': 'oom-guard-01'}
  PASS  remediation_command_issued    {'enforced': True, 'count': 1, 'expected': True}
  VERDICT: PASS
```

Audit row written by the *production* CLI, with `--playbooks-dir /nonexistent-playbooks`
(zero playbooks loaded, and it still acted correctly):

```
rule                   oom-guard-01
action                 instance.restart
action_params          {'instance_name': '01M4NNRWXVEZV6JR6GKR026BV8', '_vm_id': ...}
result                 ok        PUT /1.0/instances/01M4NNRWXVEZV6JR6GKR026BV8/state
detection_latency_s    1.165
remediation_latency_s  0.004
reason                 "The monitoring rule indicates OOM (Out of Memory) conditions.
                        Restarting the workload is the least disruptive action..."
```

The model's own rationale lands in the audit trail. The chosen target is a real
`vm_id` from the evidence window, because the grammar enumerated it.

**Unit suite unchanged: 262 tests, OK. `git status` = `?? spikes/` only.**

## Surprises and bugs found

1. **The documented `OPERATOR_CMD` workflow does not work.** `chaos/run.py` builds
   the controller env from `os.environ` but never sets `TERMINI_OPERATOR_ENABLED`,
   which defaults to **`true`**. The harness controller therefore runs its own
   in-process operator, which takes the single-owner lease (and points at `:8443`,
   not the stub port). Any external operator is a permanent standby, so the
   scenario fails `remediation_command_issued count=0` — looking like *your*
   operator is broken. `chaos/README.md` documents this workflow as supported.
   **Fix: `export TERMINI_OPERATOR_ENABLED=false`.**
2. **A GBNF literal is not a JSON string.** `"instance.restart"` in GBNF matches
   the bare text; a JSON value needs `"\"instance.restart\""`. My first grammar was
   honoured perfectly and emitted *invalid JSON*. Fixed in `_json_str()`.
3. **`remediation_latency_s` excludes the model's think-time.** It is measured from
   *after* `choose()` returns, so it reads `0.004s` while the decision actually took
   ~50s. For any LLM in the loop that metric is the dominant cost and it is
   invisible in the audit trail.
4. **The digest must be O(1) in window length.** My first version serialised the
   whole evidence window; against chaos's ~90 samples the prompt ballooned and
   prompt processing dominated. Now summarised (`n/min/max/first/last/recent`):
   978 chars for a 90-sample window.
5. **Standby logging is misleading** — the message prints the *local* `owner_id`,
   not the lease holder's, which makes "who is blocking me?" unanswerable.
   (Lease TTL is 10s, so a killed operator blocks only briefly.)

## The hard constraint: latency

Measured on this 8-vCPU box with **no GPU**, Gemma 3 4B Q4_K_M via llama.cpp:

| | measured |
|---|---|
| prompt processing | **17.85 tok/s** (474 tokens = 26.6 s) |
| generation | **~4.2 tok/s** |
| one decision | **~50–60 s** |
| stock chaos window | 15 s — **does not fit** |

The scenario had to be widened from 8s to 90s to fit a single CPU decision.
A multi-turn agent loop would be **minutes** per incident. This is the number
that decides Phase 3's GPU question: agentic remediation on CPU is not
real-time, and the earlier "15–25 tok/s" estimate for a 4B on 8 vCPU was
optimistic by ~4x in this environment.

## Verdict: **PARTIAL → VALIDATED for the architecture, constrained by latency**

- **VALIDATED:** a local model *can* replace the playbook under a GBNF tool
  schema, drive the real production loop, and pass the real chaos harness — with
  no playbook, no tracked-file changes, and an audit trail carrying the model's
  rationale.
- **VALIDATED:** the constraint is structural and deterministic (6/6, byte-identical).
- **PARTIAL:** on the two states tested, correctness did not *depend* on the
  grammar — the model complied unconstrained too. The grammar is a guarantee
  against a misbehaving model, not a correction of this one.
- **BLOCKED-ISH:** ~4 tok/s generation makes CPU-only agentic remediation take
  minutes. Fine for a demo paced around it; not fine for "self-healing in seconds".

## Recommendation for the real build

1. **Keep the per-call grammar.** It is cheap, deterministic, and it is the only
   thing that makes "the model chose a valid action" a property rather than a hope.
2. **Grammar-enumerate the candidate targets** from the evidence. It removes a
   whole class of hallucinated-target bugs for free.
3. **Bound the digest** (`n/min/max/first/last/recent`). Never send the raw window.
4. **Fix the `OPERATOR_CMD` lease conflict in `chaos/run.py`** (set
   `TERMINI_OPERATOR_ENABLED=false` for the harness controller, or document the
   requirement). Until then the repo's own documented workflow is broken.
5. **Instrument model think-time in the audit row** before this reaches a demo —
   otherwise the dominant latency is unmeasurable in the trail.
6. **Decide the latency question before Phase 3.** Gemma 3 1B/4B on CPU ≈ 4 tok/s.
   If the demo must show seconds-not-minutes, that is a GPU or a much smaller
   model, and it is a product decision, not an implementation detail.

## Repro

```bash
# runtime (llama.cpp b11556 prebuilt + Gemma 3 4B Q4_K_M)
llama-server --model gemma-3-4b-it-Q4_K_M.gguf --port 8099 --alias local \
             --ctx-size 4096 --parallel 1 --n-gpu-layers 0

python3 probe.py                       # structural safety + the honest control
bash run_chaos_with_agent.sh oom       # the real harness, real audit trail
```

`spikes/` is throwaway by design. The reusable parts — the GBNF generator, the
digest, the swap — are the Phase 2/3 material.
