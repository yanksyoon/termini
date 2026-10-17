# Live: "apply plan" materialises real instances on the standing fleet — RAW EVIDENCE

Everything below is a real `gcloud` / `ssh` / `lxc` / `curl` run against the
**standing demo fleet** (the same hosts the judge-facing link uses), not a test
node and not a stub. Outputs are pasted verbatim; the commands are in
[Reproduce](#reproduce).

**Headline:** applying an accepted plan created a REAL LXD instance on node-a,
left it **RUNNING**, and three independent views agree on it — the node's own
`lxc list`, the node's own telemetry (ingested by the controller), and the
controller's authenticated LXD REST client. Committed capacity moved by exactly
the requested amount and moved back on release. A replay is a no-op. A
materialisation that fails releases the capacity and reports `failed`.

**The run also found five things that matter more than the headline** (§7) —
including that the demo's headline prompt is currently a guaranteed `422` on this
fleet. Read §7 before the recording.

## Provenance

| | |
|---|---|
| UTC window | 2026-10-17 02:34:39 → 02:54:14 |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2`, `34.93.190.64` / `34-93-190-64.sslip.io` |
| Deployed controller tree | `DEPLOYED_COMMIT` = `d18532d9524c65d61c76fcd9f5404c8835bffaab` (read back from `/opt/termini` on the VM). `origin/main` when this was written is `58ab595`; the only difference is one evidence document (`git diff --stat d18532d 58ab595` → 1 file, docs), so the verified tree is code-identical to main |
| node-a | `termini-node-a`, `10.10.0.11`, `e2-standard-2` (2 vCPU / 8 GiB), `node_id` `01M4TGD31WXH97JAA5S6A4R10D` |
| node-c | `termini-node-c`, `10.10.0.13`, `e2-standard-8` (8 vCPU / 31 GiB), `node_id` `01M4ZQ7Q2J80ATY3K32PY3PPJH` |
| node-b | `TERMINATED` (deliberate: `CPUS_ALL_REGIONS` = 12 is consumed by the running three) |
| Project | `01J0DEM0WARM000000000000AB` |
| Project token | not recorded. `sha256(unit-file bytes)[:12]` = `04ead81ad7c7` |
| Plans exercised | `01M53VCTFMNMSEKF4JFVTEB91B` (1 VM), `01M53W8CS85XQ7KCGT9T3F2ZSD` (2 VMs), `01M53W39BG14JYQXC1YFX7YJ6W` (forced failure on node-c) |
| LXD on node-a | server version `5.21.8`, image alias `agent-vm/2026.10.0` = `agent-os/w4` = fingerprint `c3d039642a61` |

## 1. An accepted plan

`POST /v1/projects/01J0…AB/intents` with a structured spec
`[{type: agent_vm, count: 1, vcpus: 1, ram_gb: 1, disk_gb: 4, target_node: auto}]`
→ `200`, `outcome: accepted`, resolved to node-a:

```
"plan_id": "01M53VCTFMNMSEKF4JFVTEB91B",
"allocations": [{"index": 0, "status": "accepted", "node_id": "01M4TGD31WXH97JAA5S6A4R10D",
                 "realized_as": "vm", "vm_ids": []}]
```

## 2. The apply returns a terminal state and a REAL vm_id

`POST /v1/projects/01J0…AB/vms/apply {"plan_id": "01M53VCTFMNMSEKF4JFVTEB91B"}`
→ `202` in **52.3 s**:

```
{"job_id": "01M53VD720Q92N67W601JF5G80",
 "plan_id": "01M53VCTFMNMSEKF4JFVTEB91B",
 "state": "ready",
 "vm_ids": ["termini-01m53vctfmnmsekf4jfvteb9-0-0"],
 "allocations": [{"index": 0, "status": "ready",
   "node_id": "01M4TGD31WXH97JAA5S6A4R10D", "realized_as": "vm",
   "vm_ids": ["termini-01m53vctfmnmsekf4jfvteb9-0-0"],
   "created": 1, "already_applied": 0}]}
```

`state` is terminal (`ready`, not `provisioning`), and `vm_ids` names an instance
that exists (verified next). The name is the deterministic
`termini-<plan>-<index>-<slot>`, which is what makes a replay safe.

## 3. VERIFIED ON THE NODE — three independent views

### 3a. The node's own `lxc list` (over IAP SSH, on node-a)

```
Sat Oct 17 02:39:27 UTC 2026
=== lxc list node-a (after apply) ===
                  NAME                   STATE           IPV4           MEMORY USAGE
  demo-workload                         RUNNING  10.174.247.148 (eth0)  209.48MiB
  termini-01m4wjhwqr3b8pcav2tvqbgg-0-0  RUNNING  10.174.247.141 (eth0)  94.11MiB
  termini-01m4wjhwqr3b8pcav2tvqbgg-0-1  RUNNING  10.174.247.215 (eth0)  97.80MiB
  termini-01m53vctfmnmsekf4jfvteb9-0-0  RUNNING  10.174.247.137 (eth0)  93.22MiB
  termini-01m537g26sm8qgkmztn8stbt-0-0  STOPPED
  termini-01m537km0trb189a6e1gz546-0-0  RUNNING  10.174.247.138 (eth0)  93.73MiB
=== limits of the created instance ===
architecture: x86_64
config:
  image.architecture: x86_64
  image.description: Ubuntu 24.04 LTS server (20260926)
  image.os: ubuntu
  image.release: noble
  limits.cpu: "1"
  limits.memory: 1GiB
  volatile.base_image: c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4
  volatile.last_state.power: RUNNING
devices: {}
ephemeral: false
profiles:
- default
```

So: created **RUNNING** (not merely created), the requested `limits.cpu` /
`limits.memory` were honoured, and it boots from the baked workload image
(`c3d039642a61`, the `agent-vm/2026.10.0` alias the controller launches) — not
from a hand-made stand-in.

### 3b. The node's own telemetry, ingested by the controller

The node derives `lxd_instances[].vm_id` as
`26 Crockford chars of SHA-256("termini-vm-id:" + <instance name>)`
(`infra/bootstrap/node/termini-bootstrap.py`, `controller/app/operator/vmids.py`).
Deriving it from the name the apply returned and looking for it in the node's
newest sample:

```
{"instance_name": "termini-01m53vctfmnmsekf4jfvteb9-0-0",
 "derived_vm_id": "ZJ4WEAV8DW7X7WMW5KE8QSTHPB",
 "telemetry_http": 200,
 "sample_ts": "2026-10-17T02:40:29Z",
 "node_reports_this_instance": true,
 "entry": {"vm_id": "ZJ4WEAV8DW7X7WMW5KE8QSTHPB", "state": "running",
           "ram_used_bytes": 97652736, "ram_limit_bytes": 1073741824,
           "disk_used_bytes": 0, "cpu_util_pct": 0.06}}
```

The instance the controller says it made is the instance the NODE reports
running, with the right 1 GiB limit.

### 3c. The controller's authenticated LXD REST client

Run **on the controller** with the repo's own tool
(`python3 -m app.lxdtrust verify`, which probes an authenticated
`GET /1.0/instances`):

```
https://termini-node-a…internal:8443: TRUSTED — authenticated GET /1.0/instances -> 200
https://termini-node-c…internal:8443: NOT TRUSTED — GET /1.0/instances -> HTTP 403 (controller cert not trusted)
```

The controller is a **trusted** client of node-a (200, not 403). Its instance
listing agrees with the node's own `lxc list`:

```
"/1.0":           {"status": 200, "server_version": "5.21.8"}
"/1.0/instances": {"status": 200,
  "instance_urls": ["/1.0/instances/demo-workload",
                    "/1.0/instances/termini-01m4wjhwqr3b8pcav2tvqbgg-0-0",
                    "/1.0/instances/termini-01m4wjhwqr3b8pcav2tvqbgg-0-1",
                    "/1.0/instances/termini-01m537g26sm8qgkmztn8stbt-0-0",
                    "/1.0/instances/termini-01m537km0trb189a6e1gz546-0-0",
                    "/1.0/instances/termini-01m53vctfmnmsekf4jfvteb9-0-0"]}
```

The controller is a **trusted** client of node-a (200, not 403) and sees the same
set the node does.

## 4. Committed capacity matches what was created

`GET /v1/projects/…/nodes/01M4TGD31…` before → after the apply:

| | cpu_allocated | ram_allocated_gb | disk_allocated_gb |
|---|---|---|---|
| before | 2 | 2 | 8 |
| after | 3 | 3 | 12 |

+1 vCPU / +1 GiB / +4 GiB — exactly the accepted plan. The commit row carries the
instances it materialised, which is what reconciliation and release read:

```
plan_id 01M53VCTFMNMSEKF4JFVTEB91B  node 01M4TGD31WXH97JAA5S6A4R10D
  cpu 1, ram 1, disk 4, created_at 2026-10-17T02:34:59Z
  vm_ids_json ["termini-01m53vctfmnmsekf4jfvteb9-0-0"]
```

## 5. A replay is a no-op (idempotent)

Same plan applied again, instances already running:

```
{"state": "ready", "vm_ids": ["termini-01m53vctfmnmsekf4jfvteb9-0-0"],
 "allocations": [{"status": "ready", "created": 0, "already_applied": 1}]}
```

3.6 s, and the node still has exactly one instance for the plan
(`lxc list termini-01m53vctfmnmsekf4jfvteb9`):

```
  termini-01m53vctfmnmsekf4jfvteb9-0-0  RUNNING  10.174.247.137 (eth0)
```

## 6. Two VMs in one allocation, and a forced failure

### 6a. Fan-out: `count: 2` → two instances, both RUNNING

Plan `01M53W8CS85XQ7KCGT9T3F2ZSD`, `created: 2`; on the node:

```
  termini-01m53w8cs85xq7kcgt9t3f2z-0-0  RUNNING  10.174.247.8 (eth0)  150.96MiB
  termini-01m53w8cs85xq7kcgt9t3f2z-0-1  RUNNING                       89.98MiB
```

**Timing, recorded honestly:** this apply exceeded the 120 s timeout of the
client used to drive it (the client raised `TimeoutExpired`; the request had
already gone out). The server kept going and finished: re-applying the same plan
returned `state: ready, created: 0, already_applied: 2` in 0.7 s, and both
instances were `RUNNING` on the node. A 2-VM apply on this 2-vCPU node takes
~2 minutes — see finding F5.

### 6b. Failure: a create that cannot succeed keeps no capacity

Plan `01M53W39BG14JYQXC1YFX7YJ6W` explicitly targeted node-c, where the
controller's client identity is not (yet) in LXD's trust store:

```
{"state": "failed", "vm_ids": [],
 "allocations": [{"index": 0, "status": "failed",
   "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH", "vm_ids": [],
   "detail": "create termini-01m53w39bg14jyqxc1yfx7yj-0-0 failed: HTTP 403:
              {\"type\":\"error\",\"error_code\":403,\"error\":\"Forbidden\"…}",
   "rolled_back": 0, "capacity_released": true}]}
```

0.5 s, no vm_id, `capacity_released: true`. Node-c's capacity is `0/8 cpu,
0/31 GB, 0/57 GB` afterwards and `lxc list` on node-c is **empty** — no capacity
is claimed for an instance that does not exist, and nothing was half-created.

The failure also shows the diagnosis is useful rather than a generic error: the
detail names the instance and the HTTP status.

## 7. FINDINGS (read these before the recording)

### F1. The headline prompt is a guaranteed `422` on the current fleet

`POST /intents {"prompt": "deploy 3 worker agents for coding plus a fast LLM"}`
(the demo's headline prompt, exactly as a judge types it) in 60 s:

```
HTTP 422 GUARDRAIL_REJECTED
"details": [
  {"index": 0, "reason": "exceeds_node_cpu_ceiling",
   "detail": "…node 01M4TGD31WXH97JAA5S6A4R10D had cpu_free=2 vCPU free and the
              request needed 3×1 vCPU / 3×1 GiB RAM (already allocated 2 vCPU /
              2 GiB RAM)"},
  {"index": 1, "reason": "invalid_target_node_type",
   "detail": "inference_vm must resolve to the inference node"}]
```

Two independent causes, both structural:

1. **No node can be an inference node.** `role.py` assigns `inference` only to a
   node reporting a GPU with `vram_bytes >= 12 GiB` (frozen, controller-api.md
   §4.1), and `_resolve_target` resolves an `inference_vm` only to a node whose
   `role == "inference"` (`server.py` §5.2 step 3). The fleet is CPU-only after
   the proxy pivot (`t_957956e5` / `t_3fcee90b`), so **every** `inference_vm` is
   rejected — including the "plus a fast LLM" half of the headline prompt. The
   only `inference`-role row left in the DB is the SEEDER's synthetic node
   (`01M4NZK474BVARFBXYQBMVA8GV`), and `_selection_pool` correctly excludes
   synthetic rows while a real node is registered, so it cannot rescue the
   placement. This is the direct consequence of #132 (no allocation onto a
   seeder-fabricated node) landing on a GPU-less fleet: the seed no longer
   provides the only inference node, and nothing real replaced it.
2. **node-a has 2 vCPU of stale committed capacity**, so only 2 of the 3
   requested vCPU are free. The commit is `plan 01M4WJH…` from **2026-10-14
   06:45** (`released_at: null`), whose instances are still running. Releasing
   that plan alone would make the agent half fit (cpu_free 2 → 4).

**Consequence:** `t_270d09b9` step 05 ("the accepted plan: 200 accepted… no
guardrail rejection") and `t_2191eca4` step 09 cannot be captured as written
until this is decided. It is not a materialisation defect — apply works — but it
kills the judge's path.

### F2. node-c is not a trusted LXD target

From the controller, against node-c:

```
/1.0           -> 200      (reachable)
/1.0/instances -> 403 {"error": "Forbidden"}
```

The trust flow (#129) was installed on node-a only. Any allocation that resolves
to node-c therefore fails (6b). Agent placement currently avoids node-c by host
exclusion (`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS=10.10.0.13`), so the demo path
does not hit it today — but an explicit `target_node` naming node-c does, and the
fleet is not uniformly addressable.

### F3. Release does not delete the instances it materialised

`POST /vms/release` returns the capacity (`released: {cpu 1, ram 1, disk 4}`,
`rows_released: 1`) and **leaves the LXD instance running** — there is no
`delete_instance` on the release path (`server.py::_release_plan` →
`store.release_allocations` only). node-a already carries the result:

```
  termini-01m4wjh…-0-0  RUNNING   (plan 01M4WJH…, 2026-10-14, still committed)
  termini-01m4wjh…-0-1  RUNNING
  termini-01m537g…-0-0  STOPPED   (plan released 2026-10-16, vm_ids_json null)
  termini-01m537km…-0-0 RUNNING   (plan released 2026-10-16 20:50Z, release_reason released_by_user)
```

Two of those four belong to plans that are **already released** (they hold no
capacity) and one is a STOPPED leftover from an interrupted apply. On a 25-day
live window this is a slow RAM leak on the 8 GiB compute node, and it makes the
"reset for the next visitor" story misleading: capacity is released, the
containers keep running. This run deleted its own instances explicitly, which is
why the node is back at its baseline.

### F4. `GET /nodes` does not expose `lxd_endpoint`

The DB row for node-a has `lxd_endpoint = 10.10.0.11:8443`, and that is what the
apply dialled (so #128's per-node addressing IS in play — the deployed node image
reports its endpoint, contrary to the 2026-10-12 run which needed the documented
global fallback). But the API's node JSON omits the field, so an operator reading
`GET /nodes` cannot see which address will be used.

### F5. A 2-VM apply takes ~2 minutes, synchronously

6a measured >120 s end-to-end for two instances on node-a (each create waits for
its LXD operation, then a start waits for the start operation). The request is
synchronous: a client with a 60–120 s timeout will see a timeout while the
instances are still coming up, and the plan has in fact already committed. For
the dashboard's apply button this is a UX trap; for the recording it means
"instances materialise" takes minutes, not seconds.

## 8. Not verified (named, with the reason)

1. **The start-failure rollback path (`delete_instance(force=True)` after a
   failed start) was not exercised live.** The live failure in 6b failed at
   CREATE, so nothing needed rolling back (`rolled_back: 0`). Forcing a real LXD
   start failure on demand is not available on the standing fleet; the path stays
   unit-tested only.
2. **The global-fallback branch was not exercised in this run.** node-a reports
   its own endpoint (F4), so the per-node branch was taken. The fallback remains
   covered by unit tests and the 2026-10-12 live run
   (`infra/gcp/evidence/plan-materialisation-running-2026-10-12.md` §8.2).
3. **The client UI was not exercised** — that is `t_270d09b9` (browser work).
4. **Node-c materialisation is unproven** for the reason in F2 (403).

## 9. Cleanup — proven

```
Sat Oct 17 02:53:43 UTC 2026
rc=0
                  NAME                   STATE           IPV4
  demo-workload                         RUNNING
  termini-01m4wjhwqr3b8pcav2tvqbgg-0-0  RUNNING
  termini-01m4wjhwqr3b8pcav2tvqbgg-0-1  RUNNING
  termini-01m537g26sm8qgkmztn8stbt-0-0  STOPPED
  termini-01m537km0trb189a6e1gz546-0-0  RUNNING
  verif-proof                           STOPPED   <- appeared during the run; another worker's, not touched
```

(release: `status: released`, `released {cpu 2, ram 2, disk 8}`, `rows_released: 1`)

Capacity after cleanup — exactly the pre-run baseline:

```
01M4TGD31WXH97JAA5S6A4R10D   compute  cpu 2/2  ram 2/7 GB  disk 8/57 GB  free(cpu/ram) 2/3
01M4ZQ7Q2J80ATY3K32PY3PPJH   compute  cpu 0/8  ram 0/31 GB  disk 0/57 GB  free(cpu/ram) 16/29
```

The fleet was **left RUNNING** (controller + node-a + node-c; node-b stays
TERMINATED). `demo-workload` — the instance the operator's loop drives — was
never touched.

## Reproduce

```bash
# 1. the fleet
gcloud compute instances list --format="value(name,status,zone.basename(),machineType.basename(),networkInterfaces[0].networkIP,networkInterfaces[0].natIP)"

# 2. node-side view (IAP ssh: node-a/node-c have no external IP)
gcloud compute ssh termini-node-a --zone=asia-south1-a --tunnel-through-iap \
  --command='sudo lxc list -c ns4m --format compact'

# 3. controller-side trust probe (the repo's own tool; read-only authenticated GET)
gcloud compute ssh termini-controller --zone=asia-south1-a \
  --command='cd /opt/termini/controller; sudo TERMINI_DB_PATH=/data/controller.db \
     PYTHONPATH=/opt/termini/controller python3 -m app.lxdtrust verify \
     --node https://termini-node-a.asia-south1-a.c.termini-510609.internal:8443'

# 4. the loop itself holds the project token: do not echo it
gcloud compute ssh termini-controller --zone=asia-south1-a \
  --command='sudo sed -n "s/^Environment=TERMINI_PROJECT_TOKEN=//p" /etc/systemd/system/termini-controller.service'
# then, with $TOKEN:
#   POST /v1/projects/01J0DEM0WARM000000000000AB/intents   {"spec":{"allocations":[…]}}
#   POST /v1/projects/01J0DEM0WARM000000000000AB/vms/apply {"plan_id":"…"}
#   POST /v1/projects/01J0DEM0WARM000000000000AB/vms/release {"plan_id":"…"}
#   DELETE on the node: sudo lxc delete -f <instance>   (release does NOT do this — F3)
```
