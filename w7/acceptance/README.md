# W7 acceptance harness — W1–W6 evidence matrix

Single source of truth that turns each workstream's stated acceptance criteria
(W1–W6) into an independently runnable test with captured evidence. The
machine-readable matrix is [`matrix.json`](matrix.json); this README is the
human view.

## How it works

Each check **does not** reuse the implementer's own scripts or assertions
(`controller/scripts/acceptance.py`, `tests/*`, the dashboard mock, the chaos
harness). Every check independently observes the live system — HTTP probes
against the controller, SSH/LXD inspection, WebSocket terminal, reading the
controller DB, or reading committed docs — and self-determines pass/fail.

## Evidence format (standardized)

Every check writes one timestamped artifact under
`evidence/<run_id>/NN_<check_id>.json`:

```json
{
  "check_id": "W3.3",
  "title": "W3.3 llama.cpp reachability from Node A",
  "timestamp_utc": "2026-10-04T09:55:00.123Z",
  "command": "lxc exec demo-node-a -- curl http://10.14.100.23:8080/health",
  "raw_output": "{\"status\":\"ok\"} ...",
  "env": {
    "controller_url": "http://127.0.0.1:8085",
    "repo_commit": "<git sha>",
    "controller_version_header": "1",
    "nodes": [{"node_id":"...","role":"compute","status":"online",...}],
    "lxd_containers": [{"name":"demo-node-c","state":"Running","ipv4":"10.14.100.23"}],
    "collected_at_utc": "..."
  }
}
```

The invariant: a result is **reproducible from the artifact alone** — the exact
command, the raw (unmodified) output, and the environment hash (image tag, node
name, commit) are all in the file.

## Run

```bash
cd w7/acceptance

TERMINI_PROJECT_TOKEN=<project-token> \
TERMINI_PROJECT_ID=<project-id> \
TERMINI_BOOTSTRAP_TOKEN=<unused-enroll-token> \
python3 run.py
```

Optional env:

| Var | Default | Meaning |
|-----|---------|---------|
| `TERMINI_CONTROLLER_URL` | `http://127.0.0.1:8085` | controller base URL |
| `TERMINI_PROJECT_TOKEN` | (empty) | frontend/project token |
| `TERMINI_PROJECT_ID` | (empty) | demo project id |
| `TERMINI_BOOTSTRAP_TOKEN` | (empty) | single-use enroll token (enables W2.1/W3.1 register checks) |
| `TERMINI_DEMO_DB` | `/var/lib/termini/demo/controller.db` | controller SQLite (W2.2 inspection) |
| `TERMINI_EVIDENCE_DIR` | `evidence/` | where artifacts land |
| `TERMINI_OUTPUT` | `summary.json` | summary JSON path |

Checks that need credentials they were not given are marked `skipped` with an
explicit reason rather than guessing.

## Output

`run.py` emits `summary.json` with one row per criterion:

```json
{
  "run_id": "run-...",
  "generated_at_utc": "...",
  "harness_commit": "<sha>",
  "environment": {...},
  "results": [
    {"id":"W2.1","workstream":"W2 — Control plane","criterion":"a simulated node registers and gets a role",
     "test_method":"POST /v1/nodes/register ...","status":"pass","detail":"...","artifact":"w7/acceptance/evidence/.../04_W2_1.json"}
  ],
  "totals": {"pass": 6, "fail": 0, "skipped": 10, "total": 16}
}
```

Each row carries `status ∈ {pass, fail, skipped}` and either an `artifact` path
or an explicit `reason` it could not be verified. Exit code is `1` iff any
criterion hard-fails, so CI can gate on it.

## Status semantics

- **pass** — criterion independently verified true.
- **fail** — criterion verified false (evidence attached).
- **skipped** — could not be fully verified (component not provisioned / not
  running / out of this harness's reach); `reason` states exactly what is
  missing.

## Side effects

The W2.1/W3.1 register checks POST a fresh synthetic node
(`machine_id = acceptance-harness-…`, no GPU) to the controller when
`TERMINI_BOOTSTRAP_TOKEN` is set; this consumes one enroll token and adds one
`compute` node. Everything else is read-only.
