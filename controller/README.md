# Termini Controller — service core

Central control plane for Termini ("your hardware, your data, our operations").
This service implements the controller core frozen in
[`docs/contracts/controller-api.md`](../docs/contracts/controller-api.md):

- **Node registration + deterministic role allocation** (`compute` / `inference`)
- **Telemetry ingest** with strict, fail-closed schema enforcement
- **Time-series query** for the AI-operator loop and dashboard
- **Persistence + migrations** (SQLite), fully externalized config

> **Language decision:** the task allows "Python/FastAPI or Go — match repo
> conventions". This repo had no code conventions, and this build environment
> cannot install third-party packages (headless security scanner blocks `pip`).
> The controller is therefore **Python stdlib-only** (`http.server` +
> `sqlite3` + a hand-rolled JSON schema validator): zero runtime dependencies,
> runs anywhere Python ≥3.10 is present, and keeps the whole ingest path
> auditable. FastAPI/Pydantic can be layered on later without changing the wire
> contract.

## Run

```bash
# required — single-use bootstrap token(s); comma-separated
export TERMINI_BOOTSTRAP_TOKEN="$(openssl rand -base64 32)"

make run            # or: python3 -m app.main   (from this directory)
```

First startup prints a generated project token once (persisted in the DB);
set `TERMINI_PROJECT_TOKEN` to pin it instead. All config is via env — nothing
is hardcoded.

| Env var | Default | Meaning |
|---------|---------|---------|
| `TERMINI_BOOTSTRAP_TOKEN` | (required) | single-use enrollment token(s) |
| `TERMINI_PROJECT_TOKEN` | generated | frontend/dashboard token |
| `TERMINI_PROJECT_ID` | generated | demo project ULID |
| `TERMINI_HOST` / `TERMINI_PORT` | `0.0.0.0` / `8080` | listen address |
| `TERMINI_DB_PATH` | `data/controller.db` | SQLite path |
| `TERMINI_MAX_INFERENCE_NODES` | `1` | role-allocation cap |
| `TERMINI_GPU_VRAM_MIN_BYTES` | `12884901888` (12 GiB) | inference threshold |
| `TERMINI_MIN_REDACTOR_VERSION` | `3` | redactor-config minimum |
| `TERMINI_TELEMETRY_INTERVAL_MS` | `1000` | node cadence default |
| `TERMINI_INVENTORY_PATH` | (auto) | infra inventory.json (public_ip / ssh user+port) |
| `TERMINI_SSH_KEY_PATH` | (none) | break-glass private key for the terminal bridge |
| `TERMINI_SSH_PUBLIC_KEY` | (derived) | public half for the SSH-string fingerprint |
| `TERMINI_SSH_KEY_COMMENT` | `termini-breakglass` | key comment in the SSH string |
| `TERMINI_SSH_CLIENT_KEY_PATH` | `~/.ssh/termini_breakglass` | operator key path rendered in the SSH string |
| `TERMINI_SSH_USER` / `TERMINI_SSH_PORT` | `ubuntu` / `22` | fallback when no inventory is loaded |
| `TERMINI_LOG_RING_SIZE` | `10000` | operator-log ring buffer capacity |
| `TERMINI_APPROVAL_TIMEOUT_S` | `300` | pending_approval auto-deny timeout |
| `TERMINI_TERMINAL_IDLE_TIMEOUT_S` | `900` | terminal idle close (15 min) |
| `TERMINI_TERMINAL_BACKEND` | `ssh` | `ssh` (break-glass) \| `local` (`bash -i`, tests) |
| `TERMINI_DASHBOARD_ORIGINS` | (empty = any) | comma-separated Origin allowlist for the dashboard |

## Endpoints

REST (all under `/v1`):

| Method | Path | Auth | Returns |
|--------|------|------|---------|
| POST | `/v1/nodes/register` | bootstrap token | `201` node + role + node_token + config |
| POST | `/v1/nodes/{nid}/heartbeat` | node token | `204` (or `200 {pending_commands}`) |
| GET | `/v1/projects/{pid}/nodes` | project token | `200` node list (status/health/capacity) |
| GET | `/v1/projects/{pid}/nodes/{nid}` | project token | `200` node + `metrics_latest` |
| GET | `/v1/projects/{pid}/nodes/{nid}/metrics` | project token | `200` time-series (`?start&end&resolution&metrics`) |
| GET | `/v1/projects/{pid}/nodes/{nid}/ssh` | project token | `200` SSH string + fingerprint |
| POST | `/v1/projects/{pid}/nodes/{nid}/actions/reboot` | project token | `202 {command_id,type,state}` |
| POST | `/v1/projects/{pid}/nodes/{nid}/actions/drain` | project token | `202 {command_id,type,state}` |
| POST | `/v1/projects/{pid}/intents` | project token | `200` plan / `422` guardrail-rejected |
| POST | `/v1/projects/{pid}/vms/apply` | project token | `202 {job_id,state,vm_ids}` |
| POST | `/v1/projects/{pid}/remediations/{cid}/approve` | project token | `200 {command_id,state:approved}` |
| POST | `/v1/projects/{pid}/remediations/{cid}/deny` | project token | `200 {command_id,state:denied}` |
| POST | `/v1/nodes/{nid}/telemetry` | node token | `202 {accepted, rejected, backpressure}` |
| GET | `/v1/nodes/{nid}/telemetry/recent` | node token | `200` recent samples |
| GET | `/v1/telemetry/recent?node=&limit=` | project token | `200` recent samples (dashboard/operator) |

Streaming (WebSocket / SSE — token is a `?token=` query param because browsers
cannot set headers on `EventSource` / `new WebSocket`):

| Surface | Endpoint | Notes |
|---------|----------|-------|
| AI-operator logs (SSE) | `GET /v1/projects/{pid}/logs?token=…` | `text/event-stream`; `Last-Event-ID`/`?since=` backfill |
| Unified event stream (WS) | `GET /stream/v1?token=…&since=…` | `node.telemetry`, `node.health_changed`, `log.line`, `remediation.*` |
| Terminal (WS) | `GET /terminal/v1/{node_id}?token=…` | xterm.js framing; one session per node |

Roles are `compute` / `inference` (the frozen contract's vocabulary; the task
body's "agent-host / inference-host" maps to these). A node with ≥1 GPU whose
`vram_bytes ≥ 12 GiB` gets `inference`, capped to `TERMINI_MAX_INFERENCE_NODES`
(first qualifying GPU node wins — deterministic). The same rule is enforced
identically on re-registration (dedup by `machine_id`).

## Registration

Two shapes are accepted, normalized to one internal model:

1. **Canonical** (`node-bootstrap.md` §5.2) — `{schema_version, machine{...},
   node_pubkey_csr_pem, host_ssh_keys, agent}`. This is what the node bootstrap
   daemon sends.
2. **Legacy** (earlier `controller-api.md` draft) — `{registration_id, hardware,
   os, lxd, installer_version}`.

The controller returns its owned fields (`node_id`, `project_id`, `role`,
`node_token`, `config`, `telemetry`, `controller_time`). `identity`
(client cert) and `cluster` (LXD join token) are returned as `null` — they are
issued by the infra/bootstrap task, not this core.

## Telemetry schema

The enforced schema is documented in
[`docs/telemetry-schema.md`](../docs/telemetry-schema.md) and defined once in
`app/fields.py` (single source of truth). Ingest validates, in order: C2 scan →
C1 transform check → redactor version → envelope/sample schema. Malformed
batches return `422` and store nothing; unknown fields are stripped
(deny-by-default). Error envelope follows `controller-api.md` §3.

## Verify

```bash
make verify                 # boots the service + runs the core acceptance suite
python3 -m unittest discover -s tests   # unit + integration (incl. dashboard surfaces)
```

`scripts/acceptance.py` exercises the controller-core criteria (register → role,
two-node telemetry at 1 s cadence, timestamp preservation, 422-on-malformed with
no corruption, schema-doc↔code agreement). `tests/test_dashboard.py` covers the
W5 dashboard surfaces (health/status derivation, SSH string, restart/drain,
approve/deny, intent guardrail, SSE + WS log stream, and the WS terminal).

## Not in scope (other W1 cards)

Node-side command execution (the node command channel / ack delivery that turns a
queued `node.reboot`/`node.drain` into an executed action), the AI-operator
remediation loop (threshold-rule → command issuance), LXD VM fan-out behind
`/vms/apply`, and full mTLS PKI / LXD join tokens. The controller queues commands
with the frozen guardrails and exposes the streaming/terminal surfaces those
components consume.
