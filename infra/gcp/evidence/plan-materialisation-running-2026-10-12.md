# Live: plan materialisation now leaves instances RUNNING — RAW EVIDENCE

Fixes the gap #130 left open ("instances exist" vs "agents are running"). Everything
below is a real `gcloud` / `ssh` / `curl` / `lxc` run against GCP; outputs are pasted
verbatim.

**Headline: the fix is PROVEN live.** `lxc list` ON THE NODE shows both instances
`RUNNING`, and the controller's own authenticated LXD client agrees.
**But the run also found TWO real defects in the first version of this change**, both
fixed here and evidenced below (§6, §7). Read §8 for what was *not* verified and the
concurrent-worker collision that complicated the run.

## Provenance

| | |
|---|---|
| Date | 2026-10-12 |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2` / external `34.93.190.64`, `34-93-190-64.sslip.io` |
| Controller code | this PR's tree (`git archive HEAD controller`), extracted over `/opt/termini` (a plain copy, not a git checkout) |
| Test node | `termini-live-test`, `asia-south1-a`, `n2-standard-2`, internal `10.10.0.7`, image family `termini-node-os` (→ `termini-node-os-3`) |
| node_id | `01M4RMC0RXYEG74704MS77ZDC6` |
| Project / token | `01J0DEM0WARM000000000000AB` / `demo-project-token` |
| Final plan | `01M4RN4P3TREC1FM575E37QHZ5` |
| Cost | controller + one `n2-standard-2` for ~20 min ≈ **$0.05** |

## 1. Deploy + trust

Controller client identity (minted previously, still valid):

```
LXD client identity ready: /data/lxd-client/controller-lxd-client.crt (fingerprint=a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5)
```

Node: the pre-built image predates #127, so the `agent-vm` alias the controller
launches was missing. Created it from the cached image (a stand-in for what the bake
now does):

```
sudo lxc image alias create agent-vm ee3016f85cc4
| agent-vm | ee3016f85cc4 | no | ubuntu 24.04 LTS amd64 (release) (20260926) | CONTAINER | 267.06MiB |
```

Trust, observable before/after (#129's flow):

```
BEFORE: https://10.10.0.7:8443: NOT TRUSTED — GET /1.0/instances -> HTTP 403 (controller cert not trusted)
AFTER : https://10.10.0.7:8443: TRUSTED — authenticated GET /1.0/instances -> 200
```

## 2. Apply an accepted plan

```
POST /v1/projects/01J0DEM0WARM000000000000AB/vms/apply  {"plan_id":"01M4RN4P3TREC1FM575E37QHZ5"}
-> 202
{"state":"ready",
 "vm_ids":["termini-01m4rn4p3trec1fm575e37qh-0-0","termini-01m4rn4p3trec1fm575e37qh-0-1"],
 "allocations":[{"status":"ready","node_id":"01M4RMC0RXYEG74704MS77ZDC6","realized_as":"vm",
   "vm_ids":["...-0-0","...-0-1"],"created":2,"already_applied":0}]}
```

## 3. VERIFIED ON THE NODE — the decisive check

`sudo lxc list` **on the node**:

```
+--------------------------------------+---------+
|                 NAME                 |  STATE  |
+--------------------------------------+---------+
| demo-workload                        | RUNNING |
| termini-01m4rn4p3trec1fm575e37qh-0-0 | RUNNING |
| termini-01m4rn4p3trec1fm575e37qh-0-1 | RUNNING |
+--------------------------------------+---------+
```

Requested limits honoured (`lxc config show`):

```
termini-01m4rn4p3trec1fm575e37qh-0-0:  limits.cpu "1"  limits.memory 1GiB
termini-01m4rn4p3trec1fm575e37qh-0-1:  limits.cpu "1"  limits.memory 1GiB
```

The controller's **own authenticated LXD client** sees them too:

```
GET https://10.10.0.7:8443/1.0/instances  -> HTTP 200
['/1.0/instances/demo-workload',
 '/1.0/instances/termini-01m4rn4p3trec1fm575e37qh-0-0',
 '/1.0/instances/termini-01m4rn4p3trec1fm575e37qh-0-1']
```

Committed capacity for the plan (controller DB):

```
plan 01M4RN4P3TREC1FM575E37QHZ5  node 01M4RMC0RXYEG74704MS77ZDC6  cpu=2 ram_gb=2 disk_gb=16
```

## 4. Replay is a no-op (idempotent)

Re-applying the SAME plan while the instances are already running:

```
{... "state":"ready",
 "allocations":[{"status":"ready","created":0,"already_applied":2, ...}]}
```

- create → duplicate name → `already_applied`
- start  → LXD's already-running answer → `already_applied` (see §6)
- **no teardown**, instances still `RUNNING` afterwards, capacity untouched.

```
+--------------------------------------+---------+
| termini-01m4rn4p3trec1fm575e37qh-0-0 | RUNNING |
| termini-01m4rn4p3trec1fm575e37qh-0-1 | RUNNING |
```

## 5. The create really is STOPPED without a start

Confirmed live that LXD's `POST /1.0/instances` leaves the instance STOPPED (the
measured defect), so the start is what makes the difference — the run above used the
same create path plus `start_instance`.

## 6. Defect found live #1 — the operation's error was discarded

The first version of this change **failed the replay**:

```
{"state":"failed",
 "allocations":[{"status":"failed",
   "detail":"start termini-...-0-0 failed: operation Failure: /1.0/operations/6035f2dc-..."}]}
```

LXD does not answer a refused start synchronously — it returns `202` + an operation
that then FAILS, and the reason lives in the operation's own `err`:

```
sudo lxc start <running-instance>
Error: The instance is already running
```

`wait_operation` kept only the operation status and threw the `err` away, so
"already running" was indistinguishable from a real failure. Fixed: `wait_operation`
now surfaces `metadata.err`, and `start_instance` folds the already-running answer to
`already_applied`. Re-verified live in §4.

## 7. Defect found live #2 — rollback could not delete a RUNNING instance

An allocation that fails *after* a successful start rolls back by deleting its
instances; LXD refuses to delete a running instance, so the "partial instances
deleted" half of the all-or-nothing rule was silently not happening. Fixed:
`delete_instance(force=True)` stops the instance (and waits for the stop operation)
before deleting. Covered by unit tests; the live path that would exercise it (a
forced start failure) was not triggered on the node.

## 8. What was NOT verified / caveats

1. **The start-failure → failed-allocation path was not exercised live.** Forcing a
   real LXD start failure on demand was not available; it is covered by unit tests
   (`tests/test_apply_materialisation.py`, `tests/test_operator_materialisation.py`).
2. **The node's own endpoint was set by hand.** The baked daemon predates #128's
   `machine.lxd.endpoint` reporting, so the documented fallback is the path. To keep
   the run working while a concurrent worker repeatedly overrode the *global*
   `TERMINI_LXD_BASE_URL`, the node row's `lxd_endpoint` was set to
   `https://10.10.0.7:8443` — a legitimate per-node addressing test, but it means the
   node-reported-endpoint branch is still only unit-tested, not observed from a live
   registration.
3. **Concurrent-worker collision (not a code issue, but it shaped the run).** Another
   worker was live on the SAME shared controller during this run. They created a
   second node (`termini-op-node`, `10.10.0.8`) and added
   `/etc/systemd/system/termini-controller.service.d/zz-op-live-test.conf`, which
   sorts after this run's drop-in and set `TERMINI_LXD_BASE_URL=https://10.10.0.11:8443`
   (node-a, TERMINATED). That made the first applies time out against a dead node
   until the per-node endpoint (caveat 2) removed the dependency. They also restarted
   the controller mid-run. The fix itself is unaffected; the colliding global variable
   is why an earlier version of this run looked broken.
4. `termini-op-node` is **left RUNNING** — it belongs to the concurrent worker and was
   not created by this task, so it was not stopped or deleted.

## 9. Cleanup — proven

```
NAME                ZONE           STATUS
termini-controller  asia-south1-a  TERMINATED
termini-node-a      asia-south1-a  TERMINATED
termini-node-b      asia-south1-a  TERMINATED
termini-node-c      asia-south1-a  TERMINATED
termini-op-node     asia-south1-a  RUNNING     <- concurrent worker's; not this task's

https://34-93-190-64.sslip.io  -> 000
gcloud ... describe termini-live-test -> not found   (deleted)
```

The two LXD instances created on the node were force-deleted, and this run's
`zz-live-test.conf` drop-in was removed from the controller.
