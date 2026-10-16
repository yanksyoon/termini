# Operator Decision-Step Contract (model-driven)

- Version: **1.0**
- Owner: `architect` (kanban `t_624cb9df`). Implementers: `controller` (`t_8162b01c`), `agents` (`t_4173f0d5`). Verifier: `verifier` (`t_8c36a1ec`).
- Status: **FROZEN** for the 5-day build. Changes require a comment on the task and a matching update to `docs/architecture.md` §4.4.
- Date: 2026-10-11
- Audience: controller engineers, inference/adapter engineers, verifier.
- Related: `controller-api.md` (§2 governance hooks, §5 guardrail, §6 command channel, §8 deterministic fast-path), `agent-runtime.md` (§8 inference service, §14.1 intent extraction), `governance.md` (§7.2 static prompt template, §7.3 model output is constrained, not free-form, §9 hooks), `node-telemetry.md` (§4 telemetry schema, §5 trigger linkage), `architecture.md` §4.4.

This document is the authoritative interface spec for making the operator's
**decision step** (telemetry trigger → remediation action) optionally
model-driven, while keeping the existing deterministic path intact as the
default and as the mandatory fallback. It defines the exact request payload,
response schema, and hard-guardrail layer so a controller engineer and an
inference engineer can implement against it with no further clarification.

Nothing here is a TBD. Where a value is not fixed below, the default in §12 applies.

---

## 1. Current state (deterministic decision)

The decision step today is purely deterministic. Documented here as the baseline
that the amended contract must preserve byte-for-byte when in deterministic mode.

### 1.1 Where the decision is produced

- Loop: `controller/app/operator/loop.py`, `OperatorLoop._handle_event` (line ~202).
- The decision is one call:

  ```python
  action = playbook.choose(event, self.playbooks, self.instance_names, self.default_instance_name)
  ```

  `playbook.choose` (`controller/app/operator/playbook.py`) is a **pure** function:
  given a `HealthEvent`, it looks up the declarative YAML playbook for
  `event.type` and returns a `RemediationAction`, or `None` when no playbook
  covers the type.

### 1.2 The decision type

`RemediationAction` (`controller/app/operator/models.py`) — the only shape the
executor consumes:

| field      | type   | meaning |
|------------|--------|---------|
| `kind`     | string | executor action id (see §1.4) |
| `params`   | dict   | executor args, incl. resolved `instance_name` |
| `reason`   | string | one-line rationale, persisted to the decision log |
| `playbook` | string | playbook `rule_id`/`type` that produced it |
| `rule_id`  | string | the frozen rule that fired |

### 1.3 The playbook mapping

`controller/playbooks/*.yaml` map `type` → a fixed `action.kind` + `params` +
`reason`. `target: heaviest` resolves to the instance with the most used RAM
across the evidence window (`playbook._heaviest_instance`), then to an LXD
instance name via `instance_names`. The four wired playbooks:

| type          | rule_id          | action.kind      | target   |
|---------------|------------------|------------------|----------|
| `oom`         | `oom-guard-01`   | `instance.restart` | `heaviest` |
| `disk_fail`   | `disk-fail-01`   | `instance.stop`    | `heaviest` |
| `disk_degrade`| `disk-degrade-01`| `instance.stop`    | `heaviest` |
| `latency`     | `latency-01`     | `instance.restart` | `heaviest` |

### 1.4 The executor action vocabulary (the allowlist)

`LxdExecutor.execute` (`controller/app/operator/executor.py`) dispatches on
`kind` and **nothing else**. This is the AI operator's `commandAllowlist()`
(`controller-api.md` §6, `governance.md` §9 hook 4). Exactly four actions exist:

`instance.restart`, `instance.stop`, `instance.delete`, `image.prune`.

### 1.5 Idempotency (existing, unchanged)

- `idempotency_key_for(node_id, rule_id, action_kind, target, episode)` →
  `"<node_id>:<rule_id>:<action_kind>:<target>:<episode>"`.
- `audit_by_idempotency_key(key)` returns a prior `ok`/`dry_run` audit within the
  24 h window; a hit yields `already_applied` instead of a duplicate LXD call.
- `remediation_decisions.event_id` is `UNIQUE` — one event, at most one decision.

The model-driven path reuses this mechanism **unchanged** (§9).

### 1.6 Persistence (existing)

`remediation_decisions` (migration `0002_operator.sql`): `id`, `event_id` (UNIQUE),
`node_id`, `rule_id`, `playbook`, `action_kind`, `action_params` (JSON), `reason`,
`decided_at`.

---

## 2. Amended architecture (the seam)

The decision step is put behind a **`DecisionProvider`** seam. The loop no longer
calls `playbook.choose` directly; it calls `provider.decide(event, context)`.

```
OperatorLoop._handle_event
        │
        ▼
DecisionProvider.decide(event, context) -> DecisionResult
        ├─ DeterministicDecisionProvider  (default; wraps playbook.choose)
        └─ ModelDecisionProvider          (opt-in; calls the adapter, then the guardrail)
                │  DecisionInput ────────────────────────► termini.decision adapter
                │  DecisionResponse | DecisionError ◄───── (prompt + llama.cpp + parse)
                ▼
        Guardrail.validate(response, input) -> RemediationAction | fallback
                ▼
        LxdExecutor.execute(action)   (unchanged)
```

### 2.1 `DecisionProvider` interface (controller)

```python
class DecisionProvider(Protocol):
    def decide(self, event: HealthEvent, context: DecisionContext) -> DecisionResult: ...
```

`DecisionContext` — everything the provider needs, all C0 (derivable from the
event + loop state):

| field | type | source |
|-------|------|--------|
| `event` | `HealthEvent` | the fired event |
| `state` | dict | latest telemetry sample (the offending snapshot) |
| `evidence` | list[dict] | bounded evidence window (≤ 20 samples, oldest first) |
| `instances` | list[dict] | target node's `lxd_instances[]` (latest sample) |
| `instance_names` | dict[str,str] | `vm_id → LXD instance name` |
| `default_instance_name` | str | `"agent"` |
| `allowed_actions` | list[str] | action ids the model may pick (default: the 4 in §1.4) |

`DecisionResult` — the loop's only output of the step:

| field | type | meaning |
|-------|------|---------|
| `action` | `RemediationAction` | what the executor runs (always present) |
| `source` | enum | `deterministic` \| `model` \| `fallback` |
| `confidence` | float \| None | `None` for deterministic |
| `fallback_reason` | str \| None | set iff `source == fallback` |
| `guardrail` | list[dict] | per-rule decisions (what was validated/clamped/rejected) |

---

## 3. Decision request payload (`DecisionInput`)

Schema `1`. This is the structured payload the controller assembles and hands to
the decision adapter; the adapter renders it into the prompt. Every field is C0
(`governance.md` §7.2 — `externalModelCall()` asserts template version + C0 field
classes before the call is made).

```json
{
  "schema_version": 1,
  "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "rule_id": "oom-guard-01",
  "type": "oom",
  "trigger": {
    "metric": "ram_used_bytes",
    "value": 94.7,
    "threshold": ">= 95% of ram_total_bytes for 2 consecutive samples",
    "fired_at": "2026-10-04T07:49:05.000Z",
    "breach_started_at": "2026-10-04T07:49:03.000Z",
    "episode": 3
  },
  "state": {
    "seq": 1050, "ts": "2026-10-04T07:49:05.000Z",
    "cpu_util_pct": 88.0, "ram_total_bytes": 34359738368, "ram_used_bytes": 32641751449,
    "disk": [{"path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
              "write_latency_ms": 3.0, "mount_ro": false}],
    "net": [{"iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8}],
    "gpu": [{"model": "NVIDIA L4", "util_pct": 88.0, "vram_total_bytes": 25769803776,
             "vram_used_bytes": 12884901888, "temp_celsius": 62.0}]
  },
  "evidence": [ { "seq": 1049, "ts": "2026-10-04T07:49:04.000Z", "ram_used_bytes": 32300000000, "ram_total_bytes": 34359738368 } ],
  "target_resource": {
    "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
    "instances": [
      { "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "instance_name": "agent-01H4KZ9Q",
        "state": "running", "cpu_util_pct": 12.0, "ram_used_bytes": 10000000000,
        "ram_limit_bytes": 16000000000, "disk_used_bytes": 1000000000 }
    ]
  },
  "allowed_action_space": [
    { "action_id": "instance.restart",
      "params_schema": { "instance_name": "string", "force": "bool", "timeout_s": "int 1..120" } },
    { "action_id": "instance.stop",
      "params_schema": { "instance_name": "string", "force": "bool" } },
    { "action_id": "instance.delete",
      "params_schema": { "instance_name": "string" } },
    { "action_id": "image.prune", "params_schema": {} }
  ]
}
```

### 3.1 Field reference

| field | type | required | notes |
|-------|------|----------|-------|
| `schema_version` | integer | yes | always `1` |
| `request_id` | string (ULID) | yes | correlation id; echoed by the adapter |
| `project_id` | string (ULID) | yes | scoping |
| `node_id` | string (ULID) | yes | offending node |
| `rule_id` | string enum | yes | `oom-guard-01` \| `disk-fail-01` \| `disk-degrade-01` \| `latency-01` |
| `type` | string enum | yes | `oom` \| `disk_fail` \| `disk_degrade` \| `latency` |
| `trigger` | object | yes | `{metric, value, threshold, fired_at, breach_started_at, episode}` — the §5 trigger linkage |
| `state` | object | yes | latest telemetry sample, C0 fields only (`node-telemetry.md` §4) |
| `evidence` | array | yes | bounded window, ≤ 20 samples, oldest first (matches `EVIDENCE_SAMPLE_CAP`) |
| `target_resource.node_id` | string (ULID) | yes | target node (== `node_id`) |
| `target_resource.instances` | array | yes | the node's `lxd_instances[]`; the model's only valid `instance_name` targets |
| `allowed_action_space` | array | yes | the whitelist the model may choose from (generated from §5 `ACTION_SPACE`, intersected with `allowed_actions`) |

`state`, `evidence`, and `target_resource.instances` are serialized **verbatim**
from the telemetry store — no transformation, no C1/C2 content (the store already
holds only validated C0/C1 samples).

---

## 4. Decision response (`DecisionResponse`)

Schema `1`. The model must emit exactly one JSON object with this shape.

```json
{
  "schema_version": 1,
  "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
  "action_id": "instance.restart",
  "params": { "instance_name": "agent-01H4KZ9Q", "force": true, "timeout_s": 30 },
  "confidence": 0.87,
  "rationale": "host RAM at 95% with one instance holding 29% of RAM; restart it to reclaim."
}
```

### 4.1 Field reference

| field | type | required | bounds |
|-------|------|----------|--------|
| `schema_version` | integer | yes | `== 1` |
| `request_id` | string (ULID) | no | echoed for tracing; validated for ULID form if present; **not** used for correlation |
| `action_id` | string enum | yes | must be in the request's `allowed_action_space` |
| `params` | object | no (default `{}`) | keys ⊆ the action's `params_schema`; values per §5 |
| `confidence` | number | yes | `0.0 .. 1.0` |
| `rationale` | string | yes | `1 .. 1024` chars, C0-safe |

---

## 5. Allowed action space (`ACTION_SPACE`)

The canonical, versioned whitelist. Lives in `termini/decision/types.py` and is
the **single source of truth** for both the adapter (which enumerates it in the
prompt) and the controller guardrail (which validates against it). It must match
`LxdExecutor`'s dispatch exactly — adding an action here requires a matching
executor method and a playbook, and vice-versa.

```python
ACTION_SPACE: dict[str, dict] = {
    "instance.restart": {
        "params": {
            "instance_name": {"type": "string", "required": True, "resolve": "instance"},
            "force":         {"type": "bool",   "required": False, "default": True},
            "timeout_s":     {"type": "int",    "required": False, "default": 30, "min": 1, "max": 120},
        },
    },
    "instance.stop": {
        "params": {
            "instance_name": {"type": "string", "required": True, "resolve": "instance"},
            "force":         {"type": "bool",   "required": False, "default": True},
        },
    },
    "instance.delete": {
        "params": {
            "instance_name": {"type": "string", "required": True, "resolve": "instance"},
        },
    },
    "image.prune": {"params": {}},
}
```

`instance_name` resolution rule (single rule, applied identically by the adapter's
prompt and the guardrail): the value is either the literal `"heaviest"` (resolve to
the highest-`ram_used_bytes` instance across `evidence`, then map `vm_id → name`
via `instance_names`, defaulting to `default_instance_name`) or a `vm_id`/`instance_name`
present in `target_resource.instances[]`.

---

## 6. Decision adapter (inference side)

Owned by the `agents` workstream. A repo-root package `termini/decision/`,
sibling of `termini/intent/` (mirrors its structure exactly).

| module | contents |
|--------|----------|
| `types.py` | `DecisionInput`, `DecisionResponse`, `DecisionError`, `ACTION_SPACE`, `FALLBACK_REASONS`, frozen bounds |
| `prompt.py` | versioned template `operator-decision v1` + JSON schema + few-shot; `build_messages(input)` |
| `adapter.py` | `render(input) -> messages`, `parse(content) -> DecisionResponse | DecisionError`, `decide(input, client, config) -> DecisionResponse | DecisionError` |
| `config.py` | `DecisionModelConfig` (reuses `TERMINI_INFERENCE_*`, model `deepseek-flash`) |

### 6.1 Transport (the model call)

The adapter calls the endpoint named by `TERMINI_INFERENCE_BASE_URL` and nothing
else. **Since the inference split (`infra-decisions.md` D9) that endpoint is the
CONTROLLER's own provider-only listener on loopback**
(`http://127.0.0.1:8085`, `backend=gemini-proxy` → `aiplatform.googleapis.com`):
the diagnosis is served by a managed model and the prompt leaves the premises.
That is the user's decision of 2026-10-15 (Q1/Q1b) and it is what keeps the demo's
model decision at seconds instead of the minutes the CPU node needs.

The seam is unchanged — one base URL, one OpenAI-compatible call, the same
`externalModelCall()` guard (governance §7.2/§9.3), which still asserts the
template version and the C0 class of every substituted field before anything is
sent. What the split changes is only *which* listener answers: the agent-facing
surface on Node C (`10.10.0.13:8080`) serves local weights and is no longer the
diagnosis target. `setup-inference-surfaces.sh` installs the provider listener and
writes the controller drop-in; the pre-split wiring
(`infra/cloudrun/setup-operator-node-c.sh`) now refuses to run without
`--allow-unsplit`.

```
POST {TERMINI_INFERENCE_BASE_URL}/v1/chat/completions
Authorization: Bearer {TERMI...KEN}
Content-Type: application/json
X-Termini-Request-Id: <request_id>
```

```json
{
  "model": "deepseek-flash",
  "messages": [ {"role": "system", "content": "<operator-decision v1 system prompt>"},
                 ...few-shot...,
                 {"role": "user", "content": "<JSON-serialized DecisionInput>"} ],
  "temperature": 0,
  "max_tokens": 512,
  "response_format": {"type": "json_object"},
  "stream": false
}
```

- The system prompt is the versioned `operator-decision v1` template
  (`governance.md` §7.2: static template, typed C0 placeholders only).
- The user message is the `DecisionInput` serialized as compact JSON, embedded in
  the template's single `{{decision_input}}` placeholder.
- `response_format: json_object` (or a GBNF grammar) is **required** so the output
  parses deterministically and is never free-form.

### 6.2 Parsing and light repair (`adapter.parse`)

The adapter converts the model's completion text into a `DecisionResponse` or a
typed `DecisionError`. It performs **parse + light repair only**; the hard guardrail
remains controller-side (§7).

1. Strip stray markdown fences (as `termini/intent/extractor.py` does).
2. `json.loads`. On failure → `DecisionError("parse_error")`.
3. Coerce `confidence` to `float`; reject if non-numeric → `DecisionError("schema_violation")`.
4. Ensure `action_id` is a non-empty string; ensure `params` is an object (default `{}`).
5. Drop `params` keys not in the action's `params_schema` (never pass unknown keys through).
6. Default missing optional params from `ACTION_SPACE`.
7. Cap `rationale` to 1024 chars.

`DecisionError` carries exactly one of the `FALLBACK_REASONS` codes (§10.1) so the
controller can fall back deterministically. The adapter never fabricates an action:
if the output cannot be repaired into a well-shaped `DecisionResponse`, it returns
a `DecisionError`, never a guess.

---

## 7. Hard guardrail layer (controller side)

Owned by the `controller` workstream (`controller/app/operator/decision.py`). The
guardrail is **pure, LLM-free, side-effect-free** (except audit/telemetry), mirroring
`termini/intent/validator.py`. No model output reaches `LxdExecutor` without passing
every rule. Ordered, deterministic:

| # | Rule | Constrains | On violation |
|---|------|-----------|--------------|
| G1 | JSON parse | whole output | fallback `parse_error` |
| G2 | schema validation (required fields + types) | `schema_version`, `action_id`, `params`, `confidence`, `rationale` | fallback `schema_violation` |
| G3 | action whitelist | `action_id` ∈ `ACTION_SPACE` ∩ `allowed_actions` | fallback `action_not_allowed` |
| G4 | param key whitelist | `params` keys ⊆ the action's `params_schema` | strip unknown keys (never passed through) |
| G5 | param type/bounds | `force` (bool), `timeout_s` (int, clamp to `1..120`), `instance_name` (resolvable per §5) | clamp numerics; fallback `invalid_target` if `instance_name` unresolvable |
| G6 | confidence gate | `confidence ≥ decision_min_confidence` (default `0.5`) | fallback `low_confidence` |
| G7 | rationale sanitization | `rationale` length `≤ 1024`, C0-safe (re-run the C2/PII scan from `governance.md` §7) | truncate; on C2 hit → fallback `schema_violation` |
| G8 | idempotency | resulting action → idempotency key (§9) | existing dedup (`already_applied`) |

G4/G5 **clamp**, never reject, on numeric/unknown-key issues; G1/G2/G3/G6/G7 are
hard **fallbacks**. The all-or-nothing rule from `controller-api.md` §5.3 does
**not** apply here — a fallback produces a deterministic action, not a rejection.

The guardrail returns a `RemediationAction` (built from the *validated* response,
never the raw output) plus the `guardrail` decision list for the audit record.

---

## 8. Deterministic fallback (mandatory)

`DeterministicDecisionProvider.decide()` is exactly `playbook.choose(...)` with
`source="deterministic"`, `confidence=None`. The `ModelDecisionProvider` falls back
to it on **any** of:

- adapter `DecisionError` (any `FALLBACK_REASONS` code),
- HTTP error / non-2xx from the inference endpoint → `inference_unavailable`,
- timeout (connect or total) → `inference_timeout`,
- guardrail G1/G2/G3/G6/G7 rejection,
- any unexpected exception in the model path (caught, logged, fallback).

On fallback, the loop emits a structured `warn` log + operator-stream line with the
fallback reason (§11) and records `source="fallback"` in the decision log. Remediation
is **never blocked on model latency**: the deterministic path runs regardless
(`controller-api.md` §8 fast-path guarantee preserved).

---

## 9. Idempotency

Unchanged. The `RemediationAction` produced by either provider flows through the
existing `idempotency_key_for` → `audit_by_idempotency_key` gate in
`loop._handle_event`. The model cannot bypass the 24 h window or the
`event_id`-UNIQUE decision dedup. The only new requirement: the fallback-streak
state (§10.2) and provenance columns (§11) are written in the **same transaction**
as the decision/audit writes (matching the existing single-transaction guarantee).

---

## 10. Failure semantics

### 10.1 Fallback reasons (exact enum)

`inference_unavailable`, `inference_timeout`, `parse_error`, `schema_violation`,
`action_not_allowed`, `invalid_target`, `low_confidence`.

### 10.2 Repeated-failure loop

Per `(node_id, rule_id)` the loop tracks a `fallback_streak` and a `forced_until`
timestamp (persisted, §11):

- Each model-path fallback increments `fallback_streak`; each successful
  `source="model"` decision resets it to `0`.
- When `fallback_streak ≥ decision_max_consecutive_fallbacks` (default `3`), set
  `forced_until = now + decision_fallback_cooldown_s` (default `300`) and reset
  `fallback_streak` to `0`.
- While `now < forced_until`, the loop **skips the model path entirely** for that
  `(node, rule)` and uses deterministic, emitting one `warn` log line at entry.
- This caps model calls for a stuck fault and guarantees the deterministic
  fast-path still fires every poll — no remediation is ever lost to a broken model.

The existing hysteresis latch + cooldown (`trigger_state`, `recent_audit`) already
prevent re-fire within an episode; the fallback cap is a separate guard against
model-path churn.

---

## 11. Persistence (migration `0009_operator_decision.sql`)

```sql
ALTER TABLE remediation_decisions ADD COLUMN decision_source TEXT NOT NULL DEFAULT 'deterministic';
ALTER TABLE remediation_decisions ADD COLUMN confidence REAL;
ALTER TABLE remediation_decisions ADD COLUMN fallback_reason TEXT;

CREATE TABLE IF NOT EXISTS decision_fallback_state (
    node_id         TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    fallback_streak INTEGER NOT NULL DEFAULT 0,
    forced_until    TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (node_id, rule_id)
);
```

- `decision_source` ∈ `deterministic | model | fallback`.
- `confidence` is `NULL` for deterministic decisions.
- `fallback_reason` is one of the §10.1 codes, `NULL` otherwise.

### 11.1 Telemetry / log events

Every fallback and every forced-deterministic cooldown emits an operator log line
through the existing `OperatorLogs` sink (`dashboard.md` §2.1), `actor: ai_operator`,
`severity: warn`, `message` including the `rule_id` and `fallback_reason`. The
dashboard's live AI-operator panel therefore shows the decision source inline.

---

## 12. Versioning & compatibility (deterministic remains selectable)

- New config `TERMINI_OPERATOR_DECISION_MODE` ∈ `deterministic` (the code default) | `model`.
  - `deterministic`: the loop uses `DeterministicDecisionProvider` only; **no model
    call is ever made**. Byte-identical to the pre-amendment loop, and what the
    operator falls back to.
  - `model`: `ModelDecisionProvider` with the mandatory deterministic fallback.
    **This is what the shipped demo sets** (`TERMINI_OPERATOR_DECISION_MODE=model`
    on the controller); the code default remains `deterministic` so an unconfigured
    controller never makes an external call by accident. The shipped `model` mode
    dials the controller's own provider-only listener (`127.0.0.1:8085`,
    `infra-decisions.md` D9), so the external call in that mode is deliberate,
    bounded (`TERMINI_OPERATOR_DECISION_TIMEOUT_S=120`) and labelled on every
    response, log line and `externalModelCall()` audit row
    (`egress=external destination=aiplatform.googleapis.com`).
- `schema_version: 1` on both payloads. Additive field additions do not bump it;
  removal, renaming, or type-change bumps to `2` (`controller-api.md` §1.2 convention).
- The deterministic provider's output must be **byte-identical** to today's
  `playbook.choose`, so existing operator-loop tests pass unchanged in
  `deterministic` mode.
- A controller run with `mode=deterministic` must have **zero** code paths
  reachable that construct a model request (verifier checks this explicitly).

Defaults (frozen): `decision_timeout_s = 30`, `decision_min_confidence = 0.5`,
`decision_max_consecutive_fallbacks = 3`, `decision_fallback_cooldown_s = 300`.

---

## 13. Configuration

| env | default | meaning |
|-----|---------|---------|
| `TERMINI_OPERATOR_DECISION_MODE` | `deterministic` | `deterministic` \| `model` |
| `TERMINI_OPERATOR_DECISION_TIMEOUT_S` | `30` | model call timeout (connect + total) |
| `TERMINI_OPERATOR_DECISION_MIN_CONFIDENCE` | `0.5` | G6 threshold |
| `TERMINI_OPERATOR_DECISION_MAX_FALLBACKS` | `3` | fallback streak cap (G-fallback loop) |
| `TERMINI_OPERATOR_DECISION_FALLBACK_COOLDOWN_S` | `300` | forced-deterministic window |
| `TERMINI_INFERENCE_BASE_URL` | `http://10.10.0.13:8080` | existing (adapter reuses) |
| `TERMINI_INFERENCE_MODEL` | `deepseek-flash` | existing |
| `TERMINI_INFERENCE_TOKEN` | `""` | existing |
| `TERMINI_INFERENCE_TIMEOUT_S` | `30` | existing |

All added to `controller/app/config.py` `Config` + `load()`.

---

## 14. Exact call sites

### 14.1 Controller (`t_8162b01c`)

| path | change |
|------|--------|
| `controller/app/operator/decision.py` | **NEW** — `DecisionProvider` protocol, `DecisionContext`, `DecisionResult`, `DeterministicDecisionProvider`, `ModelDecisionProvider`, `Guardrail` (G1–G8), `Guardrail.validate()`, `FALLBACK_REASONS`, `DecisionInput/DecisionResponse` builders |
| `controller/app/operator/loop.py` | `_handle_event` replaces inline `playbook.choose(...)` with `self.provider.decide(event, ctx)`; `__init__` accepts + stores a `DecisionProvider` (defaults to deterministic); apply fallback-streak cap (§10.2) |
| `controller/app/operator/__init__.py` | export the new provider/guardrail symbols |
| `controller/app/main.py` | construct the provider from `cfg.operator_decision_mode`, wire into `OperatorLoop` |
| `controller/app/config.py` | add §13 env vars to `Config` + `load()` |
| `controller/app/operator/store.py` | `insert_decision` writes `decision_source`/`confidence`/`fallback_reason`; new `load/save_decision_fallback_state` + `record_fallback` helpers |
| `controller/app/operator/cli.py` | standalone CLI: honor `decision_mode` (deterministic default; `model` optional), otherwise unchanged |
| `controller/app/migrations/0009_operator_decision.sql` | **NEW** — §11 DDL |

### 14.2 Inference adapter (`t_4173f0d5`)

| path | change |
|------|--------|
| `termini/decision/__init__.py` | **NEW** — package exports |
| `termini/decision/types.py` | **NEW** — `DecisionInput`, `DecisionResponse`, `DecisionError`, `ACTION_SPACE`, `FALLBACK_REASONS`, bounds |
| `termini/decision/prompt.py` | **NEW** — `operator-decision v1` template + JSON schema + few-shot + `build_messages` |
| `termini/decision/adapter.py` | **NEW** — `render`, `parse`, `decide` (§6) |
| `termini/decision/config.py` | **NEW** — `DecisionModelConfig` (reuses `TERMINI_INFERENCE_*`) |
| `termini/decision/README.md` | **NEW** — local-run doc against §15 example payloads |

### 14.3 Tests

| path | change |
|------|--------|
| `tests/test_operator_decision_guardrail.py` | **NEW** — one test per guardrail rejection path (G1–G8), plus clamp cases |
| `tests/test_operator_decision_fallback.py` | **NEW** — fallback triggers (timeout, unavailable, parse_error, schema_violation, action_not_allowed, invalid_target, low_confidence) + fallback-streak cap + forced-deterministic cooldown |
| `tests/test_operator_loop.py` | unchanged (must still pass in `deterministic` mode) |
| `tests/test_operator_lease.py` | unchanged |

### 14.4 Docs

| path | change |
|------|--------|
| `docs/contracts/operator-decision.md` | this document |
| `docs/contracts/controller-api.md` §9 | add cross-reference line |
| `docs/architecture.md` §4.4 | note the model-driven path is now a selectable mode with deterministic default + fallback (day-2 wording updated) |

---

## 15. Example payloads (for local-run verification)

The canonical `DecisionInput`/`DecisionResponse` examples in §3/§4 are the fixtures
the adapter's local-run doc uses. The verifier's adversarial cases (§16) exercise
the guardrail against these same shapes.

---

## 16. Acceptance criteria

1. A controller engineer can implement `DecisionProvider` + `Guardrail` from §2, §7,
   §9, §10 with no further clarification.
2. An inference engineer can implement `termini/decision/` from §3–§6 with no
   further clarification.
3. Every guardrail rule is explicitly mapped to the response field it constrains
   (§7 table).
4. `deterministic` mode runs the loop end-to-end with zero model calls, and its
   output is identical to today's `playbook.choose`.
5. `model` mode runs end-to-end, and no model output reaches `LxdExecutor` without
   passing G1–G8.
6. Each fallback trigger and the repeated-failure cap are unit-tested (§14.3).
