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
  "type": "vm.resize", "payload": {...}, "issued_by": "ai_operator",
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
rejections. Accepted plans are NOT executed by this endpoint — the caller invokes
`POST /v1/projects/{project_id}/vms/apply` (below) to materialize a plan.

#### POST /v1/projects/{project_id}/vms/apply

Materialize an accepted plan. Body:
```json
{ "plan_id": "01M4349RQ2P5DPC7K59R0WFJCR" }
```
Response `202 Accepted`:
```json
{ "job_id": "01M4349RQ3BM21NNW2A68CT3SQ",
  "state": "provisioning", "vm_ids": ["01M4349RPXWDC52ZDAVMPCMJSH"] }
```
The controller fans out LXD instance creation to the target nodes via the node
command channel; frontend tracks progress via the event stream (section 7).

#### GET /v1/projects/{project_id}/nodes

```json
{ "nodes": [ { "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
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
remediation approve/deny (`POST /v1/projects/{project_id}/remediations/{command_id}/{approve,deny}`),
and the node SSH access string (`GET /v1/projects/{project_id}/nodes/{node_id}/ssh`)
are frozen in `dashboard.md` §3.2/§4 and are the frontend's manual-intervention surface.

### 4.3 SSH certificate issuance (user access — D4 CA model)

User SSH access follows `infra-decisions.md` D4: the controller holds an SSH
certificate authority; users never receive a private key from us; the browser
generates a keypair locally (or the user uploads a public key) and the private
key never transits the controller.

#### POST /v1/projects/{project_id}/ssh-key

Upload the operator's public key and receive a controller-signed, short-lived
certificate. The private key is generated and held in the browser (or supplied
by the user); it is never sent to, or generated by, the controller.

Request:
```json
{
  "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... operator@browser",
  "principal": "root",
  "validity_s": 28800
}
```

Response `200 OK`:
```json
{
  "certificate": "ssh-ed25519-cert-v01@openssh.com AAAAHHNzaC1lZDI1NTE5...",
  "ca_fingerprint": "SHA256:AbC123...==",
  "principal": "root",
  "valid_until": "2026-10-04T15:00:00Z",
  "algorithm": "ed25519"
}
```

`validity_s` is clamped to the CA policy maximum (default 8 h = 28800 s; never
more than 24 h). The certificate is principal-bound (`root` for the demo) and
optionally source-address-bound (`infra-decisions.md` D4). Every issued
certificate is recorded in the audit ledger (`governance.md` §8); the private key
is never seen by the controller.

#### POST /v1/projects/{project_id}/ssh-key/revoke

Revoke an issued certificate (by fingerprint) before its TTL. Returns `200`.
Day-2 adds full CA rotation; during the demo the CA is the trust anchor injected
at node provisioning (`node-bootstrap.md` §7).

The frontend renders the SSH access string from
`GET /v1/projects/{project_id}/nodes/{node_id}/ssh` (`dashboard.md` §3.2):
`ssh -i <browser_key> root@<node_ip>` with the signed certificate installed
alongside the browser-local private key.

### 4.4 Governance audit (verifier-facing)

#### GET /v1/governance/audit?node_id=&since=

Read-only, authenticated (project token with a `governance:audit` scope or the
operator). Returns the controller-side audit records for reconciliation
(`governance.md` §8.3):

```json
{ "records": [ { "call_id": "01M4349RPY26W8Q453PZYK05CB",
  "source_node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "ts": "...",
  "endpoint": "/v1/telemetry", "result": "accepted",
  "redactor_version_asserted": "redactor-config v3", "governance_violation": false } ],
  "chain_tip": "01M4349RPZ4DMD3MT18EBZRQB4" }
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
   and `vm_ids: []`; no VM is created and no GPU is allocated.
4. **Per-field range clamp**: clamp each numeric field into its type range
   (table above). Record `decision: clamped` with `from`/`to`. Below-min and
   above-max both clamp to the nearest bound; never reject a pure range overflow.
5. **Target-node resolution** (`agent_vm`): resolve `auto` to the compute node with
   the most free allocatable RAM (deterministic tie-break: lowest `node_id`). An
   explicit `target_node` that is not a valid compute node → `REJECT`
   (`invalid_target_node_type`).
6. **Per-node capacity clamp** (`agent_vm` only): using allocatable capacity
   (`registered − reserve − already_allocated`), clamp in this order:
   - `ram_gb` → min(requested, free RAM). If result < type min → `REJECT`
     (`exceeds_node_ram_ceiling`).
   - `vcpus` → min(requested, `cpu_overcommit_ratio × logical_cores − allocated`).
     If < 1 → `REJECT` (`exceeds_node_cpu_ceiling`).
   - `disk_gb` → min(requested, free disk). If < type min → `REJECT`
     (`exceeds_node_disk_ceiling`).
7. **Commit**: allocate capacity for accepted/clamped `agent_vm` classes (the
   `inference_vm` reference class reserves no GPU/VM capacity), append the
   `guardrail_audit` record, and return the `AllocationPlan`.

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

The controller issues remediation/operations as commands. Delivery is WebSocket
push by default, HTTP long-poll fallback. Command envelope:

```json
{
  "command_id": "01M4349RPY26W8Q453PZYK05CB",
  "type": "vm.launch | vm.resize | vm.migrate | vm.stop | vm.start | vm.destroy | node.drain | node.reboot | agent.dispatch_task | agent.cancel_task",
  "payload": { "...": "type-specific, see below" },
  "issued_by": "ai_operator | user | system",
  "issued_at": "2026-10-04T07:49:00Z",
  "timeout_s": 60
}
```

Type-specific payloads:
- `vm.launch`: `{"vm_id":"...","project_id":"...","class":"agent_vm","image":"agent-vm/<version>","member":"node-a","limits":{"vcpus":2,"ram_gb":4,"disk_gb":8},"network":{"name":"lxdbr0"},"profile":"termini-agent","cloud_init":{...},"idempotency_key":"..."}` (full contract in `agent-runtime.md` §4).
- `vm.resize`: `{"vm_id": "...", "vcpus": 4, "ram_gb": 8}` (post-guardrail values).
- `vm.migrate`: `{"vm_id": "...", "source_node": "...", "target_node": "...", "live": true}`.
- `vm.stop`/`vm.start`/`vm.destroy`: `{"vm_id": "..."}`.
- `node.drain`: `{"reason": "disk_degradation", "evacuate_to": "node-b-id"}`.
- `node.reboot`: `{"delay_s": 0}`.
- `agent.dispatch_task` / `agent.cancel_task`: `{"task_id":"...","project_id":"...","vm_id":"...","kind":"agent.run","ttl_s":900,"payload":{...}}` / `{"task_id":"..."}` (task envelope in `agent-runtime.md` §7.2).

User SSH access is NOT a node command: the controller signs short-lived certificates
server-side (`POST /v1/projects/{project_id}/ssh-key`, §4.3) and never writes
`authorized_keys` on a node. The retired `ssh.inject_key` verb is removed from the
allowlist.

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
  channel (D6) or via a short-lived operator certificate (D4); no inbound SSH is
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
| Metrics storage        | raw (1 s demo / 15 s default) → 5s/1m/5m downsample | charts read downsampled |

During the demo the operator loop consumes telemetry at 1 s so a remediation can
be issued quickly. Two mechanisms keep the OOM demo deterministic (project.md
advisory #4): (a) the `oom-guard-01` threshold rule has a **local fast-path** — a
deterministic threshold→command mapping (`ram ≥ 95% ×2` → `vm.resize`) that runs
without an external model call, so remediation is not gated on model latency; the
external model is advisory only and may refine, never delay, the resize. (b) The
demo injects a **synthetic persistent threshold trigger** to guarantee the rule
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

No field in this contract is a TBD. If a value is not fixed above, the default in
section 5.1 applies.
