# Node telemetry: `lxd_instances[]` + `services[]` from a real node — `t_8b66366d`

**Card**: `t_8b66366d` — "Node telemetry must report lxd_instances[] + services[]
(contract 4.6) — real nodes report none". **Owner**: `infra`.
**Branch/PR**: `wt/t_8b66366d-node-instances` (see the card for commit + PR + merge sha).
**Date**: 2026-10-15 (UTC). **Host**: verification host (local LXD, no GCP spend).

## 1. The defect, measured on the live fleet before it was paused

Raw samples pulled back from the **controller's own store** by `node-a` itself
(node-token endpoint `/v1/nodes/{nid}/telemetry/recent`, i.e. the ingested,
validated sample) at **2026-10-15T16:16:1xZ**, while `node-a`
(`01M4TGD31WXH97JAA5S6A4R10D`) was running two agent containers:

```
node_id=01M4TGD31WXH97JAA5S6A4R10D controller=http://10.10.0.2:8080 samples=3
---- seq=19734 ts=2026-10-15T16:16:16Z
     cpu_util_pct=0.0 ram_used_bytes=892067840
     lxd_instances = null
     services      = null
---- seq=19733 ts=2026-10-15T16:16:13Z
     cpu_util_pct=0.0 ram_used_bytes=883826688
     lxd_instances = null
     services      = null
---- seq=19732 ts=2026-10-15T16:16:10Z
     cpu_util_pct=3.33 ram_used_bytes=889917440
     lxd_instances = null
     services      = null
```

`sudo lxc list` on the same node at the same moment:

```
+--------------------------------------+---------+-----------------------+...
|                 NAME                 |  STATE  |         IPV4          |
+--------------------------------------+---------+-----------------------+...
| demo-workload                        | STOPPED |                       |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 | RUNNING | 10.174.247.141 (eth0) |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 | RUNNING | 10.174.247.215 (eth0) |
+--------------------------------------+---------+-----------------------+...
```

So the node was running two workloads and the controller stored **no** instance
inventory and **no** services for it. The field was missing by construction: the
baked daemon had no collection code at all, and filled `services[]` only when the
node ran the inference front door (`build_sample`, the `inference is not None`
branch — `node-a` is a compute node, so it never fired).

> Note on the fleet state: at 15:58Z `node-a`'s `/usr/local/bin/termini-bootstrap`
> was replaced (md5 `da785fc8162b724d558fcf55a10cbc98`, 47520 bytes) by an
> **uncommitted** revision from the concurrently running card `t_895bf6d3`, and the
> unit was never restarted — the running process was still the pre-fix daemon,
> which is exactly what the samples above show. The fleet (controller, node-a/b/c)
> was then **TERMINATED by the operator's end-of-day cost guard**, verified before
> this card went to a live window; see §5. No GCP compute was started by this card.

## 2. What changed

| File | Change |
|---|---|
| `infra/bootstrap/node/termini-bootstrap.py` | collects `lxd_instances[]` from the node's own LXD each sample; fills `services[]` unconditionally |
| `docs/contracts/node-telemetry.md` | §4.6 amended (id derivation, what is reported, omission rules, field semantics), §4.8 amended (`services[]` on every sample) |
| `tests/test_node_instance_inventory.py` | new: derivation literals, LXD→contract mapping, omission rules, ingest validation, acceptance-tool verdicts |
| `infra/gcp/tools/verify-node-instances.py` | new: the one-command live acceptance (used in §4/§5) |

* **Collection**: one entry per **RUNNING** instance from the node's own LXD over
  its unix socket (`GET /1.0/instances?recursion=2`) — the same view `lxc list`
  prints, no network and no subprocess on the sample path — with the six §4.6
  fields. `ram_limit_bytes` is LXD's effective limit, `disk_used_bytes` is what
  LXD reports for the root volume (0 on the `dir` driver, exactly as `lxc list`).
* **`vm_id`** is *derived*, not the instance name:
  `26 Crockford chars of sha256("termini-vm-id:" + name)`. The C0 id format is
  unchanged (the ingest validator still requires a 26-char Crockford ULID), the
  name — which may carry customer data — never leaves the node, and the controller
  inverts the rule over the instance names it reads back from the node it is about
  to dial. There is therefore **no per-instance mapping table that can be missing,
  and no id is ever invented**: every reported id corresponds to a real running
  instance on that node, and an id that matches none is unresolvable by
  construction (the fail-closed path). The controller half of the rule is
  `controller/app/operator/vmids.py` (card `t_895bf6d3`); the literals are pinned
  in the test so the copies cannot drift.
* **Omission rules**: the array is **omitted** (never emptied) when LXD cannot be
  read — "did not report" ≠ "runs nothing" — and `cpu_util_pct` is omitted on the
  sample where no CPU delta exists yet (first sample after a node/instance start)
  instead of being reported as `0`.
* **`services[]`** is emitted on every sample: `termini-bootstrap` (the unit
  emitting the sample), `lxd`, `sshd`, plus `llama-cpp` only on the node that
  hosts the inference front door. A service the node does not have is omitted,
  never reported as `stopped`.

## 3. Offline acceptance (real LXD, this host — no GCP)

`sudo python3 offline_acceptance.py <repo>` against the **live LXD on this host**
(7 running containers), with the controller's own validator and guardrail:

```
lxc list --format=json: 7 instance(s), 7 running
   bsp-verify     Running   container
   demo-node-a    Running   container
   demo-node-b    Running   container
   demo-node-c    Running   container
   rt-net-a       Running   container
   rt-net-b       Running   container
   rt-net-c       Running   container

lxd_instance_inventory() -> 7 entry(ies)
   {"cpu_util_pct": 0.0, "disk_used_bytes": 0, "ram_limit_bytes": 20948180992, "ram_used_bytes": 52043776, "state": "running", "vm_id": "4FZ922X02JSKERK8KSBZMMCXSS"}
   {"cpu_util_pct": 0.0, "disk_used_bytes": 0, "ram_limit_bytes": 1073741824, "ram_used_bytes": 186699776, "state": "running", "vm_id": "HWHS4QFAJW9625H9FQ897FDYT6"}
   {"cpu_util_pct": 9.75, "disk_used_bytes": 0, "ram_limit_bytes": 1073741824, "ram_used_bytes": 179286016, "state": "running", "vm_id": "CJYABWA2F2XW82YYCKZE1SYH8B"}
   {"cpu_util_pct": 0.0, "disk_used_bytes": 0, "ram_limit_bytes": 8589934592, "ram_used_bytes": 6672437248, "state": "running", "vm_id": "T81M7CV1CP3866E2KQNNKAYQNQ"}
   {"cpu_util_pct": 0.17, "disk_used_bytes": 0, "ram_limit_bytes": 1073741824, "ram_used_bytes": 50982912, "state": "running", "vm_id": "G91GMNHY1NW8PX32S7AGZTRBCB"}
   {"cpu_util_pct": 0.0, "disk_used_bytes": 0, "ram_limit_bytes": 1073741824, "ram_used_bytes": 51068928, "state": "running", "vm_id": "KRZ3W7C46ZBQSK0NN17DEYD02Y"}
   {"cpu_util_pct": 0.0, "disk_used_bytes": 0, "ram_limit_bytes": 8589934592, "ram_used_bytes": 5622341632, "state": "running", "vm_id": "RW07QAZXPWG5TYBREC2R5MKFXW"}

vm_id -> instance (the controller's reverse lookup, same rule):
   4FZ922X02JSKERK8KSBZMMCXSS  bsp-verify
   HWHS4QFAJW9625H9FQ897FDYT6  demo-node-a
   ... (all 7)

count matches `lxc list` running instances: True
every reported vm_id resolves to a running instance: True
every reported vm_id names a real, RUNNING instance in LXD: True
instance NAMES appear nowhere on the wire: True

build_sample() -> controller ingest validator errors: []
build_sample() services:      [{"name": "termini-bootstrap", "status": "running"}, {"name": "lxd", "status": "running"}, {"name": "sshd", "status": "running"}]
```

### 3.1 The guardrail resolves to a REAL instance — including with a fault injected

`demo_faults.fault_sample(<the real sample>, "oom")` — the same driver the fault
console uses, whose baseline is the node's own last stored sample — keeps the
inventory and ramps the instance usage. Feeding that window plus a model-shaped
decision into the controller's **real** `Guardrail`:

```
[instance_names={} (controller today)] model target 4FZ922X02JSKERK8KSBZMMCXSS
   -> guardrail OK: instance.restart params={'instance_name': '4FZ922X02JSKERK8KSBZMMCXSS', 'force': True, 'timeout_s': 30, '_vm_id': '4FZ922X02JSKERK8KSBZMMCXSS'}
   is a real instance on the node: True (LXD status: Running)

[instance_names from the node's live names] model target 4FZ922X02JSKERK8KSBZMMCXSS
   -> guardrail OK: instance.restart params={'instance_name': 'bsp-verify', 'force': True, 'timeout_s': 30, '_vm_id': '4FZ922X02JSKERK8KSBZMMCXSS'}
   resolves to: 'bsp-verify'
   is a real instance on the node: True (LXD status: Running)

[instance_names from the node's live names] model target 01M4N827979BD9D2762EFE24A0
   -> guardrail REFUSED (reject_reason=invalid_target)
   [{"rule": "G5", "decision": "reject", "field": "params.instance_name", "reason": "invalid_target", "detail": "instance target '01M4N827979BD9D2762EFE24A0' not resolvable"}]
```

Reading: G5 resolution **succeeds** against the real node's inventory, and the
resolved target is a real RUNNING instance — which is what this card owns. Two
adjacent facts belong to the other card and are stated rather than implied:
with an **empty** `instance_names` (the controller today) the resolved action
target is still the `vm_id`, so the executor's LXD path needs the reverse lookup
(`controller/app/operator/vmids.py`, card `t_895bf6d3`) before the action itself
stops 404ing; and the seeder's **synthetic** id is now refused by G5 as
`invalid_target` instead of resolving to a fabricated instance.

### 3.2 The acceptance tool itself

`infra/gcp/tools/verify-node-instances.py` was run end-to-end against the real
local LXD with a stub node-token endpoint serving the daemon's own sample:

```
# a MATCHING sample
... `lxc list` running instances -> derived vm_id (7 lines)
... stored sample lxd_instances = [7 entries]   services = [3 entries]
PASS: the controller's stored sample lists every running instance on this node (with vm_id/state/resources) and its services.
exit=0

# the SAME run with lxd_instances removed from the sample
FAIL:
   - lxd_instances is absent from the stored sample
exit=1
```

The failure branch is what makes a green run evidence; it is pinned in
`tests/test_node_instance_inventory.py` (`VerifyToolTest`).

## 4. Acceptance status

| # | Acceptance | Status |
|---|---|---|
| a | a sample from a real node listing every running instance, matching `lxc list` | **BLOCKED** — proven against a real LXD on the verification host (§3), needs the terminated fleet for the node-a sample (§5) |
| b | `services[]` populated on a real node | **BLOCKED** — same; proven offline (§3) |
| c | with a fault injected, a model decision resolves to a REAL instance | **BLOCKED** for the live run; the guardrail resolution + fault-window carry-through are proven offline (§3.1) |
| d | evidence file + PR + commit/pr/merge_sha | this file + the card's PR |

Everything that does not need the fleet is verified and landed; the live half of
(a)/(b)/(c) needs an approved window because the operator's cost guard stopped the
fleet (no GCP compute may be started by a worker).

## 5. The live window: exact commands (cheap, ~3 minutes)

Nothing here starts a node by itself — the window has to be approved first
(the operator's guard is explicit: `DO NOT START OR CREATE ANY GCP COMPUTE`).
Once a window is granted:

```bash
# 1. raise exactly the two machines needed (approved window only)
for n in termini-controller termini-node-a; do
  gcloud compute instances start "$n" --zone asia-south1-a; done

# 2. ship the LANDED revision to node-a (its installed daemon is currently the
#    uncommitted t_895bf6d3 revision that was never restarted)
git -C <repo> show <merge_sha>:infra/bootstrap/node/termini-bootstrap.py > /tmp/termini-bootstrap.py
gcloud compute instances add-access-config termini-node-a --zone asia-south1-a \
    --access-config-name external-nat                 # removed again at step 6
gcloud compute scp /tmp/termini-bootstrap.py \
    infra/gcp/tools/verify-node-instances.py termini-node-a:/tmp/ --zone asia-south1-a
gcloud compute ssh termini-node-a --zone asia-south1-a --command \
  'sudo install -m 0755 /tmp/termini-bootstrap.py /usr/local/bin/termini-bootstrap
   sudo systemctl restart termini-bootstrap && sleep 15
   sudo lxc list
   sudo python3 /tmp/verify-node-instances.py'      # -> PASS, exit 0

# 3. same on node-c (services[] incl. llama-cpp; no instances expected)
gcloud compute ssh termini-node-c ... 'sudo python3 /tmp/verify-node-instances.py'

# 4. the fault case: inject the oom fault for node-a through the fault console and
#    watch the operator's decision resolve to a real instance
curl -sS -X POST "$CONTROLLER/v1/projects/$PROJECT_ID/demo/faults" \
  -H "Authorization: Bearer $PROJECT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"node_id":"01M4TGD31WXH97JAA5S6A4R10D","scenario":"oom","duration_s":60}'
#    decisions/audit rows: the action's instance_name must be a REAL instance name
#    from `lxc list` (not a synthetic id), and the LXD call must not 404.

# 5. clear the fault, then
# 6. gcloud compute instances delete-access-config termini-node-a --zone ... \
#      --access-config-name external-nat
#    and stop what the window started.
```

`verify-node-instances.py` is the gate: it exits non-zero unless the controller's
stored sample lists exactly the node's running instances (with state and
resources) *and* `services[]`.
