# Customer Dashboard ↔ Controller Contract (W5)

- Owner: `architect` (kanban task `t_bc529e86`). Consumers: `frontend` (dashboard +
  web terminal) and `controller` (backend endpoints).
- Status: **FROZEN** for the 5-day build. This document is the single source of
  truth for how the customer operations dashboard talks to the Controller.
- Date: 2026-10-04
- Requirements source: `termini/project.md`, W5 owner context.

This document freezes four contract surfaces — telemetry (node status/health +
resource series), the AI-operator log stream, the terminal (WebSocket + SSH access
string), and manual intervention — plus an integration note mapping each to its
owning component and the existing telemetry source. Where this document and
`controller-api.md` disagree on a field or route, **this document wins for the
dashboard surface**; the disagreement is listed in §7 so the owning team can
reconcile the parent file. No field in this document is a TBD.

Conventions (identifiers, timestamps, error envelope, idempotency, token classes,
and the `project_token` scoping rule) are inherited verbatim from
`controller-api.md` §1–§3 and are not restated here.

---

## 1. Telemetry contract (dashboard view)

The dashboard renders node status/health and resource utilization charts. The
data originates in node telemetry (`docs/contracts/node-telemetry.md`, the single
source of truth), is ingested by the controller under the governance
gates (`controller-api.md` §2), and is served back to the dashboard over REST
(poll) and the event stream (push).

### 1.1 Node status/health — `GET /v1/projects/{project_id}/nodes`

List endpoint. `project_token` auth. Response:

```json
{
  "nodes": [
    {
      "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
      "registration_id": "termini-node-a",
      "role": "compute",
      "synthetic": false,
      "status": "online",
      "last_seen_at": "2026-10-04T07:49:00Z",
      "uptime_sec": 3600,
      "hardware": {
        "cpu": {"model": "AMD EPYC 7B13", "threads": 16, "cores": 8},
        "memory": {"total_bytes": 68719476736},
        "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 214748364800}],
        "gpus": [{"vendor": "nvidia", "model": "Tesla T4", "vram_bytes": 17179869184}]
      },
      "capacity": {
        "cpu_cores": 16,
        "ram_gb": 32,
        "disk_gb": 256,
        "disk_known": true,
        "gpu_free": 0,
        "cpu_allocated": 6,
        "ram_allocated_gb": 12
      },
      "health": {
        "cpu_util_pct": 31.2,
        "ram_used_pct": 57.5,
        "disk_ok": true,
        "gpu_ok": null,
        "inference_ok": null,
        "services": [
          {"name": "termini-bootstrap", "status": "running"},
          {"name": "lxd", "status": "running"},
          {"name": "sshd", "status": "running"}
        ],
        "degraded_reasons": []
      }
    }
  ]
}
```

Field contract:

| Field | Type | Notes |
|-------|------|-------|
| `node_id` | string (ULID) | controller-issued |
| `registration_id` | string | stable bootstrap label (1..64 chars) |
| `role` | enum | `compute \| inference` |
| `synthetic` | boolean | `true` only for a row fabricated by the demo seeder (`console/seed_demo.py`); `false` for real hardware. Placement never uses a `true` row while any real node is registered (`controller-api.md` §5.2 step 5); the dashboard labels it (e.g. "demo") so a viewer can tell them apart. **Amendment (standing-fleet default view):** the listing applies the same rule — while any real node is registered, a `synthetic` row is EXCLUDED from the default response, so the fleet a viewer sees is the real hardware (a stopped seeder's stale rows cannot make the listing contradict the deck). A seeder-only demo (no real node registered) lists its fabricated rows, and `?include=synthetic` returns every row (labelled). |
| `status` | enum | `online \| degraded \| offline \| fenced` (derivation below) |
| `last_seen_at` | RFC3339 | last heartbeat/telemetry contact |
| `uptime_sec` | integer | from the latest telemetry sample (`uptime_sec`) |
| `hardware` | object | registered hardware facts (from registration) |
| `capacity` | object | allocatable bookkeeping (registered − reserve − allocated), plus `disk_known`: `false` when the node reported no plausible disk size, so its disk accounting is **unknown**, not zero |
| `health` | object | derived from the latest telemetry sample (§1.3) |

`status` derivation (evaluated on every view, frozen):

- `offline` if `now − last_seen_at > 45 s`.
- else `fenced` if any health flag is `false` (non-null) **and** the falseness is
  a read-only disk (`disk_ok == false`). This is the F05 state: the node's disk
  remounted read-only, the operator stopped the workload, and a
  `restart`/`start` is refused until a human replaces the disk. The dashboard
  renders it as "needs a human: disk failed, workload fenced".
- else `degraded` if `now − last_seen_at > 15 s` **or** any remaining health flag
  is `false` (non-null) **or** any allowlisted service is not `running`.
- else `online`.

`role` is assigned at registration (`controller-api.md` §4.1): a node reporting
≥1 GPU with `vram_bytes ≥ 12884901888` (12 GiB) is `inference`, else `compute`;
the demo caps `inference` to node C.

### 1.2 Node detail — `GET /v1/projects/{project_id}/nodes/{node_id}`

Same object as one list element, plus:

```json
{ "node": { "...": "as above" }, "metrics_latest": { "...": "most recent telemetry Sample" } }
```

`metrics_latest` is the raw most-recent `Sample` from `node-telemetry.md` §3
(nested: host metrics, `disk[]`, `net[]`, `gpu[]`, `lxd_instances[]`,
`inference{}`, `services[]`); the flat chart names are derived per §1.4's mapping.

### 1.3 Health flag derivation (frozen)

Every flag is computed from the latest telemetry sample; a missing sample (never
reported) leaves each flag at its default.

| Flag | Type | Derivation |
|------|------|------------|
| `cpu_util_pct` | number | latest `cpu_util_pct` (0–100) |
| `ram_used_pct` | number | `100 × ram_used_bytes / ram_total_bytes` (0 if `ram_total_bytes` absent) |
| `disk_ok` | boolean | `false` if any `disk[].mount_ro == true`, else `true` |
| `gpu_ok` | boolean\|null | `inference` node: `false` if `gpu[]` empty or `gpu[0].temp_celsius ≥ 85`, else `true`; `compute` node: `null` |
| `inference_ok` | boolean\|null | `inference` node: `inference.server_up`; `compute` node: `null` |
| `services[]` | array | passthrough of `services[]` (allowlist only: `termini-bootstrap`, `lxd`, `sshd`, `llama-cpp`) |
| `degraded_reasons[]` | array\<enum\> | see below |

`degraded_reasons` enum (frozen):

`heartbeat_late` · `disk_read_only` · `disk_degrading` · `gpu_unavailable` ·
`gpu_overtemp` · `inference_down` · `service_failed`

- `disk_read_only` — any `disk[].mount_ro == true`.
- `disk_degrading` — latest `disk[].write_latency_ms ≥ 50` (the *flag*; the
  operator's `disk-degrade-01` rule fires the same signal only after 5
  consecutive samples, `node-telemetry.md` §5).
- `gpu_unavailable` / `gpu_overtemp` — per the `gpu_ok` rows above.
- `inference_down` — `inference_ok == false`.
- `service_failed` — any allowlisted `services[].status == "failed"`.

### 1.4 Resource utilization series — `GET /v1/projects/{project_id}/nodes/{node_id}/metrics`

Query params: `start` (RFC3339), `end` (RFC3339), `resolution` ∈
`raw | 5s | 1m | 5m` (default `5s`), `metrics` (comma-separated, default all).

```json
{
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "resolution": "5s",
  "series": [
    {"name": "cpu_util_pct", "unit": "percent", "points": [{"t": "2026-10-04T07:49:00.000Z", "v": 31.2}]},
    {"name": "ram_used_bytes", "unit": "bytes", "points": [{"t": "2026-10-04T07:49:00.000Z", "v": 19756849561}]},
    {"name": "gpu_util_pct", "unit": "percent", "points": [{"t": "2026-10-04T07:49:00.000Z", "v": 88.0}]}
  ]
}
```

Metric allowlist (frozen — the 19 metrics the controller serves; names and units
are the single source of truth, mirroring `controller/app/store.py METRICS`):

| name | unit |
|------|------|
| `cpu_util_pct` | percent |
| `cpu_core_count` | count |
| `ram_total_bytes` | bytes |
| `ram_used_bytes` | bytes |
| `swap_used_bytes` | bytes |
| `load_avg_1m` | loadavg |
| `cpu_temp_celsius` | celsius |
| `gpu_util_pct` | percent |
| `gpu_vram_used_bytes` | bytes |
| `gpu_vram_total_bytes` | bytes |
| `net_rx_bytes` | bytes |
| `net_tx_bytes` | bytes |
| `net_latency_ms` | ms |
| `disk_total_bytes` | bytes |
| `disk_free_bytes` | bytes |
| `disk_write_latency_ms` | ms |
| `inference_tokens_per_s` | tok/s |
| `inference_active_requests` | count |
| `inference_vram_used_bytes` | bytes |

Points are downsampled to `resolution` (mean per bucket); `raw` returns every
stored sample. Values are averaged per bucket and rounded to 4 decimals. The
frontend converts bytes → GiB for display; the contract stays in bytes.

**Nested→flat mapping (frozen).** The wire telemetry is **nested**
(`node-telemetry.md` §4: `disk[]`, `net[]`, `gpu[]`, `inference{}`, plus flat host
metrics). The controller derives these flat chart series from each sample with a
fixed aggregation (mirrors `controller/app/store.py METRICS`):

| flat name | source field | aggregation |
|-----------|--------------|-------------|
| `cpu_util_pct`, `cpu_core_count`, `ram_*`, `swap_used_bytes`, `load_avg_1m`, `cpu_temp_celsius` | flat host fields | direct |
| `gpu_util_pct`, `gpu_vram_used_bytes`, `gpu_vram_total_bytes` | `gpu[]` | first element (`gpu[0]`) |
| `net_rx_bytes`, `net_tx_bytes`, `net_latency_ms` | `net[]` | first element (`net[0]`) |
| `disk_total_bytes`, `disk_free_bytes`, `disk_write_latency_ms` | `disk[]` | first element (`disk[0]`) |
| `inference_tokens_per_s`, `inference_active_requests`, `inference_vram_used_bytes` | `inference{}` | direct |

The demo has exactly one disk/NIC/GPU per node, so "first element" is the only
element. Day-2 defines per-device series and a sum/max aggregation policy for
multi-device hosts; until then this mapping is normative and `store.py METRICS`
is the reference implementation.

### 1.5 Cadence (poll vs push)

| Channel | Cadence | Notes |
|---------|---------|-------|
| Node → controller telemetry push | 1 s (demo) / 15 s (default) | `node-telemetry.md` §1 |
| Node heartbeat | every 5 s | 15 s → degraded, 45 s → offline |
| Dashboard poll `GET /nodes`, `/nodes/{id}` | every 5 s | overview grid + status |
| Dashboard poll `GET /metrics` | on window change + every 5 s while visible | charts |
| WS push `node.telemetry` | ≤ 1/s per node | throttled from raw cadence |
| WS push `node.health_changed` | on status/health transition | event below |

The dashboard is **poll-primary for REST, push-supplemented for freshness**: the
overview grid and charts poll at 5 s; the event stream (below) delivers
`node.health_changed` immediately on a transition and `node.telemetry` at ≤ 1/s so
the UI advances without a full poll.

---

## 2. AI-operator log stream contract

Live feed of AI-operator remediation events and operator log lines. Only
AI-operator operational messages and human-intervention records are emitted here;
application/customer logs are C2 and are **never** surfaced
(`governance.md` §3).

### 2.1 Message envelope (frozen — single source of truth)

```json
{
  "log_id": "01M4349RQ1EN14D6JXQ5BAND4W",
  "ts": "2026-10-04T07:49:01.000Z",
  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
  "severity": "info",
  "actor": "ai_operator",
  "message": "Node A disk failure detected. Stopping heaviest workload.",
  "remediation": {
    "command_id": "01M4349RPY26W8Q453PZYK05CB",
    "type": "instance.stop",
    "status": "executed"
  }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `log_id` | string (ULID) | unique per line; used for backfill/resume |
| `ts` | RFC3339 (ms) | emit time |
| `node_id` | string (ULID) \| null | null for project-level messages |
| `severity` | enum | `debug \| info \| warn \| error` |
| `actor` | enum | `ai_operator \| human` |
| `message` | string | bounded, redacted (≤ 1000 chars, C0/C1 only) |
| `remediation` | object \| null | null when no remediation is attached |

`remediation` object:

| Field | Type | Notes |
|-------|------|-------|
| `command_id` | string (ULID) | the command the operator issued |
| `type` | enum | one of the `commandAllowlist()` types (`controller-api.md` §6) |
| `status` | enum | `queued \| pending_approval \| approved \| denied \| acked \| running \| succeeded \| failed \| timed_out` |

### 2.2 Transport — SSE (primary) and WS (mirror)

**Primary: Server-Sent Events.** `GET /v1/projects/{project_id}/logs`
(`text/event-stream`). The credential travels in the `Authorization: Bearer
<project_token>` header — the dashboard consumes the stream with `fetch`, not
`EventSource`, precisely so it can set that header (a credential in the URL is
forbidden, `t_5649d117`). `?token=<project_token>` remains accepted as a
fallback for curl/CLI clients. Each event:

```
event: log
data: {"log_id":"...","ts":"...","node_id":"...","severity":"info","actor":"ai_operator","message":"...","remediation":{...}}
```

**Mirror:** the same envelope is emitted as `log.line` on the unified event
stream `wss://<controller-host>/stream/v1?token=<project_token>`
(`controller-api.md` §7.1). The `log.line` `data` is this full envelope (it
supersedes the thin `{node_id?, vm_id?, level, text}` shape).

### 2.3 Reconnect / backfill (frozen)

- SSE clients resume by sending `Last-Event-ID: <log_id>` on reconnect; the
  server replays every line with `log_id > <log_id>` from the ring buffer, then
  continues live. `?since=<log_id>` is accepted as an equivalent query param.
- WS clients resume by opening `/stream/v1?token=...&since=<log_id>`; the server
  replays the buffered tail then streams live.
- Ring buffer capacity: **10,000 lines**. If the requested `log_id` has already
  fallen out of the buffer, the server first emits one control event and then
  resumes from the oldest retained line:

```
event: log
data: {"log_id":"...","ts":"...","severity":"warn","actor":"ai_operator","message":"log tail truncated; resuming from oldest retained line","node_id":null,"remediation":null,"truncated":true}
```

The `truncated: true` flag (additive, dashboard may ignore it) marks the
resumption point after a gap.

---

## 3. Terminal contract

Two surfaces: the embedded xterm.js WebSocket terminal (node-level interactive
shell) and the copyable SSH access string.

### 3.1 WebSocket terminal — `wss://<controller-host>/terminal/v1/nodes/{node_id}?token=<project_token>`

Opens an interactive shell **on the selected node** (host-level, not a VM shell).
`{node_id}` selects the target node; the controller validates that the node
belongs to the token's `project_id` (`403 PERMISSION_DENIED` / `404 NOT_FOUND`)
and then brokers the session. A **VM-level agent shell** is a separate surface at
`wss://<controller-host>/terminal/v1/vms/{vm_id}` (LXD `lxc exec`), reconciled in
`controller-api.md` §7.3 and architecture.md R14.

**Auth handshake.** The `project_token` is carried in `?token=` (browsers cannot
set a header on `new WebSocket`). The server validates the token and its project
scope **before** opening the shell. On failure it closes the socket with close
code `4401` and reason `unauthorized`; no JSON frames are exchanged. The server
SHOULD also check the `Origin` header against the dashboard's origin allowlist.

**Framing.** UTF-8 text frames; each frame is one complete JSON object. Terminal
bytes are base64-encoded inside `data` (binary-clean PTY relay).

Client → server:

```json
{"type": "input",  "data": "bHMgLWxhCg=="}
{"type": "resize", "cols": 120, "rows": 40}
{"type": "ping"}
```

Server → client:

```json
{"type": "ready",  "node_id": "01M4349RPTT29RNTC6VBCXQ0RC", "user": "ubuntu", "shell": "/bin/bash"}
{"type": "output", "data": "dG90YWwgMjQK"}
{"type": "exit",   "code": 0, "reason": "normal"}
{"type": "error",  "code": "NODE_OFFLINE", "message": "node not reachable"}
{"type": "pong"}
```

`error.code` uses the `controller-api.md` §3 error-code set (`NODE_OFFLINE`,
`NOT_FOUND`, `PERMISSION_DENIED`, `INTERNAL`, …).

**Session lifecycle (frozen).**

- One session per node: a second connection to the same `{node_id}` closes the
  first with `{"type":"error","code":"SESSION_REPLACED","message":"session replaced"}`
  and then proceeds.
- Idle > 15 min → server sends `{"type":"exit","code":-1,"reason":"idle_timeout"}`
  and closes.
- Client close (or tab unload) → controller terminates the shell.
- Node offline/unreachable at connect time → `{"type":"error","code":"NODE_OFFLINE","message":"node not reachable"}` then close (the frontend renders a clear
  message, never a hung terminal).

**How the controller reaches the node (mechanism, not wire contract).** Primary
path is the node's outbound mTLS control channel (`/node/v1/{node_id}/channel`,
D6); the demo terminal bridge MAY instead proxy via SSH using a short-lived,
CA-signed operator cert (§3.2) to the node host. The dashboard is agnostic to this:
it only sees the `/terminal/v1/nodes/{node_id}` framing above.

### 3.2 SSH access string (CA-signed — D4)

Frozen format (rendered verbatim by the dashboard, copy-to-clipboard):

```
ssh -i <private_key_path> [-p <port>] <user>@<public_ip>
```

| Part | Value | Source |
|------|-------|--------|
| `<user>` | `ubuntu` | infra inventory `ssh.user` (`SSH_USER`, default `ubuntu`) |
| `<public_ip>` | node's NAT public IP | infra inventory `nodes[].public_ip` |
| `<port>` | `22` | infra inventory `ssh.port` (omit `-p` when `22`) |
| `<private_key_path>` | local path to the **operator's browser-local** key | the key the operator generated locally; its CA-signed short-lived cert sits alongside it (default `~/.ssh/termini_operator`) |

Access model (D4): the **controller holds an SSH CA**, not a user key. The operator (or
dashboard) generates a keypair **locally in the browser** and uploads only the public
half; the controller signs it into a **short-lived (≤8 h), principal-bound certificate**
(`POST /v1/projects/{project_id}/ssh-key`, `controller-api.md` §4.3). The dashboard
renders the `ssh -i <key>` command pointing at that browser-local key; the signed cert
sits alongside it. Revocation is `POST /v1/projects/{project_id}/ssh-key/revoke`, and
every issuance/revocation lands in the `ssh_certificates` ledger.

The infra-injected **break-glass key** is a ONE-TIME recovery path, injected at
provisioning (GCP metadata `ssh-keys: ubuntu:<pub>`) and **removed after successful
registration** (`node-bootstrap.md` §7) — it is NOT standing access.

Endpoint that feeds the dashboard panel:

`GET /v1/projects/{project_id}/nodes/{node_id}/ssh` (`project_token`):

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

`command` is server-computed for copy-paste and MUST be sanitized/escaped before
rendering (no shell metacharacters in `user`/`host`). `public_ip` comes from the
infra inventory (the controller loads it at startup); it is **not** present in
node telemetry (`ip_public` is denied by `governance.md` §4.2), so the controller
MUST be seeded with the inventory (`infra/gcp` emits `inventory.json`).

---

## 4. Manual intervention contract

Human actions available from the dashboard. Every action is `project_token`
scoped, writes a `guardrail_audit` record with `actor: human`
(`controller-api.md` §5.4), honors `Idempotency-Key`, and returns the standard
error envelope. The VM-level actions (`start | stop | resize | migrate | destroy`)
are already frozen in `controller-api.md` §4.2 and are not restated here.

### 4.1 Restart node

`POST /v1/projects/{project_id}/nodes/{node_id}/actions/reboot`

Body (optional): `{"delay_s": 0}`.

```json
{ "command_id": "01M4349RPY26W8Q453PZYK05CB", "type": "node.reboot",
  "state": "queued", "issued_by": "human" }
```

`202 Accepted`. Reboot on an offline node → `503 NODE_OFFLINE`.

### 4.2 Drain node

`POST /v1/projects/{project_id}/nodes/{node_id}/actions/drain`

Body:

```json
{ "reason": "disk_degradation", "evacuate_to": "01M4349RPVPP8X0W99R3TNZXW7" }
```

`reason` enum (frozen): `disk_degradation | disk_failure | maintenance | manual`.
`evacuate_to` is optional (omit → controller picks the target).

```json
{ "command_id": "01M4349RPY26W8Q453PZYK05CB", "type": "node.drain",
  "state": "queued", "issued_by": "human" }
```

`202 Accepted`.

### 4.3 Scale intent

The dashboard's "scale intent" control submits an intent and materializes the
validated plan. It reuses the frozen intent flow (`controller-api.md` §4.2):

1. `POST /v1/projects/{project_id}/intents` with `{prompt}` or `{spec}` →
   guardrail-validated `AllocationPlan` (`outcome: accepted`, per-class
   `clamped_fields` / `reject_reasons`).
2. User reviews the plan, then `POST /v1/projects/{project_id}/vms/apply` with
   `{plan_id}` → `202 {job_id, state, vm_ids, allocations[]}`
   (`controller-api.md` §4.2; `state` is terminal — `ready` or `failed` — and
   `vm_ids[]` are the deterministic LXD instance **names**, not ids).
3. The plan's outcome lands in the **Agents panel**, not only in a toast
   (card `t_6d63e8a2`): the client hashes each returned instance name with the §4.6 rule
   (`vm_id = base32(sha256("termini-vm-id:" + name))`) and draws it as a
   `provisioning` row under its node. The node's own telemetry is the only thing
   that can turn it `running`; a row no node reports within 2 minutes stops
   claiming to be provisioning, and is dropped after 10 (`dashboard/js/core/agents.js`
   `expectedRows` / `pruneExpected`). A response that carries only the flat
   `vm_ids` roll-up still yields rows — with the node shown as not reported,
   never invented.

Guardrail = the deterministic `guardrailClamp()` validator
(`controller-api.md` §5, `vm-allocation.schema.json`). The dashboard renders
clamps as warnings and rejections as blocking errors; a rejected plan is never
applied.

### 4.4 Approve / deny AI remediation

The AI operator issues remediation actions that pass `guardrailClamp()` and
`commandAllowlist()`. Each command carries an `approval` policy: `auto` (execute
immediately) or `required` (hold in `pending_approval` until a human decides).

**Scope (t_bfd2c2f7).** This approval state machine governs **operator/playbook-
initiated** remediations. The assistant endpoint (`POST
/v1/projects/{pid}/assistant/ask`, `controller-api.md` §4.10) may execute an
action *requested through the assistant* on guardrail admission, without entering
`pending_approval`; those actions are recorded with `initiated_by: assistant` and
appear on the decision surface like any other action. The assistant does not
repeal this gate for operator-initiated remediations.

**Frozen approval policy** (the `commandAllowlist()` set in
`controller/app/commands.py`; `agent.dispatch_task` and `vm.start`/`vm.stop`/
`vm.restart` are wired — the agent task API §4.9 and the per-agent actions §4.5 —
the rest are future/day-2):

| command type | approval |
|--------------|----------|
| `vm.resize`, `vm.migrate` (future) | `auto` |
| `vm.start`, `vm.stop`, `vm.restart` (agent actions, §4.5) | `auto` |
| `vm.destroy` (future) | `required` |
| `node.drain` | `required` |
| `node.reboot` | `required` |
| `agent.dispatch_task` (task API, controller-api §4.9) | `auto` |
| `agent.cancel_task` (future) | `auto` |

**Approval is keyed to the action, not to a fixed rule → action table.**

The table below is the *policy* the demo expects the decision step to produce — the
mapping the reference deterministic provider implements, and therefore the outcome a
verifier asserts. It is no longer a contract on the mechanism: the decision step may be
a declarative playbook or a model (`governance.md` §7.3), so what is frozen is the
**approval class of each action**, looked up by action rather than by rule.

| action | LXD action | approval | demo policy (reference provider) |
|--------|------------|----------|----------------------------------|
| restart heaviest workload | `instance.restart` | `auto` (executor) | `oom-guard-01` |
| stop heaviest workload | `instance.stop` | `auto` (executor) | `disk-fail-01`, `disk-degrade-01` |
| surface only, no autonomous action | (none) | — | `gpu-oos-01` |

Because approval is looked up by action, a decider that selects a different action for a
fault cannot thereby escalate its own privilege — the new action's class is whatever the
table above says it is. An action outside the allowlist is rejected before it ever
reaches this table (`governance.md` §7.3), so this table only ever governs allowlisted
actions. The dashboard surfaces `decision_source` (`deterministic` | `model` | `fallback`) on each
remediation so an operator can see which decider chose it.

**Approve** — `POST /v1/projects/{project_id}/remediations/{command_id}/approve`

Body: `{}` (optional `{"note": "..."}`).

```json
{ "command_id": "01M4349RPY26W8Q453PZYK05CB", "state": "approved",
  "approved_by": "human", "approved_at": "2026-10-04T07:49:30Z" }
```

`200 OK`. The command is released (`approved → queued → acked → running →
succeeded|failed`).

**Deny** — `POST /v1/projects/{project_id}/remediations/{command_id}/deny`

Body: `{"reason": "maintenance_window", "note": "..."}` (`reason` free-text,
optional).

```json
{ "command_id": "01M4349RPY26W8Q453PZYK05CB", "state": "denied",
  "denied_by": "human", "denied_at": "2026-10-04T07:49:30Z" }
```

`200 OK`. The command never executes; the AI operator records the denial and does
**not** re-issue the same command for the same trigger `sample_seq` within a
60 s cooldown.

**Guardrail constraints (frozen).**

- Only commands with `approval: required` and `state: pending_approval` may be
  approved/denied. Approving or denying an `auto` command, or a command already
  decided/executed, → `409 CONFLICT`.
- A `pending_approval` command auto-denies after **300 s** (`state: denied`,
  `reason: approval_timeout`), so no command sits pending forever.
- Approve/deny are idempotent (`Idempotency-Key`).
- The remediation state machine (shared with the log stream, §2.1) is:
  `pending_approval → approved|denied → queued → acked → running → succeeded|failed`;
  `timed_out` is a terminal state distinct from `denied`.

These constraints govern operator/playbook-initiated remediations. An
assistant-initiated command (`issued_by: assistant`, §4.10) never enters
`pending_approval` — it is queued on guardrail admission — so `approve`/`deny`
and the 300 s auto-deny do not apply to it.

### 4.5 Agent actions — start / stop / restart (card `t_44a2b87b`)

The Agents panel acts on one agent instance. Routes and semantics are frozen in
`controller-api.md` §4.11:

```
POST /v1/projects/{project_id}/agents/{vm_id}/actions/start
POST /v1/projects/{project_id}/agents/{vm_id}/actions/stop
POST /v1/projects/{project_id}/agents/{vm_id}/actions/restart
```

Each is a human-issued command in the **same envelope as §4.1/§4.2**
(`command_id`, `type`, `state`, `issued_by: "human"`, one operator-log line), with
one difference the client can rely on: the controller executes the LXD state
change and **confirms the resulting state** before answering, so the response's
`state` is `succeeded` or `failed` rather than a bare `queued`. `vm_id` accepts
the telemetry id the panel displays (node-telemetry §4.6) or the instance name
from apply.

`404 AGENT_NOT_FOUND` / `503 NODE_OFFLINE` / `503 NODE_UNREACHABLE` per §3.

### 4.6 Agents panel data source (card `t_44a2b87b`)

There is **no second inventory**. The panel renders one row per
`lxd_instances[]` entry of each node's latest telemetry Sample, read through
`GET /v1/projects/{project_id}/nodes/{node_id}` → `metrics_latest`
(`dashboard.md` §1.2, `node-telemetry.md` §4.6). Consequences the UI must not
paper over:

- `vm_id` is the controller-issued one-way id (26 Crockford chars), not the
  instance name; the instance name, IP and image alias are NOT on the wire, so
  the panel renders them as "not reported" and never guesses.
- the array is **omitted** when a node cannot read its own LXD: that renders as
  "inventory not reported", never as "no agents". An empty array is a real
  report of zero running instances.
- only RUNNING instances are reported, so a client-stopped agent disappears from
  telemetry. The panel keeps the row it stopped, labelled "stopped by you".

The task affordance uses `POST/GET .../agents/{vm_id}/tasks`
(`controller-api.md` §4.9) and renders the answer with the agent id and the
endpoint's `provenance` (backend/answered_by/egress) — a forwarded managed model
is labelled as forwarded, never as local GPU inference.

---

## 5. Integration note — owner & source mapping

| Contract surface | Dashboard consumes | Owning component | Existing source of truth |
|------------------|--------------------|------------------|--------------------------|
| Telemetry (status/health + series) | `GET /nodes`, `/nodes/{id}`, `/nodes/{id}/metrics`; WS `node.telemetry`, `node.health_changed` | **controller** (`app/server.py` `_list_nodes`/`_get_node`/`_get_metrics`; `app/store.py METRICS`) | `node-telemetry.md` (single source of truth); node daemon pushes C0/C1 telemetry, controller ingests under `governanceGate` |
| AI-operator log stream | SSE `GET /logs`; WS `log.line` | **controller** (new streaming layer — backend task `t_3468e5f0`) + **AI-operator loop** | command/ack channel (`controller-api.md` §6) + `guardrail_audit` + threshold rules (`node-telemetry.md` §5) |
| Terminal (WS + SSH string) | `WS /terminal/v1/nodes/{node_id}`; `GET /nodes/{id}/ssh` | **controller** (terminal bridge — `t_3468e5f0`) + **node** outbound channel / operator cert | infra inventory (`ssh.user`, `nodes[].public_ip`), D4/D6 (`infra-decisions.md`) |
| Manual intervention | `/nodes/{id}/actions/{reboot,drain}`; `/intents` + `/vms/apply`; `/remediations/{id}/{approve,deny}` | **controller** (`t_3468e5f0`) + **guardrail validator** + **command channel** | `vm-allocation.schema.json`, `commandAllowlist()` (`controller-api.md` §5/§6), `governance.md` §9 |

As-built vs. to-build delta (for the backend task): the controller core
(`app/server.py`) already serves `POST /nodes/register`, `GET /nodes`,
`/nodes/{id}`, `/nodes/{id}/metrics`, `POST /nodes/{id}/telemetry`, and
telemetry-recent endpoints. The following are **not yet implemented** and are the
backend task's scope: heartbeat/reconnect (fallback), the WS `/stream/v1` event
stream, SSE `/logs`, WS `/terminal/v1/nodes/{node_id}`, `GET /nodes/{id}/ssh`, the
`/nodes/{id}/actions/{reboot,drain}` endpoints, `/remediations/{id}/{approve,deny}`,
and the `approval` field on remediation commands.

---

## 6. Streaming cadence summary

| Channel | Cadence | Notes |
|---------|---------|-------|
| Node heartbeat | 5 s | 15 s degraded / 45 s offline |
| Node telemetry | 1 s demo / 15 s default | push |
| Dashboard REST poll | 5 s | nodes + metrics |
| WS telemetry snapshot | ≤ 1/s per node | throttled |
| Log stream | push on append | ring buffer 10k; `Last-Event-ID` resume |
| Approval timeout | 300 s | pending remediation auto-deny |
| Terminal idle timeout | 15 min | exit code −1 |

---

## 7. Reconciliation with `controller-api.md` (status)

The two blocking disagreements from the W1 review (terminal target, SSH access
model) are now resolved in this change set; `controller-api.md` and this document
agree. The remaining entries below are additive frontend endpoints frozen here and
now cross-referenced from `controller-api.md`.

- **R-D1 — terminal target (RESOLVED).** `controller-api.md` §7.3 and this document
  now agree: the dashboard terminal is **node-level**
  (`/terminal/v1/nodes/{node_id}`, host shell, controller-brokered over the outbound
  channel), and a **VM-level** agent shell lives at `/terminal/v1/vms/{vm_id}`
  (`lxc exec`). Recorded as architecture.md R14.
- **R-D2 — SSH access string and key (RESOLVED).** The documents now agree on the
  controller-held-SSH-CA model: the dashboard surfaces a one-click web terminal plus an
  `ssh` string whose `auth_model` is `ssh_ca` — the operator generates a keypair
  locally in the browser and the controller signs a short-lived (≤8 h), principal-bound
  certificate (`controller-api.md` §4.3), with `POST /v1/projects/{project_id}/ssh-key`
  and `/ssh-key/revoke` (the `ssh_certificates` ledger). The break-glass key is a
  one-time recovery path removed after registration (`node-bootstrap.md` §7). The
  `ssh.inject_key` command and the shared project keypair are removed everywhere.
- **R-D3 — log envelope (RESOLVED).** `controller-api.md` §7.2/§7.1 now reference
  this document's §2.1 as the canonical operator-log envelope (`log_id`, `severity`,
  `actor`, `remediation`, `Last-Event-ID` resume); the thin `{ts, level, text}` shape
  is retired.
- **R-D4 — new endpoints.** `GET /nodes/{id}/ssh`,
  `POST /nodes/{id}/actions/reboot`, `POST /nodes/{id}/actions/drain`,
  `POST /remediations/{id}/approve`, `POST /remediations/{id}/deny`, and the
  `approval` field on commands are new to this document (not present in
  `controller-api.md`).

No field in this document is a TBD. Any value not fixed above is the inherited
default from `controller-api.md` (error envelope §3, guardrail defaults §5.1,
token classes §2).
