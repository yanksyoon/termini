# Termini chaos / fault-injection harness

Reproducible failure injection for the Termini control plane. This harness
proves that the operator loop does something *real*: it simulates nodes,
posts a deterministic telemetry series that crosses the frozen health
thresholds, and records the LXD REST command the loop issues in response.

## What is real vs. simulated

`chaos/node_sim.py` is a **simulator, not the on-node `termini-bootstrap`
daemon**. That daemon — and the node-side redactor the telemetry envelope
alludes to — is *specified* in
[`docs/contracts/node-bootstrap.md`](../docs/contracts/node-bootstrap.md) but
**not implemented**: the repo contains no `termini-bootstrap` binary, systemd
unit, or node redactor. Every field the simulator emits is synthetic, including
the `_redaction` envelope and the `termini-bootstrap` service entry (both are
required by the frozen telemetry contract, which names the planned daemon).

Content-based (C2) governance is therefore enforced **controller-side**: a
non-conforming telemetry batch is rejected whole with `422 GOVERNANCE_REJECT`
(`controller/app/governance.py scan_c2` → `controller/app/server.py
_ingest_telemetry`). There is no "redact on the node before the socket opens"
step in this build.

```
        telemetry (POST /v1/nodes/{id}/telemetry, 1 s cadence)
  node_sim ──────────────────────────────────────────────▶ controller
   (impersonated node)                                    (temp DB)
                                                                 ▲
                                     GET /v1/telemetry/recent     │
                                    ┌────────────────────────────┘
                                    │
                              operator loop  ── LXD REST (POST/PATCH/DELETE /1.0/...) ──▶ lxd_stub
                            (reference probe by                                       (records every call)
                             default; or the production loop)
```

## Quickstart

```bash
# one scenario, end to end (boots controller + stub + node + operator)
make chaos SCENARIO=oom

# the full acceptance suite (oom, disk-degrade, latency, healthy)
make chaos-verify

# fast unit tests (no controller)
make chaos-test
```

Everything is stdlib-only (`python3`, no third-party packages) and uses
ephemeral ports + a temp DB, so it never touches a controller you already have
running.

## Scenarios

| SCENARIO       | What it injects                                                     | Frozen rule that must fire | Expected remediation |
|----------------|---------------------------------------------------------------------|----------------------------|----------------------|
| `oom`          | RAM utilisation ramps 62%→97% over 3 s, then holds                  | `oom-guard-01` (≥95% for ≥2 samples) | `vm.restart` LXD call |
| `disk-degrade` | free disk 40%→3% and `write_latency_ms` 3→80 over 6 s, then holds   | `disk-degrade-01` (write latency ≥50 ms for ≥5, or free ≤5%) | `disk.drain` LXD call |
| `latency`      | `net[].latency_ms` held at 600 ms                                   | `latency-01` (≥250 ms for ≥3 samples) | `net.probe` LXD call |
| `healthy`      | steady baseline (62% RAM, 78% free, 3 ms write, 0.8 ms net)         | **none** (false-positive guard) | **no command** |

Rules are the frozen demo thresholds from
[`docs/contracts/node-telemetry.md`](../docs/contracts/node-telemetry.md) §5;
the emitted series is validated by the controller against
[`docs/telemetry-schema.md`](../docs/telemetry-schema.md) at ingest (a rejected
sample would fail the `telemetry_series_emitted` check immediately).

### Expected timeline (1 s cadence, defaults)

| t (s) | event |
|-------|-------|
| 0.0   | controller, LXD stub boot; ready by ~0.3 |
| 0.5   | node registers, receives a role + node_token |
| 0.5→3 | fault ramps into the threshold band (mode-dependent) |
| ~2–5  | threshold rule fires (e.g. OOM: first ≥95% sample at t=3, two consecutive at t=4) |
| ≤ +1 s after firing | operator's next poll detects the event and issues the LXD command |
| 8.5   | telemetry stream ends |
| 12.5  | grace window ends; verdict printed |

Exact per-run evidence is printed as a `+N.Ns` timeline, e.g.:

```
  PASS  telemetry_series_emitted  {'count': 8}
  PASS  injection_crosses_rule  {'expected_rule': 'oom-guard-01'}
         oom-guard-01: fired=True run=5 needed=2
  PASS  remediation_command_issued  {'enforced': True, 'count': 1, 'expected': True}
  VERDICT: PASS
```

## Layout

| Path | Purpose |
|------|---------|
| `chaos/chaos.json` | scenario parameters + thresholds + endpoints (single source of truth) |
| `chaos/config.py`  | config loader + environment overrides |
| `chaos/common.py`  | ULID, HTTP, RFC3339, and the threshold evaluator |
| `chaos/node_sim.py` | simulated node: registers + streams telemetry per mode |
| `chaos/lxd_stub.py` | stub LXD REST endpoint that records every command |
| `chaos/reference_operator.py` | reference threshold probe (stand-in for the production loop) |
| `chaos/run.py` | one-scenario orchestrator (`make chaos SCENARIO=…`) |
| `chaos/verify.py` | acceptance suite over all scenarios (`make chaos-verify`) |
| `chaos/tests/` | fast unit tests |

## Configuration

All scenario parameters live in [`chaos/chaos.json`](chaos.json) and can be
overridden from the environment (useful for CI or a live demo):

| Env var | Meaning |
|---------|---------|
| `TERMINI_CHAOS_CONFIG` | alternate config JSON path |
| `TERMINI_CHAOS_CONTROLLER_URL` | controller base URL (default: ephemeral local) |
| `TERMINI_CHAOS_BOOTSTRAP_TOKEN` / `TERMINI_CHAOS_PROJECT_TOKEN` | credentials |
| `TERMINI_CHAOS_LXD_URL` | point at a **real** LXD REST endpoint (implies real mode) |
| `TERMINI_CHAOS_LXD_MODE` | `stub` (default) or `real` |
| `TERMINI_CHAOS_CADENCE_MS` | telemetry cadence (default 1000) |
| `TERMINI_CHAOS_OPERATOR_CMD` | run the production operator loop (see below) |
| `TERMINI_CHAOS_DRY_RUN` | operator logs the intended command, sends nothing |
| `TERMINI_CHAOS_TH_<NAME>` | override a threshold, e.g. `TERMINI_CHAOS_TH_OOM_RAM_PCT=90` |
| `TERMINI_CHAOS_DURATION_<NAME>` | override a scenario duration, e.g. `TERMINI_CHAOS_DURATION_OOM=5` |

Thresholds in-tree (frozen defaults):

```json
{"oom_ram_pct": 95.0, "oom_consecutive": 2,
 "disk_free_pct": 5.0, "disk_free_consecutive": 3,
 "disk_write_latency_ms": 50.0, "disk_latency_consecutive": 5,
 "latency_ms": 250.0, "latency_consecutive": 3}
```

## Driving the production operator loop

The bundled `reference_operator.py` is a **stand-in**, not the production
remediation loop. It implements exactly the frozen rules so the harness can prove
the injection→action chain on its own. To validate the real loop (task
`t_375d9df4`) with the identical evidence, point the harness at it:

```bash
make chaos SCENARIO=oom OPERATOR_CMD="python3 -m <loop module> \
  --controller-url {controller_url} --lxd-url {lxd_url} \
  --node {node_id} --project-token {project_token}"
```

Placeholders substituted by the harness: `{controller_url}`, `{lxd_url}`,
`{node_id}`, `{project_token}`, `{thresholds_json}`. With a stub LXD the harness
still asserts that a real command reached the endpoint; with a real LXD
(`TERMINI_CHAOS_LXD_URL=…`) the command check is reported as *not enforced*
(the harness cannot see a live cluster).

## Real-LXD demo run

```bash
# stub is the default; for a live cluster point at it (needs a trusted cert/token)
TERMINI_CHAOS_LXD_URL="https://10.0.0.5:8443" make chaos SCENARIO=oom
```

## Note: latency vs. p95

The frozen telemetry schema (`docs/telemetry-schema.md`) has **no `p95` field** —
the only latency signal it carries is `net[].latency_ms` (round-trip to the
controller). The `latency` scenario therefore drives that field, and the
`latency-01` rule above is the harness's own threshold on it. If the operator
loop needs application p95 latency, a new C0 field must be added to
`governance.md` §4.1 and the telemetry schema first (per governance §10.1).
