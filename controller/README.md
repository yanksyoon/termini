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
| `TERMINI_OPERATOR_ENABLED` | `1` | run the in-process operator loop thread |
| `TERMINI_OPERATOR_POLL_INTERVAL_S` | `1.0` | detect/act poll interval (tune 1–2 s for the demo) |
| `TERMINI_OPERATOR_CADENCE_REPORT_S` | `30.0` | log a poll-cadence summary every N s |
| `TERMINI_OPERATOR_DRY_RUN` | `0` | log intended LXD command, send nothing |
| `TERMINI_OPERATOR_COOLDOWN_S` | `60` | suppress re-fire of the same rule on a node |
| `TERMINI_OPERATOR_MODEL_CALLS_PER_HOUR` | `120` | global spend guard: model-call attempts per sliding hour; `0` disables (operator-decision.md §10.3) |
| `TERMINI_OPERATOR_EVIDENCE_SAMPLES` | `6` | telemetry samples in the model's evidence window, `1..20` (operator-decision.md §10.4) |
| `TERMINI_OPERATOR_WINDOW` | `60` | telemetry samples inspected per node |
| `TERMINI_PLAYBOOKS_DIR` | `playbooks/` | remediation playbook directory |
| `TERMINI_OOM_RAM_PCT` | `95.0` | oom-guard-01: mem% enter threshold |
| `TERMINI_OOM_CLEAR_PCT` | `90.0` | oom-guard-01: mem% clear (re-arm) threshold |
| `TERMINI_OOM_CONSECUTIVE` | `2` | oom-guard-01: consecutive breaching samples |
| `TERMINI_OOM_CLEAR_CONSECUTIVE` | `3` | oom-guard-01: consecutive healthy samples to re-arm |
| `TERMINI_DISK_FAIL_CONSECUTIVE` | `1` | disk-fail-01: any mount_ro fires immediately |
| `TERMINI_DISK_FAIL_CLEAR_CONSECUTIVE` | `3` | disk-fail-01: consecutive rw samples to re-arm |
| `TERMINI_DISK_FREE_PCT` | `5.0` | disk rule: free% floor |
| `TERMINI_DISK_FREE_CLEAR_PCT` | `15.0` | disk rule: free% clear threshold |
| `TERMINI_DISK_FREE_CONSECUTIVE` | `3` | disk rule: consecutive samples at floor |
| `TERMINI_DISK_WRITE_LATENCY_MS` | `50.0` | disk rule: write-latency threshold |
| `TERMINI_DISK_WRITE_LATENCY_CLEAR_MS` | `30.0` | disk rule: write-latency clear threshold |
| `TERMINI_DISK_LATENCY_CONSECUTIVE` | `5` | disk rule: consecutive samples |
| `TERMINI_DISK_FREE_SLOPE_BPS` | `-5000000.0` | disk rule: steep-negative free slope |
| `TERMINI_DISK_CLEAR_CONSECUTIVE` | `3` | disk rule: consecutive healthy samples to re-arm |
| `TERMINI_LATENCY_MS` | `250.0` | latency rule: net latency threshold |
| `TERMINI_LATENCY_CLEAR_MS` | `150.0` | latency rule: net latency clear threshold |
| `TERMINI_LATENCY_CONSECUTIVE` | `3` | latency rule: consecutive samples |
| `TERMINI_LATENCY_CLEAR_CONSECUTIVE` | `3` | latency rule: consecutive healthy samples |
| `TERMINI_LXD_BASE_URL` | `http://127.0.0.1:8443` | LXD REST endpoint (executor) |
| `TERMINI_LXD_INSTANCE_NAMES` | `{}` | JSON `{vm_id: lxd_name}` mapping |
| `TERMINI_LXD_DEFAULT_INSTANCE_NAME` | `agent` | fallback instance name |
| `TERMINI_LXD_CLIENT_CERT` / `TERMINI_LXD_CLIENT_KEY` | unset | explicit mTLS cert/key for a real LXD (else minted — see below) |
| `TERMINI_LXD_CLIENT_DIR` | `<db_dir>/lxd-client` | where the controller mints/reuses its LXD client cert + key |
| `TERMINI_LXD_AUTO_PROVISION` | `true` | mint the client cert at startup when absent (reused thereafter) |
| `TERMINI_LXD_TRUST_NAME` | `termini-controller` | LXD trust-store entry name / cert CN |
| `TERMINI_LXD_TRUST_TOKEN` / `TERMINI_LXD_TRUST_PASSWORD` | (none) | secret to install the cert into a node's trust store |
| `TERMINI_LXD_TRUST_NODES` | (none) | comma-separated node LXD base URLs to auto-trust at startup |
| `TERMINI_LXD_SERVER_CA` | (none) | pin the node's server cert (else server verification is skipped) |
| `TERMINI_LXD_TIMEOUT_S` | `10.0` | per-request timeout |
| `TERMINI_LXD_RETRY_MAX` / `TERMINI_LXD_RETRY_BACKOFF_S` | `3` / `0.5` | retry with exponential backoff |
| `TERMINI_INVENTORY_PATH` | (auto) | infra inventory.json (public_ip / ssh user+port) |
| `TERMINI_SSH_CA_KEY_PATH` | (none) | controller-held SSH CA private key (Ed25519, D4) |
| `TERMINI_SSH_CA_PUBLIC_KEY` | (derived) | CA public half for the SSH-string fingerprint |
| `TERMINI_SSH_OPERATOR_KEY_PATH` | `~/.termini/operator_key` | controller-local operator identity key for the terminal bridge |
| `TERMINI_SSH_KEY_COMMENT` | `termini-operator-cert` | key comment in the SSH string |
| `TERMINI_SSH_CERT_PRINCIPAL` | `root` | principal bound into issued certificates |
| `TERMINI_SSH_CERT_DEFAULT_VALIDITY_S` | `28800` | default certificate TTL (8 h) |
| `TERMINI_SSH_CERT_MAX_VALIDITY_S` | `86400` | certificate TTL ceiling (24 h, never exceeded) |
| `TERMINI_SSH_CLIENT_KEY_PATH` | `~/.ssh/termini_operator` | operator key path rendered in the SSH string |
| `TERMINI_SSH_USER` / `TERMINI_SSH_PORT` | `ubuntu` / `22` | fallback when no inventory is loaded |
| `TERMINI_LOG_RING_SIZE` | `10000` | operator-log ring buffer capacity |
| `TERMINI_APPROVAL_TIMEOUT_S` | `300` | pending_approval auto-deny timeout |
| `TERMINI_TERMINAL_IDLE_TIMEOUT_S` | `900` | terminal idle close (15 min) |
| `TERMINI_TERMINAL_BACKEND` | `ssh` | `ssh` (operator cert proxy) \| `local` (`bash -i`, tests) |
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
| POST | `/v1/projects/{pid}/ssh-key` | project token | `200` signed short-lived cert (D4 CA) |
| POST | `/v1/projects/{pid}/ssh-key/revoke` | project token | `200` revoke an issued cert |
| POST | `/v1/projects/{pid}/nodes/{nid}/actions/reboot` | project token | `202 {command_id,type,state}` |
| POST | `/v1/projects/{pid}/nodes/{nid}/actions/drain` | project token | `202 {command_id,type,state}` |
| POST | `/v1/projects/{pid}/intents` | project token | `200` plan / `422` guardrail-rejected |
| POST | `/v1/projects/{pid}/vms/apply` | project token | `202 {job_id,state:ready\|failed,vm_ids}` (materialises) |
| POST | `/v1/projects/{pid}/vms/release` | project token | `200 {status:released\|already_released,released}` (idempotent) / `404` |
| POST | `/v1/projects/{pid}/vms/reconcile` | project token | `200` self-heal: release commits whose VMs are gone |
| POST | `/v1/projects/{pid}/nodes/{nid}/decommission` | project token | `200 {status:decommissioned\|already_decommissioned,released}` / `404` |
| POST | `/v1/projects/{pid}/remediations/{cid}/approve` | project token | `200 {command_id,state:approved}` |
| POST | `/v1/projects/{pid}/remediations/{cid}/deny` | project token | `200 {command_id,state:denied}` |
| POST | `/v1/nodes/{nid}/telemetry` | node token | `202 {accepted, rejected, backpressure}` |
| GET | `/v1/nodes/{nid}/telemetry/recent` | node token | `200` recent samples |
| GET | `/v1/telemetry/recent?node=&limit=` | project token | `200` recent samples (dashboard/operator) |
| GET | `/v1/operator/events?node=&limit=` | project token | `200` health events (AI operator) |
| GET | `/v1/operator/decisions?limit=` | project token | `200` remediation decision log |
| GET | `/v1/operator/audits?node=&limit=` | project token | `200` remediation audit trail |

Streaming (WebSocket / SSE — token is a `?token=` query param because browsers
cannot set headers on `EventSource` / `new WebSocket`):

| Surface | Endpoint | Notes |
|---------|----------|-------|
| AI-operator logs (SSE) | `GET /v1/projects/{pid}/logs?token=…` | `text/event-stream`; `Last-Event-ID`/`?since=` backfill |
| Unified event stream (WS) | `GET /stream/v1?token=…&since=…` | `node.telemetry`, `node.health_changed`, `log.line`, `remediation.*` |
| Terminal (WS) | `GET /terminal/v1/nodes/{node_id}?token=…` | xterm.js framing; one session per node |

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
[`docs/contracts/node-telemetry.md`](../docs/contracts/node-telemetry.md) and defined once in
`app/fields.py` (single source of truth). Ingest validates, in order: C2 scan →
C1 transform check → redactor version → envelope/sample schema. Malformed
batches return `422` and store nothing; unknown fields are stripped
(deny-by-default). Error envelope follows `controller-api.md` §3.

## AI-operator remediation loop

Closes the control loop: **detect** a failure from telemetry → **choose** a fix
from a declarative playbook → **execute** it against the node's LXD REST API →
**audit** (event → action → result).

Two run modes, sharing the same detector/chooser/executor (`app/operator/`):

1. **In-process** (default) — a daemon thread started by `app.main` polls the
   telemetry store every `TERMINI_OPERATOR_POLL_INTERVAL_S`, writes health
   events, decisions, and audits to SQLite, and issues LXD REST calls to
   `TERMINI_LXD_BASE_URL`. Queryable via the `/v1/operator/*` endpoints above.
2. **Standalone** — `python3 -m app.operator.cli` (from this directory) polls
   the controller's `/v1/telemetry/recent` over HTTP (the endpoint named in the
   task), then executes against `--lxd-url`, writing an audit JSONL. This is
   what the chaos harness drives.

```bash
# drive the production loop with the chaos harness (from repo root):
make chaos SCENARIO=oom OPERATOR_CMD="python3 controller/app/operator/cli.py \
  --controller-url {controller_url} --lxd-url {lxd_url} \
  --node {node_id} --project-token {project_token} \
  --thresholds '{thresholds_json}' --duration 40 --audit-out /tmp/op-audit.jsonl"
```

### Playbooks (declarative YAML, thresholds in config — not code)

| File | Health type | Rule | Action (LXD REST) |
|------|-------------|------|-------------------|
| `playbooks/oom.yaml` | `oom` | `oom-guard-01` | `instance.restart` the heaviest workload (`PUT /1.0/instances/{name}/state`) |
| `playbooks/disk-fail.yaml` | `disk_fail` | `disk-fail-01` | `instance.stop` to drain the node whose disk remounted read-only (EXT4 I/O error) |
| `playbooks/disk-degrade.yaml` | `disk_degrade` | `disk-degrade-01` | `instance.stop` to drain the heaviest workload off the degraded node |
| `playbooks/latency.yaml` | `latency` | `latency-01` | `instance.restart` the affected workload |

Playbooks are parsed by a bundled stdlib-only YAML-subset parser
(`app/operator/yaml.py`) — no PyYAML dependency. Every threshold is an env var
(see table above); the frozen demo defaults match `chaos/chaos.json`.

### Persisted triggers, hysteresis, and detection latency

Detection is **stateful and persisted**, not an in-memory threshold check:

- **Thresholds are durable.** On first poll the loop seeds `trigger_config`
  (SQLite, migration `0003`) from the env vars above, then reads them back on
  every poll — a value written to that table survives a controller restart and
  takes effect on the next poll with no re-deploy.
- **Hysteresis + debounce.** Each rule has an *enter* threshold (fire) and a
  distinct *clear* threshold (re-arm). A trigger fires only when the metric is
  currently breached for `*_consecutive` samples, then **latches** until the
  metric returns below the clear threshold for `*_clear_consecutive` samples.
  A single noisy sample never fires; a recovering metric never re-fires inside
  the band; and a genuine re-occurrence after recovery is a new episode (new
  command).
- **Latch persists across restarts.** `trigger_state` holds one row per
  (node, rule) — `armed`/`fired`, `last_fired_at`, `fired_count`. On restart
  the loop reloads it, so an already-remediated fault is not re-fired from a
  cold start.
- **Detection latency is measured and logged.** Every fire records
  `breach_started_at` (the first breaching sample's timestamp) and `fired_at`
  (ms precision), and every remediation logs `detection_latency_s`
  (`breach → fire`) and `remediation_latency_s` (`chooser start → remediation
  completed`, including chooser/selection time). The
  standalone CLI writes both to its `--audit-out` JSONL.

The same engine runs in-process (`app/operator/loop.py`, SQLite-backed) and
standalone (`app/operator/cli.py`, `--state-file` JSON-backed), so the chaos
harness can exercise persisted triggers from a cold operator process.

### Guarantees

- **Audit** — every remediation writes a `remediation_audits` row (node, event,
  action, result, LXD method/path/body); the decision log (`remediation_decisions`)
  links each event to its chosen action.
- **Idempotent** — one health event → at most one decision (schema-enforced);
  a repeated (node, rule, action, target) remediation dedupes by
  `idempotency_key`; LXD `delete` of an already-absent instance is a no-op.
- **Dry-run** — `TERMINI_OPERATOR_DRY_RUN=1` (or `--dry-run`) logs the intended
  method/path/body and sends nothing; the audit records `result: dry_run`.
- **Retry with backoff** — transient 5xx / connection errors retry
  (`TERMINI_LXD_RETRY_MAX`, exponential backoff); 4xx is terminal.

## Verify

```bash
make verify           # core acceptance: register→role, telemetry, 422-on-malformed
make verify-operator  # operator loop vs the chaos harness (oom/disk-fail/healthy)
python3 -m unittest discover -s tests   # unit + integration (core + dashboard surfaces)
```

`scripts/acceptance.py` exercises the core acceptance criteria (register → role,
two-node telemetry at 1 s cadence, timestamp preservation, 422-on-malformed with
no corruption, schema-doc↔code agreement). `scripts/acceptance_operator.py`
drives the production operator loop against the chaos harness and asserts the
remediation acceptance criteria; dry-run and idempotency are additionally
exercised by the unit suite (`tests/test_operator_executor.py`,
`tests/test_operator_loop.py`). `tests/test_dashboard.py` covers the W5 dashboard
surfaces (health/status derivation, SSH string, restart/drain, approve/deny,
intent guardrail, SSE + WS log stream, and the WS terminal).

## LXD mutual-TLS trust (client certificate)

The operator drives each node's LXD over HTTPS. LXD authenticates the caller by
the **TLS client certificate** it presents against the node's trust store; an
untrusted client gets `GET /1.0` → 200 but `GET /1.0/instances` → **403
Forbidden**. `app/lxdtrust.py` closes that gap end to end:

1. **Identity** — at startup the controller mints a self-signed EC client
   certificate + key once (`<db_dir>/lxd-client/`, `0600` key) and **reuses it on
   every restart** so its LXD identity never rotates. Generation shells out to
   `openssl` (Python's stdlib cannot sign X.509; the controller stays
   third-party-free). `TERMINI_LXD_CLIENT_CERT`/`_KEY` override with a
   pre-provisioned pair. The resolved paths are handed to the operator loop's
   `LxdExecutor`, which presents them on every request.
2. **Trust** — the certificate is installed into a node's trust store over the
   network with `POST /1.0/certificates` (client cert in the TLS handshake, the
   secret in the `password` field). The secret is either an LXD
   **certificate-add trust token** (`termini-bootstrap mint-trust-token` on the
   node, contract `node-bootstrap.md` §5) or the node's `core.trust_password`.
   It is **idempotent**: a pre-probe skips a node that already trusts us, and an
   LXD "already present"/409 reply folds to success.
3. **Verify** — an authenticated `GET /1.0/instances` returning **200** (trusted)
   vs **403** (not trusted) is the observable signal, surfaced by the CLI.

```bash
# mint/reuse the controller identity and print its fingerprint
python3 -m app.lxdtrust cert
# add it to a node's trust store and prove it (rc 0 trusted / 3 untrusted / 2 unreachable)
python3 -m app.lxdtrust trust  --node https://10.10.0.11:8443 --token "$TOKEN"
python3 -m app.lxdtrust verify --node https://10.10.0.11:8443
```

Or automate it: set `TERMINI_LXD_TRUST_NODES=https://10.10.0.11:8443` plus
`TERMINI_LXD_TRUST_TOKEN` and the controller installs trust at startup.

> **TLS server verification:** LXD serves a self-signed server certificate, so by
> default the executor presents its client cert without verifying the server
> (matching the demo's `curl -k`) and logs a warning. Set
> `TERMINI_LXD_SERVER_CA` to a node PEM to pin it.

## Not in scope (other W1 cards)

Node-side command execution (the node command channel / ack delivery that turns a
queued `node.reboot`/`node.drain` into an executed action), the AI-operator
remediation loop (threshold-rule → command issuance), and the LXD *cluster* join
tokens / controller-issued node certs of `node-bootstrap.md` §5. `/vms/apply` **is**
wired: it materialises the plan by driving each allocation's resolved node's LXD
REST API at that node's own registered endpoint
(`controller/app/operator/target.py`), returning real instance ids and a terminal
state. The controller's own LXD **client** certificate + node-trust installation
*are* implemented — see "LXD mutual-TLS trust" above. The controller queues commands
with the frozen guardrails and exposes the streaming/terminal surfaces those
components consume.
