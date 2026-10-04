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

## Endpoints (all under `/v1`)

| Method | Path | Auth | Returns |
|--------|------|------|---------|
| POST | `/v1/nodes/register` | bootstrap token | `201` node + role + node_token + config |
| GET | `/v1/projects/{pid}/nodes` | project token | `200` node list (role + last-seen + capacity) |
| GET | `/v1/projects/{pid}/nodes/{nid}` | project token | `200` node + `metrics_latest` |
| GET | `/v1/projects/{pid}/nodes/{nid}/metrics` | project token | `200` time-series (`?start&end&resolution&metrics`) |
| POST | `/v1/nodes/{nid}/telemetry` | node token | `202 {accepted, rejected, backpressure}` |
| GET | `/v1/nodes/{nid}/telemetry/recent` | node token | `200` recent samples |
| GET | `/v1/telemetry/recent?node=&limit=` | project token | `200` recent samples (dashboard/operator) |

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
make verify        # boots the service + runs the full acceptance suite
```

`scripts/acceptance.py` exercises every acceptance criterion (register → role,
two-node telemetry at 1 s cadence, timestamp preservation, 422-on-malformed with
no corruption, schema-doc↔code agreement).

## Not in scope (other W1 cards)

heartbeat, reconnect, command channel/acks, intents + guardrail validator,
VMs, ssh-key, WebSocket event stream, terminal, full mTLS PKI / LXD join
tokens. The route surface is stubbed to `404` for these.
