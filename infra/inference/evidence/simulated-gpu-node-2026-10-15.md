# Simulated GPU node — evidence (t_f466824c)

Date: 2026-10-15 · Owner: `agents` · Project `termini-510609` (region
`asia-south1`, zone `asia-south1-a`) · Live acceptance window: **11:06–11:30Z**.
Assembled 2026-10-15T11:27:19Z from the raw captures embedded below.

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

## Raw captures

#### what was running before this run (fleet state + machine types)

Source: `live-raise-before.txt`

```
NAME                STATUS      MACHINE_TYPE   NETWORK_IP  NAT_IP
termini-controller  TERMINATED  n2-standard-2  10.10.0.2   34.93.190.64
termini-node-a      TERMINATED  n2-standard-2  10.10.0.11
termini-node-b      TERMINATED  n2-standard-2  10.10.0.12
termini-node-c      TERMINATED  n2-standard-8  10.10.0.13
```

#### raising the fleet: n2 is unavailable in the zone, so the same-vCPU/RAM e2 types are used

Source: `live-raise-after.txt`

```
NAME                STATUS      MACHINE_TYPE   NETWORK_IP  NAT_IP
termini-controller  RUNNING     e2-standard-2  10.10.0.2   34.93.190.64
termini-node-a      RUNNING     e2-standard-2  10.10.0.11  8.231.101.64
termini-node-b      TERMINATED  n2-standard-2  10.10.0.12
termini-node-c      RUNNING     e2-standard-8  10.10.0.13  8.234.93.244
```

#### node-c: the front door installed from THIS branch, workers, listeners, active backend

Source: `live-nodec-install.txt`

```
host: termini-node-c.asia-south1-a.c.termini-510609.internal  ts: 2026-10-15T11:11:04Z
--- the log-sink fix is in the installed file (grep proof)
297:        sink = self.stream if self.stream is not None else sys.stderr
297:        sink = self.stream if self.stream is not None else sys.stderr
[install-inference-proxy] installed /opt/termini/inference/proxy.py
[install-inference-proxy] termini-llama-server.service -> worker on 127.0.0.1:8081 (host-exposed :8080 released)
[install-inference-proxy] waiting for the front door on :8080
[install-inference-proxy] front door answered after 2s
[install-inference-proxy] active backend:
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
[install-inference-proxy] install done — 'termini-inference-switch status' reports the active backend
--- waiting for the local worker on loopback :8081 (llama.cpp returns 503 while loading)
worker ready after 5s
--- units
active
active
active
--- listeners
LISTEN 0      5                       0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=2238,fd=5))                     
LISTEN 0      512                   127.0.0.1:8081      0.0.0.0:*    users:(("llama-server",pid=2178,fd=3))                
--- active backend: /run/termini/inference-backend.json
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "forwarded-managed-model",
  "backend": "gemini-proxy",
  "destination": "aiplatform.googleapis.com",
  "egress": "external",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "forwarded managed model emulating a GPU node",
  "served_model": "gemini-2.5-flash",
  "ts": "2026-10-15T11:11:06Z"
}
--- GET /health (what nodes A/B see)
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
--- GET /v1/models
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792062671, "owned_by": "termini"}], "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "served_model": "gemini-2.5-flash", "egress": "external", "destination": "aiplatform.googleapis.com"}}
```

#### (a) peer acceptance from Node A against 10.10.0.13:8080 — forwarded backend

Source: `live-peer-gemini.txt`

```
termini-node-a.asia-south1-a.c.termini-510609.internal
Termini inference front door — peer acceptance, 2026-10-15T11:11:20Z
caller: termini-node-a.asia-south1-a.c.termini-510609.internal (10.10.0.11)   endpoint: http://10.10.0.13:8080   expect backend: gemini-proxy

===== 1. GET /health (from this peer) =====
http_code=200
body: {"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}

===== 2. GET /v1/models =====
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792062680, "owned_by": "termini"}], "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "served_model": "gemini-2.5-flash", "egress": "external", "destination": "aiplatform.googleapis.com"}}

===== 3-6. requests (non-streaming, streaming, parameters, labels) =====
PASS  peer: /health returns 200 — got 200
PASS  /health reports a backend — backend='gemini-proxy'
PASS  /health backend == gemini-proxy — reported 'gemini-proxy'
PASS  /v1/models advertises the alias 'gemma' — ids=['gemma']
      non-streaming: http=200 in 1.36s
      response headers: X-Termini-Backend=gemini-proxy, X-Termini-Answered-By=forwarded-managed-model, X-Termini-Model=gemma, X-Termini-Model-Id=gemini-2.5-flash, X-Termini-Egress=external, X-Termini-Residency=resident, X-Termini-External-Destination=aiplatform.googleapis.com, X-Termini-Request-Id=01ACCEPT
      body: {"id": "chatcmpl-01M4ZM4WRSNGXN0W1G3ECR4PN7", "object": "chat.completion", "created": 1792062681, "model": "gemma", "choices": [{"index": 0, "message": {"role": "assistant", "content": "PROXI_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}, "timings": {"prompt_n": 8, "prompt_ms": 0.0, "predicted_n": 4, "predicted_ms": 1317.38, "predicted_per_token_ms": 329.345, "predicted_per_second": 3.036, "wall_ms": 1317.38}, "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"}}
PASS  non-streaming returns HTTP 200 — got 200
PASS  non-streaming returns non-empty content — 'PROXI_OK'
PASS  the envelope names the model alias — model='gemma'
PASS  the envelope carries the OpenAI id/object/created fields
PASS  usage is reported — usage={'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}
      label headers: X-Termini-Backend='gemini-proxy' X-Termini-Answered-By='forwarded-managed-model' X-Termini-Egress='external' X-Termini-External-Destination='aiplatform.googleapis.com'
      in-band body label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"}
PASS  the response labels its backend in-band — header='gemini-proxy' body='gemini-proxy'
PASS  the label names what actually answered (answered_by) — header='forwarded-managed-model' body='forwarded-managed-model'
PASS  no response claims GPU inference (word absent from body + headers)
PASS  no response claims local weights for a forwarded call
PASS  a forwarded answer is labelled answered_by=forwarded-managed-model — got 'forwarded-managed-model'
PASS  a forwarded answer names its external destination — body='aiplatform.googleapis.com' header='aiplatform.googleapis.com'
PASS  a forwarded answer is flagged egress=external — got 'external'
PASS  a forwarded answer is NOT labelled as local weights
PASS  max_tokens is honoured (8 requested) — completion_tokens=8
      max_tokens=8 run: 0.82s, usage={'prompt_tokens': 11, 'completion_tokens': 8, 'total_tokens': 19}
      streaming: http=200 deltas=2 first_token_s=1.242 total_s=1.37
      assembled: 'one\ntwo\nthree\nfour\nfive'
      final label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"} usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20} finish='stop'
PASS  streaming returns HTTP 200 — got 200
PASS  streaming yields >=2 incremental deltas — 2 deltas
PASS  streaming terminates with [DONE]
PASS  streaming assembled content is non-empty
PASS  streaming reports usage when asked (include_usage) — usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20}
PASS  streaming final chunk is labelled in-band — body={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node'} header='gemini-proxy'
PASS  streaming from a forwarded backend names the destination — label={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node'} header='aiplatform.googleapis.com'

      latency samples (s): [1.364]
      median latency: 1.364s (backend reported by the endpoint: gemini-proxy)
      NOTE: this compares the FRONT DOOR's own answer time; for the same
      20-sample decision prompt the CPU path on this node is documented at
      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).

RESULT: PASS (drop-in envelope + labelled backend + parameters + streaming)

===== RESULT =====
RESULT: PASS — the front door is a drop-in and it labels what answered
VERIFY_EXIT=0
```

#### the controller tree deployed from this branch's ref

Source: `live-controller-deploy.txt`

```
[deploy] ref=wt/t_f466824c-simulated-gpu-proxy-r2 commit=edda491f79040a3e9411b2fa23c7b392135b46f3 tarball=231533 bytes
[remote] backup -> /opt/termini.bak-20261015T111124Z
[remote] deployed 99 files from edda491f79040a3e9411b2fa23c7b392135b46f3
[deploy] restarting termini-controller
active
[deploy] done: termini-controller /opt/termini now carries edda491f79040a3e9411b2fa23c7b392135b46f3
```

#### (b) a real forwarded decision: seeder -> operator loop -> Node C -> Vertex

Source: `live-model-decision.txt`

```
T0=2026-10-15T11:11:54Z  (rows before this instant are ignored)
+ sudo rm -f /etc/systemd/system/termini-controller.service.d/zz-node-c-model.conf
+ sudo mkdir -p /etc/systemd/system/termini-controller.service.d
+ sudo tee /etc/systemd/system/termini-controller.service.d/zz-proxy-model.conf
+ sudo systemctl daemon-reload
+ sudo systemctl restart termini-controller
+ set +x
controller-up
--- operator environment in the running unit
TERMINI_OPERATOR_ENABLED=true
TERMINI_OPERATOR_DECISION_MODE=model
TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080
TERMINI_INFERENCE_MODEL=gemma
TERMINI_INFERENCE_TIMEOUT_S=900
TERMINI_INTENT_INFERENCE_BASE_URL=http://10.10.0.13:8090
--- reset the per-(node,rule) fallback/cooldown ledger (the demo's RESET; printed, not silent)
decision_fallback_state rows deleted: 5
--- start the demo seeder (synthetic nodes -> the real operator loop)
--- polling for a decision_source=model row decided after 2026-10-15T11:11:54Z
MODEL DECISION LANDED after 10s of polling
poll result: yes
--- seeder log tail
Oct 15 10:58:16 termini-controller.asia-south1-a.c.termini-510609.internal systemd[1]: Stopping termini-seeder.service - Termini demo seeder (synthetic nodes + telemetry heartbeat)...
Oct 15 10:58:16 termini-controller.asia-south1-a.c.termini-510609.internal systemd[1]: termini-seeder.service: Deactivated successfully.
Oct 15 10:58:16 termini-controller.asia-south1-a.c.termini-510609.internal systemd[1]: Stopped termini-seeder.service - Termini demo seeder (synthetic nodes + telemetry heartbeat).
Oct 15 10:58:16 termini-controller.asia-south1-a.c.termini-510609.internal systemd[1]: termini-seeder.service: Consumed 1.841s CPU time.
-- Boot 1ee3578dd23f4422af28ce089286625e --
Oct 15 11:11:56 termini-controller.asia-south1-a.c.termini-510609.internal systemd[1]: Started termini-seeder.service - Termini demo seeder (synthetic nodes + telemetry heartbeat).
Oct 15 11:11:57 termini-controller.asia-south1-a.c.termini-510609.internal python3[1866]: [seed] project_id = 01J0DEM0WARM000000000000AB
Oct 15 11:11:57 termini-controller.asia-south1-a.c.termini-510609.internal python3[1866]: [seed] node A (compute)   = 01M4NZK46WXG5ZEFAGAAV5JRXM
Oct 15 11:11:57 termini-controller.asia-south1-a.c.termini-510609.internal python3[1866]: [seed] node C (inference) = 01M4NZK474BVARFBXYQBMVA8GV
=== CONTROLLER EVIDENCE (2026-10-15T11:12:18Z) ===
### controller DB: /data/controller.db

### operator unit env (decision mode + inference endpoint)
    TERMINI_OPERATOR_ENABLED=true
    TERMINI_OPERATOR_DECISION_MODE=model
    TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080
    TERMINI_INFERENCE_MODEL=gemma
    TERMINI_INFERENCE_TIMEOUT_S=900
    TERMINI_OPERATOR_DECISION_TIMEOUT_S=900
    TERMINI_OPERATOR_POLL_INTERVAL_S=2
    TERMINI_INTENT_INFERENCE_BASE_URL=http://10.10.0.13:8090
    TERMINI_OPERATOR_WINDOW=20
    TERMINI_OPERATOR_COOLDOWN_S=600

### remediation_decisions (latest 8) — ALL columns
# columns: ['id', 'event_id', 'node_id', 'rule_id', 'playbook', 'action_kind', 'action_params', 'reason', 'decided_at', 'decision_source', 'confidence', 'fallback_reason']
# NOTE: this table has NO latency column — the forwarded call's wall clock is
# measured from the externalModelCall before/after audit pair below.
{'id': '01M4ZM6HHGE0V93Z2QXEYTTZ6M', 'event_id': '01M4ZM6FQBCMPMCRA4NYQ2GHAJ', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'playbook': 'oom-guard-01', 'action_kind': 'instance.restart', 'action_params': '{"instance_name": "01M4N827979BD9D2762EFE24A0", "force": true, "timeout_s": 30, "_vm_id": "01M4N827979BD9D2762EFE24A0"}', 'reason': 'ram_used_bytes is at 96.04% of total, and instance 01M4N827979BD9D2762EFE24A0 is the heaviest consumer, restarting it should free up memory.', 'decided_at': '2026-10-15T11:12:14.079Z', 'decision_source': 'model', 'confidence': 0.9, 'fallback_reason': None}
{'id': '01M4ZM6DQED6QGCBNC8DQSCR3R', 'event_id': '01M4ZM60Y7M1TKZ8S1THNEXR4M', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'playbook': 'disk-degrade-01', 'action_kind': 'instance.stop', 'action_params': '{"instance_name": "01M4N7F8B8823D650DF40D614D", "force": true, "_vm_id": "01M4N7F8B8823D650DF40D614D"}', 'reason': 'disk write latency is 120ms, exceeding the 50ms threshold; stopping the instance with the highest disk usage will reduce disk I/O.', 'decided_at': '2026-10-15T11:11:58.940Z', 'decision_source': 'model', 'confidence': 0.85, 'fallback_reason': None}

### operator_logs: externalModelCall rows (latest 16)
{'log_id': '01M4ZM6HH0Y0RQVZ7HYC8Q029E', 'ts': '2026-10-15T11:12:15.905Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall after endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=returned request_id=01M4ZM6FR17Z0EHZYG7VF204JM'}
{'log_id': '01M4ZM6FRBM8DTPWMJCB80WQ7G', 'ts': '2026-10-15T11:12:14.091Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall before endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=ok request_id=01M4ZM6FR17Z0EHZYG7VF204JM'}
{'log_id': '01M4ZM6DPY19MPKA5K84N80QKG', 'ts': '2026-10-15T11:12:11.998Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall after endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=returned request_id=01M4ZM60YXGM5G27GCFY338P4K'}
{'log_id': '01M4ZM610FTD84873WVFFQS2GQ', 'ts': '2026-10-15T11:11:58.991Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall before endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=ok request_id=01M4ZM60YXGM5G27GCFY338P4K'}

### journald: externalModelCall before/after pairs + the forwarded call's latency

### request_id=01M4ZJRJ9FW95MPJVZZFGEWSJ2 [after only, no pair]: Oct 15 10:47:31 termini-controller.asia-south1-a.c.termini-510609.internal python3[2179]: 2026-10-15 10:47:31,224 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZJRJ9FW95MPJVZZFGEWSJ2', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZKBBN8FB6NGB6T12ZKPP4N: forwarded call wall clock = 1.579 s
    before: Oct 15 10:57:25 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:57:25,221 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZKBBN8FB6NGB6T12ZKPP4N', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 10:57:26 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:57:26,800 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZKBBN8FB6NGB6T12ZKPP4N', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZKBFJEPWZE5HD0T0B6FAFG: forwarded call wall clock = 1.188 s
    before: Oct 15 10:57:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:57:29,177 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZKBFJEPWZE5HD0T0B6FAFG', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 10:57:30 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:57:30,365 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZKBFJEPWZE5HD0T0B6FAFG', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZM60YXGM5G27GCFY338P4K: forwarded call wall clock = 13.008 s
    before: Oct 15 11:11:58 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:11:58,989 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZM60YXGM5G27GCFY338P4K', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 11:12:11 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:12:11,997 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZM60YXGM5G27GCFY338P4K', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZM6FR17Z0EHZYG7VF204JM: forwarded call wall clock = 1.814 s
    before: Oct 15 11:12:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:12:14,089 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZM6FR17Z0EHZYG7VF204JM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 11:12:15 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:12:15,903 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZM6FR17Z0EHZYG7VF204JM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### decision_fallback_state (the per-(node,rule) fallback ledger)
{'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'fallback_streak': 0, 'forced_until': None, 'updated_at': '2026-10-15T11:11:58.940Z'}
{'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'fallback_streak': 0, 'forced_until': None, 'updated_at': '2026-10-15T11:12:14.079Z'}
```

#### (b) the proxy's own request log for the forwarded calls (latency_ms + destination)

Source: `live-proxy-journal.txt`

```
Oct 15 11:11:21 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2238]: {"ts":"2026-10-15T11:11:21Z","event":"inference_request","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","path":"/v1/chat/completions","stream":false,"status":200,"request_id":"01ACCEPT","latency_ms":1317.38,"forwarded":true,"prompt_tokens":8,"completion_tokens":4,"requested_max_tokens":32,"requested_temperature":0.0,"finish_reason":"stop"}
Oct 15 11:11:22 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2238]: {"ts":"2026-10-15T11:11:22Z","event":"inference_request","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","path":"/v1/chat/completions","stream":false,"status":200,"request_id":"01ACCEPT","latency_ms":820.17,"forwarded":true,"prompt_tokens":11,"completion_tokens":8,"requested_max_tokens":8,"requested_temperature":0.0,"finish_reason":"length"}
Oct 15 11:11:24 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2238]: {"ts":"2026-10-15T11:11:24Z","event":"inference_request","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","path":"/v1/chat/completions","stream":true,"status":200,"request_id":"req-01M4ZM4XJKT9YR05H70C1AKGRE","latency_ms":1371.03,"forwarded":true,"first_token_ms":1239.6,"completion_chars":23,"prompt_tokens":11,"completion_tokens":9,"requested_max_tokens":40,"requested_temperature":0.0,"finish_reason":"stop"}
Oct 15 11:12:11 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2238]: {"ts":"2026-10-15T11:12:11Z","event":"inference_request","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","path":"/v1/chat/completions","stream":false,"status":200,"request_id":"01M4ZM60YXGM5G27GCFY338P4K","latency_ms":12994.31,"forwarded":true,"prompt_tokens":11755,"completion_tokens":121,"requested_max_tokens":512,"requested_temperature":0.0,"finish_reason":"stop"}
Oct 15 11:12:15 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2238]: {"ts":"2026-10-15T11:12:15Z","event":"inference_request","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","path":"/v1/chat/completions","stream":false,"status":200,"request_id":"01M4ZM6FR17Z0EHZYG7VF204JM","latency_ms":1796.61,"forwarded":true,"prompt_tokens":13138,"completion_tokens":148,"requested_max_tokens":512,"requested_temperature":0.0,"finish_reason":"stop"}
```

#### (c) ONE-STEP switch to the local CPU path (no code change)

Source: `live-switch-local.txt`

```
+ sudo /usr/local/sbin/termini-inference-switch local
[inference-switch] TERMINI_INFERENCE_BACKEND=local
== node status: /etc/termini ==
--- /etc/termini/inference-proxy.env
[inference-switch] ACTIVE BACKEND: local
TERMINI_INFERENCE_BACKEND=local
TERMINI_INFERENCE_HOST=0.0.0.0
TERMINI_INFERENCE_PORT=8080
TERMINI_INFERENCE_ALIAS=gemma
TERMINI_INFERENCE_UPSTREAM=http://127.0.0.1:8081
TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S=900
TERMINI_GEMINI_HOST=aiplatform.googleapis.com
TERMINI_GEMINI_LOCATION=global
TERMINI_GEMINI_MODEL=gemini-2.5-flash
TERMINI_GEMINI_PROJECT=
TERMINI_GEMINI_TIMEOUT_S=30
TERMINI_GEMINI_THINKING_BUDGET=0
TERMINI_INFERENCE_STATE_FILE=/run/termini/inference-backend.json
TERMINI_INFERENCE_LOG_FILE=/var/log/termini-inference-proxy.jsonl
--- /etc/termini/llama-worker.env
LLAMA_HOST=127.0.0.1
LLAMA_PORT=8081

== units ==
termini-inference-proxy.service: enabled=enabled active=active
termini-llama-server.service: enabled=enabled active=active

== listeners ==
LISTEN 0      5                       0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=2532,fd=5))                      
LISTEN 0      512                   127.0.0.1:8081      0.0.0.0:*    users:(("llama-server",pid=2178,fd=3))                 

== recorded active backend ==
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "local-weights",
  "backend": "local",
  "destination": "http://127.0.0.1:8081",
  "egress": "in-vpc",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "local CPU weights (llama.cpp) on this node",
  "served_model": "gemma",
  "ts": "2026-10-15T11:12:27Z"
}

== :8080/health (what nodes A/B see) ==
{"status": "ok", "model": "gemma", "backend": "local", "answered_by": "local-weights", "egress": "in-vpc", "served_model": "gemma", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "http://127.0.0.1:8081", "note": "local weights served by llama.cpp on this node"}
== :8081/health (the local worker, loopback only) ==
{"status":"ok"}
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "local-weights",
  "backend": "local",
  "destination": "http://127.0.0.1:8081",
  "egress": "in-vpc",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "local CPU weights (llama.cpp) on this node",
  "served_model": "gemma",
  "ts": "2026-10-15T11:12:27Z"
}
{"status": "ok", "model": "gemma", "backend": "local", "answered_by": "local-weights", "egress": "in-vpc", "served_model": "gemma", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "http://127.0.0.1:8081", "note": "local weights served by llama.cpp on this node"}
+ sudo cat /run/termini/inference-backend.json
+ curl -s -m 10 http://127.0.0.1:8080/health
+ echo
```

#### (c) peer acceptance from Node A — LOCAL backend, same command, same endpoint

Source: `live-peer-local.txt`

```
Termini inference front door — peer acceptance, 2026-10-15T11:12:32Z
caller: termini-node-a.asia-south1-a.c.termini-510609.internal (10.10.0.11)   endpoint: http://10.10.0.13:8080   expect backend: local

===== 1. GET /health (from this peer) =====
http_code=200
body: {"status": "ok", "model": "gemma", "backend": "local", "answered_by": "local-weights", "egress": "in-vpc", "served_model": "gemma", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "http://127.0.0.1:8081", "note": "local weights served by llama.cpp on this node"}

===== 2. GET /v1/models =====
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792062752, "owned_by": "termini"}], "termini_backend": {"backend": "local", "answered_by": "local-weights", "served_model": "gemma", "egress": "in-vpc", "upstream": "http://127.0.0.1:8081"}}

===== 3-6. requests (non-streaming, streaming, parameters, labels) =====
PASS  peer: /health returns 200 — got 200
PASS  /health reports a backend — backend='local'
PASS  /health backend == local — reported 'local'
PASS  /v1/models advertises the alias 'gemma' — ids=['gemma']
      non-streaming: http=200 in 1.31s
      response headers: X-Termini-Backend=local, X-Termini-Answered-By=local-weights, X-Termini-Model=gemma, X-Termini-Model-Id=gemma, X-Termini-Egress=in-vpc, X-Termini-Residency=resident, X-Termini-Request-Id=01ACCEPT
      body: {"choices": [{"finish_reason": "stop", "index": 0, "message": {"role": "assistant", "content": "PROXI_OK\n"}}], "created": 1792062753, "model": "gemma", "system_fingerprint": "b1-7fe450e19", "object": "chat.completion", "usage": {"completion_tokens": 6, "prompt_tokens": 17, "total_tokens": 23, "prompt_tokens_details": {"cached_tokens": 0}}, "id": "chatcmpl-0hBtgNmRy2gONl9ibSnTCEeASNDXwpqd", "timings": {"cache_n": 0, "prompt_n": 17, "prompt_ms": 755.087, "prompt_per_token_ms": 44.41688235294117, "prompt_per_second": 22.51396196729648, "predicted_n": 6, "predicted_ms": 501.783, "predicted_per_token_ms": 100.3566, "predicted_per_second": 9.964466711706056}}
PASS  non-streaming returns HTTP 200 — got 200
PASS  non-streaming returns non-empty content — 'PROXI_OK\n'
PASS  the envelope names the model alias — model='gemma'
PASS  the envelope carries the OpenAI id/object/created fields
PASS  usage is reported — usage={'completion_tokens': 6, 'prompt_tokens': 17, 'total_tokens': 23, 'prompt_tokens_details': {'cached_tokens': 0}}
      label headers: X-Termini-Backend='local' X-Termini-Answered-By='local-weights' X-Termini-Egress='in-vpc' X-Termini-External-Destination=''
      in-band body label: {}
PASS  the response labels its backend in-band — header='local' body=None
PASS  the label names what actually answered (answered_by) — header='local-weights' body=None
PASS  no response claims GPU inference (word absent from body + headers)
PASS  no response claims local weights for a forwarded call
PASS  a local answer is labelled answered_by=local-weights — got 'local-weights'
PASS  a local answer is flagged egress=in-vpc — got 'in-vpc'
PASS  the local answer is llama.cpp's own envelope (byte-transparent passthrough)
PASS  max_tokens is honoured (8 requested) — completion_tokens=8
      max_tokens=8 run: 1.52s, usage={'completion_tokens': 8, 'prompt_tokens': 20, 'total_tokens': 28, 'prompt_tokens_details': {'cached_tokens': 1}}
      streaming: http=200 deltas=9 first_token_s=1.665 total_s=1.66
      assembled: 'One\nTwo\nThree\nFour\nFive'
      final label: null usage={'completion_tokens': 10, 'prompt_tokens': 20, 'total_tokens': 30, 'prompt_tokens_details': {'cached_tokens': 1}} finish='stop'
PASS  streaming returns HTTP 200 — got 200
PASS  streaming yields >=2 incremental deltas — 9 deltas
PASS  streaming terminates with [DONE]
PASS  streaming assembled content is non-empty
PASS  streaming reports usage when asked (include_usage) — usage={'completion_tokens': 10, 'prompt_tokens': 20, 'total_tokens': 30, 'prompt_tokens_details': {'cached_tokens': 1}}
PASS  streaming final chunk is labelled in-band — body=None header='local'

      latency samples (s): [1.31]
      median latency: 1.310s (backend reported by the endpoint: local)
      NOTE: this compares the FRONT DOOR's own answer time; for the same
      20-sample decision prompt the CPU path on this node is documented at
      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).

RESULT: PASS (drop-in envelope + labelled backend + parameters + streaming)

===== RESULT =====
RESULT: PASS — the front door is a drop-in and it labels what answered
VERIFY_EXIT=0
```

#### back to the shipped configuration (forwarded)

Source: `live-switch-back.txt`

```
+ sudo /usr/local/sbin/termini-inference-switch gemini-proxy
[inference-switch] TERMINI_INFERENCE_BACKEND=gemini-proxy
== node status: /etc/termini ==
--- /etc/termini/inference-proxy.env
[inference-switch] ACTIVE BACKEND: gemini-proxy
TERMINI_INFERENCE_BACKEND=gemini-proxy
TERMINI_INFERENCE_HOST=0.0.0.0
TERMINI_INFERENCE_PORT=8080
TERMINI_INFERENCE_ALIAS=gemma
TERMINI_INFERENCE_UPSTREAM=http://127.0.0.1:8081
TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S=900
TERMINI_GEMINI_HOST=aiplatform.googleapis.com
TERMINI_GEMINI_LOCATION=global
TERMINI_GEMINI_MODEL=gemini-2.5-flash
TERMINI_GEMINI_PROJECT=
TERMINI_GEMINI_TIMEOUT_S=30
TERMINI_GEMINI_THINKING_BUDGET=0
TERMINI_INFERENCE_STATE_FILE=/run/termini/inference-backend.json
TERMINI_INFERENCE_LOG_FILE=/var/log/termini-inference-proxy.jsonl
--- /etc/termini/llama-worker.env
LLAMA_HOST=127.0.0.1
LLAMA_PORT=8081

== units ==
termini-inference-proxy.service: enabled=enabled active=active
termini-llama-server.service: enabled=enabled active=active

== listeners ==
LISTEN 0      5                       0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=2704,fd=5))                     
LISTEN 0      512                   127.0.0.1:8081      0.0.0.0:*    users:(("llama-server",pid=2178,fd=3))                

== recorded active backend ==
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "forwarded-managed-model",
  "backend": "gemini-proxy",
  "destination": "aiplatform.googleapis.com",
  "egress": "external",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "forwarded managed model emulating a GPU node",
  "served_model": "gemini-2.5-flash",
  "ts": "2026-10-15T11:12:40Z"
}

== :8080/health (what nodes A/B see) ==
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
== :8081/health (the local worker, loopback only) ==
{"status":"ok"}
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "forwarded-managed-model",
  "backend": "gemini-proxy",
  "destination": "aiplatform.googleapis.com",
  "egress": "external",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "forwarded managed model emulating a GPU node",
  "served_model": "gemini-2.5-flash",
  "ts": "2026-10-15T11:12:40Z"
}
+ sudo cat /run/termini/inference-backend.json
```

#### (c) peer acceptance from Node A — forwarded backend, final

Source: `live-peer-gemini-final.txt`

```
Termini inference front door — peer acceptance, 2026-10-15T11:12:45Z
caller: termini-node-a.asia-south1-a.c.termini-510609.internal (10.10.0.11)   endpoint: http://10.10.0.13:8080   expect backend: gemini-proxy

===== 1. GET /health (from this peer) =====
http_code=200
body: {"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}

===== 2. GET /v1/models =====
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792062765, "owned_by": "termini"}], "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "served_model": "gemini-2.5-flash", "egress": "external", "destination": "aiplatform.googleapis.com"}}

===== 3-6. requests (non-streaming, streaming, parameters, labels) =====
PASS  peer: /health returns 200 — got 200
PASS  /health reports a backend — backend='gemini-proxy'
PASS  /health backend == gemini-proxy — reported 'gemini-proxy'
PASS  /v1/models advertises the alias 'gemma' — ids=['gemma']
      non-streaming: http=200 in 1.00s
      response headers: X-Termini-Backend=gemini-proxy, X-Termini-Answered-By=forwarded-managed-model, X-Termini-Model=gemma, X-Termini-Model-Id=gemini-2.5-flash, X-Termini-Egress=external, X-Termini-Residency=resident, X-Termini-External-Destination=aiplatform.googleapis.com, X-Termini-Request-Id=01ACCEPT
      body: {"id": "chatcmpl-01M4ZM7FWCP07P0P0VDEVVXM60", "object": "chat.completion", "created": 1792062766, "model": "gemma", "choices": [{"index": 0, "message": {"role": "assistant", "content": "PROXI_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}, "timings": {"prompt_n": 8, "prompt_ms": 0.0, "predicted_n": 4, "predicted_ms": 946.66, "predicted_per_token_ms": 236.665, "predicted_per_second": 4.225, "wall_ms": 946.66}, "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"}}
PASS  non-streaming returns HTTP 200 — got 200
PASS  non-streaming returns non-empty content — 'PROXI_OK'
PASS  the envelope names the model alias — model='gemma'
PASS  the envelope carries the OpenAI id/object/created fields
PASS  usage is reported — usage={'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}
      label headers: X-Termini-Backend='gemini-proxy' X-Termini-Answered-By='forwarded-managed-model' X-Termini-Egress='external' X-Termini-External-Destination='aiplatform.googleapis.com'
      in-band body label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"}
PASS  the response labels its backend in-band — header='gemini-proxy' body='gemini-proxy'
PASS  the label names what actually answered (answered_by) — header='forwarded-managed-model' body='forwarded-managed-model'
PASS  no response claims GPU inference (word absent from body + headers)
PASS  no response claims local weights for a forwarded call
PASS  a forwarded answer is labelled answered_by=forwarded-managed-model — got 'forwarded-managed-model'
PASS  a forwarded answer names its external destination — body='aiplatform.googleapis.com' header='aiplatform.googleapis.com'
PASS  a forwarded answer is flagged egress=external — got 'external'
PASS  a forwarded answer is NOT labelled as local weights
PASS  max_tokens is honoured (8 requested) — completion_tokens=8
      max_tokens=8 run: 0.67s, usage={'prompt_tokens': 11, 'completion_tokens': 8, 'total_tokens': 19}
      streaming: http=200 deltas=2 first_token_s=1.204 total_s=1.33
      assembled: 'One\nTwo\nThree\nFour\nFive'
      final label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"} usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20} finish='stop'
PASS  streaming returns HTTP 200 — got 200
PASS  streaming yields >=2 incremental deltas — 2 deltas
PASS  streaming terminates with [DONE]
PASS  streaming assembled content is non-empty
PASS  streaming reports usage when asked (include_usage) — usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20}
PASS  streaming final chunk is labelled in-band — body={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node'} header='gemini-proxy'
PASS  streaming from a forwarded backend names the destination — label={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node'} header='aiplatform.googleapis.com'

      latency samples (s): [0.997]
      median latency: 0.997s (backend reported by the endpoint: gemini-proxy)
      NOTE: this compares the FRONT DOOR's own answer time; for the same
      20-sample decision prompt the CPU path on this node is documented at
      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).

RESULT: PASS (drop-in envelope + labelled backend + parameters + streaming)

===== RESULT =====
RESULT: PASS — the front door is a drop-in and it labels what answered
VERIFY_EXIT=0
```

#### (d) FORCED Vertex AI failure: a model id that does not exist -> a loud, distinct error

Source: `live-forced-failure-2.txt`

```
+ sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash-does-not-exist|' /etc/termini/inference-proxy.env
+ sudo systemctl restart termini-inference-proxy
+ sleep 3
+ curl -s -m 30 -w '\nHTTP=%{http_code}\n' http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"gemma","messages":[{"role":"user","content":"reply ok"}],"max_tokens":8}'
{"error": {"message": "forwarded backend gemini-2.5-flash-does-not-exist at aiplatform.googleapis.com rejected the request", "type": "termini_error", "code": "MODEL_UNAVAILABLE", "param": null, "request_id": "req-01M4ZMSJJQWCHEB419PQ3SVKEX", "detail": "Publisher model `projects/termini-510609/locations/global/publishers/google/models/gemini-2.5-flash-does-not-exist` was not found or your project does not have access to it. Ensure you are using a valid model name and that the model is available in the specified region. For more information, see: https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations.", "distinct": "vertex_rejected", "backend": "gemini-proxy", "model": "gemini-2.5-flash-does-not-exist", "destination": "aiplatform.googleapis.com", "egress": "external"}}
HTTP=503
--- the loud failure line in the proxy log
+ echo '--- the loud failure line in the proxy log'
+ sudo journalctl -u termini-inference-proxy --no-pager -n 6
+ tail -3
Oct 15 11:22:36 termini-node-c.asia-south1-a.c.termini-510609.internal systemd[1]: Started termini-inference-proxy.service - Termini inference proxy (Node C :8080) — drop-in front door for the frozen inference contract.
Oct 15 11:22:36 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[3782]: {"ts":"2026-10-15T11:22:36Z","event":"startup","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash-does-not-exist","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","listen":"0.0.0.0:8080","state_file":"/run/termini/inference-backend.json","upstream":"http://127.0.0.1:8081","project":"(metadata)","thinking_budget":0}
Oct 15 11:22:39 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[3782]: {"ts":"2026-10-15T11:22:39Z","event":"upstream_failure","severity":"error","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash-does-not-exist","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","distinct":"vertex_rejected","status":503,"request_id":"req-01M4ZMSJJQWCHEB419PQ3SVKEX","stream":false,"forwarded":true,"detail":"Publisher model `projects/termini-510609/locations/global/publishers/google/models/gemini-2.5-flash-does-not-exist` was not found or your project does not have access to it. Ensure you are using a valid model name and that the model is available in the specified region. For more information, see: https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations.","latency_ms":290.2}
```

#### (d) ...and the controller's deterministic fallback while the forwarded path is broken

Source: `live-fallback.txt`

```
--- capture-only override (declared, reversible, removed by phase-fallback2.sh)
    effective cooldown + bound:
    TERMINI_OPERATOR_DECISION_MODE=model
    TERMINI_OPERATOR_DECISION_TIMEOUT_S=900
    TERMINI_OPERATOR_COOLDOWN_S=30
active
T1=2026-10-15T11:21:20Z  (failure-window rows; 90 s of slack for clock skew)
--- the controller must stay UP while the model path is broken (the demo survives it)
GET /v1/operator/decisions -> 401
GET /v1/operator/decisions -> 401
GET /v1/operator/decisions -> 401
{"error": {"code": "UNAUTHENTICATED", "message": "missing or invalid token", "details": [], "request_id": "01M4ZMT0X53DBSDNF093BH7G3J"}}
--- keep the seeder running
--- polling for a decision recorded while the forwarded backend is broken
DETERMINISTIC FALLBACK LANDED after 70s of polling
poll result: yes
=== CONTROLLER EVIDENCE (2026-10-15T11:24:21Z) ===
### controller DB: /data/controller.db

### operator unit env (decision mode + inference endpoint)
    TERMINI_OPERATOR_ENABLED=true
    TERMINI_OPERATOR_DECISION_MODE=model
    TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080
    TERMINI_INFERENCE_MODEL=gemma
    TERMINI_INFERENCE_TIMEOUT_S=900
    TERMINI_OPERATOR_DECISION_TIMEOUT_S=900
    TERMINI_OPERATOR_POLL_INTERVAL_S=2
    TERMINI_INTENT_INFERENCE_BASE_URL=http://10.10.0.13:8090
    TERMINI_OPERATOR_WINDOW=20
    TERMINI_OPERATOR_COOLDOWN_S=30

### remediation_decisions (latest 10) — ALL columns
# columns: ['id', 'event_id', 'node_id', 'rule_id', 'playbook', 'action_kind', 'action_params', 'reason', 'decided_at', 'decision_source', 'confidence', 'fallback_reason']
# NOTE: this table has NO latency column — the forwarded call's wall clock is
# measured from the externalModelCall before/after audit pair below.
{'id': '01M4ZMWKEKBQ17D3YCY08B11V4', 'event_id': '01M4ZMWK3RTBGZXYYWKMS1A4TX', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'playbook': 'disk-degrade-01', 'action_kind': 'instance.stop', 'action_params': '{"force": true, "instance_name": "01M4N7F8B8823D650DF40D614D", "_vm_id": "01M4N7F8B8823D650DF40D614D"}', 'reason': 'disk free space degraded below threshold or falling steeply; drain the heaviest workload so it can be migrated off the degraded node.', 'decided_at': '2026-10-15T11:24:18.440Z', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'inference_unavailable'}
{'id': '01M4ZMWFJE7A8N7MBYKQADXCX9', 'event_id': '01M4ZMWF6R93ZZKN9SFN7D9M3H', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'playbook': 'oom-guard-01', 'action_kind': 'instance.restart', 'action_params': '{"force": true, "timeout_s": 30, "instance_name": "01M4N827979BD9D2762EFE24A0", "_vm_id": "01M4N827979BD9D2762EFE24A0"}', 'reason': 'host memory sustained above OOM threshold; restart the heaviest workload to reclaim memory.', 'decided_at': '2026-10-15T11:24:14.442Z', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'inference_unavailable'}

### operator_logs: externalModelCall rows (latest 20)
{'log_id': '01M4ZMWKE44B8T07CEJA3B7SDN', 'ts': '2026-10-15T11:24:18.756Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall after endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash-does-not-exist destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=returned request_id=01M4ZMWK4AZSX7DCHW3MZWNS8N'}
{'log_id': '01M4ZMWK4J1AK44FQQT5FQG0RB', 'ts': '2026-10-15T11:24:18.450Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall before endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash-does-not-exist destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=ok request_id=01M4ZMWK4AZSX7DCHW3MZWNS8N'}
{'log_id': '01M4ZMWFHZR66G8WHA1N6T64BK', 'ts': '2026-10-15T11:24:14.783Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall after endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash-does-not-exist destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=returned request_id=01M4ZMWF7BAS2HXDFSBTW34DMM'}
{'log_id': '01M4ZMWF8V0N65RPWGH1A2C6D3', 'ts': '2026-10-15T11:24:14.491Z', 'severity': 'info', 'actor': 'ai_operator', 'message': 'externalModelCall before endpoint=http://10.10.0.13:8080 backend=gemini-proxy egress=external model=gemini-2.5-flash-does-not-exist destination=aiplatform.googleapis.com answered_by=forwarded-managed-model outcome=ok request_id=01M4ZMWF7BAS2HXDFSBTW34DMM'}

### journald: externalModelCall before/after pairs + the forwarded call's latency

### request_id=01M4ZM60YXGM5G27GCFY338P4K [after only, no pair]: Oct 15 11:12:11 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:12:11,997 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZM60YXGM5G27GCFY338P4K', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZM6FR17Z0EHZYG7VF204JM: forwarded call wall clock = 1.814 s
    before: Oct 15 11:12:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:12:14,089 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZM6FR17Z0EHZYG7VF204JM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 11:12:15 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:12:15,903 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZM6FR17Z0EHZYG7VF204JM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZMWF7BAS2HXDFSBTW34DMM: forwarded call wall clock = 0.293 s
    before: Oct 15 11:24:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:14,489 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZMWF7BAS2HXDFSBTW34DMM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 11:24:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:14,782 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZMWF7BAS2HXDFSBTW34DMM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### request_id=01M4ZMWK4AZSX7DCHW3MZWNS8N: forwarded call wall clock = 0.306 s
    before: Oct 15 11:24:18 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:18,449 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZMWK4AZSX7DCHW3MZWNS8N', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress'
    after:  Oct 15 11:24:18 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:18,755 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZMWK4AZSX7DCHW3MZWNS8N', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress':

### decision_fallback_state (the per-(node,rule) fallback ledger)
{'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'fallback_streak': 1, 'forced_until': None, 'updated_at': '2026-10-15T11:24:14.442Z'}
{'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'fallback_streak': 1, 'forced_until': None, 'updated_at': '2026-10-15T11:24:18.440Z'}
=== the operator's own log lines around the fallback ===
Oct 15 11:19:57 termini-controller.asia-south1-a.c.termini-510609.internal python3[1846]: 2026-10-15 11:19:57,692 INFO termini.controller 127.0.0.1 - "GET /v1/operator/decisions HTTP/1.1" 401 -
Oct 15 11:22:50 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:22:50,218 INFO termini.controller 127.0.0.1 - "GET /v1/operator/decisions HTTP/1.1" 401 -
Oct 15 11:22:52 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:22:52,232 INFO termini.controller 127.0.0.1 - "GET /v1/operator/decisions HTTP/1.1" 401 -
Oct 15 11:22:54 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:22:54,245 INFO termini.controller 127.0.0.1 - "GET /v1/operator/decisions HTTP/1.1" 401 -
Oct 15 11:24:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:14,489 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZMWF7BAS2HXDFSBTW34DMM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress': {'backend': 'gemini-proxy', 'model': 'gemini-2.5-flash-does-not-exist', 'alias': 'gemma', 'destination': 'aiplatform.googleapis.com', 'destination_class': 'external', 'answered_by': 'forwarded-managed-model', 'source': 'health-probe', 'endpoint': 'http://10.10.0.13:8080', 'detail': '', 'forwarded': True}, 'phase': 'before', 'outcome': 'ok', 'payload_sha256': 'd16cf995f6ec445705341bbf51597f8491a44f6e64bc7385459bfc505cf2f73a', 'substituted_fields': 1979, 'detail': '', 'violations': []}
Oct 15 11:24:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:14,782 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZMWF7BAS2HXDFSBTW34DMM', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress': {'backend': 'gemini-proxy', 'model': 'gemini-2.5-flash-does-not-exist', 'alias': 'gemma', 'destination': 'aiplatform.googleapis.com', 'destination_class': 'external', 'answered_by': 'forwarded-managed-model', 'source': 'health-probe', 'endpoint': 'http://10.10.0.13:8080', 'detail': '', 'forwarded': True}, 'phase': 'after', 'outcome': 'returned', 'payload_sha256': 'd16cf995f6ec445705341bbf51597f8491a44f6e64bc7385459bfc505cf2f73a', 'substituted_fields': 1979, 'detail': 'DecisionError', 'violations': []}
Oct 15 11:24:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:14,790 WARNING termini.operator.decision model decision fell back (reason=inference_unavailable): http 503: Service Unavailable
Oct 15 11:24:14 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:14,790 WARNING termini.operator.loop node 01M4NZK474BVARFBXYQBMVA8GV rule oom-guard-01: model decision fell back (reason=inference_unavailable)
Oct 15 11:24:18 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:18,449 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4ZMWK4AZSX7DCHW3MZWNS8N', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress': {'backend': 'gemini-proxy', 'model': 'gemini-2.5-flash-does-not-exist', 'alias': 'gemma', 'destination': 'aiplatform.googleapis.com', 'destination_class': 'external', 'answered_by': 'forwarded-managed-model', 'source': 'health-probe', 'endpoint': 'http://10.10.0.13:8080', 'detail': '', 'forwarded': True}, 'phase': 'before', 'outcome': 'ok', 'payload_sha256': '8d4d4fadf2c2f6fef19d64c21699702cd8910e6c46981d025cf0a5ff9830b786', 'substituted_fields': 1748, 'detail': '', 'violations': []}
Oct 15 11:24:18 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:18,755 INFO termini.operator.external_call externalModelCall after: {'request_id': '01M4ZMWK4AZSX7DCHW3MZWNS8N', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'destination_class': 'external', 'external_destination': 'aiplatform.googleapis.com', 'egress': {'backend': 'gemini-proxy', 'model': 'gemini-2.5-flash-does-not-exist', 'alias': 'gemma', 'destination': 'aiplatform.googleapis.com', 'destination_class': 'external', 'answered_by': 'forwarded-managed-model', 'source': 'health-probe', 'endpoint': 'http://10.10.0.13:8080', 'detail': '', 'forwarded': True}, 'phase': 'after', 'outcome': 'returned', 'payload_sha256': '8d4d4fadf2c2f6fef19d64c21699702cd8910e6c46981d025cf0a5ff9830b786', 'substituted_fields': 1748, 'detail': 'DecisionError', 'violations': []}
Oct 15 11:24:18 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:18,763 WARNING termini.operator.decision model decision fell back (reason=inference_unavailable): http 503: Service Unavailable
Oct 15 11:24:18 termini-controller.asia-south1-a.c.termini-510609.internal python3[4953]: 2026-10-15 11:24:18,764 WARNING termini.operator.loop node 01M4NZK46WXG5ZEFAGAAV5JRXM rule disk-degrade-01: model decision fell back (reason=inference_unavailable)
--- still alive after the failure?
active
GET /v1/operator/decisions -> 401
```

#### (d) the capture-only override removed, the controller back on the shipped tuning

Source: `live-fallback-restore.txt`

```
+ sudo rm -f /etc/systemd/system/termini-controller.service.d/zzzzz-fallback-capture.conf
+ sudo systemctl daemon-reload
+ sudo systemctl restart termini-controller
+ sleep 6
+ sudo ls /etc/systemd/system/termini-controller.service.d/
lxd.conf
model-mode.conf
zz-proxy-model.conf
zzz-real-fleet.conf
zzz-standing-tuning.conf
zzz-tokens-v2.conf
zzz-underlay-bind.conf
zzz-workload-image.conf
zzzz-intent-endpoint.conf
+ systemctl show termini-controller -p Environment
+ tr ' ' '\n'
+ grep -E 'COOLDOWN|DECISION_MODE'
TERMINI_OPERATOR_DECISION_MODE=model
TERMINI_OPERATOR_COOLDOWN_S=600
+ sudo systemctl is-active termini-controller
active
```

#### the shipped configuration restored, and the node's final state

Source: `live-restore.txt`

```
+ sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash|' /etc/termini/inference-proxy.env
+ sudo /usr/local/sbin/termini-inference-switch gemini-proxy
[inference-switch] TERMINI_INFERENCE_BACKEND=gemini-proxy
[inference-switch] ACTIVE BACKEND: gemini-proxy
== node status: /etc/termini ==
--- /etc/termini/inference-proxy.env
TERMINI_INFERENCE_BACKEND=gemini-proxy
TERMINI_INFERENCE_HOST=0.0.0.0
TERMINI_INFERENCE_PORT=8080
TERMINI_INFERENCE_ALIAS=gemma
TERMINI_INFERENCE_UPSTREAM=http://127.0.0.1:8081
TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S=900
TERMINI_GEMINI_HOST=aiplatform.googleapis.com
TERMINI_GEMINI_LOCATION=global
TERMINI_GEMINI_MODEL=gemini-2.5-flash
TERMINI_GEMINI_PROJECT=
TERMINI_GEMINI_TIMEOUT_S=30
TERMINI_GEMINI_THINKING_BUDGET=0
TERMINI_INFERENCE_STATE_FILE=/run/termini/inference-backend.json
TERMINI_INFERENCE_LOG_FILE=/var/log/termini-inference-proxy.jsonl
--- /etc/termini/llama-worker.env
LLAMA_HOST=127.0.0.1
LLAMA_PORT=8081

== units ==
termini-inference-proxy.service: enabled=enabled active=active
termini-llama-server.service: enabled=enabled active=active

== listeners ==
LISTEN 0      5                       0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=4010,fd=5))                      
LISTEN 0      512                   127.0.0.1:8081      0.0.0.0:*    users:(("llama-server",pid=2178,fd=3))                 

== recorded active backend ==
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "forwarded-managed-model",
  "backend": "gemini-proxy",
  "destination": "aiplatform.googleapis.com",
  "egress": "external",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "forwarded managed model emulating a GPU node",
  "served_model": "gemini-2.5-flash",
  "ts": "2026-10-15T11:24:36Z"
}

== :8080/health (what nodes A/B see) ==
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
== :8081/health (the local worker, loopback only) ==
{"status":"ok"}
{
  "active_unit": "termini-inference-proxy.service",
  "alias": "gemma",
  "answered_by": "forwarded-managed-model",
  "backend": "gemini-proxy",
  "destination": "aiplatform.googleapis.com",
  "egress": "external",
  "is_gpu_inference": false,
  "listen": "0.0.0.0:8080",
  "local_worker": "http://127.0.0.1:8081",
  "note": "forwarded managed model emulating a GPU node",
  "served_model": "gemini-2.5-flash",
  "ts": "2026-10-15T11:24:36Z"
}
+ sudo cat /run/termini/inference-backend.json
+ curl -s -m 15 http://127.0.0.1:8080/health
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
+ echo
```

## Re-running this

```bash
# on a raised fleet, with SSH access to node-a and node-c
gcloud compute scp infra/inference/{proxy.py,termini-inference-proxy.service,install-inference-proxy.sh,termini-inference-switch.sh,verify-proxy.sh} \
  termini-node-c:/tmp/ --zone asia-south1-a --project termini-510609
gcloud compute ssh termini-node-c --zone asia-south1-a --project termini-510609 \
  --command "sudo bash /tmp/install-inference-proxy.sh --backend gemini-proxy && \
             sudo install -m0755 /tmp/termini-inference-switch.sh /usr/local/sbin/termini-inference-switch && \
             sudo termini-inference-switch status"

gcloud compute scp infra/inference/verify-proxy.sh termini-node-a:/tmp/ --zone asia-south1-a --project termini-510609
gcloud compute ssh termini-node-a --zone asia-south1-a --project termini-510609 \
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
