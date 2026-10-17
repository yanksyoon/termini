# Agent task API — live acceptance on the standing fleet (2026-10-17)

Card: `t_1a2c913c` — "Agent task API: give an agent a task through the controller and stream the answer".
Code: PR #170 → `425f609` (the feature) and PR #228 → `850ee52` (the two proxy defects this
live run found, with their regression tests). Contracts: `agent-runtime.md` §4.4/§5.6/§7.4 AT-1,
`controller-api.md` §4.9.

Everything below is raw output from an executed command, not a summary. No GCP compute was started,
stopped or created by this card; the fleet was already RUNNING (operator window, 2026-10-16 ~21:25Z).

## Environment

| thing | value |
| --- | --- |
| controller | `termini-controller` (asia-south1-a), public `34.93.190.64`; live acceptance ran on `c95b722` (the branch tip of PR #228, tree-identical to the merge), then `/opt/termini` was re-deployed to `origin/main` = `850ee52` (`/opt/termini/DEPLOYED_COMMIT` reads `850ee52ee45cb932d79966792937aa5649734a86`, 158 files) with `infra/gcp/tools/deploy-termini-tree.sh`; backups `/opt/termini.bak-20261017T174334Z` and `/opt/termini.bak-20261017T174811Z` |
| node | `termini-node-a` (asia-south1-a), LXD, `agent-os/w5` fingerprint `eb7806889646` placed 2026-10-17T17:21Z |
| instance | `termini-01m55dz5fzksstj3fkf91n5hae-0-0`, launched from `agent-os/w5`, 1 vCPU / 1 GiB, `10.174.247.102` |
| inference | node C front door `http://10.10.0.13:8080`, alias `gemma`, `backend=gemini-proxy` (provenance echoed in the (a) record below) |
| project / vm ids | `01J0DEM0WARM000000000000AB` / `termini-01m55dz5fzksstj3fkf91n5hae-0-0`, node id `01M4TGD31WXH97JAA5S6A4R10D` |

The instance's rendered config (`/etc/termini/agent.yaml`, §4.4 shape):

```yaml
inference:
  base_url: http://10.10.0.13:8080
  default_model: gemma
  allowed_models: [gemma]
tasks:
  max_concurrent: 2
```

## (a) A real task through the controller, a real answer

HTTP exchange, from the controller VM (project token auth):

```
--- controller health ---
GET /v1/health -> 200

--- POST /v1/projects/01J0DEM0WARM000000000000AB/agents/termini-01m55dz5fzksstj3fkf91n5hae-0-0/tasks ---
HTTP 202 in 0.736425s
HTTP/1.1 202 Accepted
Content-Type: application/json
X-Termini-Version: 1

{
    "task_id": "01M55FDKNTEHMJ1AM4D3P0M96D",
    "vm_id": "termini-01m55dz5fzksstj3fkf91n5hae-0-0",
    "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
    "state": "queued",
    "actor": "human:ops",
    "model_preference": null,
    "model": null,
    "max_tokens": 16,
    "command_id": "01M55FDKN59PDHWX44MRH8D3D6",
    "agent_task_id": "01M55FDM01A4Y76TF5N47JPQGW",
    "submitted_at": "2026-10-17T17:44:11Z",
    ...
}

--- GET the task until terminal ---
poll 1 -> state=queued
poll 2 -> state=done

{
    "task_id": "01M55FDKNTEHMJ1AM4D3P0M96D",
    "state": "done",
    "model": "gemma",
    "started_at": "2026-10-17T17:44:11Z",
    "ended_at": "2026-10-17T17:44:14Z",
    "duration_ms": 915,
    "provenance": {
        "backend": "gemini-proxy",
        "answered_by": "forwarded-managed-model",
        "model": "gemini-2.5-flash",
        "alias": "gemma",
        "egress": "external",
        "destination": "aiplatform.googleapis.com",
        "forwarded": true,
        "note": "answered by a forwarded managed model, not by local weights on this node and not by an accelerator on this node",
        "source": "endpoint-in-band"
    },
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    "error": null,
    "error_code": null,
    "answer": "pong"
}
```

The agent's own log inside the instance (`lxc exec … tail /var/log/termini/agent.jsonl`):

```json
{"ts":"2026-10-17T17:44:11+0000","logger":"termini_agent.taskapi","msg":"task accepted","task_id":"01M55FDM01A4Y76TF5N47JPQGW","state":"queued","actor":"human:ops","max_tokens":16,"prompt_bytes":24,"in_flight":1}
{"ts":"2026-10-17T17:44:11+0000","logger":"termini_agent.taskapi","msg":"task running","task_id":"01M55FDM01A4Y76TF5N47JPQGW","state":"running"}
{"ts":"2026-10-17T17:44:12+0000","logger":"httpx","msg":"HTTP Request: POST http://10.10.0.13:8080/v1/chat/completions \"HTTP/1.1 200 OK\""}
{"ts":"2026-10-17T17:44:12+0000","logger":"termini_agent.taskapi","msg":"task done","task_id":"01M55FDM01A4Y76TF5N47JPQGW","state":"done","model":"gemma","tokens":1,"backend":"gemini-proxy"}
```

## Progress states a UI renders (queued → running → done)

One task, polled every 0.3 s (`GET .../tasks/{task_id}`):

```
POST HTTP 202
t+65ms    -> state=queued
t+2606ms  -> state=running
t+9525ms  -> state=done
{"task_id": "01M55FKH0PX5QBJWE2NZXCEJ1S", "state": "done", "model": "gemma", "duration_ms": 7579,
 "usage": {"prompt_tokens": 7, "completion_tokens": 637, "total_tokens": 644}}
```

The controller also publishes `task.queued` → `task.state_changed` → `task.completed` on the WS
stream and a content-free operator-log line per transition (§4.9), which is what the dashboard
consumes; the two tests in `tests/test_agent_tasks.py::ObservedByTheClientTest` pin that surface.

## (b) Failures are distinct, actionable, and never look like a slow success

Four independent failures, all on the live deployment:

```
--- b1: retired model alias ---
HTTP 400
{"error": {"code": "MODEL_NOT_ALLOWED",
           "message": "model 'deepseek-flash' not in ('gemma',)",
           "details": [{"field": "model_preference",
                        "reason": "the alias is not in this agent's inference.allowed_models: the config must name the alias the endpoint actually serves"}],
           "request_id": "01M55FEVFAG0WYPX1EN806JMJ1"}}

--- b2: unknown vm_id ---
HTTP 404
{"error": {"code": "AGENT_NOT_FOUND",
           "message": "no agent termini-nope-0-0 on this project's nodes",
           "details": [{"field": "vm_id", "reason": "unknown",
                        "known_vm_ids": ["demo-workload", "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0", "termini-01m4wjhwqr3b8pcav2tvqbgg-0-1", "termini-01m537g26sm8qgkmztn8stbt-0-0", "termini-01m537km0trb189a6e1gz546-0-0", "termini-01m55dz5fzksstj3fkf91n5hae-0-0"]}]}}

--- b3: bound exceeded (two tasks in flight, tasks.max_concurrent=2) ---
slot1 HTTP 202
slot2 HTTP 202
third HTTP 429
HTTP/1.1 429 Too Many Requests
Retry-After: 5
{"error": {"code": "TASK_BOUND_EXCEEDED",
           "message": "this instance's task bound is reached: 2 of 2 task(s) in flight (tasks.max_concurrent=2)",
           "details": [{"field": "vm_id", "reason": "this instance's task bound is already in flight: wait for a running task to finish (Retry-After) or use another instance"}],
           "request_id": "01M55FEX7E3Q4YGWQX21TBGFBZ"}}

--- b4: model endpoint unreachable (inference.base_url pointed at the dead 10.10.0.99:8080, service restarted) ---
POST HTTP 202
terminal after 4.84s
{"task_id": "01M55FHA7W0MRGM88DGJA25VCG", "state": "error", "duration_ms": 2246,
 "error": {"code": "INFERENCE_UNREACHABLE",
           "message": "inference endpoint http://10.10.0.99:8080 is unreachable: timed out",
           "actionable": "the on-prem inference endpoint is not answering at all: check that node C is up and that inference.base_url names its underlay address"},
 "error_code": "INFERENCE_UNREACHABLE", "error_message": "inference endpoint http://10.10.0.99:8080 is unreachable: timed out",
 "answer": null}
```

and the agent's own log for b4 — a failure, not a finished task:

```json
{"ts":"2026-10-17T17:46:13+0000","msg":"task accepted","task_id":"01M55FHAZEDH1V996CHZE4RDKE","state":"queued","in_flight":1}
{"ts":"2026-10-17T17:46:13+0000","msg":"task running","task_id":"01M55FHAZEDH1V996CHZE4RDKE","state":"running"}
{"ts":"2026-10-17T17:46:15+0000","level":"warning","msg":"task failed","task_id":"01M55FHAZEDH1V996CHZE4RDKE","state":"error","code":"INFERENCE_UNREACHABLE"}
```

The config was then restored (`base_url: http://10.10.0.13:8080`, service restarted) and the same
task succeeded again — `POST HTTP 202` → `{"state": "done", "answer": "pong", "model": "gemma",
"duration_ms": 697, "error_code": null}`. A 2.2 s `INFERENCE_UNREACHABLE` is visually and
semantically distinct from the 7.6 s `done` generation above; a model that is merely slow reaches
the per-task deadline as `TASK_TIMEOUT`, never `done` with an empty answer.

## (c) The task and its result are enumerable and human-audited

`GET /v1/projects/{pid}/agents/{vm}/tasks?limit=10`:

```
count= 10
  01M55FEWV2H9DA4G41TWWKVNTS error   actor=human:ops code=TASK_BOUND_EXCEEDED dur_ms=None cmd=01M55FEWT4THPNKY3718C9WXZ2
  01M55FEVXBZNACJXCRR11XHTNW queued  actor=human:ops code=None dur_ms=None cmd=01M55FEVV1WM17J1WE52CZS145
  01M55FEVVMX0HSHGWER08HJWMV queued  actor=human:ops code=None dur_ms=None cmd=01M55FEVTVG4KVPPYRNB0AT0AS
  01M55FEV2V1P7X5QJ3AMNPHTAR error   actor=human:ops code=MODEL_NOT_ALLOWED dur_ms=None cmd=01M55FEV278JG3DQF2TY819GXM
  01M55FDKNTEHMJ1AM4D3P0M96D done    actor=human:ops code=None dur_ms=915 cmd=01M55FDKN59PDHWX44MRH8D3D6
```

(The two `queued` rows are the two in-flight tasks of b3, read while still running; they completed
7.2 s / 7.3 s later — see the audit lines below.)

Audit trail on the operator log the dashboard consumes
(`GET /v1/projects/{pid}/logs`, SSE, `data:` frames):

```json
{"log_id":"01M55FEX6DNCWFPJRAH7SADB6T","ts":"2026-10-17T17:44:53.709Z","node_id":"01M4TGD31WXH97JAA5S6A4R10D","severity":"warn","actor":"human","message":"agent task 01M55FEWV2H9DA4G41TWWKVNTS on termini-01m55dz5fzksstj3fkf91n5hae-0-0 error (TASK_BOUND_EXCEEDED: this instance's task bound is reached: 2 of 2 task(s) in flight (tasks.max_concurrent=2))","remediation":{"command_id":"01M55FEWT4THPNKY3718C9WXZ2","type":"agent.dispatch_task","status":"error"}}
{"log_id":"01M55FF5NDBQDD1YZP947S52B4","ts":"2026-10-17T17:45:02.381Z","node_id":"01M4TGD31WXH97JAA5S6A4R10D","severity":"info","actor":"human","message":"agent task 01M55FEVVMX0HSHGWER08HJWMV on termini-01m55dz5fzksstj3fkf91n5hae-0-0 done (model gemma, 7219ms)","remediation":{"command_id":"01M55FEVTVG4KVPPYRNB0AT0AS","type":"agent.dispatch_task","status":"done"}}
{"log_id":"01M55FF62TZJNC2YM8QWXSC3SV","ts":"2026-10-17T17:45:02.810Z","node_id":"01M4TGD31WXH97JAA5S6A4R10D","severity":"info","actor":"human","message":"agent task 01M55FEVXBZNACJXCRR11XHTNW on termini-01m55dz5fzksstj3fkf91n5hae-0-0 done (model gemma, 7337ms)","remediation":{"command_id":"01M55FEVV1WM17J1WE52CZS145","type":"agent.dispatch_task","status":"done"}}
```

`actor: human` on every line, node-scoped, `remediation.type: agent.dispatch_task` with the
`command_id` that also appears on the task record — the same shape a node action produces —
and **content-free**: the prompt and the answer appear in neither the operator log nor the
stream events, only the task id, vm id, model, duration and (on failure) the error code.

## Defects this live run found (fixed in PR #228 → `850ee52`)

1. **`exec_instance()` parsed LXD's `record-output` log as JSON.** `GET /1.0/instances/{n}/logs/<f>`
   is a file download: the body is the recorded bytes, not `{"metadata": …}`. The code read
   `metadata.metadata` (always `null` for a download), so stdout was always empty and every POST
   answered `503 "no task surface"` **after the task had already run** — reproduced twice before
   the fix; the task was visible in the instance's own `/v1/tasks` while the controller reported a
   missing surface. The unit stub served the log in a sync envelope, which is why CI was green: the
   stub encoded the bug. Fixed in `_read_log_text()`; the stub now serves `text/plain`; three tests.
2. **`Retry-After` promised by §5.6 on the 429 was dropped by the proxy**, and `details[0].field`
   was always `vm_id`, even for a model-alias refusal. Now the agent's header and `param` are
   carried through (`Retry-After: 5` above; `field: model_preference` in b1).

## Same path on the merged `main` (post-landing smoke)

After `scripts/land.sh` merged PR #228 and `/opt/termini` was re-deployed to `850ee52`:

```
--- deployed commit on the controller ---
850ee52ee45cb932d79966792937aa5649734a86
158
--- smoke: same task through the controller ---
POST HTTP 202
{"task_id": "01M55FNTG5SQRM5Y9BSNY6VSKT", "state": "done", "answer": "pong", "model": "gemma",
 "duration_ms": 877, "error_code": null}
--- tasks now enumerable ---
count= 28 states= ['done', 'error']
```

## What is NOT verified here

- **No browser screenshot.** The acceptance asks for one "once the UI consumes it"; the consuming
  card `t_44a2b87b` (Agents panel) is still `todo`, so no client surface renders this endpoint yet.
  The dashboard-side proof is the operator-log line and the WS/SSE events, both exercised above and
  by `ObservedByTheClientTest`.
- **No image rebuild was needed** for this run: `agent-os/w5` (PR #174) already carries the task
  surface, so the instance was launched from the published image rather than hand-patched.
- The `queued` → `running` transition is visible only for tasks long enough to outlive one poll
  cycle (2.6 s here); a `max_tokens: 16` generation goes `queued` → `done` inside ~1 s.

## Reproduce

```bash
# controller VM, project token
TOKEN=demo-project-token; PID=01J0DEM0WARM000000000000AB
VM=termini-01m55dz5fzksstj3fkf91n5hae-0-0; CTRL=http://127.0.0.1:8080
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"Reply with exactly: pong","max_tokens":16,"actor":"human:ops"}' \
  "$CTRL/v1/projects/$PID/agents/$VM/tasks"
curl -sS -H "Authorization: Bearer $TOKEN" "$CTRL/v1/projects/$PID/agents/$VM/tasks/<task_id>"
curl -sS -H "Authorization: Bearer $TOKEN" "$CTRL/v1/projects/$PID/agents/$VM/tasks?limit=10"
curl -sN -H "Authorization: Bearer $TOKEN" "$CTRL/v1/projects/$PID/logs?since=0" | grep agent.dispatch_task
```
