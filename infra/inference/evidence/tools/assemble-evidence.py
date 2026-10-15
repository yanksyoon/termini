#!/usr/bin/env python3
"""Assemble infra/inference/evidence/simulated-gpu-node-2026-10-15.md.

Nothing is paraphrased: every capture block is the verbatim content of a file in
the task workspace, named so a reader can re-run the step. The prose says which
acceptance it backs and, where a claim is weaker than it looks, says so.
"""
from __future__ import annotations

import datetime
import pathlib

OUT = pathlib.Path("/home/ubuntu/.hermes/kanban/boards/termini/workspaces/t_f466824c")
DEST = pathlib.Path("/home/ubuntu/infra/inference/evidence/simulated-gpu-node-2026-10-15.md")

RAW_FILES = [
    ("what was running before this run (fleet state + machine types)", "live-raise-before.txt"),
    ("raising the fleet: n2 is unavailable in the zone, so the same-vCPU/RAM e2 types are used", "live-raise-after.txt"),
    ("node-c: the front door installed from THIS branch, workers, listeners, active backend", "live-nodec-install.txt"),
    ("(a) peer acceptance from Node A against 10.10.0.13:8080 — forwarded backend", "live-peer-gemini.txt"),
    ("the controller tree deployed from this branch's ref", "live-controller-deploy.txt"),
    ("(b) a real forwarded decision: seeder -> operator loop -> Node C -> Vertex", "live-model-decision.txt"),
    ("(b) the proxy's own request log for the forwarded calls (latency_ms + destination)", "live-proxy-journal.txt"),
    ("(c) ONE-STEP switch to the local CPU path (no code change)", "live-switch-local.txt"),
    ("(c) peer acceptance from Node A — LOCAL backend, same command, same endpoint", "live-peer-local.txt"),
    ("back to the shipped configuration (forwarded)", "live-switch-back.txt"),
    ("(c) peer acceptance from Node A — forwarded backend, final", "live-peer-gemini-final.txt"),
    ("(d) FORCED Vertex AI failure: a model id that does not exist -> a loud, distinct error", "live-forced-failure-2.txt"),
    ("(d) ...and the controller's deterministic fallback while the forwarded path is broken", "live-fallback.txt"),
    ("(d) the capture-only override removed, the controller back on the shipped tuning", "live-fallback-restore.txt"),
    ("the shipped configuration restored, and the node's final state", "live-restore.txt"),
]


def block(title: str, name: str) -> str:
    path = OUT / name
    if not path.exists():
        return f"\n#### {title}\n\n_(NOT CAPTURED: {name} missing)_\n"
    text = path.read_text(errors="replace")
    if not text.strip():
        return f"\n#### {title}\n\n_(NOT CAPTURED: {name} empty)_\n"
    return f"\n#### {title}\n\nSource: `{name}`\n\n```\n{text.strip()}\n```\n"


now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
head = f"""# Simulated GPU node — evidence (t_f466824c)

Date: 2026-10-15 · Owner: `agents` · Project `termini-510609` (region
`asia-south1`, zone `asia-south1-a`) · Live acceptance window: **11:06–11:30Z**.
Assembled {now} from the raw captures embedded below.

This is the raw record for **Node C's inference front door**: Nodes A/B keep
calling `http://10.10.0.13:8080` with the model alias `gemma`, and what answers
there is a proxy whose BACKEND is configuration, not code. It EMULATES a GPU node;
it is not one.

| Field | Value |
|---|---|
| Branch | `wt/t_f466824c-simulated-gpu-proxy-r2` |
| Commits | `7a3f2b8` (the front door, re-applied), `edda491` (the log-sink fix) |
| PR | https://github.com/yanksyoon/termini/pull/159 |
| Code | `infra/inference/proxy.py`; `controller/app/operator/egress.py` |
| Deployed to the controller | `infra/gcp/tools/deploy-termini-tree.sh --ref wt/t_f466824c-simulated-gpu-proxy-r2` |
| Repo suite | `python3 -m unittest discover -s tests` -> **683 tests OK** |
| Frozen contracts | `docs/contracts/` untouched by this card |

## Why there is a second run, and what went wrong in the first one

The first landing squashed to `bcf138e` and merged (#157). The **post-merge**
`ci / sanity` run on `main` was RED — `tests/test_inference_proxy.py`, 682 tests,
`failures=1 errors=1` — so the repository's ci-watchdog reverted it on `main` as
`ae51f7c` ("[watchdog] revert red commit bcf138e on main").

Root cause, reproduced locally at 10/10 failures: three of the new log-line tests
swapped the process-global `sys.stderr`, POSTed, restored it and then read the
buffer. The handler runs on its own thread and writes its request line AFTER the
response body, so the reader raced the writer — the PR's own run passed and the
main run lost the race.

The fix (this branch): `RequestLog` takes an explicit sink (`stream=None` still
resolves `sys.stderr` per write, so journald is unchanged) and appends every
record to `.records`; `make_server()` accepts an injectable `log_stream`; the log
tests start the proxy with their own `StringIO` and wait (bounded) for the line.
A test was added for the sink + records path. `TestLoggingAndConfig` was run 20
times consecutively: 20 OK.

## What this run verified

| # | Acceptance | Verdict | Where the raw output is |
|---|-----------|---------|-------------------------|
| a | From Node A (a peer), a real chat completion against `10.10.0.13:8080` returns a Gemini answer, and the response/log names the backend and destination | **PASS** | "peer acceptance from Node A — forwarded backend": `content: PROXI_OK`, `X-Termini-Backend: gemini-proxy`, `X-Termini-External-Destination: aiplatform.googleapis.com`, `answered_by=forwarded-managed-model`, `VERIFY_EXIT=0` |
| b | The operator loop in model mode records `decision_source=model` with a latency in **seconds**, and the audit row shows the external destination | **PASS** | "a real forwarded decision": two rows, `decision_source=model`, confidence 0.85 / 0.90; `operator_logs` carries `externalModelCall before/after ... egress=external model=gemini-2.5-flash destination=aiplatform.googleapis.com answered_by=forwarded-managed-model`; the forwarded call's own wall clock, measured from the audit pair, is **1.814 s** (13.008 s for the first, cold, call after the proxy restarted). The CPU baseline for the same 20-sample decision prompt is 402 s (`infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md`) |
| c | Switching `TERMINI_INFERENCE_BACKEND` to `local` restores the CPU path with no code change (both runs shown) | **PASS** — both backends' peer runs in this file, `VERIFY_EXIT=0` for each, one step (`termini-inference-switch local`), no rebuild, no code change | "the one-step switch to the local CPU path" and "peer acceptance — LOCAL backend" |
| d | A forced Vertex AI failure produces a loud error plus the deterministic fallback, and the demo survives it | **PASS** | "FORCED Vertex AI failure" — HTTP 503, `code=MODEL_UNAVAILABLE`, `distinct=vertex_rejected`, a JSON error naming the model and `destination=aiplatform.googleapis.com`, seen from the node AND from Node A. The forwarded call **failed fast (0.293 s / 0.306 s)**, not at the bound; the operator logged `model decision fell back (reason=inference_unavailable): http 503: Service Unavailable` and recorded **two `decision_source=fallback` rows** (11:24:14.442Z, 11:24:18.440Z) with the deterministic playbook action; the API and the loop stayed up |
| e | The proxy passes the repo's existing inference-contract tests unchanged | **PASS** — `infra/inference/verify-inference.sh`'s semantics (health 200, alias `gemma` advertised, non-empty completion, >=2 SSE deltas + `[DONE]`) are exactly what the peer runs assert, and the repo suite is green (683 tests OK, twice) | "peer acceptance from Node A" + the suite output above |
| f | Evidence file + PR through `scripts/land.sh` + commit/pr/merge_sha on the card | this file; PR #159 | card metadata |

## Findings (nothing below is a silent hole)

1. **The standing controller's decision bound is 900 s, not the contract's 120 s.**
   `systemctl show termini-controller -p Environment` reports
   `TERMINI_OPERATOR_DECISION_TIMEOUT_S=900` and `TERMINI_INFERENCE_TIMEOUT_S=900`,
   although `zz-proxy-model.conf` and `model-mode.conf` set 120:
   `zzz-real-fleet.conf` (2026-10-13) sets 900 and sorts after them, so 900 wins.
   This is pre-existing standing configuration (the CPU decider needs 402 s per
   decision, so a 120 s bound would break the local path) and this card does NOT
   change it. The reason it does not weaken (d): the failure path does not wait for
   the bound at all — a broken forwarded backend returns 503 in **0.293 s**, the
   loop records the fallback immediately. Flagged, not quietly edited, per the
   card's rule on `docs/contracts/`.
2. **One decision was suppressed by the demo's own rate limits during the first
   (d) attempt.** `TERMINI_OPERATOR_COOLDOWN_S=600` had just fired a decision, so
   a 240 s poll recorded none. The second attempt declares a **capture-only**
   drop-in, `zzzzz-fallback-capture.conf`, setting the cooldown to 30 s — written
   by the script, printed in the capture, and **deleted again** immediately after
   (`live-fallback-restore.txt` shows the directory without it and the effective
   tuning back to 600 s). It changes when a decision fires, never how one is
   decided or what a failure does.
3. **In `local` mode the response BODY is llama.cpp's own bytes** (byte-transparent
   passthrough by design: same weights, same envelope, maximum drop-in fidelity).
   The local labels are therefore carried by the response HEADERS and by `/health`
   + `/v1/models`, and the committed `verify-proxy.sh` checks headers for the local
   backend and the `termini_backend` body block for the forwarded one — both PASS
   in this run. The first run's capture was taken with an older script and showed
   this as a failure; that capture is superseded by the runs below.
4. **`n2-standard-2` / `n2-standard-8` were `ZONE_RESOURCE_POOL_EXHAUSTED` in
   `asia-south1-a` again.** The three standing instances were started on
   `e2-standard-2` / `e2-standard-2` / `e2-standard-8` (same vCPU/RAM) and are
   restored to their documented n2 types at teardown. No disk, instance id or
   identity changed.
5. **Temporary external IPs** were added to node-a and node-c for SSH only (both
   are `--no-address` by design) and deleted at teardown. The controller keeps its
   own external IP: it is the public-facing API.
6. **Cost.** The three instances ran for roughly half an hour of wall clock in this
   run. `scripts/teardown.sh --pause` semantics are used at the end (stop, keep
   disks): the disk / static-IP / image lines continue to bill, exactly as
   `t_9232df6d` documents. Nothing here is a "free" run — but Private Google
   Access, which is the forwarded path's egress, is $0/h and $0/GiB.

## The mechanism, in one paragraph

`TERMINI_INFERENCE_BACKEND` in `/etc/termini/inference-proxy.env` decides the
backend; `termini-inference-switch local|gemini-proxy|legacy|status` is the
one-step switch and writes the active backend to
`/run/termini/inference-backend.json` (with `"is_gpu_inference": false`). In
`gemini-proxy` mode the proxy translates the OpenAI request to Vertex AI
`generateContent` (`gemini-2.5-flash`, GLOBAL endpoint, project read from the
instance metadata server and a token minted per request from the same metadata
server — no key file on the node), honours `max_tokens` and `temperature`, and
returns the same envelope including `usage` and `timings`. The local llama.cpp
worker moved to loopback `127.0.0.1:8081`, which is also the frozen AR-4 shape
(front door on `:8080`, workers on loopback) and is what makes the CPU path one
env-var away. The controller's `external_model_call()` guard records the
destination class on both audit records, so the operator's audit row names
`destination=aiplatform.googleapis.com` rather than implying local weights.
"""

parts = [head, "\n## Raw captures\n"]
for title, name in RAW_FILES:
    parts.append(block(title, name))

tail = """
## Re-running this

```bash
# on a raised fleet, with SSH access to node-a and node-c
gcloud compute scp infra/inference/{proxy.py,termini-inference-proxy.service,install-inference-proxy.sh,termini-inference-switch.sh,verify-proxy.sh} \\
  termini-node-c:/tmp/ --zone asia-south1-a --project termini-510609
gcloud compute ssh termini-node-c --zone asia-south1-a --project termini-510609 \\
  --command "sudo bash /tmp/install-inference-proxy.sh --backend gemini-proxy && \\
             sudo install -m0755 /tmp/termini-inference-switch.sh /usr/local/sbin/termini-inference-switch && \\
             sudo termini-inference-switch status"

gcloud compute scp infra/inference/verify-proxy.sh termini-node-a:/tmp/ --zone asia-south1-a --project termini-510609
gcloud compute ssh termini-node-a --zone asia-south1-a --project termini-510609 \\
  --command "bash /tmp/verify-proxy.sh 10.10.0.13 gemini-proxy"   # and: local
```

The controller half (model mode, the seeder, the audit rows and the fallback)
runs from `infra/gcp/tools/deploy-termini-tree.sh --ref <branch>` plus the
systemd drop-in shown in this file; the collector scripts used for the raw output
are committed next to this file in `infra/inference/evidence/tools/`.

Frozen contracts (`docs/contracts/`) are untouched by this card; the wire shape,
the error envelope, the telemetry envelope and the model-alias surface are all
unchanged, and the additions (response headers, a `termini_backend` body block,
audit fields) are additive.
"""

DEST.write_text("".join(parts) + tail, encoding="utf-8")
print(f"wrote {DEST} ({DEST.stat().st_size} bytes, {len(RAW_FILES)} capture blocks)")
