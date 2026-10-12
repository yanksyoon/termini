# Termini — Governance Contract: PII Redaction & External-Call Boundary

Status: frozen for W1 build
Owner: architect
Consumed by: controller (t_260694d8), infra (t_134e2f24), agents (t_1addb6fd), integration (t_5fb81d14)
Supersedes: project.md adversarial note #1 ("prompt parser and AIOps error log analyzer are constantly sending workload context to Google Cloud APIs")

This document defines (a) the on-prem PII/regex redactor that runs on customer nodes before any GCP-bound call, and (b) the hard boundary for any external model call. It is normative: the controller, node bootstrap daemon, and agent runtime MUST implement the hooks below. Where this doc and another contract disagree, this doc wins for anything that crosses the premises boundary.

---

## 1. Scope and purpose

Termini's value proposition is "your hardware, your data, our operations." The promise that customer data never leaves the premises is an enforceable property, not a slogan. This contract makes it enforceable by defining:

1. A **data classification** taxonomy with exactly three levels (C0/C1/C2).
2. A **redaction pipeline** that runs on the customer node, in a fixed order, before any bytes leave for GCP.
3. **Fail-closed** behavior for redaction failure, bypass, or anomaly.
4. An **allowlist** of telemetry fields that may leave the premises, and an explicit deny list.
5. The **external model call boundary**: external calls are scoped to hardware/OS telemetry only, never application logs or customer payloads.
6. **Controller enforcement hooks** so the cloud side re-verifies every inbound payload and every outbound model call.
7. **Audit logging and traceability** so a verifier can reconstruct and reconcile every external call.
8. **Developer rules** that are specific enough to be enforced in code review and CI.

Two egress points exist and both are governed by this doc:

- **Egress A — customer node → GCP Controller.** Node bootstrap daemon sends registration, heartbeat, telemetry, and structured allocation intents. The on-node redactor gates this.
- **Egress B — GCP Controller → external model API.** The AI operator loop (and, if used, intent extraction) may call a cloud model. The controller-side gate and prompt-template rules govern this.

---

## 2. Threat model and adversarial assumptions

We assume an adversary who:

- Can read any payload that leaves the premises (network capture, GCP-side logs).
- Can submit crafted input designed to smuggle PII or log content through the redactor (e.g., base64, Unicode homoglyphs, split tokens, log lines pasted into a "note" field).
- Can attempt to disable or bypass the redactor via config, environment, or a code change.
- May replay or mutate a captured payload to tamper with the audit chain.

Consequences that shape the design:

- **Deny-by-default** for egress fields (an unknown field is dropped, never passed).
- **Fail-closed** everywhere: if the redactor cannot guarantee a payload is safe, the payload does not leave.
- **Defense-in-depth**: redaction on the node is the first gate; the controller re-verifies independently and rejects on any discrepancy.
- **Tamper-evident audit chain**: call ids + payload hashes are chained so silent insertion/rewrite is detectable.

Rejected alternative (recorded for review): a **controller-side-only** redactor. Rejected because by the time the controller can redact, the data has already left the customer's premises and transited the network — the promise is already broken. Redaction MUST occur before the egress socket is opened.

Rejected alternative: **denylist-only** filtering. Rejected because it cannot keep up with novel identifiers. We use an allowlist for structure plus a denylist for content.

---

## 3. Data classification taxonomy

Every datum that can appear in a payload, log line, or model input carries exactly one class. The registry below is the single source of truth; the controller and node share it.

| Class | Name | Definition | Egress rule |
|-------|------|------------|-------------|
| C0 | SYSTEM_TELEMETRY | Hardware/OS metrics with no identifying or content-bearing value | Allow after allowlist + schema validation |
| C1 | RESTRICTED_IDENTIFIER | Identifiers/metadata that could deanonymize or correlate (hostname, IP, MAC, serials) | Allow only after transformation (hash/truncate/tokenize) |
| C2 | CUSTOMER_DATA | Application logs, customer payloads, prompts, file contents, credentials, process details | NEVER egress; presence in an egress structure triggers fail-closed |

C2 is the critical class: **application logs and customer payloads are C2 and may never leave the premises.** Any telemetry field that would carry such content is classified C2 and is therefore excluded from the egress allowlist entirely.

---

## 4. Telemetry allowlist (Egress A)

The node bootstrap daemon may send ONLY the following fields to the controller. The same list is the controller-side re-verification allowlist. Field names are canonical; implementations MUST use these exact keys.

### 4.1 C0 fields — allow (no content redaction, still logged and counted)

| Field | Type | Notes |
|-------|------|-------|
| `node_id` | string (ULID) | Controller-assigned; not customer-revealing |
| `ts` | string (RFC3339) | Sample timestamp |
| `uptime_sec` | uint64 | Host uptime |
| `cpu_util_pct` | float32 | 0–100 per-core aggregate |
| `cpu_core_count` | uint16 | |
| `ram_total_bytes` | uint64 | |
| `ram_used_bytes` | uint64 | |
| `swap_used_bytes` | uint64 | |
| `disk_total_bytes` | uint64 | Per-device |
| `disk_free_bytes` | uint64 | Per-device |
| `disk_write_latency_ms` | float32 | Scenario B signal |
| `disk_mount_ro` | bool | Read-only detection |
| `gpu_present` | bool | |
| `gpu_model` | string | Fixed enum-ish string (e.g. "T4"); no serial |
| `gpu_util_pct` | float32 | |
| `gpu_vram_total_bytes` | uint64 | |
| `gpu_vram_used_bytes` | uint64 | |
| `net_rx_bytes` / `net_tx_bytes` | uint64 | Per-interface counters |
| `net_latency_ms` | float32 | |
| `cpu_temp_celsius` | float32 | Thermals |
| `gpu_temp_celsius` | float32 | |
| `load_avg_1m` | float32 | |
| `os_name` | string | Enum ("Ubuntu") |
| `os_version` | string | Enum ("24.04") |
| `kernel_version` | string | Enum-ish; sanitized |
| `lxd_cluster_role` | string | Enum ("member") |

#### 4.1.1 C0 extension groups (registered)

The following structured C0 groups are registered in the egress allowlist
(same deny-by-default review as the flat fields above). They carry no customer
content and are required by the AI operator and charts. Canonical definitions:

| Group | Where defined | Notes |
|-------|---------------|-------|
| `lxd_instances[]` | `node-telemetry.md` §4.6 | Per-VM C0 metrics, keyed by controller-issued `vm_id` (never the LXD instance name). |
| `inference{}` | `node-telemetry.md` §4.7 | llama.cpp router health/throughput (present on the inference node only). |
| `services[]` | `node-telemetry.md` §4.8 | systemd unit state, fixed name allowlist. |
| `agents[]` | `agent-runtime.md` §13.2 | Per-agent-VM C0 status counters (state, queue depth, task counters). |

### 4.2 C1 fields — allow only after transformation

| Field | Type | Transformation |
|-------|------|----------------|
| `hostname` | string | SHA-256 hash, hex, truncated to 16 chars + salt derived from `project_id` |
| `ip_local` | string | Redact: replace last octet with `.0/24` for IPv4; for IPv6 keep only /48 prefix |
| `ip_public` | string | DENY by default — see 4.3; when permitted, /24 truncation |
| `mac_address` | string | SHA-256 hash only |
| `project_id` | string (ULID) | Allow verbatim (controller-issued, non-revealing) |
| `lxd_cluster_member` | string | Replace with `node_id` |

### 4.3 C2 fields — never egress (explicit deny list)

Any of the following in an egress structure causes fail-closed (see §6):

- Application log lines / syslog content (`/var/log/*` tails)
- Customer payloads: prompts, agent conversation text, task bodies, code, documents
- File contents or paths under customer data dirs
- Process command lines (`/proc/*/cmdline`), environment variables, usernames, `/etc/passwd`-derived data
- SSH keys (public or private), certificates, tokens, credentials
- MAC addresses in raw form, full IP addresses, serial numbers, asset tags
- Any field not listed in 4.1 or 4.2 (deny-by-default)

**Registration bootstrap exception (bounded C1).** The one-time registration body
(`controller-api.md` §4.1, `node-bootstrap.md` §5.2) carries the node's own
machine-inventory identifiers in the clear as a bounded C1 bootstrap exception:
`machine.hostname`, `machine.underlay_ip`, and `machine.nics[].addresses`. These are
the node's own addresses (not customer data) and are required for LXD cluster
join/routing and controller inventory. Raw MAC is **not** part of this exception:
the `nics[].mac` field is removed from the registration body, and MAC never
egresses except as the hashed C1 `mac_address` telemetry field (§4.2). The
exception applies to the `register`/`reconnect` endpoints only and is recorded in
the audit log.

---

## 5. On-node redaction pipeline (Egress A)

The redactor runs inside the node bootstrap daemon process, immediately before the egress adapter opens a socket. It is a pure function: `payload' = redact(payload, config)` where `config` is versioned and pinned. Stages run in this exact order:

1. **Structural allowlist.** Reject/remove any key not present in the allowlist (4.1 + 4.2). Unknown keys are dropped, not passed through.
2. **Size and count caps.** Reject if field count > 256 or serialized size > 16 KiB. Reject if any string field exceeds 512 bytes. (Prevents bulk smuggling.)
3. **C2 presence scan.** Walk the remaining structure; if any value is tagged C2 or matches a C2 signature (log format, key material, prompt-like text), fail-closed.
4. **C1 transformation.** Apply the transformations in 4.2 (hash hostname, truncate IPs, hash MACs).
5. **Regex + entity redaction.** Over all remaining free-text values, apply the entity denylist (5.1). Replace matches with `[REDACTED:<type>]`. This is defense-in-depth in case a C0 field accidentally carried content.
6. **Schema re-validation.** Re-check types, ranges, and field presence; fail-closed on any violation.
7. **Attach redaction metadata.** Add `_redaction` object: `{version, config_hash, result}`.
8. **Serialize + egress** via HTTPS with mutual TLS/API key, or **block** per §6.

### 5.1 Entity denylist (regex)

Ordered; applied left-to-right. All replacements are irreversible (original value never appears in output).

| Entity | Pattern (PCRE) | Replacement |
|--------|----------------|-------------|
| Email | `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` | `[REDACTED:email]` |
| Phone (E.164) | `\+?[0-9]{7,15}` guarded by non-digit boundaries | `[REDACTED:phone]` |
| IPv4 | `\b(?:\d{1,3}\.){3}\d{1,3}\b` | `[REDACTED:ipv4]` |
| IPv6 | `\b(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\b` | `[REDACTED:ipv6]` |
| MAC | `\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b` | `[REDACTED:mac]` |
| SSH/private key | `-----BEGIN [A-Z ]*PRIVATE KEY-----` … `-----END` | `[REDACTED:key]` |
| Cloud creds | `\b(AKIA|ASIA)[A-Z0-9]{16}\b` | `[REDACTED:aws-key]` |
| API key/token/secret | `(?i)\b(api[_-]?key\|token\|secret\|password)\s*[:=]\s*\S+` | `[REDACTED:credential]` |
| Card number | `\b(?:\d[ -]?){13,16}\b` | `[REDACTED:card]` |
| UUID | `\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b` | `[REDACTED:uuid]` |
| Base64 blob (>40 chars) | `\b[A-Za-z0-9+/=]{40,}\b` | `[REDACTED:b64]` (content is opaque; drop) |

Note on adversarial input: base64/hex/Unicode obfuscation of PII is addressed by (a) the 512-byte string cap, (b) the base64-blob rule, and (c) the fact that C2 detection is signature-based and fail-closed. An adversary who can fully control a C0 field's content can at worst trigger a block — they cannot force content to egress.

### 5.2 Config versioning

The redactor config (`patterns`, `allowlist`, `caps`, version) is a versioned artifact (`redactor-config v{N}`) distributed with the bootstrap image. The node sends `_redaction.version` and `_redaction.config_hash` on every call. The controller asserts the received version is >= the minimum it expects (see §7); a stale config is grounds for rejection.

---

## 6. Fail-closed behavior

The egress adapter blocks the call (opens no socket, sends nothing) when ANY of the following occurs:

- Redactor module missing, disabled, or crashed during redaction.
- Regex catastrophic backtracking or redaction timeout (> 50 ms for the whole pipeline).
- Any C2 content detected (structural or signature).
- Output fails schema re-validation or a cap check.
- A string field required to be redacted (C1) was found unredacted.
- Redaction produced zero change where the config expected a change (configurable heuristic).
- Redactor config version below the controller's minimum.

On block, the node:

1. Writes a local audit record with `result: blocked` and a reason (see §8).
2. Increments a local `governance_fail` counter exposed only as an aggregate (itself C0).
3. MAY emit a single fixed-schema signal to the controller: `{node_id, code: "REDACTION_FAILED", ts}` — no payload, no detail. This is optional; the controller's heartbeat-gap detector independently flags a node that has stopped sending telemetry as "governance-stalled" and raises an alert.

The controller treats a missing/stalled node the same as a rejected payload: alert the operator, do not auto-remediate across the boundary, and do not retry external model calls on unverified input.

---

## 7. External model call boundary (Egress B)

External model calls (the controller's AI operator loop, and intent extraction if not run on-prem) are permitted ONLY on the following inputs, never on application logs or customer payloads:

1. **C0 telemetry** from the node allowlist (§4.1) — after controller re-verification.
2. **C1 identifiers** only in their transformed form (hashed hostname, truncated IP).
3. **Structured allocation intents** — the JSON output of intent extraction (counts, vcpus, ram_gb, target_node), which is non-sensitive. Intent extraction itself runs **on-prem by default** (DeepSeek flash on node C) so raw prompts never cross the boundary; the external-model fallback path is restricted and requires the prompt to have passed on-node redaction first (§5), and even then is classified C1.

Explicitly prohibited as external-model input:

- Application logs, syslog content, error-log analyzer output derived from customer logs.
- Customer payloads, prompts, agent conversation, task bodies.
- Any unredacted C1 identifier or any C2 content.

### 7.1 Model routing (records the fixed decision)

- On-prem inference (llama.cpp, node C): **deepseek-flash** and **deepseek-v4-pro** — these are the only model aliases wired to on-prem inference. They handle customer payloads, agent workloads, and on-prem intent extraction. Nothing these models touch leaves the premises.
- The aliases are **distills**, not the DeepSeek-V4 MoE family (pinned by BQ-1, `docs/architecture.md` §8 R11): `deepseek-flash` = DeepSeek-R1-Distill-Llama-8B Q4_K_M (~6 GiB), `deepseek-v4-pro` = DeepSeek-R1-Distill-Qwen-14B Q4_K_M (~18 GiB). Target GPU NVIDIA L4 24 GiB; T4 16 GiB → `deepseek-v4-pro` unavailable (flash-only).
- External model (GCP): the boundary below is defined for any **off-prem** model call. In the shipped demo the operator's decision model is the **on-prem llama.cpp service on Node C** (`http://10.10.0.13:8080`, CPU-only), so the operator loop makes **no external call at all** — a strictly stronger position than an off-prem decision model. The prompt template is static and versioned; see 7.2. Any future off-prem model call must be added here with a rationale.

If a future change needs a second external model, it must be added here with a rationale; no implicit external call is allowed.

### 7.2 Static prompt template rule

The AI operator's prompt is a versioned template (`operator-prompt v{N}`) whose only dynamic placeholders are **typed C0 fields** (`{{cpu_util_pct}}`, `{{ram_used_bytes}}`, `{{disk_write_latency_ms}}`, `{{gpu_vram_used_bytes}}`, `{{net_latency_ms}}`, etc.). No free-text, log tail, or user string may be interpolated. The controller wraps every model call with `externalModelCall()` which asserts the template version and the class of every substituted field (all C0) before sending.

`externalModelCall()` is implemented as `controller/app/operator/external_call.py::external_model_call()`, invoked from `controller/app/operator/decision.py::ModelDecisionProvider._call_model` so it wraps **every** decision call. It reuses the same taxonomy as the inbound gate (`fields.py` allowlist, `governance.scan_c2`, `governance.check_c1_transforms`) and is **deny-by-default**: a field that is not on the C0/C1 allowlist is a violation, not a pass, because "not on the allowlist" is not the same as "known to be C0". A violation raises before `send()` is called, so the payload is **never transmitted**; the decision loop maps it onto the deterministic fallback (`schema_violation`), fail-closed per §6. The guard is **destination-agnostic** — it applies to the on-prem Node C endpoint exactly as it would to an off-prem one, because hosting the model on-prem narrows the blast radius of a mis-classified field but does not repeal the boundary.

### 7.3 Model output is constrained, not free-form

The **decision step** — the step that turns a fired trigger into a remediation
action — may be served by a model (the AI operator) or by a declarative playbook.
Both are conforming implementations of this contract. What is normative here is the
constraint set, not the mechanism that produces the choice.

A model output never reaches execution directly. Every chosen action passes through:

1. The deterministic **guardrail/clamp validator** (`docs/contracts/vm-allocation.schema.json`; controller contract t_260694d8) — clamps/rejects out-of-range specs.
2. A **remediation command allowlist** — only pre-approved LXD/controller actions may execute. The model **selects among them**; it never emits free-form shell, and an action outside the allowlist has no valid encoding in the decision schema, so it is rejected before it can be applied.
3. A **mandatory deterministic fallback** — if the model is unavailable, times out, exceeds its latency budget, or returns output that fails schema validation or the allowlist, the operator falls back to the deterministic provider and records the fallback *and its reason*. Fail-closed is never traded for availability.

**Provenance is recorded, never inferred.** Every audit row for an autonomous action
carries the decider's identity — `decision_source` (`deterministic` | `model` | `fallback`), plus the
provider and prompt-template version when a model was used — together with the
`rationale` for the action (§8.2). An autonomous action whose provenance is missing or
inconsistent is a validation failure, not a default.

Unchanged by this amendment: the model never emits free-form shell; no C2 content and
no unredacted C1 identifier may enter a prompt (§7, §4.3); no implicit external call is
permitted (§7.1); the prompt template stays static and versioned (§7.2). "Advisory" was
the old name for this section — the constraint set, not the word, is what is frozen.

The trade this amendment makes, stated plainly: the *mechanism* of the decision is no
longer guaranteed to be deterministic or reproducible — only its *safety envelope* is.
Reproducibility still holds for the playbook provider, and can be approximated for a
model provider by pinning seed/temperature, but a verifier must not assume that two
runs of a model decider produce the same action. Assert the envelope, not the choice.

---

## 8. Audit logging and traceability

### 8.1 On-node audit record (JSONL, local, append-only)

Written for every egress attempt — success and block:

```
{
  "call_id": "01H...ULID",
  "ts": "2026-10-04T07:50:00Z",
  "dest": "https://controller.<project>.run.app/v1/telemetry",
  "payload_sha256": "…",
  "pre_bytes": 812, "post_bytes": 540,
  "redactor_version": "redactor-config v3",
  "redactor_config_hash": "…",
  "result": "ok" | "blocked",
  "block_reason": "…" | null,
  "dropped_fields": ["hostname","ip_local","mac_address"],
  "redacted_entities": {"email":1,"ipv4":2},
  "class_counts": {"C0": 22, "C1": 2, "C2": 0}
}
```

### 8.2 Controller-side audit record

Written for every inbound payload and every external model call:

```
{
  "call_id": "01H...ULID",
  "source_node_id": "01H...",
  "ts": "…",
  "endpoint": "/v1/telemetry",
  "result": "accepted" | "rejected",
  "rejection_reason": "…" | null,
  "redactor_version_asserted": "redactor-config v3",
  "governance_violation": false
}
```

External model calls additionally record `{model, prompt_template_version, input_field_classes:["C0"], output_action}`.

### 8.3 How a verifier traces every external call

1. Every call has a unique **call_id** (ULID). Node and controller record the same id.
2. Each side records **payload_sha256**; a verifier recomputes the hash and matches.
3. **Hash chain**: the node maintains `chain = H(prev_chain || payload_sha256 || call_id)`; the controller maintains a parallel chain per node. Divergence between the two chains proves tampering or missing calls. The node exposes its chain tip as a C0 field (`governance_chain_tip`) so the controller can reconcile continuously.
4. The controller exposes a read-only, authenticated `GET /v1/operator/audits?node_id=&limit=` endpoint (`controller-api.md` §4.4) so a verifier can pull and reconcile recent records. (There is no `GET /v1/governance/audit` endpoint.)
5. Every external model call is traceable via call_id → prompt template version → input field classes (all C0). A verifier can confirm no C1/C2 ever entered a model call.

---

## 9. Controller enforcement hooks (normative)

The controller MUST implement these as code, not policy prose:

1. `governanceGate` middleware wraps every inbound endpoint (`/register`, `/telemetry`, `/intent`, `/heartbeat`). It runs the same allowlist + caps + C2 scan + C1-transform-check as the node and rejects on any discrepancy with HTTP 422 `GOVERNANCE_REJECT`.
2. `assertRedactorVersion(min)` — reject payloads whose `_redaction.version` is below the minimum.
3. `externalModelCall()` wrapper — asserts static template version, all-C0 field classes, and writes the audit record before and after the call. Implemented: `controller/app/operator/external_call.py` (wired into `ModelDecisionProvider._call_model`; see §7.2).
4. Model output → `guardrailClamp()` → `commandAllowlist()` before any action; free-form model output is never executed.
5. Any `governance_violation: true` event fires an operator alert and is retained in a separate violation ledger, never silently dropped.

---

## 10. Developer rules (enforceable in CI/review)

1. The egress field registry (§4) is the single source of truth. Adding a field requires a C-class, a redaction rule if C1, and review. **New egress fields default to denied.**
2. No raw log, payload, prompt, or user string may be interpolated into any egress payload or model prompt.
3. The redactor ships with adversarial fixtures (PII in every field, base64 smuggling, oversized payloads) that must pass in CI before merge.
4. `redact=false`, dry-run, or bypass flags are removed at build time in production artifacts.
5. Every external call writes an audit record before send and after response; no call without a call_id.
6. Remediation actions are restricted to the command allowlist + clamp validator; no free-form execution.
7. Controller-side logs may contain C0/C1 only; writing C2 to controller logs is a violation.

---

## 11. Acceptance mapping

| Acceptance criterion | Where satisfied |
|---------------------|-----------------|
| Every GCP-bound payload has a documented, enforceable redaction gate | §5 pipeline + §7 controller gate + §9 hooks |
| No application logs or customer data can leave unredacted | §3 C2 class + §4.3 deny list + §6 fail-closed |
| Governance rules specific enough for implementation and adversarial review | §4 field schema, §5.1 regex table, §5.2 versioning, §8 audit schema, §10 dev rules |
| External calls scoped to hardware/OS telemetry only | §7 + §7.2 static template |
| Verifiers can trace every external call | §8.3 call_id + hash chain + audit endpoint |

## 12. Cross-document alignment notes

- `node-telemetry.md` (t_260694d8) MUST mark each telemetry field with its C-class from §4 and emit the `_redaction` metadata block.
- `controller-api.md` (t_260694d8) MUST expose `governanceGate`, `externalModelCall`, and the operator audit surface `GET /v1/operator/audits` (there is no `GET /v1/governance/audit`); rejection code `GOVERNANCE_REJECT` (422) is reserved here.
- `node-bootstrap.md` (t_134e2f24) MUST place the redactor in the daemon's egress path and provide the redactor config as a versioned artifact.
- `agent-runtime.md` (t_1addb6fd) MUST route agent logs/payloads to on-prem inference only and must not expose an egress path for C2.
- `vm-allocation.schema.json` (t_260694d8) is the clamp validator referenced by §7.3; model output is constrained against it (allowlist-only, never free-form, fail-closed on any violation).
- **Ratified invariants (G-AR-1 / G-AR-2)** — from `agent-runtime.md` §7.1: the node→controller direction **never carries C2** (only C0/C1 egress, enforced by §4/§6); the controller carries task content (C2) **only transiently** on the inbound path — it never persists, logs, or model-processes task content (external model calls are C0-only per §7). These are normative for the controller, node daemon, and agent runtime.
