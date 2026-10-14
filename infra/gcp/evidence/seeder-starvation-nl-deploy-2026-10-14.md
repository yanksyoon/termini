# Seeder starvation + plain-NL deploy — RAW EVIDENCE (2026-10-14)

Two defects that made the demo non-demoable, the mechanism behind each, and the
live proof of the fix. Every block below is verbatim output from the standing GCP
fleet; nothing is reconstructed from memory.

| | |
|---|---|
| Branch | `wt/cardfix` |
| Base | `origin/main` @ `0f64d7b` |
| Deployed commits | `0d19886` then `864af2f` (`/opt/termini/DEPLOYED_COMMIT`) |
| Controller | `termini-controller` (10.10.0.2 / 34.93.190.64), code at `/opt/termini` |
| Fleet during the run | `termini-node-a` (compute, LXD `https://10.10.0.11:8443`, **n2-standard-2**), `termini-node-c` (8 vCPU/32 GiB, gemma on `:8080` / `:8090`); `termini-node-b` TERMINATED (CPUS_ALL_REGIONS 12/12) |
| Live config | operator `ENABLED=true`, `DECISION_MODE=model`, `WINDOW=20`, `COOLDOWN_S=600`, `INFERENCE_TIMEOUT_S=900`, `DECISION_TIMEOUT_S=900` |

---

## DEFECT A — the seeder starved the intent path. The mechanism.

**It was not SQLite and it was not the controller's thread pool.** The controller
kept serving telemetry at 2/s (`202`) for the whole window, on a
`ThreadingHTTPServer`. What was blocked was the *model call*.

The chain, measured:

1. `console/seed_demo.py` drives faults → the operator fires a rule → it issues a
   decision to node-c. The decision prompt is ~12,000 tokens (the 20-sample
   evidence window) and takes ~400 s to prefill on CPU.
2. node-c's llama.cpp was started **`--parallel 1`** — a ONE-slot queue. A request
   arriving while that slot was busy was held open with no response until the
   decision finished.
3. `POST /intents` does its extraction as a synchronous model call **inside the
   request**, so the user's request sat in that queue. The client gave up first:
   `HTTP 000`. No access-log line was ever written for it, because
   `BaseHTTPRequestHandler` logs when the response is *sent* — the handler was
   still blocked inside the model call.

### The failing request (verbatim, seeder running)

```
2026-10-14T11:33:22Z
=== POST /intents (prompt) while the operator decision holds the single model slot ===
HTTP=000 time=220.002288s
body:
2026-10-14T11:37:02Z
=== telemetry kept flowing? (last 6 ingest lines) ===
2026-10-14 11:37:00,249 INFO termini.controller 127.0.0.1 - "POST /v1/nodes/01M4NZK474BVARFBXYQBMVA8GV/telemetry HTTP/1.1" 202 -
2026-10-14 11:37:01,253 INFO termini.controller 127.0.0.1 - "POST /v1/nodes/01M4NZK46WXG5ZEFAGAAV5JRXM/telemetry HTTP/1.1" 202 -
2026-10-14 11:37:01,296 INFO termini.controller 10.10.0.11 - "POST /v1/nodes/01M4TGD31WXH97JAA5S6A4R10D/telemetry HTTP/1.1" 202 -
2026-10-14 11:37:02,260 INFO termini.controller 127.0.0.1 - "POST /v1/nodes/01M4NZK46WXG5ZEFAGAAV5JRXM/telemetry HTTP/1.1" 202 -
```

`723` telemetry `202`s in the last 6 minutes of that window — the database and the
request path were healthy throughout.

### The decision that was holding the only slot (node-c's own log)

```
--- ExecStart (BEFORE) ---
ExecStart=/opt/termini/llama.cpp/bin/llama-server --model ... --port 8080 --ctx-size 16384 --parallel 1 --threads 8 --reasoning-format none

7.25.265.057 I slot get_availabl: id  0 | task -1 | selected slot by LRU, t_last = 413820969
7.25.573.775 I slot launch_slot_: id  0 | task 251 | processing task, is_child = 0
7.53.923.751 I slot print_timing: id  0 | task 251 | prompt processing, n_tokens =   1005, progress = 0.08, t =  28.35 s / 35.45 tokens per second
8.53.547.201 I slot print_timing: id  0 | task 251 | prompt processing, n_tokens =   3053, progress = 0.23, t =  87.97 s / 34.70 tokens per second
9.54.076.000 I slot print_timing: id  0 | task 251 | prompt processing, n_tokens =   5101, progress = 0.39, t = 148.50 s / 34.35 tokens per second
10.55.635.002 I slot print_timing: id  0 | task 251 | prompt processing, n_tokens =   7149, progress = 0.54, t = 210.06 s / 34.03 tokens per second
```

`id 0` is the only slot; `task 251` is the operator's ~12k-token decision, still
prefilling after 210 s. Each earlier `task 65/127/189` in the same log is a
`deploy 2 worker agents` extraction (815 tokens, ~5.5 s) — cheap and fast *when the
slot is free*.

### Fix, and the second finding

`0d19886` gave the decision server two slots with the full context each
(`--ctx-size 32768 --parallel 2`). Live confirmation after the change:

```
0.04.176.397 I srv    load_model: initializing, n_slots = 2, n_ctx_slot = 16384, kv_unified = 'false'
```

That removed the hard queue — an intent now gets its own slot:

```
10.34.987.074 I slot launch_slot_: id  0 | task 65 | processing task, is_child = 0
10.59.851.157 I slot launch_slot_: id  1 | task 68 | processing task, is_child = 0
```

**but it was NOT sufficient**, and the live run said so: `task 68` (the extraction)
launched on slot 1 and then made no progress at all while `task 65` prefilled —
llama.cpp fills its batch from the big prompt first. The intent then hit its own
bound and answered a real status at last, instead of hanging:

```
=== POST /intents (prompt) while the operator decision holds a slot ===
HTTP=503 time=120.070002s
{"error": {"code": "INTERNAL", "message": "intent extraction failed: the on-prem model did not return a usable allocation spec in time", "details": [{"index": null, "reason": "extraction_failed", "detail": "timed out"}], "request_id": "01M4X4EHEB9QX4YQYSA4D1Z0TF"}}
```

So `864af2f` gives the extraction its **own process**: node-c serves
`termini-llama-intent` (`:8090`, `--ctx-size 4096 --threads 3`) beside the decision
model (`termini-llama-server`, `:8080`, `--ctx-size 32768 --parallel 2 --threads 6`),
and the controller points at it with `TERMINI_INTENT_INFERENCE_BASE_URL`. Exact
instruction in `infra/gcp/cloud-init/llama-serve.sh`; underlay firewall `tcp:8090`
in `infra/gcp/main.tf` and applied live.

### AFTER — an intent is SERVED while the seeder runs

<!--AFTER_A-->
With the seeder running, both model instances up, and `POST /intents` routed to the
dedicated instance:

```
=== seeder: active ; controller: active ===
--- POST /intents while :8080 is prefilling ---
HTTP=200 time=8.664972s
BODY: outcome accepted plan 01M4X6KZRW8MGZ2ZW8NXME5FM7 | node 01M4TGD31WXH97JAA5S6A4R10D accepted
      {"type": "agent_vm", "count": 2, "vcpus": 1, "ram_gb": 1, "disk_gb": 8, "target_node": "auto"}
released the verification plan
```

and the *dedicated instance's own* timing line — proof of which server answered:

```
--- :8090 (intent instance) ---
14.31.295.726 I slot launch_slot_: id  0 | task 127 | processing task, is_child = 0
14.39.944.576 I slot print_timing: id  0 | task 127 | prompt eval time =     572.41 ms /     5 tokens
14.39.944.589 I slot print_timing: id  0 | task 127 |        eval time =    8076.41 ms /    60 tokens
14.39.944.591 I slot print_timing: id  0 | task 127 |       total time =    8648.82 ms /    65 tokens
```

Repeated runs while the seeder drove faults (telemetry `202`s `38` per 60 s
throughout — ingest never stopped):

```
HTTP=200 time=44.003235s  outcome accepted plan 01M4X6GS2NTY2YA1X0BXMS3Y6R node 01M4TGD31… accepted
HTTP=200 time=8.709151s   outcome accepted plan 01M4X6JDGZTFWBPP8MP8V897EH node 01M4TGD31… accepted
HTTP=200 time=8.664972s   outcome accepted plan 01M4X6KZRW8MGZ2ZW8NXME5FM7 node 01M4TGD31… accepted
```

(Each verification plan was released again with `POST /vms/release`; the one plan
that was materialised had its instances deleted and was released by
`POST /vms/reconcile` as `instances_gone`. No database row was hand-edited.)

<!--A_COINCIDENT-->


---

## DEFECT B — a plain natural-language deploy was refused

### The failing request (verbatim, seeder stopped, before any fix)

```
=== POST /intents {"prompt": "deploy 2 worker agents"} ===
HTTP=422 time=27.110814s
{"error": {"code": "GUARDRAIL_REJECTED", "message": "allocation plan rejected by guardrail",
 "details": [{"index": 0, "reason": "exceeds_node_ram_ceiling",
              "detail": "aggregate RAM exceeds the node's free capacity"}],
 "request_id": "01M4X2XR6C5M8EVJZEVBS4WGJP"}}
```

Even the *smallest possible* two-agent spec was refused:

```
{"spec":{"allocations":[{"type":"agent_vm","count":2,"vcpus":1,"ram_gb":1,"disk_gb":8,"target_node":"auto"}]}}
-> HTTP=422 ... "reason": "exceeds_node_ram_ceiling"
```

### What the model was asked to emit

```
model cfg: http://10.10.0.13:8080 gemma timeout 30.0 max_tokens 512
extract_seconds=5.5
raw: [{"type": "agent_vm", "count": 2, "vcpus": 2, "ram_gb": 4, "disk_gb": 8, "passthrough_gpu": false, "target_node": "auto"}]
```

The **few-shot example itself** said 2 vCPU / 4 GiB per agent, so the model copied
it: 2 agents demanded 4 vCPU / 8 GiB from a node with 5 GiB allocatable.

### Why nothing fit at all: a permanent reservation

`GET /v1/projects/…/nodes`, before:

```
01M4TGD31WXH97JAA5S6A4R10D compute online
   hw: {'cpu': {..., 'cores': 2, 'threads': 2}, 'memory': {'total_bytes': 8319729664}, 'disks': [{'size_bytes': 61285326848}]}
   capacity: {"cpu_cores": 2, "ram_gb": 7, "disk_gb": 57, "cpu_allocated": 4, "ram_allocated_gb": 4,
              "disk_allocated_gb": 40, "cpu_free": 0, "ram_free_gb": 1, "disk_free_gb": 12}
```

Two live commits on node-a: the 2 agents actually running
(`01M4WJHWQR…`, 2 vCPU/2 GiB/8 GiB) and `01M4WX6FMK6Q90B5A2NMZS4XKT`
(2 vCPU / 2 GiB / **32 GiB**) with `vm_ids_json = NULL` — accepted by a
*previous process*, never applied, and with **no route to release it**: `POST
/vms/reconcile` only releases a commit whose materialised instances are gone, and
a never-applied commit has none (controller-api.md § `vms/reconcile`, unchanged).
`ram_free_gb` was therefore 1, and with `count = 2` the per-VM RAM share
(`free_ram // count`) floors to **0**, so *any* two-agent request was refused.

### Fix

* `termini.intent.types.DEFAULT_AGENT_VM = {vcpus 1, ram_gb 1, disk_gb 8}` is now
  the documented unspecified-size default, and the extraction prompt's few-shot
  is built from it, so the prompt cannot drift from it. Two agents cost 2 vCPU /
  2 GiB — never the whole node.
* A capacity refusal names the node and the numbers instead of a bare rule:
  `detail = "aggregate RAM exceeds the node's free capacity: node <id> had
  ram_free_gb=1 GiB free and the request needed 2×1 vCPU / 2×1 GiB RAM (already
  allocated 2 vCPU / 4 GiB RAM)"`. The frozen reason code is unchanged.
* A startup pass releases never-applied reservations whose plan died with a
  previous process (`release_orphaned_reservations`, reason
  `plan_orphaned_restart`). The proof is stronger than reconcile's: the
  accepted-plan map is in-memory only and `vms/apply` is its only consumer, so
  after a restart such a plan cannot become an instance — the client's apply would
  `404 plan not found`. Live, on restart of `864af2f`:

```
2026-10-14 11:45:26,549 INFO termini.controller startup released 1 orphaned capacity reservation(s)
    (never applied; their plan died with a previous process): 01M4WX6FMK6Q90B5A2NMZS4XKT(2 vCPU/2 GiB)
2026-10-14 11:45:26,549 INFO termini.controller controller listening on 0.0.0.0:8080 (db=/data/controller.db)
```

node-a's capacity, immediately after:

```
capacity: {"cpu_cores": 2, "ram_gb": 7, "disk_gb": 57, "cpu_allocated": 2, "ram_allocated_gb": 2,
           "disk_allocated_gb": 8, "cpu_free": 2, "ram_free_gb": 3, "disk_free_gb": 44}
```

### AFTER — the plain NL deploy produces RUNNING agents on node-a

```
=== POST /intents {"prompt": "deploy 2 worker agents"} ===
HTTP=200 time=31.032653s
outcome: accepted plan_id: 01M4X3S51D66GFPWCBWEMGSNQ0
  index 0 status accepted node 01M4TGD31WXH97JAA5S6A4R10D
  resolved: {"type": "agent_vm", "count": 2, "vcpus": 1, "ram_gb": 1, "disk_gb": 8, "passthrough_gpu": false, "target_node": "auto"}
  clamped_fields: []
```

(31 s is the cold model: the server had just restarted, so the prompt prefix was
not cached. Warm extraction is ~5.5 s.)

```
=== POST /vms/apply ===
HTTP=202 time=74.121416s
{"job_id": "01M4X3SBPM94X618S89825P2FV", "plan_id": "01M4X3S51D66GFPWCBWEMGSNQ0", "state": "ready",
 "vm_ids": ["termini-01m4x3s51d66gfpwcbwemgsn-0-0", "termini-01m4x3s51d66gfpwcbwemgsn-0-1"],
 "allocations": [{"index": 0, "status": "ready", "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
                  "realized_as": "vm", "created": 2, "already_applied": 0}]}
```

and on the node itself:

```
### node-a lxc list
+--------------------------------------+---------+-----------------------+
|                 NAME                 |  STATE  |         IPV4          |
+--------------------------------------+---------+-----------------------+
| demo-workload                        | STOPPED |                       |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 | RUNNING | 10.174.247.141 (eth0) |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 | RUNNING | 10.174.247.215 (eth0) |
| termini-01m4x3s51d66gfpwcbwemgsn-0-0 | RUNNING | 10.174.247.247 (eth0) |
| termini-01m4x3s51d66gfpwcbwemgsn-0-1 | RUNNING | 10.174.247.62 (eth0)  |
+--------------------------------------+---------+-----------------------+
```

The two `termini-01m4x3s51…` instances are the plan above. (They were deleted
again after the proof, and `POST /vms/reconcile` released the reservation:
`{"released": [{"plan_id": "01M4X3S51D66GFPWCBWEMGSNQ0", "reason": "instances_gone", …}],
"released_count": 1}` — no database was hand-edited, and the node was left as it was
found plus the inherited demo agents.)

---

## DEFECT C — where the ~66 s NL intent goes

Measured against the real model on node-c, seeder stopped:

| Call | Wall time | Why |
|---|---|---|
| First `POST /intents` after a controller restart | 27.1 s | cold prompt prefix: ~815 tokens of prefill |
| `extract()` directly, same messages, model warm | **5.5 s** | llama.cpp prefix cache: `prompt eval time = 423.50 ms / 5 tokens` |
| Second/third `POST /intents`, warm | 5.9 s | cache hit |
| First NL intent after the *model* was restarted (`864af2f`) | 31.0 s | cold prefix again |

Where it goes: the round-trip is a single synchronous CPU prefill of the frozen
extraction prompt (system + 3 few-shot examples + the user's sentence) plus a
short generation — **inherent to CPU inference on Gemma 3 4B**. The `.66 s` figure
is the cold/contended case. Nothing in the frozen decision contract needs to change
for it, and the cache makes the demo's repeated keystrokes ~5 s; the only
"contention" component is the subject of Defect A and is fixed there (the
extraction no longer waits behind a decision). Left as-is, deliberately.

---

## Gates

```
$ python3 -m unittest discover -s tests -t .
Ran 624 tests in 94.692s
OK
$ python3 scripts/check-doc-ulids.py
all example identifiers are 26-char ULIDs
$ python3 scripts/check-readme-links.py
checked 58 unique link/path target(s)
all README links and repo paths resolve
```

(602 → 624 tests: `tests/test_intent_backpressure.py`,
`tests/test_nl_deploy_defaults.py`, `tests/test_orphaned_reservations.py`. No
regressions.)

## Still wrong / unverified (disclosed)

1. **The decision is still minutes on CPU** — that is the model, not the fix. With
   the extraction on its own instance the two are independent, but a *decision*
   itself still takes ~400 s on the 20-sample window.
2. **node-a is small and now genuinely full of demo agents.** 5 GiB allocatable
   RAM / 4 vCPU of overcommit means two 2-agent deploys fit and a third does not.
   The default (1 GiB/agent) makes the demo repeatable, but the node is the limit;
   node-c (8 vCPU / 31 GiB) is the bigger compute node and is not used for agents
   because it serves the model (`_placement_excluded_hosts`).
3. **`intent_inference_base_url` defaults to empty** (shared endpoint). A
   deployment that does not set it gets the old coupling, minus the hard queue
   (`--parallel 2`); the shipped cloud-init and `setup-operator-node-c.sh` both
   set it.
4. **The dedicated instance is not yet in the standing node-c image.** It is in
   `infra/gcp/cloud-init/llama-serve.sh` (so a rebuild gets it) and was installed
   live by hand on the running node; a rebake is what makes it durable.
5. GPU path out of scope; node-b stays TERMINATED (quota).
