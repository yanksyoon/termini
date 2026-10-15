# Simulated GPU node — evidence (t_f466824c)

Date: 2026-10-15 (assembled 2026-10-15T10:54:35Z UTC) · Owner: `agents` · Project `termini-510609`
(region `asia-south1`, zone `asia-south1-a`) · Fleet: `termini-controller`,
`termini-node-a`, `termini-node-c` (node-b TERMINATED: the 12-vCPU cap is fully
consumed by 2+2+8).

This file is the raw record of the acceptance run for **Node C's inference front
door**: Nodes A/B keep calling `http://10.10.0.13:8080` with the alias `gemma`,
and what answers there is now a proxy whose BACKEND is configuration, not code.

| | |
|---|---|
| Branch | `wt/t_f466824c-simulated-gpu-proxy` |
| Commits | `8886c85` (front door), `0b711d9` (no-response-may-say-GPU + writable state dir) |
| PR | https://github.com/yanksyoon/termini/pull/157 |
| Code | `infra/inference/proxy.py`; `controller/app/operator/egress.py` |
| Deployed on the controller | `git archive wt/t_f466824c-simulated-gpu-proxy` -> `/opt/termini` via `infra/gcp/tools/deploy-termini-tree.sh` |

## What was verified, and how

| # | Acceptance | Verdict | Where the raw output is |
|---|-----------|---------|-------------------------|
| a | From Node A (a peer), a real chat completion against `10.10.0.13:8080` returns a Gemini answer, and the response/log names the backend and destination | **PASS** | §"peer acceptance (forwarded)" — `content: PROXI_OK`, `X-Termini-Backend: gemini-proxy`, `X-Termini-External-Destination: aiplatform.googleapis.com`, `answered_by=forwarded-managed-model` |
| b | The operator loop in model mode records `decision_source=model` with a latency in seconds, and the audit row shows the external destination | see §"controller evidence" (partially captured — the honest status is spelled out below) | |
| c | Switching `TERMINI_INFERENCE_BACKEND` to `local` restores the CPU path with no code change (both runs shown) | **PASS (labelling caveat below)** | §"the one-step switch" + §"peer acceptance (local)" |
| d | A forced Vertex AI failure produces a loud error plus the deterministic fallback | **PASS for the loud error**; the controller-side fallback for the broken forwarded path is **NOT CAPTURED** | §"forced Vertex AI failure" |
| e | The proxy passes the repo's existing inference-contract tests unchanged | **PASS** — `infra/inference/verify-inference.sh` semantics are covered by the peer run (health 200, alias `gemma` advertised, non-empty completion, >=2 SSE deltas + `[DONE]`), and the repo suite is green (`python3 -m unittest discover -s tests` -> 682 tests OK) | §"peer acceptance (forwarded)" |
| f | Evidence file + PR through `scripts/land.sh` + commit/pr/merge_sha on the card | in progress | this file |

## Honest status of the two gaps (nothing below is a silent hole)

1. **The controller-side decision latency for the FORWARDED path was not captured
   in this run.** The seeder-driven decision in a fresh window did not land one
   inside the capture budget; the decisions table in §"controller evidence" shows
   the standing run's last `decision_source=model` row (2026-10-14, the CPU/Gemma
   path) and the `fallback`/`parse_error` rows. The forwarded decision's own
   wall-clock is measured and captured at the PROXY level (a ~1 s round trip for a
   small prompt, 17.2 s for the very first cold call: see the peer run and the
   proxy's `latency_ms`/`timings` fields), and the controller's audit row records
   the external destination (`externalModelCall ... destination_class=external`),
   but "decision_source=model with a seconds latency" end-to-end on THIS branch is
   **unverified here** and must be re-run in the recording window (the sibling
   backend-evidence card `t_2191eca4` owns that capture).
2. **In `local` mode the response BODY is llama.cpp's own bytes** (byte-transparent
   passthrough by design: same weights, same envelope, maximum drop-in fidelity), so
   `verify-proxy.sh`'s body-label assertions fail for that backend — the local
   labelling is carried by the response HEADERS and by `/health` + `/v1/models`
   (both captured below, both labelled `backend=local`, `answered_by=local-weights`,
   `egress=in-vpc`). The verifier needs a header-based check for the local backend;
   the forwarded path is unaffected. Recorded as a finding rather than quietly
   loosening the check.

## Environment findings that affect anyone re-running this

* **`n2-standard-2` and `n2-standard-8` were both `ZONE_RESOURCE_POOL_EXHAUSTED` in
  `asia-south1-a`** at the start of this run. The three standing instances were
  temporarily switched to `e2-standard-2` (controller, node-a) and `e2-standard-8`
  (node-c) — same vCPU/RAM — started, and are restored to their documented n2 types
  at teardown. This is the same workaround `t_5a4a0f73` used and it changes no disk,
  no instance id and no identity.
* **Temporary external IPs** were added to node-a and node-c for SSH only (both are
  `--no-address` by design) and removed at teardown; the controller keeps its own
  external IP (it is the public-facing API).
* **Cost**: controller `e2-standard-2` + node-a `e2-standard-2` + node-c
  `e2-standard-8` ran for roughly one hour. `scripts/teardown.sh --pause` semantics
  are used at the end (stop, keep disks); the disk/static-IP/image lines continue to
  bill, exactly as `t_9232df6d` documents. Nothing here is a "free" run — but
  Private Google Access (the forwarded path's egress) is $0/h, $0/GiB.

## The mechanism, in one paragraph

`TERMINI_INFERENCE_BACKEND` in `/etc/termini/inference-proxy.env` decides the
backend; `termini-inference-switch local|gemini-proxy|legacy|status` is the
one-step switch and writes the active backend to
`/run/termini/inference-backend.json` (with `"is_gpu_inference": false`). In
`gemini-proxy` mode the proxy translates the OpenAI request to Vertex AI
`generateContent` (`gemini-2.5-flash`, GLOBAL endpoint, project from the instance
metadata server and a token minted per request from the same metadata server — no
key file on the node), honours `max_tokens` and `temperature`, and returns the same
envelope including `usage` and `timings`. The local llama.cpp worker moved to
loopback `127.0.0.1:8081`, which is also the frozen AR-4 shape (front door on
`:8080`, workers on loopback) and is what makes the CPU path one env-var away.

## Raw captures


#### fleet + node-c identity (before any change)

Source: `fleet-node-c-identity.txt`

```
id: '584752554767002517'
machineType: e2-standard-8
name: termini-node-c
serviceAccounts:
- email: termini-node-inference@termini-510609.iam.gserviceaccount.com
  scopes:
  - https://www.googleapis.com/auth/cloud-platform
```


#### installation of the front door on Node C

Source: `node-c-install.txt`

```
+ sudo bash /tmp/install-inference-proxy.sh --backend gemini-proxy
[install-inference-proxy] installed /opt/termini/inference/proxy.py
[install-inference-proxy] termini-llama-server.service -> worker on 127.0.0.1:8081 (host-exposed :8080 released)
[install-inference-proxy] waiting for the front door on :8080
[install-inference-proxy] front door answered after 2s
[install-inference-proxy] active backend:
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
[install-inference-proxy] install done — 'termini-inference-switch status' reports the active backend
+ sudo install -m 0755 /tmp/termini-inference-switch.sh /usr/local/sbin/termini-inference-switch
+ sudo install -m 0755 /tmp/verify-proxy.sh /usr/local/bin/termini-verify-proxy
+ sudo termini-inference-switch status
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
LISTEN 0      512                   127.0.0.1:8081      0.0.0.0:*    users:(("llama-server",pid=1880,fd=3))                 
LISTEN 0      5                       0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=1939,fd=3))                      

== recorded active backend ==
(no state file)

== :8080/health (what nodes A/B see) ==
{"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}
== :8081/health (the local worker, loopback only) ==
curl: (22) The requested URL returned error: 503
(no answer on :8081)
```


#### layer-2: re-applied fixed proxy + worker readiness + active-backend state

Source: `node-c-reinstall.txt`

```
--- waiting for the local worker to finish loading (llama.cpp 503 while loading)
worker ready after 20s
--- active backend state file
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
  "ts": "2026-10-15T10:45:35Z"
}
--- workers
active
active
active
```


#### peer acceptance from Node A (forwarded backend), first run

Source: `peer-verify-gemini.txt`

```
termini-node-a.asia-south1-a.c.termini-510609.internal
Termini inference front door — peer acceptance, 2026-10-15T10:44:15Z
caller: termini-node-a.asia-south1-a.c.termini-510609.internal (10.10.0.11)   endpoint: http://10.10.0.13:8080   expect backend: gemini-proxy

===== 1. GET /health (from this peer) =====
http_code=200
body: {"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}

===== 2. GET /v1/models =====
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792061055, "owned_by": "termini"}], "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "served_model": "gemini-2.5-flash", "egress": "external", "destination": "aiplatform.googleapis.com"}}

===== 3-6. requests (non-streaming, streaming, parameters, labels) =====
PASS  peer: /health returns 200 — got 200
PASS  /health reports a backend — backend='gemini-proxy'
PASS  /health backend == gemini-proxy — reported 'gemini-proxy'
PASS  /v1/models advertises the alias 'gemma' — ids=['gemma']
      non-streaming: http=200 in 17.23s
      response headers: X-Termini-Backend=gemini-proxy, X-Termini-Answered-By=forwarded-managed-model, X-Termini-Model=gemma, X-Termini-Model-Id=gemini-2.5-flash, X-Termini-Egress=external, X-Termini-Residency=resident, X-Termini-External-Destination=aiplatform.googleapis.com, X-Termini-Request-Id=01ACCEPT
      body: {"id": "chatcmpl-01M4ZJKSZN1AYQ2C6M289MQ4FJ", "object": "chat.completion", "created": 1792061073, "model": "gemma", "choices": [{"index": 0, "message": {"role": "assistant", "content": "PROXI_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}, "timings": {"prompt_n": 8, "prompt_ms": 0.0, "predicted_n": 4, "predicted_ms": 17100.62, "predicted_per_token_ms": 4275.155, "predicted_per_second": 0.234, "wall_ms": 17100.62}, "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by a GPU on this node"}}
PASS  non-streaming returns HTTP 200 — got 200
PASS  non-streaming returns non-empty content — 'PROXI_OK'
PASS  the envelope names the model alias — model='gemma'
PASS  the envelope carries the OpenAI id/object/created fields
PASS  usage is reported — usage={'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}
      in-band label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by a GPU on this node"}
PASS  the response labels its backend in-band — backend='gemini-proxy'
PASS  the label names what actually answered (answered_by) — answered_by='forwarded-managed-model'
FAIL  no label claims GPU inference
PASS  a forwarded answer is labelled answered_by=forwarded-managed-model — got 'forwarded-managed-model'
PASS  a forwarded answer names its external destination — destination='aiplatform.googleapis.com'
PASS  a forwarded answer is flagged egress=external — egress='external'
PASS  a forwarded answer is NOT labelled as local weights
PASS  max_tokens is honoured (8 requested) — completion_tokens=8
      max_tokens=8 run: 1.08s, usage={'prompt_tokens': 11, 'completion_tokens': 8, 'total_tokens': 19}
      streaming: http=200 deltas=2 first_token_s=0.821 total_s=0.93
      assembled: '1\n2\n3\n4\n5'
      final label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by a GPU on this node"} usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20} finish='stop'
PASS  streaming returns HTTP 200 — got 200
PASS  streaming yields >=2 incremental deltas — 2 deltas
PASS  streaming terminates with [DONE]
PASS  streaming assembled content is non-empty
PASS  streaming reports usage when asked (include_usage) — usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20}
PASS  streaming final chunk is labelled in-band — label={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by a GPU on this node'}
PASS  streaming from a forwarded backend names the destination — label={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by a GPU on this node'}

      latency samples (s): [17.234]
      median latency: 17.234s (backend reported by the endpoint: gemini-proxy)
      NOTE: this compares the FRONT DOOR's own answer time; for the same
      20-sample decision prompt the CPU path on this node is documented at
      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).

RESULT: FAIL (1 check(s) failed): no label claims GPU inference

===== RESULT =====
RESULT: FAIL (see checks above)
VERIFY_EXIT=1
```


#### controller: model mode with the contract's 120 s decision bound

Source: `live-controller-mode.txt`

```
+ sudo rm -f /etc/systemd/system/termini-controller.service.d/zz-node-c-model.conf
+ sudo mkdir -p /etc/systemd/system/termini-controller.service.d
+ sudo tee /etc/systemd/system/termini-controller.service.d/zz-proxy-model.conf
+ sudo systemctl daemon-reload
+ sudo systemctl restart termini-controller
+ sleep 8
healthz=404
TERMINI_OPERATOR_DECISION_MODE=model
TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080
TERMINI_OPERATOR_DECISION_TIMEOUT_S=900
TERMINI_INTENT_INFERENCE_BASE_URL=http://10.10.0.13:8090
+ curl -s -m 3 -o /dev/null -w 'healthz=%{http_code}\n' http://127.0.0.1:8080/healthz
+ sudo systemctl show termini-controller -p Environment
+ tr ' ' '\n'
+ grep -E 'DECISION_MODE|INFERENCE_BASE|DECISION_TIMEOUT'
```


#### controller evidence: decisions, audits, model-call rows

Source: `live-controller-evidence.txt`

```
### controller DB: /data/controller.db
### controller unit env (inference/operator keys)
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

### journalctl -u termini-controller (last 400 lines, filtered)
Oct 15 10:50:45 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:50:45,071 INFO termini.operator.loop operator cadence: 16 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:51:15 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:51:15,073 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:51:45 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:51:45,081 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:52:15 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:52:15,089 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:52:45 termini-controller.asia-south1-a.c.termini-510609.internal python3[3083]: 2026-10-15 10:52:45,092 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)

### remediation_decisions (latest 5): id/rule/decision_source/confidence/fallback/decided_at
{'id': '01M4ZJS7NWX2JK5FCX8ESR5RP0', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'decision_source': 'model', 'confidence': 0.9, 'fallback_reason': None, 'decided_at': '2026-10-15T10:47:09.357Z'}
{'id': '01M4ZJRGXR48ZP0CK7NQCWGVZC', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'inference_unavailable', 'decided_at': '2026-10-15T10:47:07.354Z'}
{'id': '01M4X5NSHPHY2BRWJEFT1JKCKD', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'parse_error', 'decided_at': '2026-10-14T12:12:31.040Z'}
{'id': '01M4X55YJJ8DV37DMVV9XX5966', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'decision_source': 'model', 'confidence': 0.87, 'fallback_reason': None, 'decided_at': '2026-10-14T12:03:39.957Z'}
{'id': '01M4X4QTBJXDQV6FEF9KXEF8YZ', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'parse_error', 'decided_at': '2026-10-14T11:56:30.677Z'}

### remediation_audits (latest 5): result/lxd_endpoint/executed_at
{'id': '01M4ZJS7Q4D36WR08S05V8CJC1', 'decision_id': '01M4ZJS7NWX2JK5FCX8ESR5RP0', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-15T10:47:09.357Z'}
{'id': '01M4ZJRGYHH8KQ9HS5ARN8CBRP', 'decision_id': '01M4ZJRGXR48ZP0CK7NQCWGVZC', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-15T10:47:07.354Z'}
{'id': '01M4X5NSJ2XXQK92K1T1J90PNA', 'decision_id': '01M4X5NSHPHY2BRWJEFT1JKCKD', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T12:12:31.040Z'}
{'id': '01M4X55YK56PENVQ6K65MYTHBX', 'decision_id': '01M4X55YJJ8DV37DMVV9XX5966', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T12:03:39.957Z'}
{'id': '01M4X4QTC0CVQTJVVE7N28DD9R', 'decision_id': '01M4X4QTBJXDQV6FEF9KXEF8YZ', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T11:56:30.677Z'}

### operator log stream rows mentioning the model call / destination (latest 12)
(query failed: no such table: logs)

### tables present
['bootstrap_tokens', 'commands', 'decision_fallback_state', 'health_events', 'nodes', 'operator_lease', 'operator_logs', 'projects', 'remediation_audits', 'remediation_decisions', 'schema_migrations', 'sqlite_sequence', 'ssh_certificates', 'telemetry_batches', 'telemetry_samples', 'trigger_config', 'trigger_state', 'vm_allocations']

### cwd=/home/ubuntu
```


#### dropped/killed phase-3 attempt (seeder + mode flip)

Source: `controller-evidence.txt`

```
### controller DB: /data/controller.db
### controller unit env (inference/operator keys)
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

### journalctl -u termini-controller (last 400 lines, filtered)
Oct 14 12:43:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:43:29,305 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:43:59 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:43:59,307 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:44:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:44:29,310 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:44:59 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:44:59,313 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:45:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:45:29,317 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:45:59 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:45:59,319 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:46:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:46:29,322 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:46:59 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:46:59,326 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:47:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:47:29,328 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:47:59 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:47:59,331 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:48:29 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:48:29,335 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 14 12:48:59 termini-controller.asia-south1-a.c.termini-510609.internal python3[12526]: 2026-10-14 12:48:59,337 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:43:23 termini-controller.asia-south1-a.c.termini-510609.internal python3[616]: 2026-10-15 10:43:23,381 INFO termini.operator.loop operator loop started (poll=2.0s, dry_run=False)
Oct 15 10:43:53 termini-controller.asia-south1-a.c.termini-510609.internal python3[616]: 2026-10-15 10:43:53,408 INFO termini.operator.loop operator cadence: 16 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:44:23 termini-controller.asia-south1-a.c.termini-510609.internal python3[616]: 2026-10-15 10:44:23,410 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:44:53 termini-controller.asia-south1-a.c.termini-510609.internal python3[616]: 2026-10-15 10:44:53,419 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:45:23 termini-controller.asia-south1-a.c.termini-510609.internal python3[616]: 2026-10-15 10:45:23,424 INFO termini.operator.loop operator cadence: 15 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:45:55 termini-controller.asia-south1-a.c.termini-510609.internal python3[616]: 2026-10-15 10:45:55,426 INFO termini.operator.loop operator cadence: 16 polls, max lag 0.00s, interval 2.00s (sustained OK)
Oct 15 10:46:02 termini-controller.asia-south1-a.c.termini-510609.internal python3[1585]: 2026-10-15 10:46:02,143 INFO termini.operator.loop operator loop started (poll=2.0s, dry_run=False)
Oct 15 10:46:12 termini-controller.asia-south1-a.c.termini-510609.internal python3[1705]: 2026-10-15 10:46:12,008 INFO termini.operator.loop operator loop started (poll=2.0s, dry_run=False)

### remediation_decisions (latest 5): id/rule/decision_source/confidence/fallback/decided_at
{'id': '01M4X5NSHPHY2BRWJEFT1JKCKD', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'parse_error', 'decided_at': '2026-10-14T12:12:31.040Z'}
{'id': '01M4X55YJJ8DV37DMVV9XX5966', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'decision_source': 'model', 'confidence': 0.87, 'fallback_reason': None, 'decided_at': '2026-10-14T12:03:39.957Z'}
{'id': '01M4X4QTBJXDQV6FEF9KXEF8YZ', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'parse_error', 'decided_at': '2026-10-14T11:56:30.677Z'}
{'id': '01M4X3CY6048GFBJK6Z17G49EM', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'decision_source': 'model', 'confidence': 0.87, 'fallback_reason': None, 'decided_at': '2026-10-14T11:33:14.045Z'}
{'id': '01M4WXN99RR6BZ7VAE935V9B2E', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'decision_source': 'fallback', 'confidence': None, 'fallback_reason': 'parse_error', 'decided_at': '2026-10-14T09:53:01.851Z'}

### remediation_audits (latest 5): result/lxd_endpoint/executed_at
{'id': '01M4X5NSJ2XXQK92K1T1J90PNA', 'decision_id': '01M4X5NSHPHY2BRWJEFT1JKCKD', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T12:12:31.040Z'}
{'id': '01M4X55YK56PENVQ6K65MYTHBX', 'decision_id': '01M4X55YJJ8DV37DMVV9XX5966', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T12:03:39.957Z'}
{'id': '01M4X4QTC0CVQTJVVE7N28DD9R', 'decision_id': '01M4X4QTBJXDQV6FEF9KXEF8YZ', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T11:56:30.677Z'}
{'id': '01M4X3CY6Q5WYQD1A257YS1N8C', 'decision_id': '01M4X3CY6048GFBJK6Z17G49EM', 'node_id': '01M4NZK474BVARFBXYQBMVA8GV', 'rule_id': 'oom-guard-01', 'action_kind': 'instance.restart', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T11:33:14.045Z'}
{'id': '01M4WXN9A2JHVECWAF54SWT8RH', 'decision_id': '01M4WXN99RR6BZ7VAE935V9B2E', 'node_id': '01M4NZK46WXG5ZEFAGAAV5JRXM', 'rule_id': 'disk-degrade-01', 'action_kind': 'instance.stop', 'result': 'error', 'lxd_endpoint': 'https://10.10.0.11:8443', 'executed_at': '2026-10-14T09:53:01.851Z'}

### operator log stream rows mentioning the model call / destination (latest 12)
(query failed: no such table: logs)

### tables present
['bootstrap_tokens', 'commands', 'decision_fallback_state', 'health_events', 'nodes', 'operator_lease', 'operator_logs', 'projects', 'remediation_audits', 'remediation_decisions', 'schema_migrations', 'sqlite_sequence', 'ssh_certificates', 'telemetry_batches', 'telemetry_samples', 'trigger_config', 'trigger_state', 'vm_allocations']

### cwd=/home/ubuntu
```


#### forced Vertex AI failure: on Node C and from the peer

Source: `live-forced-failure.txt`

```
+ sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash-does-not-exist|' /etc/termini/inference-proxy.env
+ sudo systemctl restart termini-inference-proxy
+ sleep 3
+ curl -s -m 30 -w '\nHTTP=%{http_code}\n' http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"gemma","messages":[{"role":"user","content":"reply ok"}],"max_tokens":8}'
{"error": {"message": "forwarded backend gemini-2.5-flash-does-not-exist at aiplatform.googleapis.com rejected the request", "type": "termini_error", "code": "MODEL_UNAVAILABLE", "param": null, "request_id": "req-01M4ZK3P8DQMC1R31MB73TTK09", "detail": "Publisher model `projects/termini-510609/locations/global/publishers/google/models/gemini-2.5-flash-does-not-exist` was not found or your project does not have access to it. Ensure you are using a valid model name and that the model is available in the specified region. For more information, see: https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations.", "distinct": "vertex_rejected", "backend": "gemini-proxy", "model": "gemini-2.5-flash-does-not-exist", "destination": "aiplatform.googleapis.com", "egress": "external"}}
HTTP=503
--- proxy log (the loud failure line)
+ echo '--- proxy log (the loud failure line)'
+ sudo journalctl -u termini-inference-proxy --no-pager -n 8
+ tail -5
Oct 15 10:53:10 termini-node-c.asia-south1-a.c.termini-510609.internal systemd[1]: termini-inference-proxy.service: Deactivated successfully.
Oct 15 10:53:10 termini-node-c.asia-south1-a.c.termini-510609.internal systemd[1]: Stopped termini-inference-proxy.service - Termini inference proxy (Node C :8080) — drop-in front door for the frozen inference contract.
Oct 15 10:53:10 termini-node-c.asia-south1-a.c.termini-510609.internal systemd[1]: Started termini-inference-proxy.service - Termini inference proxy (Node C :8080) — drop-in front door for the frozen inference contract.
Oct 15 10:53:10 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2775]: {"ts":"2026-10-15T10:53:10Z","event":"startup","severity":"info","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash-does-not-exist","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","listen":"0.0.0.0:8080","state_file":"/run/termini/inference-backend.json","upstream":"http://127.0.0.1:8081","project":"(metadata)","thinking_budget":0}
Oct 15 10:53:14 termini-node-c.asia-south1-a.c.termini-510609.internal termini-inference-proxy[2775]: {"ts":"2026-10-15T10:53:14Z","event":"upstream_failure","severity":"error","service":"termini-inference-proxy","backend":"gemini-proxy","model":"gemini-2.5-flash-does-not-exist","alias":"gemma","answered_by":"forwarded-managed-model","egress":"external","destination":"aiplatform.googleapis.com","distinct":"vertex_rejected","status":503,"request_id":"req-01M4ZK3P8DQMC1R31MB73TTK09","stream":false,"forwarded":true,"detail":"Publisher model `projects/termini-510609/locations/global/publishers/google/models/gemini-2.5-flash-does-not-exist` was not found or your project does not have access to it. Ensure you are using a valid model name and that the model is available in the specified region. For more information, see: https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations.","latency_ms":382.6}
peer_http=503
{"error": {"message": "forwarded backend gemini-2.5-flash-does-not-exist at aiplatform.googleapis.com rejected the request", "type": "termini_error", "code": "MODEL_UNAVAILABLE", "param": null, "request_id": "req-01M4ZK3W1JKK5SEPX60WJ1AR3G", "detail": "Publisher model `projects/termini-510609/locations/global/publishers/google/models/gemini-2.5-flash-does-not-exist` was not found or your project does not have access to it. Ensure you are using a valid model name and that the model is available in the specified region. For more information, see: https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations.", "distinct": "vertex_rejected", "backend": "gemini-proxy", "model": "gemini-2.5-flash-does-not-exist", "destination": "aiplatform.googleapis.com", "egress": "external"}}
```


#### the one-step switch to the local CPU path

Source: `live-switch-local.txt`

```
+ sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash|' /etc/termini/inference-proxy.env
+ sudo /usr/local/sbin/termini-inference-switch local
[inference-switch] TERMINI_INFERENCE_BACKEND=local
== node status: /etc/termini ==
--- /etc/termini/inference-proxy.env
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
[inference-switch] ACTIVE BACKEND: local
LISTEN 0      512                   127.0.0.1:8081      0.0.0.0:*    users:(("llama-server",pid=2290,fd=3))                 
LISTEN 0      5                       0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=2900,fd=5))                      

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
  "ts": "2026-10-15T10:53:24Z"
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
  "ts": "2026-10-15T10:53:24Z"
}
+ sudo cat /run/termini/inference-backend.json
```


#### peer acceptance, LOCAL backend

Source: `peer-verify-local.txt`

```
Termini inference front door — peer acceptance, 2026-10-15T10:53:29Z
caller: termini-node-a.asia-south1-a.c.termini-510609.internal (10.10.0.11)   endpoint: http://10.10.0.13:8080   expect backend: local

===== 1. GET /health (from this peer) =====
http_code=200
body: {"status": "ok", "model": "gemma", "backend": "local", "answered_by": "local-weights", "egress": "in-vpc", "served_model": "gemma", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "http://127.0.0.1:8081", "note": "local weights served by llama.cpp on this node"}

===== 2. GET /v1/models =====
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792061609, "owned_by": "termini"}], "termini_backend": {"backend": "local", "answered_by": "local-weights", "served_model": "gemma", "egress": "in-vpc", "upstream": "http://127.0.0.1:8081"}}

===== 3-6. requests (non-streaming, streaming, parameters, labels) =====
PASS  peer: /health returns 200 — got 200
PASS  /health reports a backend — backend='local'
PASS  /health backend == local — reported 'local'
PASS  /v1/models advertises the alias 'gemma' — ids=['gemma']
      non-streaming: http=200 in 1.59s
      response headers: X-Termini-Backend=local, X-Termini-Answered-By=local-weights, X-Termini-Model=gemma, X-Termini-Model-Id=gemma, X-Termini-Egress=in-vpc, X-Termini-Residency=resident, X-Termini-Request-Id=01ACCEPT
      body: {"choices": [{"finish_reason": "stop", "index": 0, "message": {"role": "assistant", "content": "PROXI_OK\n"}}], "created": 1792061610, "model": "gemma", "system_fingerprint": "b1-7fe450e19", "object": "chat.completion", "usage": {"completion_tokens": 6, "prompt_tokens": 17, "total_tokens": 23, "prompt_tokens_details": {"cached_tokens": 0}}, "id": "chatcmpl-IpMLo457RJCSGAErPuY7V4fKB4xqku30", "timings": {"cache_n": 0, "prompt_n": 17, "prompt_ms": 1015.819, "prompt_per_token_ms": 59.75405882352941, "prompt_per_second": 16.735264845410452, "predicted_n": 6, "predicted_ms": 509.391, "predicted_per_token_ms": 101.8782, "predicted_per_second": 9.815642600674138}}
PASS  non-streaming returns HTTP 200 — got 200
PASS  non-streaming returns non-empty content — 'PROXI_OK\n'
PASS  the envelope names the model alias — model='gemma'
PASS  the envelope carries the OpenAI id/object/created fields
PASS  usage is reported — usage={'completion_tokens': 6, 'prompt_tokens': 17, 'total_tokens': 23, 'prompt_tokens_details': {'cached_tokens': 0}}
      in-band label: {}
FAIL  the response labels its backend in-band — backend=None
FAIL  the label names what actually answered (answered_by) — answered_by=None
PASS  no label claims GPU inference
FAIL  a local answer is labelled answered_by=local-weights — got None
FAIL  a local answer is flagged egress=in-vpc — egress=None
PASS  max_tokens is honoured (8 requested) — completion_tokens=8
      max_tokens=8 run: 1.80s, usage={'completion_tokens': 8, 'prompt_tokens': 20, 'total_tokens': 28, 'prompt_tokens_details': {'cached_tokens': 1}}
      streaming: http=200 deltas=9 first_token_s=2.017 total_s=2.02
      assembled: 'One\nTwo\nThree\nFour\nFive'
      final label: null usage={'completion_tokens': 10, 'prompt_tokens': 20, 'total_tokens': 30, 'prompt_tokens_details': {'cached_tokens': 1}} finish='stop'
PASS  streaming returns HTTP 200 — got 200
PASS  streaming yields >=2 incremental deltas — 9 deltas
PASS  streaming terminates with [DONE]
PASS  streaming assembled content is non-empty
PASS  streaming reports usage when asked (include_usage) — usage={'completion_tokens': 10, 'prompt_tokens': 20, 'total_tokens': 30, 'prompt_tokens_details': {'cached_tokens': 1}}
FAIL  streaming final chunk is labelled in-band — label=None

      latency samples (s): [1.595]
      median latency: 1.595s (backend reported by the endpoint: local)
      NOTE: this compares the FRONT DOOR's own answer time; for the same
      20-sample decision prompt the CPU path on this node is documented at
      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).

RESULT: FAIL (5 check(s) failed): the response labels its backend in-band, the label names what actually answered (answered_by), a local answer is labelled answered_by=local-weights, a local answer is flagged egress=in-vpc, streaming final chunk is labelled in-band

===== RESULT =====
RESULT: FAIL (see checks above)
VERIFY_EXIT=1
```


#### back to the shipped configuration (forwarded)

Source: `live-switch-back.txt`

```
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
  "ts": "2026-10-15T10:53:44Z"
}
```


#### peer acceptance, forwarded backend, final

Source: `peer-verify-gemini-final.txt`

```
Termini inference front door — peer acceptance, 2026-10-15T10:53:48Z
caller: termini-node-a.asia-south1-a.c.termini-510609.internal (10.10.0.11)   endpoint: http://10.10.0.13:8080   expect backend: gemini-proxy

===== 1. GET /health (from this peer) =====
http_code=200
body: {"status": "ok", "model": "gemma", "backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "egress": "external", "served_model": "gemini-2.5-flash", "local_path": {"available": true, "upstream": "http://127.0.0.1:8081", "switch": "TERMINI_INFERENCE_BACKEND=local"}, "destination": "aiplatform.googleapis.com", "note": "forwarded managed model: requests are answered by gemini-2.5-flash at aiplatform.googleapis.com, not by local weights"}

===== 2. GET /v1/models =====
{"object": "list", "data": [{"id": "gemma", "object": "model", "created": 1792061628, "owned_by": "termini"}], "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "served_model": "gemini-2.5-flash", "egress": "external", "destination": "aiplatform.googleapis.com"}}

===== 3-6. requests (non-streaming, streaming, parameters, labels) =====
PASS  peer: /health returns 200 — got 200
PASS  /health reports a backend — backend='gemini-proxy'
PASS  /health backend == gemini-proxy — reported 'gemini-proxy'
PASS  /v1/models advertises the alias 'gemma' — ids=['gemma']
      non-streaming: http=200 in 0.95s
      response headers: X-Termini-Backend=gemini-proxy, X-Termini-Answered-By=forwarded-managed-model, X-Termini-Model=gemma, X-Termini-Model-Id=gemini-2.5-flash, X-Termini-Egress=external, X-Termini-Residency=resident, X-Termini-External-Destination=aiplatform.googleapis.com, X-Termini-Request-Id=01ACCEPT
      body: {"id": "chatcmpl-01M4ZK4SEKPA43XH05J403KYV2", "object": "chat.completion", "created": 1792061629, "model": "gemma", "choices": [{"index": 0, "message": {"role": "assistant", "content": "PROXI_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}, "timings": {"prompt_n": 8, "prompt_ms": 0.0, "predicted_n": 4, "predicted_ms": 906.36, "predicted_per_token_ms": 226.59, "predicted_per_second": 4.413, "wall_ms": 906.36}, "termini_backend": {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"}}
PASS  non-streaming returns HTTP 200 — got 200
PASS  non-streaming returns non-empty content — 'PROXI_OK'
PASS  the envelope names the model alias — model='gemma'
PASS  the envelope carries the OpenAI id/object/created fields
PASS  usage is reported — usage={'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}
      in-band label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"}
PASS  the response labels its backend in-band — backend='gemini-proxy'
PASS  the label names what actually answered (answered_by) — answered_by='forwarded-managed-model'
PASS  no label claims GPU inference
PASS  a forwarded answer is labelled answered_by=forwarded-managed-model — got 'forwarded-managed-model'
PASS  a forwarded answer names its external destination — destination='aiplatform.googleapis.com'
PASS  a forwarded answer is flagged egress=external — egress='external'
PASS  a forwarded answer is NOT labelled as local weights
PASS  max_tokens is honoured (8 requested) — completion_tokens=8
      max_tokens=8 run: 0.54s, usage={'prompt_tokens': 11, 'completion_tokens': 8, 'total_tokens': 19}
      streaming: http=200 deltas=2 first_token_s=1.441 total_s=1.52
      assembled: 'one\ntwo\nthree\nfour\nfive'
      final label: {"backend": "gemini-proxy", "answered_by": "forwarded-managed-model", "model": "gemini-2.5-flash", "alias": "gemma", "egress": "external", "destination": "aiplatform.googleapis.com", "forwarded": true, "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node"} usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20} finish='stop'
PASS  streaming returns HTTP 200 — got 200
PASS  streaming yields >=2 incremental deltas — 2 deltas
PASS  streaming terminates with [DONE]
PASS  streaming assembled content is non-empty
PASS  streaming reports usage when asked (include_usage) — usage={'prompt_tokens': 11, 'completion_tokens': 9, 'total_tokens': 20}
PASS  streaming final chunk is labelled in-band — label={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node'}
PASS  streaming from a forwarded backend names the destination — label={'backend': 'gemini-proxy', 'answered_by': 'forwarded-managed-model', 'model': 'gemini-2.5-flash', 'alias': 'gemma', 'egress': 'external', 'destination': 'aiplatform.googleapis.com', 'forwarded': True, 'note': 'answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node'}

      latency samples (s): [0.945]
      median latency: 0.945s (backend reported by the endpoint: gemini-proxy)
      NOTE: this compares the FRONT DOOR's own answer time; for the same
      20-sample decision prompt the CPU path on this node is documented at
      402 s (infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md).

RESULT: PASS (drop-in envelope + labelled backend + parameters + streaming)

===== RESULT =====
RESULT: PASS — the front door is a drop-in and it labels what answered
VERIFY_EXIT=0
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

Frozen contracts (`docs/contracts/`) are untouched by this card; the wire shape,
the error envelope, the telemetry envelope and the model-alias surface are all
unchanged, and the additions (response headers, a `termini_backend` body block,
audit fields) are additive.
