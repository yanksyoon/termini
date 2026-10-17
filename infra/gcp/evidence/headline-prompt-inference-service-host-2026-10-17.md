# Live: the headline prompt is ACCEPTED on the standing fleet — `inference_vm` resolves to the agent-facing inference host

Everything below is a real `gcloud` / `ssh` / `lxc` / `curl` run against the
**standing demo fleet** (the same hosts the judge-facing link uses), not a test
node and not a stub. Outputs are pasted verbatim; commands are in
[Reproduce](#reproduce).

**Headline:** the demo's headline prompt, exactly as a judge types it —
`deploy 3 worker agents for coding plus a fast LLM` — now returns **`200` with an
accepted plan**. The agent half (3 `agent_vm`) resolves to node-a and, when
applied, materialises **three RUNNING LXD instances**; the "fast LLM" half
(`inference_vm`) resolves to **node-c**, the node that serves the agent-facing
inference front door (`10.10.0.13:8080`, `backend=gemini-proxy`), and is recorded
as **`realized_as: host_service`** with `vm_ids: []` — no VM, no GPU, no committed
capacity. A genuinely wrong target still returns `422 invalid_target_node_type`,
now with a detail that names the node and what the resolver looked for.

## Provenance

| | |
|---|---|
| UTC window | 2026-10-17 03:12 → 03:36 |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2`, `34.93.190.64` |
| Deployed at proof time | `controller/app/server.py` sha256 `5dfa03ae3dbbc8cdb9ca7b27538190a441ebc52bf38370dcec508ecc4be8b9e7` (this branch, `wt/t_d7e0bf4e-inference-service-host` off `6a364c6`), installed 03:17:52 UTC, controller restarted → MainPID 85585. The pre-change file is preserved at `/tmp/server.py.pre-t_d7e0bf4e` (sha256 `01f21e87…`). |
| ⚠️ Later in the window | a **concurrent worker redeployed** `/opt/termini` at **03:27:20** (MainPID 88918, `server.py` sha256 `0b7d4b41…`, which is **not** on `origin/main` and contains release-time instance deletion that main does not). Every capture up to 03:27:17 ran on the tree above; the 03:29 release capture ran on that other tree. See §5. |
| node-a | `termini-node-a`, `10.10.0.11`, `e2-standard-2` (2 vCPU / 8 GiB), `node_id` `01M4TGD31WXH97JAA5S6A4R10D`, `role=compute` |
| node-c | `termini-node-c`, `10.10.0.13`, `e2-standard-8` (8 vCPU / 31 GiB), `node_id` `01M4ZQ7Q2J80ATY3K32PY3PPJH`, `role=compute`, runs `llama-cpp` (the inference proxy) |
| node-b | `TERMINATED` (unchanged: `CPUS_ALL_REGIONS` = 12) |
| Third real node | `01M53X03YFPR71J2SAZVRG37JY` (2 vCPU, `degraded`, lxd/sshd stopped) — another card's throwaway, seen in the fleet listing and **not touched** |
| Project | `01J0DEM0WARM000000000000AB` |
| Fleet | `gcloud compute instances list` → controller RUNNING, node-a RUNNING, node-b TERMINATED, node-c RUNNING |
| Plans exercised | `01M53XX9QJNP286TEY54QJAYXM` (inference-only smoke), `01M53Y2H2QXV99CF7EQHKW6583` (the exact headline prompt) |

## 1. The defect as found (carried from `t_ece9d14f` F1, PR #199)

`POST /v1/projects/01J0DEM0WARM000000000000AB/intents {"prompt":"deploy 3 worker
agents for coding plus a fast LLM"}` answered `422 GUARDRAIL_REJECTED` with

```
{"index":0,"reason":"exceeds_node_cpu_ceiling", … "node … had cpu_free=2 vCPU free …"}
{"index":1,"reason":"invalid_target_node_type",
 "detail":"inference_vm must resolve to the inference node"}
```

Two independent causes: (1) `role=inference` is GPU-gated (§4.1, `>= 12 GiB`
VRAM) and the post-pivot fleet has no GPU, so **no node could ever satisfy the
reference class**; (2) node-a still carried **2 vCPU / 2 GiB / 8 GB** committed
by plan `01M4WJHWQR3B8PCAV2TVQBGG79` (created 2026-10-14, never released), leaving
`cpu_free=2` — one short of 3 agents.

## 2. What changed

* `controller/app/server.py` — §5.2 step 3 now resolves an `inference_vm` in order:
  (1) a real `role=inference` node (unchanged, GPU path first), (2) **else the real
  node that serves the agent-facing inference front door** — the host of
  `TERMINI_INFERENCE_BASE_URL` (or an operator-named `TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`
  entry), (3) else `None` → `invalid_target_node_type`. The role rule in §4.1 is
  **not** changed; `realized_as` stays `host_service`; a seeder-fabricated row can
  never be the front-door host; the 422 detail now says what was looked for.
* `docs/contracts/controller-api.md` — §4.1 (role rule stays GPU-gated; the
  service-host path is a *resolution* rule), §5.2 step 3 (the ordered resolution),
  §5.3 table row.
* `docs/contracts/infra-decisions.md` — **D9 Amendment (2026-10-17)** recording the
  decision and the alternatives (a GPU node is blocked by the L4 quota; remapping
  the extractor would hide the defect instead of fixing the contract).
* `tests/test_inference_service_host_resolution.py` — 17 unit tests for the new
  path, including "no inference node registered" still rejecting and "the seeder's
  row is never the front-door host".

## 3. Live proof

### 3.1 The front door answers, and says what it is

`curl -i http://10.10.0.13:8080/health` from the controller:

```
HTTP/1.1 200 OK
Server: termini-inference-proxy/1.0 Python/3.12.3
Content-Type: application/json
Content-Length: 451
X-Termini-Backend: gemini-proxy
X-Termini-Answered-By: forwarded-managed-model
X-Termini-Model: gemma
X-Termini-Model-Id: gemini-2.5-flash
X-Termini-Egress: external
X-Termini-Residency: resident
X-Termini-External-Destination: aiplatform.googleapis.com
```

That is the address the controller is configured with
(`TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080`) and the address the
resolution rule matches on. Nothing here claims a local GPU or local weights.

### 3.2 The stale commit is released (cause 2)

`POST /v1/projects/01J0…AB/vms/release {"plan_id":"01M4WJHWQR3B8PCAV2TVQBGG79", "reason":"stale committed capacity …"}` → `200`:

```
{"plan_id": "01M4WJHWQR3B8PCAV2TVQBGG79", "status": "released",
 "released": {"cpu_allocated": 2, "ram_allocated_gb": 2, "disk_allocated_gb": 8},
 "rows_released": 1, "released_at": "2026-10-17T03:20:29Z"}
```

node-a before: `cpu_allocated 2 / ram_allocated_gb 2 / disk_allocated_gb 8`,
`cpu_free 2 / ram_free_gb 3 / disk_free_gb 44`.
node-a after: `cpu_allocated 0 / ram_allocated_gb 0 / disk_allocated_gb 0`,
`cpu_free 4 / ram_free_gb 5 / disk_free_gb 52`.

Its two instances (`termini-01m4wjhwqr3b8pcav2tvqbgg-0-0/-0-1`) stay **RUNNING** on
node-a — release returns capacity, it does not delete instances (on the tree that
was deployed at the time). Whether to drop them is the operator's demo-content
call (card `t_28ab1be5`); they were left untouched.

### 3.3 The reference class resolves to the service host

`POST …/intents` with a structured spec, `inference_vm` only → `200`:

```
{"plan_id": "01M53XX9QJNP286TEY54QJAYXM", "outcome": "accepted", …
 "allocations": [{"index": 0, "status": "accepted",
   "resolved": {"type": "inference_vm", "count": 1, "vcpus": 4, "ram_gb": 8, "disk_gb": 32,
                "passthrough_gpu": false, "target_node": "auto"},
   "guardrail": {"clamped_fields": [], "reject_reasons": []},
   "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH",
   "realized_as": "host_service", "vm_ids": []}]}
```

### 3.4 A genuinely wrong target still rejects, with a useful detail

`inference_vm` explicitly targeting node-a (`01M4TGD31…`, `role=compute`, not the
front door) → `422`:

```
{"error": {"code": "GUARDRAIL_REJECTED", "message": "allocation plan rejected by guardrail",
 "details": [{"index": 0, "reason": "invalid_target_node_type",
   "detail": "inference_vm must resolve to the inference node or to the node serving the
              agent-facing inference service: node 01M4TGD31WXH97JAA5S6A4R10D is neither an
              inference node (a node reporting a >= 12 GiB GPU) nor the host of the
              agent-facing inference service"}],
 "request_id": "01M53XYNGFZZMXPYKD2023P8N5"}}
```

### 3.5 THE EXACT PROMPT → `200` accepted

Request body, verbatim:

```json
{"prompt": "deploy 3 worker agents for coding plus a fast LLM"}
```

`POST /v1/projects/01J0DEM0WARM000000000000AB/intents` (03:21:18 → 03:21:49 UTC,
~29 s for the model round-trip) → **`HTTP 200`**:

```json
{"plan_id": "01M53Y2H2QXV99CF7EQHKW6583", "request_id": "01M53Y2H2RCWD7NHTH6F3FYM1B",
 "project_id": "01J0DEM0WARM000000000000AB", "outcome": "accepted",
 "created_at": "2026-10-17T03:21:47Z",
 "allocations": [
   {"index": 0, "status": "accepted",
    "resolved": {"type": "agent_vm", "count": 3, "vcpus": 1, "ram_gb": 1, "disk_gb": 8,
                 "passthrough_gpu": false, "target_node": "auto"},
    "guardrail": {"clamped_fields": [], "reject_reasons": []},
    "node_id": "01M4TGD31WXH97JAA5S6A4R10D", "realized_as": "vm", "vm_ids": []},
   {"index": 1, "status": "accepted",
    "resolved": {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "disk_gb": 64,
                 "passthrough_gpu": false, "target_node": "auto"},
    "guardrail": {"clamped_fields": [], "reject_reasons": []},
    "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH", "realized_as": "host_service", "vm_ids": []}],
 "audit": {"actor": "human", "guardrail": "termini.intent.clamp_validate"}}
```

No `reject_reasons`; no `422`.

### 3.6 Apply materialises three REAL RUNNING instances

`POST …/vms/apply {"plan_id":"01M53Y2H2QXV99CF7EQHKW6583"}` → `202` in **281 s**
(03:22:36 → 03:27:17; slower than the 52 s single-VM run of `t_ece9d14f` because
node-a was at ~83 % CPU while creating/starting three instances):

```json
{"job_id": "01M53Y4MC17T5MFVDJCXEJR8BB", "plan_id": "01M53Y2H2QXV99CF7EQHKW6583",
 "state": "ready",
 "vm_ids": ["termini-01m53y2h2qxv99cf7eqhkw65-0-0",
            "termini-01m53y2h2qxv99cf7eqhkw65-0-1",
            "termini-01m53y2h2qxv99cf7eqhkw65-0-2"],
 "allocations": [
   {"index": 0, "status": "ready", "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
    "realized_as": "vm",
    "vm_ids": ["termini-01m53y2h2qxv99cf7eqhkw65-0-0",
               "termini-01m53y2h2qxv99cf7eqhkw65-0-1",
               "termini-01m53y2h2qxv99cf7eqhkw65-0-2"],
    "created": 3, "already_applied": 0},
   {"index": 1, "status": "ready", "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH",
    "realized_as": "host_service", "vm_ids": [],
    "detail": "host_service reference; no VM materialised"}]}
```

The node's own `lxc list` immediately after the apply (node-a, via IAP ssh):

```
NAME                                   STATE    IPV4                    MEMORY USAGE
demo-workload                          RUNNING
termini-01m4wjhwqr3b8pcav2tvqbgg-0-0   RUNNING  10.174.247.141 (eth0)   94.80MiB
termini-01m4wjhwqr3b8pcav2tvqbgg-0-1   RUNNING  10.174.247.215 (eth0)   93.16MiB
termini-01m53y2h2qxv99cf7eqhkw65-0-0   RUNNING  10.174.247.17 (eth0)   146.58MiB
termini-01m53y2h2qxv99cf7eqhkw65-0-1   RUNNING  10.174.247.249 (eth0)  208.32MiB
termini-01m53y2h2qxv99cf7eqhkw65-0-2   RUNNING                          128.94MiB
termini-01m537g26sm8qgkmztn8stbt-0-0   STOPPED
termini-01m537km0trb189a6e1gz546-0-0   RUNNING  10.174.247.138 (eth0)   95.16MiB
verif-fail                             RUNNING  10.174.247.11 (eth0)   111.24MiB
verif-proof                            RUNNING  10.174.247.176 (eth0)  204.09MiB
```

### 3.7 Capacity moves exactly, and only for the agent half

node-a after the apply: `cpu_allocated 3 / ram_allocated_gb 3 / disk_allocated_gb
24`, `cpu_free 1 / ram_free_gb 2 / disk_free_gb 28` — exactly 3 × (1 vCPU, 1 GiB,
8 GB). The `inference_vm` reference committed **nothing** (step 7 still skips it).

### 3.8 Cleanup — the fleet is back to its baseline

The three instances were deleted (`sudo lxc delete -f termini-01m53y2h2…-0-0 …-0-1 …-0-2`,
rc 0) and the plan released:

```
{"plan_id": "01M53Y2H2QXV99CF7EQHKW6583", "status": "released",
 "released": {"cpu_allocated": 3, "ram_allocated_gb": 3, "disk_allocated_gb": 24},
 "rows_released": 1, "released_at": "2026-10-17T03:29:22Z",
 "deleted": ["termini-01m53y2h2qxv99cf7eqhkw65-0-0",
             "termini-01m53y2h2qxv99cf7eqhkw65-0-1",
             "termini-01m53y2h2qxv99cf7eqhkw65-0-2"],
 "failed": [], "unknown_instances": false}
```

(The `deleted` key comes from the tree the concurrent worker deployed at 03:27:20,
not from this branch — on this branch release returns capacity only. See §5.)

Final state, 03:35 UTC:

```
NAME                                   STATE    IPV4                    MEMORY USAGE
demo-workload                          RUNNING  10.174.247.148 (eth0)  198.18MiB
termini-01m4wjhwqr3b8pcav2tvqbgg-0-0   RUNNING  10.174.247.141 (eth0)   93.26MiB
termini-01m4wjhwqr3b8pcav2tvqbgg-0-1   RUNNING  10.174.247.215 (eth0)   93.03MiB
termini-01m537g26sm8qgkmztn8stbt-0-0   STOPPED
termini-01m537km0trb189a6e1gz546-0-0   RUNNING  10.174.247.138 (eth0)   93.69MiB
verif-fail                             RUNNING  10.174.247.11 (eth0)   109.99MiB
verif-proof                            RUNNING  10.174.247.176 (eth0)  167.66MiB
```

node-a capacity: `cpu_allocated 0 / ram_allocated_gb 0 / disk_allocated_gb 0`,
`cpu_free 4 / ram_free_gb 5 / disk_free_gb 52` — identical to the state after §3.2,
i.e. exactly the baseline minus the stale commit. The demo workload and the
pre-existing instances were never touched (the same seven container names are
present before and after; `demo-workload` shows an IPv4 in this later capture where
the earlier one had a blank column — not this run's doing, and its state is
`RUNNING` in both).

## 4. Tests

* `python3 -m unittest tests.test_inference_service_host_resolution -v` → **17 tests OK**
  (front-door resolution, GPU-path precedence, operator-named host, no-match
  rejection, no-config rejection, synthetic row never chosen, explicit-target
  cases, plan-level accepted/rejected headline prompt, capacity committed only for
  the agent half).
* `python3 -m unittest discover -s tests -t .` → **983 tests, OK** (457 s).
* `python3 scripts/check-doc-ulids.py` → `all example identifiers are 26-char ULIDs`;
  `python3 scripts/check-readme-links.py` → `all README links and repo paths resolve`.

## 5. What this does NOT prove, and the one operational caveat

* **The live controller is not currently running this branch.** At 03:27:20 a
  concurrent worker restarted `termini-controller` with a different tree
  (`server.py` sha256 `0b7d4b41…`) that is **not** on `origin/main`. The headline
  prompt on the standing link therefore only works again once **merged `main`** is
  deployed there (demo-refresh card `t_4c293d8d` is the vehicle). This card's live
  acceptance was measured **inside its own deploy window** (03:17:52–03:27:17),
  and the code that produced it is the commit this evidence lands with.
* **Inference does not run on a GPU.** The service host answers through the
  managed-model proxy (`X-Termini-Backend: gemini-proxy`,
  `X-Termini-External-Destination: aiplatform.googleapis.com`). `realized_as:
  host_service` is exactly what happens; the plan's `node_id` names node-c, whose
  `role` stays `compute`. Nothing here should be read as "a GPU node exists".
* **Release-time instance deletion** is not part of this change (the `deleted` key
  in §3.8 came from the other tree). On this branch release returns capacity and
  leaves instances RUNNING — the known defect on card `t_28ab1be5`.
* The 3-agent apply is slow (~4m41s) on node-a, which was at ~83 % CPU. A client
  with a 60–120 s timeout will give up before the server finishes (same finding as
  `t_ece9d14f`); a replay reports `already_applied`.

## Reproduce

```bash
# fleet
gcloud compute instances list

# the front door (read-only)
gcloud compute ssh termini-controller --zone=asia-south1-a \
  --command='curl -s -i http://10.10.0.13:8080/health'

# release the stale commit (idempotent; a repeat reports already_released)
#   POST /v1/projects/01J0DEM0WARM000000000000AB/vms/release
#   {"plan_id":"01M4WJHWQR3B8PCAV2TVQBGG79","reason":"stale committed capacity"}

# the exact prompt
#   POST /v1/projects/01J0DEM0WARM000000000000AB/intents
#   {"prompt":"deploy 3 worker agents for coding plus a fast LLM"}
# then
#   POST /v1/projects/01J0DEM0WARM000000000000AB/vms/apply {"plan_id":"<plan_id>"}
#   POST /v1/projects/01J0DEM0WARM000000000000AB/vms/release {"plan_id":"<plan_id>"}

# node-side view
gcloud compute ssh termini-node-a --zone=asia-south1-a --tunnel-through-iap \
  --command='sudo lxc list -c ns4m --format compact'

# the controller's project token lives in its unit file — do not echo it
gcloud compute ssh termini-controller --zone=asia-south1-a \
  --command='sudo sed -n "s/^Environment=TERMINI_PROJECT_TOKEN=//p" /etc/systemd/system/termini-controller.service'
```
