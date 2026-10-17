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
| `incident` | dict \| None | the §4.2 incident record (title/explanation/evidence/actions + `generated`); `None` only in `deterministic` mode |

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
  ],
  "derived": {
    "window_samples": 20,
    "oom_kill_count_delta": 3,
    "pgmajfault_count_delta": 4512,
    "tcp_retransmit_segs_delta": 18
  }
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
| `derived` | object | yes | controller-computed deltas over the evidence window (see §3.2); `{}` when no delta could be computed |

`state`, `evidence`, and `target_resource.instances` are serialized **verbatim**
from the telemetry store — no transformation, no C1/C2 content (the store already
holds only validated C0/C1 samples). `derived` is the one controller-computed
field: it is built from the same C0 samples, so it stays C0, but it is a derived
aggregate, never a raw sample.

### 3.2 The `derived` object (controller-computed, never raw)

The cumulative counters in `state`/`evidence` — `oom_kill_count`,
`pgmajfault_count`, `tcp_retransmit_segs` (and the per-array cumulative
counters in `disk[]`/`net[]`/`lxd_instances[]`) — are **monotonic** totals. Their
diagnostic signal is the *delta over the window* ("is it rising?"), not the raw
total at one sample. The controller therefore computes the window delta for the
three flat node-level counters and ships it under `derived`, labelled as derived:

| key | type | meaning |
|-----|------|---------|
| `window_samples` | integer | the number of samples in the evidence window |
| `oom_kill_count_delta` | integer | `newest - oldest` of `oom_kill_count` over the window |
| `pgmajfault_count_delta` | integer | `newest - oldest` of `pgmajfault_count` over the window |
| `tcp_retransmit_segs_delta` | integer | `newest - oldest` of `tcp_retransmit_segs` over the window |

A delta is **omitted** (never a fabricated `0`, never negative) when the field is
absent at either end or the counter reset (`newest < oldest`, e.g. a node
reboot). This mirrors the collector's omit-not-fabricate rule (`node-telemetry.md`
§4.12–§4.14): a missing `pressure{}` is an absent key, not a `0.0`. The per-array
cumulative counters remain raw samples in the window and are read across it; a
delta over them is still derived, never presented as a raw sample. `derived` is
additive within schema `1` (§12) and is validated deny-by-default by
`externalModelCall()` exactly like every other substituted field (§7.2).

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
  "rationale": "host RAM at 95% with one instance holding 29% of RAM; restart it to reclaim.",
  "incident_title": "Memory exhaustion on node 01M4349RPTT29RNTC6VBCXQ0RC",
  "explanation": "ram_used_bytes reached 96.0% of ram_total_bytes and stayed there for two consecutive samples, and instance 01M4349RPXWDC52ZDAVMPCMJSH holds 29% of the node's RAM by itself. That is a single-instance leak rather than fleet-wide pressure, so reclaiming that instance's memory resolves the breach without touching the node.",
  "evidence": [
    { "field": "ram_used_bytes", "value": 32641751449, "seq": 1050, "ts": "2026-10-04T07:49:05.000Z" },
    { "field": "ram_total_bytes", "value": 34359738368, "seq": 1050, "ts": "2026-10-04T07:49:05.000Z" },
    { "field": "lxd_instances.ram_used_bytes", "value": 10000000000, "seq": 1050 }
  ],
  "actions": [
    { "action_kind": "instance.restart", "target": "01M4349RPXWDC52ZDAVMPCMJSH",
      "note": "restart the heaviest instance to reclaim its resident memory" }
  ]
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
| `incident_title` | string | no | `1 .. 160` chars, C0-safe (§4.2) |
| `explanation` | string | no | `1 .. 2048` chars, C0-safe (§4.2) |
| `evidence[]` | array of objects | no | `0 .. 12` entries, each per §4.2 |
| `actions[]` | array of objects | no | `0 .. 8` entries, each per §4.2 |

The four incident fields are **additive within schema `1`** (§12: additive field
additions do not bump the version). A model that emits none of them is still a
valid response — the deterministic fields above are the whole decision, and the
record is the *explanation* of it. `kind`/`params`/`reason` (the
`RemediationAction` the executor consumes, §1.2) are unchanged and remain
mandatory for the action to be executable at all.

**Wire-level shape repairs (not a schema change).** Two of the required values may
arrive inside the reply's own `actions[]` record rather than at the top level,
because that is what models actually emit:

* `actions[0].action_kind`, when it is one of the ids in `ACTION_SPACE`, supplies a
  missing top-level `action_id`;
* `actions[0].target` supplies a missing `params.instance_name`.

Both are recorded on the response (`DecisionResponse.repairs`) and both are
**shape repairs only**: the controller's guardrail still authorizes the action
(G3 whitelist, G5 target resolution against the live inventory, G6 confidence), so
nothing is allowed that the whitelist would otherwise refuse. A reply that names
no action anywhere is still a `schema_violation` fallback — the playbook never
decides under the model's name.

### 4.2 The incident record (why the answer stops being a log line)

`rationale` is one line: enough to justify a command to an auditor, not enough to
explain an incident to a human at 03:00. The incident record is the structured
answer to the four questions the log stream cannot answer — *what happened, why
does the model think so, on what evidence, and what did it do*. It is
**contractual**, not prompt-only: the shape, the types and the bounds below are
enforced by the controller's guardrail (G9, §7) before anything is persisted.

```json
"incident_title": "<one line, ≤160 chars>",
"explanation":     "<why the evidence implies it, ≤2048 chars>",
"evidence": [ { "field": "ram_used_bytes", "value": 32641751449, "seq": 1050, "ts": "..." } ],
"actions":  [ { "action_kind": "instance.restart", "target": "01M4349RPXWDC52ZDAVMPCMJSH", "note": "..." } ]
```

| field | type | required | bounds | meaning |
|-------|------|----------|--------|---------|
| `incident_title` | string | no | `1 .. 160` chars | One line naming the incident, not the action |
| `explanation` | string | no | `1 .. 2048` chars | Why the evidence implies the incident, in the model's own words |
| `evidence[].field` | string | yes (per entry) | `≤ 64` chars, pattern `^[a-z0-9_]+(\.[a-z0-9_]+)*$` | The telemetry field the entry is about — a **name already in the accepted sample** (`node-telemetry.md` §4), never free text |
| `evidence[].value` | string \| number \| boolean | yes (per entry) | strings `≤ 128` chars | The value observed for that field |
| `evidence[].seq` | integer | no | `≥ 0` | The sample `seq` the value came from |
| `evidence[].ts` | string RFC3339 | no | — | The sample timestamp the value came from |
| `actions[].action_kind` | string enum | yes (per entry) | ∈ §5 `ACTION_SPACE` | What the model decided to do |
| `actions[].target` | string | no | `≤ 64` chars | The instance/node the action names, if any |
| `actions[].note` | string | no | `≤ 256` chars | Why that action, in one clause |

**Evidence quoting (recorded decision, t_957956e5): yes — C0 values only.**
`evidence[]` may quote the raw *values* of C0 telemetry fields already inside the
accepted sample, because that is what makes the record checkable against the
telemetry store: an auditor can re-read `seq 1050` and see the same number. It may
never quote C2 material (application log lines, `argv`, environment variables,
file contents, prompt text), and `field` may not be an invented path — the G9 scan
(§7) runs the same C2 signature scan over every incident string that G7 runs over
`rationale`.

**Numeric evidence is authored grouped in the prompt (t_220a468a).** The prompt
renders the input's byte-magnitude integers thousands-grouped (`4119142400` →
`"4,119,142,400"`), so a model quoting `ram_used_bytes` in `explanation` or
`evidence[].value` quotes the grouped, C2-safe form; the telemetry still carries
the real ungrouped integers and must not be dropped from the prompt (§6.1).

**The new metric groups are quotable by name, with no pattern change
(t_922b3268).** The `^[a-z0-9_]+(\.[a-z0-9_]+)*$` field-name pattern already
admits every new name, so they are *added to the accepted set*, never enabled by
loosening the pattern. The quotable names are: `pressure.cpu_some_avg10`,
`pressure.io_some_avg10`, `pressure.memory_some_avg10`,
`pressure.memory_full_avg10`, `oom_kill_count`, `pgmajfault_count`,
`mem_available_bytes`, `tcp_retransmit_segs`, `conntrack_count`, `conntrack_max`,
`disk.io_ticks_ms`, `disk.io_in_flight`, `disk.inode_used`, `disk.inode_total`,
`net.rx_errors`, `net.tx_errors`, `net.rx_dropped`, `net.tx_dropped`,
`lxd_instances.oom_kill_count`, `lxd_instances.pgmajfault_count`.

- `pressure.*` is the remediation-changing group (research `t_cae0dfa8` row 1):
  cpu/memory PSI high means memory-bound (restart is right); io PSI high with
  healthy RAM means I/O-bound (shed I/O, restart is wrong). The model quotes
  `pressure.*` **first** in `evidence[]` when it is present.
- The cumulative counters (`oom_kill_count`, `pgmajfault_count`,
  `tcp_retransmit_segs`, and the per-array counters) are raw monotonic totals
  in the samples; their **delta is derived** (the `derived` object, §3.2) and is
  never quoted in `evidence[]` as if it were a raw sample value. A quoted
  cumulative counter must carry its raw value and `seq`, never a delta.
- A field that is absent from the sample is not quotable at all — there is no
  value to quote, and a quoted `0.0` must be a *measured* `0.0` (genuinely no
  stall), not a stand-in for "unreadable".

`actions[]` is what the model **decided**; the *executed* result and its audit id
are joined by the reader (the controller API pairs the decision row with its
`remediation_audits` row), so the record never asserts an outcome the executor has
not produced. On the deterministic fallback the record is built from the fired
event instead and is labelled `generated: "fallback"` — see §8.1.

### 4.3 The decision trace (how the answer was reached, not just what it was)

The incident record answers *what happened, why, on what evidence, what was
done* — the conclusion. It does not answer **how the answer was reached**: which
rule fired, what evidence window the prompt carried, which template version/hash
was asserted, which endpoint answered (and with what latency/tokens), what the
model actually returned, which guardrail rules were evaluated and what each
decided, and what the executor then did. The decision trace is that record: one
ordered array of reasoning steps, persisted on the same row as the decision, so
the panel can show a thinking log rather than only a conclusion.

```json
"decision_trace": {
  "trace_version": 1,
  "decided_by": "model",
  "steps": [
    { "step": "threshold_fired", "ts": "2026-10-04T07:49:05.000Z",
      "label": "rule oom-guard-01 fired",
      "detail": "rule_id=oom-guard-01 event_id=01M4… fired_at=2026-10-04T07:49:05.000Z detection_latency_ms=2000",
      "state": "ok" },
    { "step": "evidence_window", "ts": "…",
      "label": "evidence window captured",
      "detail": "seq 1049..1050 (2 samples) C0 fields: cpu_util_pct, ram_total_bytes, ram_used_bytes",
      "state": "ok" },
    { "step": "prompt_built", "ts": "…",
      "label": "prompt rendered (C0 assertion ok)",
      "detail": "template=operator-decision v2 sha256=<payload-sha256> substituted_fields=N C0_assertion=ok",
      "state": "ok" },
    { "step": "model_call", "ts": "…",
      "label": "model answered",
      "detail": "endpoint=external model=deepseek-flash answered_by=forwarded-managed-model latency_ms=812 tokens={…} request_id=…",
      "state": "ok" },
    { "step": "reply_received", "ts": "…",
      "label": "model reply received",
      "detail": "{\"schema_version\":1,\"action_id\":\"instance.restart\",…}",
      "state": "ok", "truncated": false },
    { "step": "gates", "ts": "…", "label": "G7 rationale",
      "detail": "rule=G7 field=rationale decision=accept (C0-safe, within bounds)", "state": "ok" },
    { "step": "gates", "ts": "…", "label": "G9 incident record",
      "detail": "rule=G9 decision=accept (record clean or absent)", "state": "ok" },
    { "step": "action_decided", "ts": "…", "label": "action decided: instance.restart",
      "detail": "action_kind=instance.restart target=01M4349RPXWDC52ZDAVMPCMJSH", "state": "ok" },
    { "step": "executed", "ts": "…", "label": "action executed: ok",
      "detail": "result=ok latency_ms=45", "state": "ok" },
    { "step": "audit_recorded", "ts": "…", "label": "audit row written",
      "detail": "audit_id=… decision_id=…", "state": "ok" }
  ]
}
```

#### Step order (fixed, oldest first)

| # | `step` | produced at | carries |
|---|--------|-------------|---------|
| 1 | `threshold_fired` | provider (decision site) | `rule_id`, `event_id`, `fired_at`, detection latency ms |
| 2 | `evidence_window` | provider | the sample `seq` range and the C0 field names the prompt carried |
| 3 | `prompt_built` | `externalModelCall()` before-audit | template version + payload sha256 + substituted field count + the C0 assertion result |
| 4 | `model_call` | `externalModelCall()` after-audit | endpoint class `local`\\|`external`, model id, `answered_by`, latency ms, token counts (when reported), request id |
| 5 | `reply_received` | provider | parse outcome; the raw reply text, bounded and C2-scanned |
| 6 | `gates` (one per G7/G9 rule evaluated) | provider | which field, which rule, accept/clamp/drop/reject, reason |
| 7 | `action_decided` | provider | `action_kind`, `target` (or the fallback action + reason) |
| 8 | `executed` | loop, in the audit transaction | the audit row's result + remediation latency, or the error |
| 9 | `audit_recorded` | loop, in the audit transaction | the audit id and decision id |

The `evidence_window` step's `detail` names the C0 fields the prompt carried via
`_c0_field_names` — so with the metric groups it reads e.g.
`C0 fields: conntrack_count, conntrack_max, disk, lxd_instances, mem_available_bytes,
net, oom_kill_count, pgmajfault_count, pressure, tcp_retransmit_segs, …` — the new
field *names*, never their C1 identifiers or values.

#### Field reference

| field | type | required | bounds | meaning |
|-------|------|----------|--------|---------|
| `trace_version` | integer | yes | `== 1` | top-level version (§12: additive within schema `1`) |
| `decided_by` | string enum | yes | `model` \\| `fallback` \\| `deterministic` | who made the decision — the decider is visible in-band |
| `steps` | array | yes | `0 .. 24` entries | the ordered reasoning steps |
| `steps[].step` | string enum | yes | the nine names above | the step kind |
| `steps[].ts` | string RFC3339 | yes | — | when the step was recorded |
| `steps[].label` | string | yes | `≤ 128` chars | one-line human label for the panel |
| `steps[].detail` | string | yes | `≤ 512` chars | the specifics; C0-safe (see below) |
| `steps[].state` | string enum | yes | `ok` \\| `clamped` \\| `dropped` \\| `rejected` \\| `fallback` \\| `skipped` | the step's outcome |
| `steps[].truncated` | boolean | no | reply step only | true when the reply text was cut to the reply bound |

#### What must NOT appear (and how it is enforced)

The trace is sanitized with the **SAME gate as the incident record (G9,
`governance.scan_c2`) — never a parallel path.** C1 identifiers stay
transformed exactly as §4.2 requires (the trace names C0 *field names* and
C-classes, never the C1 values themselves).

* **The rendered prompt is NOT stored.** Per the owner's reserved disclosure
  decision, the `prompt_built` step stores the template version + payload
  sha256 + the substituted field count and their C0 assertion result — not the
  prompt text. The prompt remains C0-only by §7.2 and is never a client-visible
  string.
* The **raw model reply IS in scope** (the same class of content as
  `explanation`, which §4.2 already allows), bounded to `≤ 4096` chars with
  truncation flagged on the step.
* A **C2 hit in a step's `detail` drops that step** and records the drop in the
  guardrail list (`rule=G9, decision=drop`, mirroring how G9 drops a malformed
  incident entry).
* A **C2 hit in the model reply** does not drop the step: the reply text is
  replaced with `[dropped: C2]`, `state` becomes `rejected`, and the drop is
  recorded in the guardrail list.
* C2 material is the same class as everywhere else: application log lines,
  `argv`/command lines, environment variables, credentials, card numbers, raw
  identifiers, file paths/content, prompt text.

#### `decided_by` — label, never flatter

`decided_by` makes it impossible for a reader to mistake controller-generated
text for model output, or a forwarded answer for a local one:

* `model` — the trace must name the endpoint class (`local`/`external`) and
  `answered_by` in its `model_call` step.
* `fallback` — a **controller** trace: it must carry the fallback reason and the
  gate that rejected the model's answer (`rejecting_gate` in the
  `action_decided` detail), and `decided_by` is `fallback`, never `model`.
* `deterministic` — no trace is written at all (§8.1): a deterministic-mode row
  stays byte-identical and carries no trace columns.

The trace is **additive within schema `1`** (§12): it does not bump the
version, a model that produces no trace steps is unaffected, and the API
returns it alongside the existing fields without touching their types.

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
| `prompt.py` | versioned template `operator-decision v2` + JSON schema + few-shot; `build_messages(input)` |
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
  "messages": [ {"role": "system", "content": "<operator-decision v2 system prompt>"},
                 ...few-shot...,
                 {"role": "user", "content": "<JSON-serialized DecisionInput>"} ],
  "temperature": 0,
  "max_tokens": 2048,
  "response_format": {"type": "json_object"},
  "stream": false
}
```

- `max_tokens: 2048`. The v2 reply is the decision **plus** the §4.2 incident
  record, which costs ~700–1000 completion tokens; 512 (the value copied from the
  intent extractor) cut the JSON off mid-string on roughly half the live fires
  (measured 2026-10-17: `finish_reason=length`, 502 completion tokens, a 1458-char
  reply ending inside `"value":"`) and each one fell back as `schema_violation`
  (t_77f26781). `TERMINI_INFERENCE_MAX_TOKENS` overrides the default.

- The system prompt is the versioned `operator-decision v2` template
  (`governance.md` §7.2: static template, typed C0 placeholders only).
- The user message is the `DecisionInput` serialized as compact JSON, embedded in
  the template's single `{{decision_input}}` placeholder. Integer metric values of
  byte magnitude are rendered **thousands-grouped** (`4119142400` →
  `"4,119,142,400"`, the shape `safe_int_display` produces) so a model that quotes
  a metric while explaining itself emits the C2-safe grouped form, not a bare
  10–15 digit run that G9 would read as a phone number (t_220a468a); the telemetry
  values themselves are never changed — do not "fix" this by dropping the numbers
  from the prompt.
- `response_format: json_object` (or a GBNF grammar) is **required** so the output
  parses deterministically and is never free-form.

### 6.2 Parsing and light repair (`adapter.parse`)

The adapter converts the model's completion text into a `DecisionResponse` or a
typed `DecisionError`. It performs **parse + light repair only**; the hard guardrail
remains controller-side (§7).

1. Strip stray markdown fences (as `termini/intent/extractor.py` does).
2. `json.loads`; if the whole string is not JSON, decode **the first** JSON
   object/array in it (a fenced or prose-wrapped reply). A later opener is not
   tried: in a *truncated* reply it is the `params`/`evidence` fragment of the
   cut-off decision, and decoding that fragment reports a plausible, wrong cause
   ("action_id must be a non-empty string, got None") for a reply that carried the
   id. A reply with no complete JSON → `DecisionError("parse_error")`; when the
   reply is *truncated* (`finish_reason == "length"`, or structurally unterminated
   JSON) the detail says so and names the length, so the next investigation starts
   from the real cause.
3. Coerce `confidence` to `float`; reject if non-numeric → `DecisionError("schema_violation")`.
4. Ensure `action_id` is a non-empty string — falling back to `actions[0].action_kind`
   when that names a real action (§4.1) — and ensure `params` is an object (default `{}`);
   fill a missing `params.instance_name` from `actions[0].target` (§4.1).
5. Drop `params` keys not in the action's `params_schema` (never pass unknown keys through).
6. Default missing optional params from `ACTION_SPACE`.
7. Cap `rationale` to 1024 chars.

**One bounded repair retry (`adapter.decide`).** When the first reply fails for a
*mechanical* reason — cut off at `max_tokens`, an incident record with no action
anywhere, or a missing rationale — the adapter asks the same model once more,
appending the first answer and a corrective instruction (return one complete,
shorter object). A usable second answer is returned as the decision and the
response records `repairs`; a second failure returns its own `DecisionError` naming
both attempts. The retry is exactly one call: it cannot loop, and it never invents
an action.

`DecisionError` carries exactly one of the `FALLBACK_REASONS` codes (§10.1) so the
controller can fall back deterministically. The adapter never fabricates an action:
if the output cannot be repaired into a well-shaped `DecisionResponse`, it returns
a `DecisionError`, never a guess. The error also carries `raw` (bounded), and the
controller records both that text and a C0-safe shape summary (`chars`, top-level
keys, `truncated`) on the decision trace and in the operator journal, so a
violation is diagnosable from the record (§4.3, §11.1).

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
| G9 | incident-record sanitization | `incident_title` `≤ 160`, `explanation` `≤ 2048`, `evidence[]` `≤ 12`, `actions[]` `≤ 8`; every string C0-safe; `evidence[].field` matching `^[a-z0-9_]+(\.[a-z0-9_]+)*$` and `≤ 64`; `actions[].action_kind` ∈ `ACTION_SPACE` | truncate to the bound; drop an entry that fails its pattern/enum/C2 scan and record the drop in the guardrail list; on a C2 hit in `incident_title`/`explanation` → fallback `schema_violation` (same rule as G7) |

G4/G5 **clamp**, never reject, on numeric/unknown-key issues; G1/G2/G3/G6/G7 are
hard **fallbacks**. G9 never rejects the *decision* over an entry — a malformed
evidence row is dropped and the record keeps the rest — but a title or an
explanation carrying C2 material is a hard fallback, exactly as `rationale` is.
The all-or-nothing rule from `controller-api.md` §5.3 does **not** apply here — a
fallback produces a deterministic action, not a rejection.

**No new gate for the metric groups (t_922b3268).** None of the §4.12–§4.14
fields introduces a new remediation precondition (the research card `t_cae0dfa8`
§4 reached the same conclusion): they change *which* action the model chooses and
how it is explained, not *whether* an action is safe to execute. G1–G9 run
unchanged, and G9's `evidence[].field` pattern already admits every new dotted
name, so no G9 bound changes either. The new `derived` object is guarded by the
§7.2 class assertion in `externalModelCall()` (deny-by-default on its keys), not
by a new guardrail rule — removal of that assertion is pinned by a test.

**No new detection rule either (same reason, t_922b3268).** The §4.12–§4.14
fields carry **no threshold and no rule id**: `node-telemetry.md` §5 still lists
only the four frozen rules (`oom-guard-01`, `disk-fail-01`, `disk-degrade-01`,
`gpu-oos-01`), and none of the new fields participates in deterministic
detection. They are **model inputs only** — the diagnosis reads `pressure.*` /
`oom_kill_count` / the deltas in the prompt and reasons over them, within the
existing G1–G9 boundary. A dashboard must render their values without inventing
a client-side threshold (a client-side flag would be a second, silently-diverging
rule set), and a deterministic PSI/OOM rule — if one is ever wanted — is a
separate contract + detection change (`node-telemetry.md` §5 + the controller's
detection layer), owned by a follow-up card, not something to assume exists.

The guardrail returns a `RemediationAction` (built from the *validated* response,
never the raw output) plus the `guardrail` decision list for the audit record.

### 7.1 F05 restart-refusal guard (execution-time, not decision-time)

A distinct, **execution-time** guard next to G1–G9: while a node's **latest
telemetry sample** still shows any `disk[].mount_ro == true`, an
`instance.restart` or `instance.start` on that node is **refused** before it
reaches `LxdExecutor`, with the machine-readable reason `disk_fenced` recorded on
the audit row (`reason` column) and a `fenced` node status on the dashboard
("needs a human: disk failed, workload fenced"). `instance.stop` /
`instance.delete` / `image.prune` are always allowed — stopping writes is the
correct half of the response.

The guard lives in `operator/decision.py` (`fenced_action_refusal` /
`fenced_refusal_detail` / `read_only_mounts`) and is applied by the loop and the
standalone CLI against the C0 `DecisionContext.state`. It is **guardrail-level,
not prompt-level**: it is enforced in code regardless of what the model or the
playbook decided, and it needs no new action in the allowlist (a `restart`/`start`
that would resume writes onto the failed disk never reaches the executor).

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

### 8.1 The fallback still produces an incident record — labelled

An incident that silently loses its explanation the moment the model is down is
the same defect as a blank dashboard: the reader cannot tell "no incident" from
"no model". So the fallback path **also** writes an incident record, built from
the fired `HealthEvent` (its `rule_id`, `metric`, `value`, `threshold`, sample
`seq` and the target the deterministic provider chose) — and it is **labelled**:

- `generated: "fallback"` in the record, and `incident_generated = "fallback"`
  on the persisted row, which the dashboard renders as its own visible state;
- `explanation` names the reason in the text itself
  (`"model unavailable (<fallback_reason>); the deterministic playbook chose …"`);
- `evidence[]` contains **only** the event's own C0 fields — never a model
  sentence, never a fabricated sample. Nothing that the fallback produces is
  presented as model output, and no field of a fallback record claims the model
  said anything.

`generated` is `"model"` only when the string that reaches the record is the
model's own validated output (i.e. `source == "model"` and the response passed
G9). A record with `generated: "model"` that did not come from a model is a
contract violation, and the test suite pins the two paths apart.

**Fallback parity with the metric groups (t_922b3268).** The fallback record is
built only from the fired event's own C0 fields (`metric`, `value`, `threshold`,
sample `seq`). None of the §4.12–§4.14 fields is part of the `HealthEvent`, so
there is nothing new to fill — and the fallback never fabricates a pressure or
counter value. The labelled record therefore stays exactly as it was: a fallback
is labelled, not blank, and the metric groups change nothing on this path.

In `deterministic` mode (`TERMINI_OPERATOR_DECISION_MODE=deterministic`, §12) no
incident record is written at all: that mode must stay **byte-identical** to the
pre-amendment loop, so the decision row carries no incident columns. The
dashboard renders that state as "no incident record (deterministic mode)" rather
than an empty card.

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

Migration `0016_incident_record.sql` adds the incident record (§4.2) to the same
row — same transaction as the decision write, so a record can never exist without
its decision:

```sql
ALTER TABLE remediation_decisions ADD COLUMN incident_title TEXT;
ALTER TABLE remediation_decisions ADD COLUMN incident_explanation TEXT;
ALTER TABLE remediation_decisions ADD COLUMN incident_evidence TEXT;   -- JSON array (§4.2)
ALTER TABLE remediation_decisions ADD COLUMN incident_actions TEXT;    -- JSON array (§4.2)
ALTER TABLE remediation_decisions ADD COLUMN incident_generated TEXT;  -- model | fallback
```

Migration `0017_decision_trace.sql` adds the §4.3 decision trace to the same row —
same statement/transaction as the decision write, so a trace can never exist
without its decision:

```sql
ALTER TABLE remediation_decisions ADD COLUMN decision_trace TEXT;   -- JSON object (§4.3)
ALTER TABLE remediation_decisions ADD COLUMN trace_version INTEGER; -- §4.3 top-level version
```

- `decision_source` ∈ `deterministic | model | fallback`.
- `confidence` is `NULL` for deterministic decisions.
- `fallback_reason` is one of the §10.1 codes, `NULL` otherwise.
- `incident_generated` ∈ `model | fallback`; all five incident columns are `NULL`
  for a `deterministic`-mode row (§8.1), which is what keeps that mode
  byte-identical to the pre-amendment loop.
- `decision_trace` is the §4.3 trace object (or `NULL`); `trace_version` is its
  top-level version (or `NULL`). Both are `NULL` for a `deterministic`-mode row
  (§8.1). The trace is written in the same statement as the decision with its
  steps through `action_decided`; the `executed` and `audit_recorded` steps are
  appended in the **audit's** transaction (`store.append_decision_trace`) — the
  earliest they can exist — so a trace never asserts an outcome before the
  executor produced it, mirroring §4.2's rule for the incident record.
- The API (`GET /v1/operator/decisions`) returns the row verbatim (with
  `decision_trace` parsed to an object, not a string), so the incident record
  and the trace are exposed without a second endpoint or a second query.

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
- `derived` (and the metric groups in the prompt) are computed/assembled only on
  the model path (`build_decision_input`); the deterministic provider never
  reaches it, so deterministic mode writes no `derived`, no incident and no trace
  columns — the byte-identical guarantee is unchanged by the metric groups, and a
  test pins it.
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
| `termini/decision/prompt.py` | **NEW** — `operator-decision v2` template + JSON schema + few-shot + `build_messages` |
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

### 14.5 Incident record — exact call sites (t_957956e5)

| path | change |
|------|--------|
| `termini/decision/types.py` | `DecisionResponse` gains `incident_title`/`explanation`/`evidence`/`actions` + the §4.2 bound constants |
| `termini/decision/prompt.py` | the JSON schema, the rules block and the few-shot examples carry the incident record (template then `operator-decision v1`; bumped to `operator-decision v2` by t_922b3268 for the metric groups) |
| `termini/decision/adapter.py` | parse + light repair of the four fields (bounded, typed, entries dropped when malformed) |
| `controller/app/operator/decision.py` | `DecisionResult.incident`, `GuardrailResult.incident`, guardrail **G9**, and the §8.1 labelled fallback record |
| `controller/app/operator/store.py` | `insert_decision(..., incident=None)` writes the five columns in the same statement |
| `controller/app/operator/loop.py` | passes `result.incident` to `insert_decision` |
| `controller/app/migrations/0016_incident_record.sql` | **NEW** — §11 DDL |
| `dashboard/js/core/decisions.js` | maps the row's incident columns into the view model (`incident`, `incidentSource`) |
| `dashboard/js/ui/decisions.js` | renders the **incident card** (title, explanation, evidence list, actions + audit result); the log stream stays a secondary view |
| `tests/test_incident_record.py` | **NEW** — stub provider: parsed, persisted, exposed, and the labelled fallback path |

### 14.6 Decision trace — exact call sites (t_e07abffc)

| path | change |
|------|--------|
| `termini/decision/types.py` | the §4.3 trace bound constants + `DecisionResponse.raw`/`usage` (additive) |
| `termini/decision/adapter.py` | `parse()` retains the raw completion text on `DecisionResponse.raw` |
| `controller/app/operator/decision.py` | **NEW** `DecisionTrace` (bounded, G9-sanitized); `DecisionResult.trace`; `ModelDecisionProvider` records `threshold_fired`/`evidence_window`/`prompt_built`/`model_call`/`reply_received`/`gates`/`action_decided`; the §8.1 fallback trace |
| `controller/app/operator/store.py` | `insert_decision(..., trace=None)` writes `decision_trace`/`trace_version` in the same statement; `append_decision_trace`; `list_decisions` parses `decision_trace` |
| `controller/app/operator/loop.py` | passes `result.trace` to `insert_decision`; appends `executed`/`audit_recorded` in the audit's transaction |
| `controller/app/migrations/0017_decision_trace.sql` | **NEW** — §11 DDL |
| `tests/test_decision_trace.py` | **NEW** — step order, caps, C2 in a step detail and in a reply, deterministic writes no trace, labelled fallback trace |

### 14.7 Metric expansion in the diagnosis — exact call sites (t_922b3268)

| path | change |
|------|--------|
| `termini/decision/types.py` | `DecisionInput` gains `derived` (additive within schema `1`, §3.2) |
| `termini/decision/prompt.py` | template bumped `operator-decision v1` → `v2`; the system prompt documents the §4.12–§4.14 fields and the `derived` deltas; the few-shot OOM example quotes `pressure.memory_some_avg10` first |
| `termini/decision/adapter.py` | unchanged (the additive `derived` field rides the existing `to_dict`/`from_dict` path) |
| `controller/app/operator/decision.py` | **NEW** `build_window_deltas()` (the controller-computed derived deltas, §3.2); `build_decision_input` fills `derived` |
| `controller/app/operator/external_call.py` | `derived` admitted to `_DECISION_INPUT_KEYS` + a deny-by-default check over `_DERIVED_KEYS`; template fallback literal bumped to `operator-decision v2` |
| `tests/test_metric_expansion_diagnosis.py` | **NEW** — new fields reach the payload + are provably C0; derived deltas (positive, missing, reset); a wrongly-classed field fires the assertion and never reaches the model; fallback still records; deterministic computes nothing |

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
