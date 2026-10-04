# Controller API Contract

Version 1.0 — frozen. Owners: controller + frontend teams.

This document is the single source of truth for the GCP Controller's REST and
WebSocket surface. The node-side bootstrap handshake that *calls* these endpoints
lives in `node-bootstrap.md`; the agent VM / llama.cpp model-routing surface lives
in `agent-runtime.md`; the PII/redaction gate and C0/C1/C2 taxonomy live in
`governance.md`. The structural allocation schema lives in
`vm-allocation.schema.json`; this document defines the deterministic guardrail
rules that operate on it.

Fixed decisions this contract honors:
- Controller runs on a GCP VM (central control plane).
- Node A/B are compute nodes; node C is a real GPU node serving llama.cpp.
- The only wired models are DeepSeek-flash and DeepSeek-v4-pro (served locally on
  node C; intent extraction uses DeepSeek-flash on-prem — no external model call
  for customer prompts).

Governance enforcement hooks this contract MUST implement (normative,
`governance.md` §9): `governanceGate`, `assertRedactorVersion(min)`,
`externalModelCall()`, `guardrailClamp()`, and `commandAllowlist()`. These are
referenced inline below where they attach to endpoints.

---

## 1. Conventions

### 1.1 Base URLs

| Surface   | URL                                            |
|-----------|------------------------------------------------|
| REST      | `https://<controller-host>/v1`                 |
| Events    | `wss://<controller-host>/stream/v1`            |
| Terminal  | `wss://<controller-host>/terminal/v1`          |

`<controller-host>` is the GCP VM address, injected via the env var
`TERMINI_CONTROLLER_URL`. No path under `/v1` may be shared with `/stream` or
`/terminal` — WebSockets never live under `/v1`.

### 1.2 Versioning

- REST and WebSocket surfaces are versioned by the URL segment (`/v1`).
- Every response carries header `X-Termini-Version: 1`. Every request SHOULD send
  `X-Termini-Version: 1`.
- Additive field additions do not bump the version. Removal, renaming, or
  type-change of an existing field bumps to `/v2`. During the 5-day build only
  `/v1` exists; `/v2` is out of scope.
- A client sending an unsupported major version receives `426 Upgrade Required`
  with `supported_versions` in the error details.

### 1.3 Identifiers and timestamps

- All identifiers (`project_id`, `node_id`, `vm_id`, `command_id`, `audit_id`,
  `request_id`, `plan_id`, `call_id`) are ULIDs: 26 characters of Crockford
  base32 (uppercase, alphabet `0-9A-HJKMNP-TV-Z`, no I/L/O/U), e.g.
  `01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C0D`. This matches `governance.md` §4.
- All timestamps are RFC 3339 UTC (`2026-10-04T07:49:00Z`).
- No field may contain a raw application log line, customer payload, process
  argument, or credential. Every field is an identifier, a number, a boolean, an
  enum, or a bounded redacted string (see `governance.md` for the C0/C1/C2 rules).

### 1.4 Content types and encoding

- `Content-Type: application/json` for all request/response bodies.
- Request bodies are UTF-8 JSON, max 1 MiB. Responses are UTF-8 JSON unless a
  streaming endpoint is documented otherwise.
- WebSocket frames are UTF-8 text; each frame is one complete JSON object
  (no partial objects, no binary frames).

### 1.5 Idempotency

State-changing POST endpoints accept an optional `Idempotency-Key` header
(8–128 chars). Two requests with the same key within 24h return the same
resource/plan and do not double-apply the change. The structured allocation path
also honors `idempotency_key` in the body (`vm-allocation.schema.json`).

---

## 2. Authentication, authorization, and governance gates

Three token classes, all carried as `Authorization: Bearer <token>` (opaque,
256-bit base64url secrets, ≥ 32 chars):

| Token class      | Bearer of                 | Scope                                        | Issued by     |
|------------------|---------------------------|----------------------------------------------|---------------|
| `bootstrap_token`| Node installer (one-time) | `POST /v1/nodes/register` only               | Project create|
| `node_token`     | Node bootstrap daemon     | Its own `node_id` resources only             | Registration  |
| `project_token`  | Frontend (browser)        | All frontend endpoints for one `project_id`  | Project create|

Rules:
- `bootstrap_token` is a single-use token burned on successful registration; a
  reused or invalid token returns `401 UNAUTHENTICATED`.
- `node_token` may only read/write paths under its own `node_id`. Cross-node
  access returns `403 PERMISSION_DENIED`.
- `project_token` scopes all access to its own `project_id`. A project token used
  against a foreign project returns `403 PERMISSION_DENIED`.
- The AI operator is a server-side process on the controller; it is not a token
  class. It issues remediation via the node command channel (section 6) and
  records `actor: ai_operator` in the audit log.
- Bootstrap token is delivered to the node via the pre-seeded installer image
  (cloud-init) — see `node-bootstrap.md` for custody; the controller API only
  receives it, never returns it after registration.

Governance gates (attach to the named endpoints, `governance.md` §9):
- `governanceGate` middleware wraps `/register`, `/telemetry`, `/intent`, and
  `/heartbeat`. It re-runs the on-node allowlist + caps + C2 scan +
  C1-transform-check on every inbound payload and rejects discrepancies with
  `422 GOVERNANCE_REJECT`.
- `assertRedactorVersion(min)` rejects payloads whose `_redaction.version` is
  below the controller's minimum.
- `externalModelCall()` wraps the AI-operator loop's model calls; it asserts the
  static prompt-template version and that every substituted field is C0
  (`governance.md` §7.2), and writes audit records before/after.
- `guardrailClamp()` is the validator in section 5; `commandAllowlist()` restricts
  the AI operator to the command types in section 6 (no free-form shell).

---

## 3. Error envelope

Every non-2xx response uses this shape:

```json
{
  "error": {
    "code": "NODE_OFFLINE",
    "message": "Node 01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E has not heartbeated in 45s",
    "details": [{"field": "node_id", "reason": "offline"}],
    "request_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C7M"
  }
}
```

| HTTP | Meaning                                            |
|------|----------------------------------------------------|
| 400  | Malformed JSON or schema violation (`INVALID_ARGUMENT`) |
| 401  | Missing/invalid/expired token (`UNAUTHENTICATED`) |
| 403  | Valid token, wrong scope (`PERMISSION_DENIED`)    |
| 404  | Unknown id (`NOT_FOUND`)                           |
| 409  | State conflict (`CONFLICT`, e.g. VM mid-transition)|
| 422  | Guardrail rejection (`GUARDRAIL_REJECTED`) or governance rejection (`GOVERNANCE_REJECT`) |
| 426  | Unsupported version (`UNSUPPORTED_VERSION`)        |
| 429  | Rate limited (`RATE_LIMITED`)                      |
| 503  | Node offline / unreachable (`NODE_OFFLINE`)        |
| 500  | Controller internal error (`INTERNAL`)             |

Error `code` is always one of: `INVALID_ARGUMENT`, `UNAUTHENTICATED`,
`PERMISSION_DENIED`, `NOT_FOUND`, `CONFLICT`, `GUARDRAIL_REJECTED`,
`GOVERNANCE_REJECT`, `UNSUPPORTED_VERSION`, `RATE_LIMITED`, `NODE_OFFLINE`,
`INTERNAL`. `GOVERNANCE_REJECT` is reserved by `governance.md` §12 and is used
exclusively by the governance gates.

---

## 4. REST endpoints

### 4.1 Node-facing (node → controller)

#### POST /v1/nodes/register

One-time bootstrap registration. Requires `X-Bootstrap-Token` header.

Request:
```json
{
  "registration_id": "termini-node-a",
  "hardware": {
    "cpu": {"model": "AMD EPYC 7B13", "logical_cores": 16, "physical_cores": 8},
    "ram_gb": 32,
    "disk": [{"path": "/dev/sda", "size_gb": 256, "type": "ssd"}],
    "gpu": [{"model": "NVIDIA L4", "vram_gb": 24, "count": 1}]
  },
  "os": {"distro": "ubuntu", "version": "24.04"},
  "lxd": {"version": "5.21", "clustered": true},
  "installer_version": "2026.10.0"
}
```

`registration_id` is a stable bootstrap label (1..64 chars, unique per project)
and maps to governance.md's C1 `hostname` field — in telemetry it is always sent
hashed (§`node-telemetry.md` §4.2). During this one-time registration it is sent
in the clear over the bootstrap-token channel (a bounded C1 bootstrap exception,
recorded in the audit log). Raw IP and MAC are NOT sent in the body: the
controller records the TLS connection's source IP as the node's routable address,
and the MAC never egresses (governance.md §4.2/§4.3).

Response `201 Created`:
```json
{
  "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E",
  "project_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C0D",
  "role": "compute",
  "node_token": "opaque-node-secret",
  "config": {
    "heartbeat_interval_s": 5,
    "telemetry_interval_s": 2,
    "telemetry_batch_max": 200,
    "command_channel": "websocket"
  }
}
```

Role assignment: a node reporting ≥ 1 GPU with `vram_gb >= 12` is assigned
`inference`; otherwise `compute`. The controller caps `inference` role to the
single node C in the demo (first GPU node wins; later GPU nodes are `compute`
unless project config raises the limit).

#### POST /v1/nodes/{node_id}/heartbeat

Liveness ping. `node_token` auth. `governanceGate` applies.

Request (optional body; empty allowed):
```json
{ "seq": 1042, "status": "healthy" }
```

Response `204 No Content`. If commands are pending, the response is `200 OK` with
`{"pending_commands": 3}` to hint the daemon to poll/connect immediately.

#### POST /v1/nodes/{node_id}/telemetry

Batch telemetry ingest. `node_token` auth. `governanceGate` +
`assertRedactorVersion` apply. Body is the batch envelope defined in
`node-telemetry.md`. Response `202 Accepted`:
```json
{ "accepted": 200, "rejected": 0, "backpressure": "none" }
```
`backpressure` ∈ `none | drop_metrics | pause_5s` — see `node-telemetry.md` §1.1.

#### GET /v1/nodes/{node_id}/commands   (long-poll fallback)

Returns queued commands for this node. Blocks up to 25s (long-poll) when empty,
then returns `200` with an empty list. Requires `node_token`.

```json
{ "commands": [ { "command_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C5J",
  "type": "vm.resize", "payload": {...}, "issued_by": "ai_operator",
  "issued_at": "...", "timeout_s": 60 } ] }
```

### 4.2 Frontend-facing (frontend → controller)

All use `project_token` auth and are scoped to the token's `project_id`. The
`{project_id}` path parameter is validated against the token; a mismatch returns
`403`. `governanceGate` applies to `/intent` (the only inbound path here that
could carry free text).

#### POST /v1/projects/{project_id}/intents

Submit resource intent as natural language OR a structured spec; receive the
guardrail-validated plan.

Request (one of the two forms):
```json
{ "prompt": "Deploy 3 worker agents and 1 fast LLM model for coding tasks" }
```
```json
{ "spec": { "allocations": [ { "type": "agent_vm", "count": 3, "vcpus": 2,
  "ram_gb": 4, "target_node": "auto" },
  { "type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16,
    "passthrough_gpu": true } ] } }
```

Flow: `prompt` → on-prem DeepSeek-flash extraction (via the node C inference
endpoint in `agent-runtime.md`; runs on-prem so raw prompts never cross the
premises boundary — `governance.md` §7.3) → structured `AllocationRequest` →
guardrail validator (section 5) → `AllocationPlan`. A `spec` skips extraction and
is validated directly.

Response `200 OK` (plan), or `422` if the plan outcome is rejected:
```json
{
  "plan_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C8N",
  "request_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C7M",
  "project_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C0D",
  "outcome": "accepted",
  "created_at": "2026-10-04T07:49:00Z",
  "allocations": [ { "index": 0, "status": "accepted", "requested": {...},
    "resolved": {...}, "guardrail": {"clamped_fields": [], "reject_reasons": []},
    "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E", "vm_ids": [] } ],
  "audit": { "...": "guardrail_audit object per schema" }
}
```
The plan is accepted or rejected; a rejected plan returns HTTP 422 with
`code: GUARDRAIL_REJECTED` and the same `allocations` array explaining per-class
rejections. Accepted plans are NOT executed by this endpoint — the caller invokes
`POST /v1/projects/{project_id}/vms/apply` (below) to materialize a plan.

#### POST /v1/projects/{project_id}/vms/apply

Materialize an accepted plan. Body:
```json
{ "plan_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C8N" }
```
Response `202 Accepted`:
```json
{ "job_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C9P",
  "state": "provisioning", "vm_ids": ["01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C4H"] }
```
The controller fans out LXD instance creation to the target nodes via the node
command channel; frontend tracks progress via the event stream (section 7).

#### GET /v1/projects/{project_id}/nodes

```json
{ "nodes": [ { "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E",
  "registration_id": "termini-node-a", "role": "compute", "status": "online",
  "last_seen_at": "...",
  "hardware": { "cpu": {...}, "ram_gb": 32, "gpu": [...] },
  "capacity": { "cpu_cores": 16, "ram_gb": 32, "disk_gb": 256, "gpu_free": 0,
                "cpu_allocated": 6, "ram_allocated_gb": 12 } } ] }
```
`status` ∈ `online | degraded | offline`. `capacity` is allocatable bookkeeping
(registered minus reserve minus allocated).

#### GET /v1/projects/{project_id}/nodes/{node_id}

Same shape as one element of the list, plus `metrics_latest` (the most recent
telemetry sample).

#### GET /v1/projects/{project_id}/nodes/{node_id}/metrics

Resource utilization time-series for charts.

Query params: `start` (RFC3339), `end` (RFC3339), `resolution` ∈
`raw | 5s | 1m | 5m` (default `5s`), `metrics` (comma list, default all).

```json
{ "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E", "resolution": "5s", "series": [
  { "name": "cpu_util_pct", "unit": "percent", "points": [ {"t": "...", "v": 31.2}, ... ] },
  { "name": "ram_used_bytes", "unit": "bytes", "points": [ {"t": "...", "v": 19756849561}, ... ] },
  { "name": "disk_write_latency_ms", "unit": "ms", "points": [...] },
  { "name": "net_rx_bytes", "unit": "bytes", "points": [...] },
  { "name": "gpu_util_pct", "unit": "percent", "points": [...] }
] }
```
The frontend converts bytes→GiB for display; the contract stays in bytes to match
`node-telemetry.md`.

#### GET /v1/projects/{project_id}/vms

```json
{ "vms": [ { "vm_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C4H",
  "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E", "type": "agent_vm",
  "state": "running", "vcpus": 2, "ram_gb": 4, "disk_gb": 8,
  "ip": "10.0.100.11", "created_at": "..." } ] }
```
`state` ∈ `provisioning | running | stopped | migrating | error | destroyed`.

#### GET /v1/projects/{project_id}/vms/{vm_id}

Single VM object plus `console` hint (`ws_url`) for the terminal.

#### POST /v1/projects/{project_id}/vms/{vm_id}/actions/{action}

Human remediation control. `action` ∈ `start | stop | resize | migrate | destroy`.

- `resize` body: `{ "vcpus": 4, "ram_gb": 8 }` (goes through the guardrail, actor `user`).
- `migrate` body: `{ "target_node": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C2F" }`.
- `start`/`stop`/`destroy`: empty body.

Response `202 Accepted` with the queued `command_id`. Any action while the VM is
`provisioning` or `migrating` returns `409 CONFLICT`.

### 4.3 SSH key UX

#### GET /v1/projects/{project_id}/ssh-key

```json
{
  "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... termini@project-01H4KZ",
  "fingerprint": "SHA256:AbC123...==",
  "algorithm": "ed25519",
  "created_at": "2026-10-04T07:00:00Z"
}
```
The private key is generated at project creation, shown to the user ONCE in the
frontend (download), and never returned again by any endpoint.

#### POST /v1/projects/{project_id}/ssh-key/rotate

Rotates the project keypair. Returns `200` with the new public key/fingerprint.
The controller pushes a `ssh.inject_key` command to every online node (replacing
`/root/.ssh/authorized_keys` with the new public key) and re-seeds agent VM
images. The old key is invalidated immediately on the controller; nodes process
the injection command within one heartbeat interval. Rotation details/custody on
the node side are in `node-bootstrap.md`.

The frontend renders the SSH access string from this data as
`ssh root@<node_ip>` where `<node_ip>` is from `GET /v1/nodes/{id}`.

### 4.4 Governance audit (verifier-facing)

#### GET /v1/governance/audit?node_id=&since=

Read-only, authenticated (project token with a `governance:audit` scope or the
operator). Returns the controller-side audit records for reconciliation
(`governance.md` §8.3):

```json
{ "records": [ { "call_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C5J",
  "source_node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E", "ts": "...",
  "endpoint": "/v1/telemetry", "result": "accepted",
  "redactor_version_asserted": "redactor-config v3", "governance_violation": false } ],
  "chain_tip": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C6K" }
```
A verifier reconciles the controller chain against the node's
`governance_chain_tip` (`node-telemetry.md` §2).

---

## 5. Guardrail & Clamp Validator

The deterministic choke point between "DeepSeek/Gemini recommends" and "LXD
executes" (`governance.md` §7.3 `guardrailClamp()`). Every allocation — human
intent or AI-operator remediation — passes through this validator. It is pure and
side-effect-free except for writing the audit log. No allocation may reach LXD
without a passing validator run.

### 5.1 Frozen default parameters

| Parameter             | Default | Meaning                                        |
|-----------------------|---------|------------------------------------------------|
| `cpu_overcommit_ratio`| 2.0     | vCPUs may overcommit logical cores by 2×       |
| `ram_overcommit`      | 0       | RAM is never overcommitted                     |
| `cpu_reserve_ratio`   | 0.10    | Host cores reserved for OS/LXD                 |
| `ram_reserve_gb`      | 2       | Host RAM (GiB) reserved for OS/LXD             |
| `disk_reserve_ratio`  | 0.10    | Host disk reserved for OS/LXD/overlay          |
| agent_vm `vcpus`      | [1, 8]  | Preferred range (clamped, not rejected)        |
| agent_vm `ram_gb`     | [1, 16] | Preferred range (clamped, not rejected)        |
| agent_vm `disk_gb`    | [4, 64] | Preferred range (clamped, not rejected)        |
| inference_vm `vcpus`  | [4, 32] | Preferred range (clamped, not rejected)        |
| inference_vm `ram_gb` | [8, 64] | Preferred range (clamped, not rejected)        |
| inference_vm `disk_gb`| [32,512]| Preferred range (clamped, not rejected)        |
| GPU allocation        | 1 per node | Node C has exactly 1 GPU; 1 GPU-bound VM max |

Defaults are configurable per-project but the values above are frozen for the
demo. Absolute caps (never exceedable, encoded in the schema): `vcpus ≤ 128`,
`ram_gb ≤ 512`, `disk_gb ≤ 4096`, `count ≤ 64`.

### 5.2 Validation order (deterministic, run in this exact sequence)

1. **Schema validation** (structural): unknown fields, wrong types, missing
   required `type`/`count`, `agent_vm` with `passthrough_gpu: true`, enum
   violations → `REJECT` (`400 INVALID_ARGUMENT`). Structural errors are never
   clamped.
2. **Defaulting**: fill omitted optionals — `target_node → "auto"`,
   `disk_gb` → type floor (agent 8, inference 64), `passthrough_gpu` → false.
   Record `decision: defaulted`.
3. **Role validation**: `inference_vm` must have `passthrough_gpu: true` and must
   target a GPU node (explicit GPU node, or `auto` resolving to one). Violation →
   `REJECT` that class (`gpu_required_for_inference`).
4. **Per-field range clamp**: clamp each numeric field into its type range
   (table above). Record `decision: clamped` with `from`/`to`. Below-min and
   above-max both clamp to the nearest bound; never reject a pure range overflow.
5. **Target-node resolution**: resolve `auto` to the node with the most free
   allocatable RAM (deterministic tie-break: lowest `node_id`). An explicit
   `target_node` that is not a valid node, or an `inference_vm` targeting a
   compute node, → `REJECT` (`invalid_target_node_type`).
6. **Per-node capacity clamp**: using allocatable capacity
   (`registered − reserve − already_allocated`), clamp in this order:
   - `ram_gb` → min(requested, free RAM). If result < type min → `REJECT`
     (`exceeds_node_ram_ceiling`).
   - `vcpus` → min(requested, `cpu_overcommit_ratio × logical_cores − allocated`).
     If < 1 → `REJECT` (`exceeds_node_cpu_ceiling`).
   - `disk_gb` → min(requested, free disk). If < type min → `REJECT`
     (`exceeds_node_disk_ceiling`).
7. **GPU check**: `inference_vm` with `passthrough_gpu` requires a free GPU
   (`gpu_free ≥ 1`) on the resolved node. No free GPU → `REJECT`
   (`gpu_unavailable`). GPU is never clamped.
8. **Commit**: allocate capacity for accepted/clamped classes, append the
   `guardrail_audit` record, and return the `AllocationPlan`.

### 5.3 Reject vs clamp — reference table

| Condition                                   | Behavior             |
|---------------------------------------------|----------------------|
| Unknown field, wrong type, missing required | REJECT (400)         |
| Enum value out of set                       | REJECT (400)         |
| `agent_vm` + `passthrough_gpu: true`        | REJECT (400)         |
| `inference_vm` + no GPU / no GPU node       | REJECT (422)         |
| Numeric below min or above absolute max     | CLAMP to bound       |
| RAM exceeds free node RAM                   | CLAMP, then REJECT if < min |
| vCPU exceeds overcommit ceiling             | CLAMP, then REJECT if < 1 |
| Disk exceeds free node disk                 | CLAMP, then REJECT if < min |
| Second GPU-bound VM, no free GPU            | REJECT (422)         |

Outcome is all-or-nothing: if any class is REJECTED, the controller creates no
VMs and returns `422`. Classes that merely CLAMP proceed with the clamped value.

### 5.4 Audit-log fields

Every validator run writes one immutable `guardrail_audit` record (schema in
`vm-allocation.schema.json`), never mutable, never deletable:

`audit_id`, `request_id`, `project_id`, `timestamp`, `actor`
(`user | ai_operator | system`), `raw_spec` (pre-clamp), `resolved_spec`
(post-clamp), `outcome`, and `node_snapshot` (per-node registered capacity +
allocated at decision time). The per-class `clamped_fields` list records each
`{field, decision, from, to, reason}`. The AI-operator remediation path MUST
also write this record so every autonomous change is traceable to a telemetry
trigger (see `node-telemetry.md` §5).

---

## 6. Node command channel (controller → node)

The controller issues remediation/operations as commands. Delivery is WebSocket
push by default, HTTP long-poll fallback. Command envelope:

```json
{
  "command_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C5J",
  "type": "vm.resize | vm.migrate | vm.stop | vm.start | vm.destroy | node.drain | node.reboot | ssh.inject_key",
  "payload": { "...": "type-specific, see below" },
  "issued_by": "ai_operator | user | system",
  "issued_at": "2026-10-04T07:49:00Z",
  "timeout_s": 60
}
```

Type-specific payloads:
- `vm.resize`: `{"vm_id": "...", "vcpus": 4, "ram_gb": 8}` (post-guardrail values).
- `vm.migrate`: `{"vm_id": "...", "source_node": "...", "target_node": "...", "live": true}`.
- `vm.stop`/`vm.start`/`vm.destroy`: `{"vm_id": "..."}`.
- `node.drain`: `{"reason": "disk_degradation", "evacuate_to": "node-b-id"}`.
- `node.reboot`: `{"delay_s": 0}`.
- `ssh.inject_key`: `{"public_key": "ssh-ed25519 ...", "target": "root"}`.

This set is the `commandAllowlist()` from `governance.md` §9 — the AI operator may
select among these types only; free-form shell is never executed.

Node acknowledges each command via:

#### POST /v1/nodes/{node_id}/commands/{command_id}/ack

Body:
```json
{ "status": "acked | running | succeeded | failed", "detail": "...", "result": { } }
```
`succeeded`/`failed` include a `result` object: e.g. resize returns the new
effective limits; migrate returns the destination node; drain returns the list of
evacuated `vm_id`s. `failed` includes `detail` with the LXD/OS error string.
These transitions are surfaced to the frontend as events (section 7).

---

## 7. WebSocket and streaming contracts

### 7.1 Unified event stream — `/stream/v1?token=<project_token>`

Server→client only (client may send `{"type":"ping"}` and expect `pong`).
Every message:

```json
{ "event": "<name>", "ts": "2026-10-04T07:49:01Z", "project_id": "...", "data": { } }
```

Event catalog (`data` shape):

| event                  | data |
|------------------------|------|
| `node.registered`      | node object (list shape) |
| `node.health_changed`  | `{node_id, status, last_seen_at}` |
| `node.telemetry`       | throttled telemetry sample (1/s max) |
| `vm.provisioned`       | VM object |
| `vm.state_changed`     | `{vm_id, state, node_id}` |
| `remediation.queued`   | command envelope |
| `remediation.completed`| `{command_id, vm_id?, status, result}` |
| `intent.validated`     | `AllocationPlan` (summary) |
| `log.line`             | `{node_id?, vm_id?, level, text}` (AI operator log) |

### 7.2 AI operator logs — `GET /v1/projects/{project_id}/logs`

Server-Sent Events (`text/event-stream`). Each event:
```
event: log
data: {"ts":"...","level":"info","text":"Node A disk failure predicted. ...","node_id":"...","vm_id":null}
```
`level` ∈ `debug | info | warn | error`. Only AI-operator operational messages
are emitted here; application/customer logs are NEVER surfaced on this channel
(`governance.md` — C2). The same `log.line` payload is also mirrored on the WS
stream.

### 7.3 xterm.js terminal — `/terminal/v1/{vm_id}?token=<project_token>`

Binary-clean PTY relay. Each frame is one JSON object, UTF-8 only (terminal
bytes are base64-encoded inside `data`).

Client → server:
```json
{ "type": "input",  "data": "bHMgLWxhCg==" }
{ "type": "resize", "cols": 120, "rows": 40 }
{ "type": "ping" }
```

Server → client:
```json
{ "type": "output", "data": "dG90YWwgMjQK" }
{ "type": "exit",   "code": 0 }
{ "type": "error",  "message": "vm not running" }
{ "type": "pong" }
```

Rules: one session per `vm_id` (a second connection closes the first with
`{"type":"error","message":"session_replaced"}`). Idle > 15 min closes with
`{"type":"exit","code":-1}`. The controller execs into the VM via LXD
(`lxc exec`) on the owning node; it never streams host-console or node-level
shells (those are `ssh root@<node-ip>` UX, not the web terminal).

---

## 8. Streaming cadence

| Channel                | Cadence (default)       | Notes |
|------------------------|-------------------------|-------|
| Node heartbeat         | every 5 s               | 15 s → `degraded`, 45 s → `offline` |
| Node telemetry         | every 2 s (1–2 s demo)  | see `node-telemetry.md` |
| Command long-poll      | every 2 s, 25 s block   | WS push preferred |
| WS telemetry snapshots | ≤ 1/s per node          | throttled from raw cadence |
| AI operator log        | push on append          | ring buffer 10k lines |
| Metrics storage        | raw (2 s) → 5s/1m/5m downsample | charts read downsampled |

During the demo the operator loop consumes telemetry at 1–2 s so a remediation
can be issued before the kernel OOM-killer terminates the fault process
(project.md advisory #4). No endpoint polls slower than 2 s during the demo.

---

## 9. Cross-document references

- Node bootstrap handshake (image inputs, LXD join, SSH injection, retry) →
  `node-bootstrap.md`.
- Open spec decisions (LXD vs MicroCloud, OVN vs fan, image strategy, GPU) →
  `infra-decisions.md`.
- Agent VM image, llama.cpp endpoint, DeepSeek model routing, streaming →
  `agent-runtime.md`.
- PII redaction gate, C0/C1/C2 taxonomy, egress allowlist, external-call tracing →
  `governance.md`.
- Structural allocation schema → `vm-allocation.schema.json`.
- Telemetry field schema → `node-telemetry.md`.

No field in this contract is a TBD. If a value is not fixed above, the default in
section 5.1 applies.
