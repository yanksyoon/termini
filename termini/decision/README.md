# termini.decision — model-driven operator decision adapter (inference side)

The inference side of the operator **decision step**: given a C0
`DecisionInput`, render a prompt, call the existing on-prem llama.cpp endpoint,
and parse the completion into a well-shaped `DecisionResponse` (or a typed
`DecisionError` so the controller can fall back deterministically).

Authoritative spec: [`docs/contracts/operator-decision.md`](../../docs/contracts/operator-decision.md)
(§3 request, §4 response, §5 action space, §6 adapter, §10.1 fallback reasons).
Sibling of `termini/intent/` and structured the same way.

```
termini/decision/
  types.py      DecisionInput / DecisionResponse / DecisionError, ACTION_SPACE,
                FALLBACK_REASONS, frozen bounds  (§3/§4/§5/§10.1)
  prompt.py     operator-decision v1 template + JSON schema + few-shot,
                build_messages()  (§6.1)
  adapter.py    render() / parse() / decide()  (§6)
  config.py     DecisionModelConfig — reuses the frozen TERMINI_INFERENCE_* (§13)
  __main__.py   thin CLI for local runs against the contract's example payloads
  README.md     this file
```

## Where the security boundary is

This package is **not** the boundary. `adapter.parse` does *parse + light repair
only* (§6.2):

1. strip stray markdown fences; 2. `json.loads`; 3. coerce `confidence` to float
(else `schema_violation`); 4. require `schema_version == 1`, a non-empty
`action_id` string, an object `params`, and a non-empty `rationale`;
5. drop `params` keys not in the chosen action's `params_schema`; 6. default the
missing optional params; 7. cap `rationale` at 1024 chars.

It guarantees the answer is *well-shaped*. It does **not** decide whether the
action is *authorized* — that is the controller-side guardrail
(`controller/app/operator/decision.py`, G1–G8):

| not enforced here (by contract) | where | fallback reason |
|---|---|---|
| G3 `action_id` ∈ `ACTION_SPACE` ∩ `allowed_action_space` | controller | `action_not_allowed` |
| G5 param type/bounds (`timeout_s` clamp 1..120), `instance_name` resolvable | controller | `invalid_target` |
| G6 `confidence ≥ decision_min_confidence` | controller | `low_confidence` |
| G7 rationale C0/C2 re-scan | controller | `schema_violation` |

So an out-of-whitelist action, a `timeout_s` of `999`, or a low-confidence
answer is *passed through by the adapter by design* and rejected/clamped by the
guardrail — the adapter never rejects on policy, only on shape. There is exactly
one safe way to consume the output: the controller's `ModelDecisionProvider`,
which runs G1–G8 before `LxdExecutor`. `ActionSpace` itself is defined once in
`types.py` (`ACTION_SPACE`) and the prompt's enumeration is *generated* from it,
so prompt and guardrail cannot drift.

## Run it locally (no model needed)

Parse the contract's §4 example response through the exact parse path the live
call uses — offline, deterministic, no endpoint:

```bash
python -m termini.decision --example-response
```

```json
{
  "outcome": "response",
  "schema_version": 1,
  "action_id": "instance.restart",
  "params": {"instance_name": "agent-01H4KZ9Q", "force": true, "timeout_s": 30},
  "confidence": 0.87,
  "rationale": "host RAM at 95% with one instance holding 29% of RAM; restart it to reclaim.",
  "request_id": "01M4349RQ09HMAX6RC9YPYQH4W"
}
```

Malformed output is detected and reported rather than passed through (exit code
1):

```bash
python -m termini.decision --raw '{"action_id": "rm -rf /", "confidence": "high"}'
python -m termini.decision --raw 'not json at all'
```

See exactly what would be sent to the model for the contract's §3 example input:

```bash
python -m termini.decision --example --prompt
```

## Run it live

Needs the on-prem router reachable and the frozen env vars
(`agent-runtime.md` §8; all pre-existing):

```bash
export TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080
export TERMINI_INFERENCE_MODEL=gemma
export TERMINI_INFERENCE_TOKEN=...        # optional on-prem bearer
export TERMINI_INFERENCE_TIMEOUT_S=30

python -m termini.decision --example       # render + call + parse
python -m termini.decision --input my_input.json
```

## As a library

```python
from termini.decision import decide, parse, DecisionError

# Offline parse of a canned completion (unit tests, replay):
result = parse(model_text)

# Live call (never raises on timeout/transport — you get a DecisionError):
result = decide(decision_input)            # uses DecisionModelConfig.from_env()
if isinstance(result, DecisionError):
    # fall back deterministically; result.reason is a FALLBACK_REASONS code
    ...
else:
    # hand to the controller guardrail — NOT straight to the executor
    ...
```

The model client is injectable for tests (`client(messages, request_id=None) -> str`).

## Contract example payloads

Request (`DecisionInput`, §3 above; also shipped as `--example`):

```json
{
  "schema_version": 1,
  "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "rule_id": "oom-guard-01",
  "type": "oom",
  "trigger": {"metric": "ram_used_bytes", "value": 94.7, "episode": 3},
  "state": {"seq": 1050, "ram_total_bytes": 34359738368, "ram_used_bytes": 32641751449},
  "evidence": [{"seq": 1049, "ram_used_bytes": 32300000000}],
  "target_resource": {
    "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
    "instances": [{"vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "instance_name": "agent-01H4KZ9Q"}]
  },
  "allowed_action_space": [{"action_id": "instance.restart"}, {"action_id": "instance.stop"}]
}
```

Response (`DecisionResponse`, §4) — the model emits this, `parse` validates it:

```json
{
  "schema_version": 1,
  "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
  "action_id": "instance.restart",
  "params": {"instance_name": "agent-01H4KZ9Q", "force": true, "timeout_s": 30},
  "confidence": 0.87,
  "rationale": "host RAM at 95% with one instance holding 29% of RAM; restart it to reclaim."
}
```

## Tests

```bash
python3 -m unittest tests.test_decision_adapter -v     # this package only
python3 -m unittest discover -s tests -v               # whole suite
```
