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
| Node ctrl | `wss://<controller-host>/node/v1/{node_id}/channel` |

`<controller-host>` is the GCP VM address, injected via the env var
`TERMINI_CONTROLLER_URL`. No path under `/v1` may be shared with `/stream`,
`/terminal`, or `/node` — WebSockets never live under `/v1`. The node control
channel (`/node/v1/{node_id}/channel`) is the primary steady-state channel for
heartbeat, telemetry, command delivery, and command acks (D6); the REST
endpoints in §4.1 are the HTTP fallback for environments that block the
WebSocket upgrade.

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
  `01M4349RPSZFTB60AVJEG0ZZY9`. This matches `governance.md` §4.
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
    "message": "Node 01M4349RPTT29RNTC6VBCXQ0RC has not heartbeated in 45s",
    "details": [{"field": "node_id", "reason": "offline"}],
    "request_id": "01M4349RQ09HMAX6RC9YPYQH4W"
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

Node→controller steady-state traffic (heartbeat, telemetry, command acks) rides the
mTLS WebSocket control channel at `/node/v1/{node_id}/channel` (D6). The REST
endpoints below are the authoritative wire formats and the **HTTP fallback** used
when the WebSocket upgrade is blocked; `register` and `reconnect` are always REST.

#### POST /v1/nodes/register

One-time bootstrap registration. Requires `Authorization: Bearer <bootstrap_token>`
(the single-use, short-TTL enrollment token; custody in `node-bootstrap.md` §3).
The canonical request/response schema is `node-bootstrap.md` §5; this section
states the endpoint, auth, and role assignment, and the two documents agree on
every field.

Request (canonical `NodeRegistration`, full field tables in `node-bootstrap.md` §5.2):

```json
{
  "schema_version": "1.0",
  "machine": {
    "machine_id": "9f2c1e...e7",
    "dmi_product_uuid": "4c4c4544-...",
    "hostname": "node-b",
    "image_version": "2026.10.01-abc1234",
    "os": {"name": "ubuntu", "version": "24.04.1"},
    "lxd": {"version": "5.21.3", "snap_revision": "31234"},
    "cpu": {"model": "Intel Xeon ...", "sockets": 1, "cores": 8, "threads": 16},
    "memory": {"total_bytes": 68719476736},
    "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 214748364800, "health": "ok"}],
    "nics": [{"name": "ens4", "addresses": ["10.10.0.12/24"], "role": "underlay"}],
    "gpus": [{"vendor": "nvidia", "model": "Tesla T4", "vram_bytes": 17179869184, "pci_id": "0000:00:04.0", "driver": "535.183", "health": "ok"}],
    "underlay_ip": "10.10.0.12",
    "role_hint": "auto"
  },
  "node_pubkey_csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\n...\n-----END CERTIFICATE REQUEST-----\n",
  "host_ssh_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... root@node-b"],
  "agent": {"version": "0.1.0", "boot_id": "8f0e..."}
}
```

`machine.hostname`, `machine.underlay_ip`, and `machine.nics[].addresses` map to
governance.md's C1 machine-inventory fields. During this one-time registration they
are sent in the clear over the bootstrap-token channel as a **bounded C1 bootstrap
exception** (recorded in the audit log, `governance.md` §4.3): the node's underlay
addresses are required for LXD cluster join/routing and controller inventory. Raw MAC
is NOT sent in the body — the `nics[].mac` field is removed, and MAC never egresses in
any form except the hashed C1 `mac_address` telemetry field (`governance.md` §4.2).

Response `201 Created` (canonical `NodeRegistrationResponse`; joins
`node-bootstrap.md` §5.3 with the controller-owned token/scope fields):

```json
{
  "schema_version": "1.0",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "role": "compute",
  "node_token": "opaque-node-secret",
  "identity": {
    "client_cert_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "ca_chain_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"
  },
  "cluster": {
    "name": "termini",
    "role": "joiner",
    "member_name": "node-b",
    "server_address": "10.10.0.11:8443",
    "join_token": "eyJzZX...ZSI6...joiner-token"
  },
  "telemetry": {"interval_ms": 1000, "channel": "wss://controller.termini.example/node/v1/01M4349RPTT29RNTC6VBCXQ0RC/channel"},
  "config": {
    "heartbeat_interval_s": 5,
    "telemetry_interval_ms": 1000,
    "telemetry_batch_max": 200,
    "command_channel": "websocket"
  },
  "controller_time": "2026-10-04T07:59:00Z",
  "registration_ttl_s": 3600
}
```

Seed node response: `cluster.role = "seed"`, `join_token = null`.
`identity` carries the controller-signed client cert + CA chain for the mTLS
control channel (D4/D6); `node_token` authenticates the REST fallback endpoints
in §4.1.

Role assignment: a node reporting ≥ 1 GPU with `vram_bytes >= 12884901888`
(12 GiB) is assigned `inference`; otherwise `compute`. The controller caps
`inference` role to the single node C in the demo (first GPU node wins; later GPU
nodes are `compute` unless project config raises the limit).

#### POST /v1/nodes/{node_id}/reconnect

Idempotent re-enrollment after reboot or client-cert reissue (`node-bootstrap.md`
§10). `node_token` + mTLS auth. Request: `{}` (identity carried by the TLS client
cert). Response `200 OK` mirrors the `register` response (minus the one-time
`cluster.join_token`, which is re-issued only if cluster membership is missing).
A `409 CONFLICT` with a valid cert means "reconnect accepted"; without a valid
cert the node must fall back to `POST /v1/nodes/register` with a fresh token.

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
{ "commands": [ { "command_id": "01M4349RPY26W8Q453PZYK05CB",
  "type": "node.drain", "payload": {...}, "issued_by": "human",
  "issued_at": "...", "timeout_s": 60 } ] }
```

### 4.2 Frontend-facing (frontend → controller)

All use `project_token` auth and are scoped to the token's `project_id`. The
`{project_id}` path parameter is validated against the token; a mismatch returns
`403`. `governanceGate` applies to `/intent` (the only inbound path here that
could carry free text).

#### POST /v1/projects

Create a project. This is the bootstrap entry point: the frontend calls it once
per customer to obtain the tokens that everything else is scoped by. Authenticated
with an operator/management credential (not a `project_token`, which does not
exist yet). Body `{"name": "<1..64 chars>"}`.

```json
{ "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "name": "acme-labs",
  "project_token": "opaque-project-secret",
  "bootstrap_token": "opaque-one-time-secret",
  "created_at": "2026-10-04T07:00:00Z" }
```

Response `201 Created`. `bootstrap_token` is single-use and short-TTL, delivered
to the node installer via cloud-init (custody in `node-bootstrap.md` §3.2);
`project_token` is the frontend's long-lived bearer (§2). Neither token is ever
returned again after this call.

#### GET /v1/projects

List projects visible to the caller. Returns `{ "projects": [ { "project_id": "...", "name": "...", "created_at": "..." } ] }`.

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
  { "type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16 } ] } }
```

Flow: `prompt` → on-prem DeepSeek-flash extraction (via the node C inference
endpoint in `agent-runtime.md`; runs on-prem so raw prompts never cross the
premises boundary — `governance.md` §7.3) → structured `AllocationRequest` →
guardrail validator (section 5) → `AllocationPlan`. A `spec` skips extraction and
is validated directly.

Extraction is a **synchronous model call inside this request**, and the model is
shared with the AI operator's decision step. Two consequences are contractual:

- the extraction round-trip is bounded by `TERMINI_INTENT_MODEL_TIMEOUT_S`
  (default 120 s), NOT by `TERMINI_INFERENCE_TIMEOUT_S` — the latter is sized for
  the operator's minutes-long decisions on a CPU-only fleet and must not be
  inherited by a user-facing request;
- the extraction runs against its own model endpoint,
  `TERMINI_INTENT_INFERENCE_BASE_URL` (empty falls back to the shared model). It
  is not enough to give one model server extra slots: llama.cpp fills its batch
  from the operator's ~12k-token decision prompt first, so a small concurrent
  request on another slot gets no compute until that prefill ends. The shipped
  deployment therefore serves a dedicated small-context instance beside the
  decision model (`infra/gcp/cloud-init/llama-serve.sh`, node-c `:8090`), and the
  decision instance serves ≥2 slots (`--ctx-size 32768 --parallel 2`) so a
  request that does share it is not queued behind a whole decision.

If extraction does not produce a spec in that window (timeout, connection error,
unparseable JSON) the endpoint answers `503 INTERNAL` with the
`extraction_failed` detail — an explicit, retryable "the model did not answer",
kept distinct from a guardrail refusal. `422 GUARDRAIL_REJECTED` is reserved for
a *real* spec that the guardrail refused.

Response `200 OK` (plan), or `422` if the plan outcome is rejected:
```json
{
  "plan_id": "01M4349RQ2P5DPC7K59R0WFJCR",
  "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "outcome": "accepted",
  "created_at": "2026-10-04T07:49:00Z",
  "allocations": [ { "index": 0, "status": "accepted", "requested": {...},
    "resolved": {...}, "guardrail": {"clamped_fields": [], "reject_reasons": []},
    "node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "vm_ids": [] } ],
  "audit": { "...": "guardrail_audit object per schema" }
}
```
The plan is accepted or rejected; a rejected plan returns HTTP 422 with
`code: GUARDRAIL_REJECTED` and the same `allocations` array explaining per-class
rejections, plus a plan-level `reject_reasons[]` whose `detail` names the node and
the numbers (`ram_free_gb`, what was already allocated, what the request needed).
Accepted plans are NOT executed by this endpoint — the caller invokes
`POST /v1/projects/{project_id}/vms/apply` (below) to materialize a plan, at which
point each allocation's `vm_ids` is filled with the real LXD instance ids.

#### POST /v1/projects/{project_id}/vms/apply

Accept an already-validated plan and **materialise it**. Body:
```json
{ "plan_id": "01M4349RQ2P5DPC7K59W4WJCR" }
```
Response `202 Accepted`:
```json
{ "job_id": "01M4349RQ3BM21NNW2A68CT3SQ",
  "plan_id": "01M4349RQ2P5DPC7K59W4WJCR",
  "state": "ready",
  "vm_ids": ["termini-01m4349rq2p5dp...-0-0"],
  "allocations": [
    { "index": 0, "status": "ready", "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
      "realized_as": "vm", "vm_ids": ["termini-01m4349rq2p5dp...-0-0"],
      "created": 1, "already_applied": 0 } ] }
```

For each accepted/clamped `agent_vm` allocation, the controller creates `count`
LXD instances on the allocation's resolved node by driving that node's LXD REST
API (`POST /1.0/instances`) and waiting on the returned operation. It addresses
each node at its **own** LXD endpoint (reported at registration, see
`node-bootstrap.md` §5.2) and falls back to the single global
`TERMINI_LXD_BASE_URL` only for a node that reported no endpoint.

- **`state` is terminal**: `ready` (every allocation materialised) or `failed`
  (at least one allocation did not). It is never left at `provisioning`.
- **`vm_ids` are the LXD instance names** of the created workloads, not minted
  ULIDs: the name is deterministic (`termini-<plan_id>-<index>-<slot>`), which is
  what makes a replayed apply idempotent (LXD rejects the duplicate name and the
  executor maps that to an idempotent no-op), and it is the identity an operator
  finds on the node. This supersedes the ULID-typed `vm_ids` item sketched in
  `vm-allocation.schema.json`.
- **Failure semantics:** a create that fails or times out is never reported as
  success. That allocation's partially-created instances are deleted, its
  committed capacity is released (so the plan never claims an instance that does
  not exist), the allocation is reported `failed` with the error, and the job
  `state` is `failed`. `allocations[]` carries the per-class evidence, mirrored as
  a `vm.provisioned` event (section 7) and an operator log line.
- `inference_vm` is a reference to the node's host inference service (D5) and
  creates no VM (`realized_as: "host_service"`, `vm_ids: []`).

The image alias is `TERMINI_LXD_WORKLOAD_IMAGE` (default `agent-vm`) — an image
preloaded on the node, never a registry pull (D3).

#### POST /v1/projects/{project_id}/vms/release

Release a plan's **committed capacity** back to its node's pool. This is the
inverse of the §5.2 step 7 commit. Body:

```json
{ "plan_id": "01M4349RQ2P5DPC7K59W4WJCR", "reason": "stale_demo_state" }
```

Response `200 OK`:

```json
{ "plan_id": "01M4349RQ2P5DPC7K59W4WJCR",
  "status": "released",
  "released": { "cpu_allocated": 6, "ram_allocated_gb": 12, "disk_allocated_gb": 24 },
  "rows_released": 1,
  "released_at": "2026-10-12T15:32:00Z" }
```

Without this route, committed capacity was permanent: nothing decremented a
`vm_allocations` row once its instances were gone, so a node eventually had no
allocatable capacity left and every subsequent plan was rejected `422`
`exceeds_node_ram_ceiling` — recoverable only by wiping the DB, which destroys the
audit trail. `reason` is optional (≤200 chars); the default is `released_by_user`.

- **Idempotent.** The commit row is *soft-released* (marked `released_at`), never
  deleted, so the audit trail survives. Releasing the same plan again finds no
  live commitment and returns `200` with `"status": "already_released"` and a zero
  `released` object — never a double decrement, never an error. The node's
  `capacity.*_free` (and the §5.2 step 6 clamp) immediately reflect the returned
  capacity.
- A `plan_id` that never committed anything is `404 NOT_FOUND`; a missing or
  malformed `plan_id` is `422 INVALID_ARGUMENT`. Neither crashes.
- A plan committed but never applied may be released with this route; the *automatic*
  path (§ `vms/reconcile`) deliberately leaves such a reservation standing.

#### POST /v1/projects/{project_id}/vms/reconcile

Self-heal: release every live commit whose **materialised instances no longer
exist** on its node. Body `{}`. Response `200 OK`:

```json
{ "nodes_checked": 1,
  "released": [ { "plan_id": "01M4349RQ2P5DPC7K59W4WJCR",
                  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
                  "reason": "instances_gone",
                  "vm_ids": ["termini-01m4349rq2p5dp-0-0"] } ],
  "released_count": 1 }
```

A successful `vms/apply` records the LXD instance names it created on the plan's
commit (`vm_allocations.vm_ids_json`); reconciliation asks each node's LXD whether
any of those names still exist and releases the commit when none do. The controller
also runs this pass **once at startup**, so a restarted demo self-heals before a
plan can be refused.

Two deliberate conservatisms (absence must be *proven*, not assumed):

- a commit with **no recorded instances** (committed but never applied) is never
  reconciled — it has materialised nothing, so no reservation is released;
- a node that **cannot be reached** (untrusted, unreachable, timeout, non-JSON) is
  skipped, never treated as empty — an unreachable node must not release every
  plan's capacity.

One reservation shape *can* be proven dead, and is released by a separate
startup pass (`release_orphaned_reservations`): a never-applied commit whose
`plan_id` is not among the plans this controller process holds. The accepted-plan
map is in-memory only and `vms/apply` is the only thing that can materialise a
plan, so after a controller restart such a reservation can never become an
instance — the client's apply would `404 plan not found`. It is released once at
startup with `reason: "plan_orphaned_restart"`, so a node's advertised free
capacity is capacity it can really hand out. This closes the case that otherwise
had no recovery route at all: `POST /intents` commits at acceptance (§5.2 step 7),
so an intent submitted and never applied used to shrink a node forever.

#### POST /v1/projects/{project_id}/nodes/{node_id}/decommission

Retire a node from the fleet. Body `{ "reason": "throwaway test node" }` (optional,
≤200 chars). Response `200 OK`:

```json
{ "node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "status": "decommissioned",
  "decommissioned_at": "2026-10-12T15:32:00Z",
  "released": { "cpu_allocated": 6, "ram_allocated_gb": 12, "disk_allocated_gb": 24 },
  "rows_released": 1 }
```

The node is dropped from `GET /v1/projects/{project_id}/nodes` (soft retire — the
row survives, so its telemetry and audit history still resolve) and its live
committed capacity is released. This is what stops phantom `OFFLINE` entries
accumulating until the dashboard's fleet contradicts the deck. Idempotent: a
second call returns `200` with `"status": "already_decommissioned"`; an unknown
`node_id` is `404 NOT_FOUND`. A decommissioned node that re-registers with a valid
bootstrap token is revived (`status: online`, decommission cleared).

#### GET /v1/projects/{project_id}/nodes

```json
{ "nodes": [ { "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "registration_id": "termini-node-a", "role": "compute", "synthetic": false,
  "status": "online",
  "last_seen_at": "...",
  "hardware": { "cpu": {...}, "memory": { "total_bytes": 34359738368 }, "gpus": [] },
  "capacity": { "cpu_cores": 8, "ram_gb": 32, "disk_gb": 256, "disk_known": true,
                "gpu_free": 0,
                "cpu_allocated": 6, "ram_allocated_gb": 12 } } ] }
```
`status` ∈ `online | degraded | offline`. `capacity` is allocatable bookkeeping
(registered minus reserve minus allocated); `disk_known` is false when the node
reported no plausible disk size (its disk accounting is unknown, not zero).
`synthetic` is `true` for a row fabricated by the demo seeder and `false` for real
hardware — selection never places work on a `true` row while any real node is
registered (§5.2 step 5), and the dashboard labels it.

#### GET /v1/projects/{project_id}/nodes/{node_id}

Same shape as one element of the list, plus `metrics_latest` (the most recent
telemetry sample).

#### GET /v1/projects/{project_id}/nodes/{node_id}/metrics

Resource utilization time-series for charts.

Query params: `start` (RFC3339), `end` (RFC3339), `resolution` ∈
`raw | 5s | 1m | 5m` (default `5s`), `metrics` (comma list, default all).

```json
{ "node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "resolution": "5s", "series": [
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
{ "vms": [ { "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "type": "agent_vm",
  "state": "running", "vcpus": 2, "ram_gb": 4, "disk_gb": 8,
  "ip": "10.0.100.11", "created_at": "..." } ] }
```
`state` ∈ `provisioning | running | stopped | migrating | error | destroyed`.

#### GET /v1/projects/{project_id}/vms/{vm_id}

Single VM object plus `console` hint (`ws_url`) for the terminal.

#### POST /v1/projects/{project_id}/vms/{vm_id}/actions/{action}

Human remediation control. `action` ∈ `start | stop | resize | migrate | destroy`.

- `resize` body: `{ "vcpus": 4, "ram_gb": 8 }` (goes through the guardrail, actor `user`).
- `migrate` body: `{ "target_node": "01M4349RPVPP8X0W99R3TNZXW7" }`.
- `start`/`stop`/`destroy`: empty body.

Response `202 Accepted` with the queued `command_id`. Any action while the VM is
`provisioning` or `migrating` returns `409 CONFLICT`.

Node-level human actions (`POST /v1/projects/{project_id}/nodes/{node_id}/actions/{reboot,drain}`),
node retirement (`POST /v1/projects/{project_id}/nodes/{node_id}/decommission`),
remediation approve/deny (`POST /v1/projects/{project_id}/remediations/{command_id}/{approve,deny}`),
and the node SSH access string (`GET /v1/projects/{project_id}/nodes/{node_id}/ssh`)
are frozen in `dashboard.md` §3.2/§4 and are the frontend's manual-intervention surface.

### 4.3 SSH access (user access — controller-held SSH CA, D4)

User SSH access follows `infra-decisions.md` D4: the **controller holds an SSH CA**, and
the operator's keypair is generated **locally in the browser** — only the public half is
uploaded. Access is surfaced in the dashboard as a **one-click web terminal** plus a
copy-paste **`ssh`** string. No passwords and no project private key displayed in the UI.

#### GET /v1/projects/{project_id}/nodes/{node_id}/ssh

Returns the node's access string (the `command` field is server-computed and sanitized;
no shell metacharacters in `user`/`host`). The dashboard renders it verbatim for
copy-paste; the web terminal is served separately at `/terminal/v1/nodes/{node_id}`
(§7.3).

```json
{
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "user": "ubuntu",
  "host": "34.136.10.11",
  "port": 22,
  "auth_model": "ssh_ca",
  "key_algorithm": "ed25519",
  "key_fingerprint": "SHA256:AbC123...==",
  "key_comment": "termini-operator-cert",
  "command": "ssh -i ~/.ssh/termini_operator ubuntu@34.136.10.11"
}
```

`auth_model` is `"ssh_ca"`; `key_algorithm`/`key_fingerprint`/`key_comment` derive from the
CA public key (the trust anchor), and `command` references the operator's **browser-local**
key (`~/.ssh/termini_operator`), never a controller-held key. `public_ip` comes from the
infra inventory (`infra/gcp` emits `inventory.json`); it is **not** present in node
telemetry (`ip_public` is denied by `governance.md` §4.2).

**SSH certificate issuance & revocation (day-1, D4).** The controller holds an Ed25519
SSH CA. The operator (or dashboard) generates a keypair **locally in the browser** and
uploads only the public half; the controller signs it into a **short-lived (≤8 h default,
≤24 h max), principal-bound OpenSSH certificate** via `ssh-keygen -s`, and the node
accepts it against the `TrustedUserCAKeys` trust anchor (`node-bootstrap.md` §7).

#### POST /v1/projects/{project_id}/ssh-key

Body: `{ "public_key": "<openssh public key line>", "principal": "root", "validity_s": 28800 }`
(`principal`/`validity_s` optional; default principal and TTL come from controller config,
default TTL 8 h, hard ceiling 24 h). Private-key material is rejected (`422`). Returns
`200`:

```json
{
  "cert_id": "01M4349RPY26W8Q453PZYK05CB",
  "certificate": "ssh-ed25519-cert-v01@openssh.com AAAA...",
  "ca_fingerprint": "SHA256:...",
  "principal": "root",
  "valid_from": "2026-10-04T07:00:00Z",
  "valid_until": "2026-10-04T15:00:00Z",
  "algorithm": "ed25519",
  "key_fingerprint": "SHA256:..."
}
```

`503 INTERNAL` when no CA key is configured at boot. Every issuance is recorded in the
`ssh_certificates` ledger (migration `0006_ssh_ca.sql`).

#### POST /v1/projects/{project_id}/ssh-key/revoke

Body: `{ "fingerprint": "SHA256:..." }` — the public key fingerprint (`ssh-keygen` reports
the same fingerprint for a key and its cert). Marks the ledger row `revoked` without
deleting it. Returns `200 { "fingerprint": ..., "revoked": true }`; an unknown fingerprint
returns `404`.

The `ssh.inject_key` command and the shared project keypair are removed everywhere. The
infra-injected break-glass key is a one-time recovery path removed after registration
(`node-bootstrap.md` §7).

### 4.4 Operator audit (verifier-facing)

#### GET /v1/operator/audits?node_id=&limit=

Read-only, `project_token` auth. Returns the controller-side remediation audit
records for reconciliation (`governance.md` §8.3). Response `200 OK`:

```json
{ "audits": [ { "audit_id": "01M4349RPY26W8Q453PZYK05CB",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "rule_id": "oom-guard-01",
  "action_kind": "instance.restart", "result": "ok",
  "executed_at": "2026-10-04T07:49:05Z", "dry_run": false } ] }
```

A verifier reconciles the controller's audit/decision log against the node's
`governance_chain_tip` (`node-telemetry.md` §2). There is **no
`GET /v1/governance/audit` endpoint**; `GET /v1/operator/audits` (alongside
`GET /v1/operator/events` and `GET /v1/operator/decisions`) is the canonical
operator surface (`controller/app/server.py`).

### 4.5 Operator lease (single owner)

The AI-operator remediation loop has a **single-owner guarantee per project**: at
most one operator instance may act at a time. The in-process `OperatorLoop` and
the standalone CLI (`controller/app/operator/cli.py`, the documented
`OPERATOR_CMD` path) both acquire the same per-project lease before issuing any
remediation; a non-owner is a standby that issues nothing until the owner's lease
lapses (crash/failover). This is what makes remediation idempotent across
operator instances — two operators can never both issue an LXD command for one
fault episode.

#### POST /v1/operator/lease

`project_token` auth. Body `{ "owner_id": "loop:01J0DEM0WARM000000000000AB" }`
(the in-process loop uses `loop:<project_id>`; the standalone CLI uses
`cli:<ulid>`). Acquires or renews the caller's lease (TTL
`TERMINI_OPERATOR_LEASE_TTL_S`, default 10 s). Response `200 OK`:

```json
{ "owner": true, "owner_id": "loop:01J0DEM0WARM000000000000AB",
  "acquired_at": "2026-10-04T07:49:05.000Z", "expires_at": "2026-10-04T07:49:15.000Z" }
```

`owner: false` means another operator holds the lease and this caller must stand
by. The lease is persisted in the controller DB (`operator_lease`, migration
0007), so the in-process loop and the standalone CLI share one source of truth.

### 4.6 Operator provisioning (seeder self-bootstrap)

#### POST /v1/bootstrap-tokens

`project_token` auth **and direct-loopback only**. Body `{ "count": 2 }` (`count`
an integer in `[1, 32]`, default `2`). Mints `count` fresh **unused single-use**
bootstrap tokens — the same rows `TERMINI_BOOTSTRAP_TOKEN` seeds at startup — and
returns them:

```json
{ "schema_version": "1.0", "count": 2,
  "tokens": ["<opaque>", "<opaque>"],
  "created_at": "2026-10-04T07:49:05.000Z", "single_use": true }
```

Why it exists: the demo seeder (`console/seed_demo.py`, systemd unit
`termini-seeder`) registers its synthetic nodes through `POST /v1/nodes/register`,
which **burns** a single-use bootstrap token. A burned token cannot be reused, so
a seeder restart used to need hand-minted tokens written into its unit — and even
then a drop-in could override them (drop-ins load after the unit; the last one
setting a key wins). Starting the seeder therefore failed `401 bootstrap token
already used`, which is a recording landmine. With this route the seeder fetches
fresh tokens at startup (`--bootstrap-mint N`) and a bare
`systemctl start termini-seeder` works.

**The single-use property is unchanged for real nodes.** A minted token is an
ordinary `bootstrap_tokens` row: registration still burns it
(`store.burn_bootstrap_token`) and a replayed token is still rejected by
`_require_bootstrap` with `401 bootstrap token already used`. Minting is
restricted to a **direct** loopback connection precisely so that the only
credential the demo hands out (the project token) does not become a token-minting
capability for a remote caller: the demo controller is reached publicly through a
reverse proxy on the same host (`:443 -> 127.0.0.1:8080`), so a loopback TCP peer
alone would not distinguish a public request from the seeder's — a request carrying
any proxy-forwarding header (`X-Forwarded-For`, `X-Real-Ip`, `Forwarded`, `Via`) is
treated as remote and refused `403`.

---

## 4.7 Operator demo fault injection (operator-only)

**Operator console, not a client surface.** These endpoints exist for the operator
who runs the demo (`console/fault_console/`, served at `/console/`). They are
authenticated with `project_token` only — like §4.2 — and they are **never** part
of the client-facing dashboard: the route is not linked from the dashboard's
navigation, and a request that presents a `node_token` (or no token at all) is
refused. A client must not be able to inject a fault.

**Why the route exists.** Fault injection used to be `console/seed_demo.py`: a CLI
that registers two synthetic nodes and oscillates their telemetry. There was no way
to trigger a *specific* fault, no way to target a chosen node, and no way to clear a
running fault — so a demo run depended on the always-on seeder and on which half of
its oscillation was on screen. These endpoints make injection an explicit,
reversible operator action, and they make the seeder *optional* (it remains usable;
nothing about the demo requires it — see `console/seed_demo.py`).

**What an injection is.** There is no on-node fault agent in this build
(`node-bootstrap.md` is specified, not shipped), so an injection is a
**controller-owned telemetry series**: for its TTL the injector writes one sample
per second for the target node through the *same* store path a node's own telemetry
goes through (`telemetry_samples` / `telemetry_batches`, plus the `node.telemetry`
stream). The charts, the frozen §8 rules and the AI-operator loop consume it exactly
as if the node had reported it. Two consequences are normative:

- **While an injection is open for a node, that node's own telemetry is
  superseded.** A `POST /v1/nodes/{node_id}/telemetry` from that node is still
  accepted at the transport level but stores **none** of its samples
  (`202 {"accepted": 0, "rejected": <n>, "backpressure": "none"}`), so the injected
  series is authoritative and the rule holds across its debounce count instead of
  flapping between the fault and the node's healthy reports. Only one injection may
  hold a node's series per (node, rule) — see the refusals below.
- **The `demo_faults` row is the durable provenance** (migration `0014`), together
  with the operator-log lines described below. The stored samples themselves are not
  individually marked.

### Scenarios

The scenario set is the one the harness already implements (`chaos/chaos.json`), so
an injected fault has the same signature the chaos runs prove.

| `scenario`     | rule it drives     | injected signal |
|----------------|--------------------|-----------------|
| `oom`          | `oom-guard-01`     | host RAM → 97 % of `ram_total_bytes`, CPU raised, nested `lxd_instances[].ram_used_bytes` kept consistent |
| `disk-degrade` | `disk-degrade-01`  | every `disk[].free_bytes` → 3 % of total, `write_latency_ms` → 80 |
| `disk-fail`    | `disk-fail-01`     | every `disk[].mount_ro` → `true` (EXT4 remount-ro, Scenario B) |
| `latency`      | `latency-01`       | every `net[].latency_ms` → 600 |

`clear` is not a scenario: it is the `DELETE` below.

### POST /v1/projects/{project_id}/demo/faults

Start one injection. `project_token` auth, scoped to the token's project.

Request:
```json
{ "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "scenario": "oom",
  "duration_s": 30,
  "target": "real" }
```

- `node_id` must be a node of this project (the console offers the fleet listing,
  `GET …/nodes`, including `?include=synthetic`).
- `scenario` ∈ the table above.
- `duration_s` is the injection's TTL, `5 ≤ duration_s ≤ 600` (default `30`).
- `target` ∈ `real | synthetic` and must match the node's provenance
  (`nodes.synthetic`); it exists so the operator states which kind of node they are
  faulting, and a mismatch is refused rather than silently honoured.

Response `201 Created`:
```json
{ "schema_version": "1.0",
  "injection_id": "01M4349RQ3BM21NNW2A68CT3SQ",
  "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "node_registration_id": "termini-node-a",
  "node_role": "compute",
  "scenario": "oom", "rule_id": "oom-guard-01", "target": "real",
  "state": "active",
  "duration_s": 30.0,
  "started_at": "2026-10-04T07:49:00.000Z",
  "expires_at": "2026-10-04T07:49:30.000Z",
  "cleared_at": null, "cleared_by": null, "clear_reason": null,
  "samples_written": 0, "recovery_samples": 0,
  "clear_samples_needed": 3, "open": true }
```

`state` ∈ `active | recovering | cleared | expired`. `clear_samples_needed` is the
number of consecutive healthy samples the driven rule's hysteresis needs to re-arm
(`operator/detect.recovered`, per-rule `*_clear_consecutive`; 3 by default).

### GET /v1/projects/{project_id}/demo/faults

`project_token` auth. Query: `state=all|open` (default `all`), `limit` (as §4.2).
Returns every injection this project has run, newest first:

```json
{ "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
  "scenarios": ["oom", "disk-degrade", "disk-fail", "latency"],
  "faults": [ { "…": "injection view as above" } ],
  "open_count": 1 }
```

### DELETE /v1/projects/{project_id}/demo/faults/{injection_id}

Clear one injection. `project_token` auth. Optional `?reason=<≤200 chars>`
(default `cleared_by_operator`). Response `200 OK` with the injection view, whose
`state` is now `recovering`:

```json
{ "injection_id": "01M4349RQ3BM21NNW2A68CT3SQ", "state": "recovering",
  "clear_samples_needed": 3, "recovery_samples": 0,
  "samples_written": 12, "open": true, "…": "…" }
```

Clearing **starts a recovery burst** rather than stopping: the injector keeps
writing the node's healthy baseline for `clear_samples_needed` more samples (one per
second) and then closes the episode, so the rule provably returns below its clear
threshold and the operator log records the completion. Clearing an already-cleared
injection is idempotent — `200` with `"status": "already_cleared"` (and
`"already_expired"` for one closed by its TTL or a restart); an unknown
`injection_id` is `404 NOT_FOUND`. A fault is always clearable: this endpoint never
refuses on state.

### POST /v1/projects/{project_id}/demo/faults/clear

`project_token` auth. Clear every open injection, or just one node's. Body
`{"node_id": "<id>", "reason": "<text>"}` (both optional) → `200`
`{"cleared": [<injection view>…], "count": 2}`. This is the clear path for an
environment whose reverse proxy drops `DELETE` (both are in the contract and both
run the same recovery burst).

### Refusals (both `409 CONFLICT`)

| Condition | HTTP | `code` | `details[].reason` |
|---|---|---|---|
| An open injection already holds this `(node, rule)` while it is `active`/`recovering` | `409` | `CONFLICT` | `fault_already_active` (+ `injection_id`) |
| A remediation for this `(node, rule)` is **in flight** | `409` | `CONFLICT` | `remediation_in_flight` (+ the shape: `pending_remediation` = the loop fired and its audit is not written yet, `recent_remediation` = an audit within the settle window) |
| No telemetry and no hardware facts to build a baseline from | `409` | `CONFLICT` | `no_telemetry_baseline` |

The second is safety rule §3 of the card: injecting onto a rule the operator is
mid-remediation on would stack a second fault on the same episode. The settle window
is `TERMINI_DEMO_FAULT_GUARD_S` (seconds, default `60`), configurable per
environment — a demo that wants to re-inject the same scenario quickly lowers it.

Malformed bodies are `422 INVALID_ARGUMENT` (unknown scenario, `duration_s` out of
range, `target` missing or not matching the node's provenance, unknown `node_id` →
`404 NOT_FOUND`). No token is `401 UNAUTHENTICATED`; a `node_token` is
`403 PERMISSION_DENIED` — **the route is not reachable from a node context**.

### TTL and restarts

An injection may never outlive its TTL silently:

- at `expires_at` the driver logs `fault TTL expired after <n>s — auto-clearing: …`
  and runs the recovery burst (`state: recovering` → `cleared`,
  `clear_reason: "ttl_expired"`);
- an injection left `active`/`recovering` by a **previous controller process** is
  swept at boot (`state: expired`, `cleared_by: "controller_restart"`) and logged —
  the driver that owned it is gone, so reporting it as live would be a lie.

Both are also events on the operator log (§7.2), which is where the client
dashboard shows them.

### Operator log (client-visible provenance)

Every transition is appended to the project's operator log with `actor: "human"`, so
the client dashboard's live AI-operator panel shows the injection the way
`dashboard.md` §2.1 renders human-actor lines:

```
fault injected by operator: node termini-node-a scenario=oom rule=oom-guard-01
  target=real ttl=30s (injection …, node telemetry superseded while active)
node telemetry superseded by active fault injection … (warn, once per injection)
fault cleared by operator: node termini-node-a scenario=oom rule=oom-guard-01 —
  restoring the healthy series, 3 sample(s) to re-arm (injection …)
fault cleared: node termini-node-a rule oom-guard-01 back below threshold after 3
  healthy sample(s) (injection …)
fault TTL expired after 30s — auto-clearing: … (injection …)
```

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `TERMINI_DEMO_FAULT_GUARD_S` | `60` | settle window during which a remediation for the same `(node, rule)` is treated as still in flight (the `409` above) |
| `TERMINI_DEMO_FAULTS` | `on` | `off` disables the surface entirely (the endpoints then answer `503 INTERNAL` instead of pretending to inject) |

Additive contract notes (§1.2): the `DELETE` verb is now part of the controller's
CORS `Access-Control-Allow-Methods`, and the four routes above are added to §4.
Nothing existing changed.

---

## 4.8 Client install surface (public, unauthenticated, secret-free)

The routes a **customer's own machine** uses to join the deployment. They serve
**static files** from the controller itself and carry **no credential of any
class**: the credential in this flow is the customer's own single-use
`bootstrap_token`, which their provider mints for them (§4.6) and the customer
passes to the installer. The controller's `project_token` is never part of this
flow — not in the artifact, not in the URL, not in a step of the install — which
is exactly what this surface exists to make possible.

| Route | Serves |
|---|---|
| `GET /install/termini-node.sh` | the customer-facing installer (`infra/bootstrap/client/termini-node-install.sh`), meant to be piped into `sh` |
| `GET /install/termini-node-status.py` | `termini-node-status`, the client's own view of its node (installed by the installer) |
| `GET /install/termini-bootstrap.py` | the node daemon, for a machine that did not come from the Termini node image |
| `GET /install/termini-bootstrap.service` | the daemon's systemd unit |

Rules:

- **No token of any class.** `/install/` is the only unauthenticated prefix on the
  controller. Requiring a token here would defeat the purpose: the only credential
  the customer holds at this point is their one-time enrollment token, and the
  project token must never reach a client (the card's guardrail).
- **Named artifacts only.** The artifact is chosen by exact name from a table in
  `controller/app/server.py` (`INSTALL_ARTIFACTS`); no part of the request path is
  ever used to build a filesystem path, so there is no traversal to escape.
  Anything that is not exactly `/install/<known-name>` is `404 NOT_FOUND`.
- **Content type is the artifact's own** (`text/x-shellscript`, `text/x-python`,
  `text/plain`) with `Cache-Control: no-store`, so rolling out a new installer
  cannot be masked by an intermediary's cache.
- **Deployment.** The artifacts are read from `infra/bootstrap/` in the deployed
  tree (`TERMINI_INSTALL_DIR` overrides the location);
  `infra/gcp/tools/deploy-termini-tree.sh` ships that directory with the
  controller. A controller deployed without it answers `503 INTERNAL` rather than
  `404`, so a missing artifact cannot be mistaken for a missing feature.

The command a customer runs, end to end:

```
curl -fsSL https://<controller-host>/install/termini-node.sh | sudo sh -s -- \
  --controller https://<controller-host> --token <their single-use credential>
```

After registration the customer reads the node back with the node's **own**
`node_token` (`GET /v1/nodes/{node_id}/telemetry/recent`, §4.1) — never with the
project token. Provider-side: `infra/bootstrap/client/issue-enrollment.py` mints
the credential, preflights this surface, prints the command above and writes the
handover file. Customer-facing documentation: `infra/bootstrap/client/README.md`.

Additive contract note (§1.2): a public, unauthenticated, static `/install/`
prefix is added to the controller's route surface. No existing route, payload or
token rule changed.

## 4.9 Agent tasks (client → controller → agent) — card `t_1a2c913c`

Give an agent a task and read its answer. The agent-facing surface is inside the instance and bound to loopback; the controller **proxies** the call through the owning node's LXD exec (decision **AT-1** in `agent-runtime.md` §7.4), so the client needs no route to the instance and receives no instance credentials. Every submission is audited like a node action.

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/v1/projects/{project_id}/agents/{vm_id}/tasks` | `project_token`. Body `{ "prompt": str, "model_preference": str?, "max_tokens": int?, "actor": str? }`. `202` + the task record. |
| `GET` | `/v1/projects/{project_id}/agents/{vm_id}/tasks/{task_id}` | `project_token`. State + answer (`answer`, `model`, `provenance`, `usage`, `duration_ms`, `error`). |
| `GET` | `/v1/projects/{project_id}/agents/{vm_id}/tasks?limit=N` | `project_token`. The enumerable record (summaries only — no prompts, no answers). |

`vm_id` is the agent's LXD instance name (`controller-api.md` §5). Task record:
```json
{
  "task_id": "01M506S2JJ5JFCR3K30BJY26NK",
  "vm_id": "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0",
  "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
  "command_id": "01M506S2JJP0CJQ5C1C4V3N7T9",
  "agent_task_id": "01M506S2JK3S4T5V6W7X8Y9Z01",
  "state": "done",
  "actor": "human:ops",
  "model": "gemma",
  "provenance": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "forwarded": true},
  "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
  "answer": "pong",
  "error_code": null,
  "submitted_at": "2026-10-15T16:31:02Z",
  "ended_at": "2026-10-15T16:31:06Z",
  "duration_ms": 4120
}
```

States: `queued | running | done | error` (agent-runtime §5.6). The controller's deadline for one task is 300 s; a task that outlives it is `error: TASK_TIMEOUT`, and a controller restart fails any inherited row with `TASK_ORPHANED` rather than leaving it looking slow.

**Errors** (§3 envelope): `404 AGENT_NOT_FOUND`, `503 NODE_OFFLINE`, `503 NODE_UNREACHABLE`, `503 AGENT_UNREACHABLE`, `429 TASK_BOUND_EXCEEDED`, `400 MODEL_NOT_ALLOWED`, `413 PROMPT_TOO_LARGE`. Codes the controller adds outside the §3 list are task-specific and always carry `details[0].reason` with the action to take.

**Audit and events.** Each submission writes an `agent.dispatch_task` command (`§6` allowlist, `issued_by: human`, node-scoped) plus a content-free operator-log line, and emits `task.queued` → `task.state_changed` → `task.completed` on the WS stream (§7.1) with `{task_id, vm_id, state, duration_ms, model}` — never content.

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
| agent_vm unspecified size | `vcpus 1 / ram_gb 1 / disk_gb 8` | The size an `agent_vm` gets when the request names none (`termini.intent.types.DEFAULT_AGENT_VM`, also the extraction prompt's few-shot). Sized for the smallest node the demo places on (`n2-standard-2`: 2 vCPU / 8 GiB ⇒ 5 GiB allocatable after the 2 GiB reserve), so a plain "deploy 2 worker agents" is accepted with headroom instead of refused `exceeds_node_ram_ceiling`. A user who names a size still gets literal extraction + the §5.2 step 6 clamp. |
| inference_vm `vcpus`  | [4, 32] | Reference sizing for the Node C host service (day-2 VM budget; not launched in the demo) |
| inference_vm `ram_gb` | [8, 64] | Reference sizing (day-2 VM budget; not launched in the demo) |
| inference_vm `disk_gb`| [32,512]| Reference sizing (day-2 VM budget; not launched in the demo) |
| GPU allocation        | none     | `inference_vm` is realized as the always-on Node C host service (D5); no GPU-bound VM is allocated, and GPU passthrough is day-2 only. `passthrough_gpu: true` is a structural rejection (`gpu_passthrough_not_in_demo`). |

Defaults are configurable per-project but the values above are frozen for the
demo. Absolute caps (never exceedable, encoded in the schema): `vcpus ≤ 128`,
`ram_gb ≤ 512`, `disk_gb ≤ 4096`, `count ≤ 64`.

### 5.2 Validation order (deterministic, run in this exact sequence)

1. **Schema validation** (structural): unknown fields, wrong types, missing
   required `type`/`count`, `passthrough_gpu: true` on ANY class, `labels` that
   violate the bounding rules, enum violations → `REJECT` (`400 INVALID_ARGUMENT`).
   Structural errors are never clamped. (`passthrough_gpu: true` rejects with
   reason `gpu_passthrough_not_in_demo` — GPU passthrough is day-2 only, D5.)
2. **Defaulting**: fill omitted optionals — `target_node → "auto"`,
   `disk_gb` → type floor (agent 8, inference 64), `passthrough_gpu` → false.
   Record `decision: defaulted`.
3. **Reference-class resolution** (`inference_vm` only): `inference_vm` is a
   reference to the always-on Node C host service, not a launchable VM. Its
   `target_node` must resolve to the `inference` node (Node C in the demo) — an
   explicit `target_node` that is not the inference node, or an `auto` that cannot
   resolve to one (no inference node registered), → `REJECT`
   (`invalid_target_node_type`). The plan records `realized_as: "host_service"`
   and `vm_ids: []`; no VM is created and no GPU is allocated. Provenance applies
   here too (§5.2 step 5): while any real node is registered, `auto` resolves
   `inference_vm` only to a **real** inference node — a seeder-fabricated row is not
   the host inference service.
4. **Per-field range clamp**: clamp each numeric field into its type range
   (table above). Record `decision: clamped` with `from`/`to`. Below-min and
   above-max both clamp to the nearest bound; never reject a pure range overflow.
5. **Target-node resolution** (`agent_vm`): resolve `auto` to the compute node with
   the most free allocatable RAM (deterministic tie-break: lowest `node_id`). An
   explicit `target_node` that is not a valid compute node → `REJECT`
   (`invalid_target_node_type`).
   **Model-host exclusion (normative).** `auto` never resolves to the node that
   serves the always-on decision model. That node is a *service* host — it runs
   llama.cpp for the whole fleet — and nothing in the frozen role vocabulary says
   so: role is GPU-gated (§4.1 / `node-bootstrap.md` §5.3), so a CPU-only model
   host registers `compute` and, on a fleet where it has more RAM than the real
   compute node, won every max-free-RAM comparison (agents materialised on the
   machine busy serving the model, and the operator's remediation — which dials
   each node's own endpoint — then touched a different node than the one that
   holds the agents). The host is identified from the model address the controller
   already knows (`TERMINI_INFERENCE_BASE_URL`) matched against each node's
   reported LXD endpoint / source address, plus any operator-named hosts
   (`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`). The exclusion is dropped when it
   would leave **no** candidate at all, so a single-node / seeder-only demo stays
   placeable; an explicit `target_node` naming the model host is still honoured,
   because naming a node is not `auto`. This does **not** change role assignment.
   **Provenance rule (normative).** A row fabricated by the demo seeder
   (`nodes.synthetic = true`; declared on the registration wire, `node-bootstrap.md`
   §5.2) is never preferred over a real registered node, and is not resolvable at
   all while any real node is registered: `auto` considers real nodes only, and an
   explicit `target_node` that names a fabricated row resolves to nothing — which
   surfaces as the frozen `invalid_target_node` (`agent_vm`) /
   `invalid_target_node_type` (`inference_vm`) rejection, never a silent
   redirection to a different node. Fabricated rows are candidates only when NO
   real node is registered (a seeder-only demo). Before this rule, resolution was
   a pure capacity comparison and the seeder's inflated facts — 64 GiB against a
   real node's real 1-8 GiB — won every time, so `auto` placed agents on a node
   that does not exist.
6. **Per-node capacity clamp** (`agent_vm` only): using allocatable capacity
   (`registered − reserve − already_allocated`), clamp in this order:
   - `ram_gb` → min(requested, free RAM). If result < type min → `REJECT`
     (`exceeds_node_ram_ceiling`).
   - `vcpus` → min(requested, `cpu_overcommit_ratio × logical_cores − allocated`).
     If < 1 → `REJECT` (`exceeds_node_cpu_ceiling`).
   - `disk_gb` → min(requested, free disk). If < type min → `REJECT`
     (`exceeds_node_disk_ceiling`).

   The ceiling is the **resolved node's own registered capacity**, so once step 5
   resolves to a real node, that node's facts are authoritative (a fabricated row
   can neither win the placement nor widen the ceiling). `disk_gb` is clamped only
   when the node registered a *plausible* disk (`capacity.disk_known`): a node
   whose every registered device is below 1 GiB — including the `size_bytes: 1`
   placeholder older node builds sent — has **unknown** disk and the disk step is
   skipped, rather than being driven by a nonsense number. The same real-node
   preference derives the coarse per-role host maxima used by step 4.
7. **Commit**: allocate capacity for accepted/clamped `agent_vm` classes (the
   `inference_vm` reference class reserves no GPU/VM capacity), append the
   `guardrail_audit` record, and return the `AllocationPlan`. A committed class
   releases its capacity when its materialisation fails, when the plan is released
   (`POST …/vms/release`), when its node is decommissioned, or automatically when
   its materialised instances are gone (`POST …/vms/reconcile`); the commit row is
   soft-released, so the audit trail survives.

### 5.3 Reject vs clamp — reference table

| Condition                                   | Behavior             |
|---------------------------------------------|----------------------|
| Unknown field, wrong type, missing required | REJECT (400)         |
| Enum value out of set                       | REJECT (400)         |
| `passthrough_gpu: true` on any class        | REJECT (400) `gpu_passthrough_not_in_demo` |
| `inference_vm` targeting a non-inference node | REJECT (422) `invalid_target_node_type` |
| `labels` exceeding bounds (count/keys/value)| REJECT (400) `INVALID_ARGUMENT` |
| Numeric below min or above absolute max     | CLAMP to bound       |
| RAM exceeds free node RAM                   | CLAMP, then REJECT if < min |
| vCPU exceeds overcommit ceiling             | CLAMP, then REJECT if < 1 |
| Disk exceeds free node disk                 | CLAMP, then REJECT if < min |

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

The demo wires two command surfaces (`architecture.md` R5):

- **Human node actions** — `node.reboot` and `node.drain`, issued via
  `POST /v1/projects/{project_id}/nodes/{node_id}/actions/{reboot,drain}` (queued as
  commands). Payloads: `node.reboot` `{"delay_s": 0}`; `node.drain`
  `{"reason": "disk_degradation", "evacuate_to": "<node-id>"}`.
- **AI-operator remediation** — the LXD REST actions the executor issues
  (`controller/app/operator/executor.py`): `instance.restart`, `instance.stop`,
  `instance.delete`, `image.prune`. These are issued directly against the LXD
  REST API of **the node that reported the fault** — the loop resolves a per-node
  executor exactly as apply-plan materialisation does
  (`app/operator/target.executor_for_node`), so remediation reaches the machine
  that holds the instances; the global `TERMINI_LXD_BASE_URL` is only the fallback
  for a node that reported no endpoint. They are **not** `vm.*` command-envelope
  verbs.

Command envelope (the queued-command store, `controller/app/commands.py`):

```json
{
  "command_id": "01M4349RPY26W8Q453PZYK05CB",
  "type": "node.drain | node.reboot",
  "payload": { "...": "type-specific, see above" },
  "issued_by": "ai_operator | user | system",
  "issued_at": "2026-10-04T07:49:00Z",
  "timeout_s": 60
}
```

The broader `commandAllowlist()` set (`vm.resize`, `vm.migrate`, `vm.stop`, `vm.start`,
`vm.destroy`, `agent.dispatch_task`, `agent.cancel_task`) is **declared in
`controller/app/commands.py` but not wired to any execution path in the demo** — marked
future/day-2. `vm.launch` is **not** in the allowlist and there is no launch path —
apply-plan materialisation instead drives the resolved node's LXD REST API directly
from the controller (`_apply_plan` creates the instances and returns their ids). The retired `ssh.inject_key` verb is removed
everywhere; user SSH access is the controller-held CA signing short-lived certs,
surfaced as a web terminal + `ssh` string (§4.3), never an `authorized_keys` write on a node.

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
| `task.queued`          | `{task_id, vm_id, project_id}` |
| `task.state_changed`   | `{task_id, vm_id, state}` |
| `task.completed`       | `{task_id, vm_id, duration_ms, model}` |
| `log.line`             | full operator-log envelope (see `dashboard.md` §2.1) |

### 7.2 AI operator logs — `GET /v1/projects/{project_id}/logs`

Server-Sent Events (`text/event-stream`). The canonical log envelope is frozen in
`dashboard.md` §2.1 (`log_id`, `ts`, `node_id`, `severity`, `actor`, `message`,
`remediation`, and `Last-Event-ID` resume semantics); this endpoint streams that
envelope. Each event:
```
event: log
data: {"log_id":"...","ts":"...","node_id":"...","severity":"info","actor":"ai_operator","message":"Node A disk failure predicted. ...","remediation":{...}}
```
`severity` ∈ `debug | info | warn | error`. Only AI-operator operational messages
are emitted here; application/customer logs are NEVER surfaced on this channel
(`governance.md` — C2). The same envelope is also mirrored as `log.line` on the WS
stream (§7.1).

### 7.3 xterm.js terminal — node-level and VM-level

Two terminal surfaces (reconciled with `dashboard.md` §3.1 — see architecture.md R14):

- **Node-level host shell** — `wss://<controller-host>/terminal/v1/nodes/{node_id}?token=<project_token>`:
  the primary dashboard terminal. Opens an interactive shell on the selected node
  host. The controller brokers the session over the node's outbound mTLS control
  channel (D6) or via a short-lived CA-signed operator cert (D4); no inbound SSH is
  required.
- **VM-level agent shell** — `wss://<controller-host>/terminal/v1/vms/{vm_id}?token=<project_token>`:
  opens a shell inside an agent VM via LXD (`lxc exec`) on the owning node.

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

Rules: one session per target (a second connection to the same node/vm closes the
first with `{"type":"error","message":"session_replaced"}`). Idle > 15 min closes
with `{"type":"exit","code":-1}`. The `project_token` is a query param because
browsers cannot set a header on `new WebSocket` (documented demo tradeoff; day-2
uses a short-lived ticket — `dashboard.md` §3.1).

---

## 8. Streaming cadence

| Channel                | Cadence (default)       | Notes |
|------------------------|-------------------------|-------|
| Node heartbeat         | every 5 s               | 15 s → `degraded`, 45 s → `offline` |
| Node telemetry         | default 15 s; 1 s demo   | see `node-telemetry.md` |
| Command long-poll      | every 2 s, 25 s block   | WS push preferred |
| WS telemetry snapshots | ≤ 1/s per node          | throttled from raw cadence |
| AI operator log        | push on append          | ring buffer 10k lines |
| Demo fault series      | 1 s while an injection is open | §4.7 — a controller-owned sample per second, superseding the node's own reports for the (node, rule) |
| Metrics storage        | raw (1 s demo / 15 s default) → 5s/1m/5m downsample | charts read downsampled |

During the demo the operator loop consumes telemetry at 1 s so a remediation can
be issued quickly. Two mechanisms keep the OOM demo deterministic (project.md
advisory #4): (a) the `oom-guard-01` threshold rule has a **local fast-path** — a
deterministic threshold→action mapping (`ram ≥ 95% ×2` → `instance.restart` of the
heaviest workload) that runs with **no model call at all**, so remediation is not
gated on model latency. (b) The demo drives a **deterministic chaos ramp** — the
chaos harness pushes the metric across the threshold on cue — to guarantee the rule
fires even when the stress workload (`stress-ng --vm-bytes 85%`) would not reach
the 95%×2 threshold on its own. The kernel-OOM race is therefore handled as a
*best-effort* guarantee, not an absolute one: if the kernel OOM-killer wins before
two samples arrive, the operator reports the process death rather than a
fabricated success. No endpoint polls slower than 2 s during the demo.

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
- Operator decision-step contract (model-driven decision, guardrail, fallback) →
  `operator-decision.md`.

No field in this contract is a TBD. If a value is not fixed above, the default in
section 5.1 applies.
