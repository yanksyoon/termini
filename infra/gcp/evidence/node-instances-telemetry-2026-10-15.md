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
| a | a sample from a real node listing every running instance, matching `lxc list` | **PASS on the live fleet** — §6.1 (node-a: 4/4 running instances, vm_id + state + resources) |
| b | `services[]` populated on a real node | **PASS on the live fleet** — §6.1 (3 services) and §6.2 (4, incl. `llama-cpp`) |
| c | with a fault injected, a model decision resolves to a REAL instance | **PASS for target resolution** — §6.3: the injected `oom` fires the rule and the guard resolves `NNB92…` → `demo-workload`, a real RUNNING instance; LXD `PUT …/state` → `ok`. The decision is `decision_source=fallback` because G9 rejects the model's explanation (§6.4, separate defect, follow-up card) |
| d | evidence file + PR + commit/pr/merge_sha | this file + the card's PR (see the card for the sha) |

The offline half (§3) was verified first; the live half needed the operator's
approved window and was run on 2026-10-17 (§6).

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

---

## 6. Live acceptance on the standing fleet (2026-10-17, operator-approved window)

Fleet at the time of the run (verified with `gcloud compute instances list`):
`termini-controller` (34.93.190.64, project `01J0DEM0WARM000000000000AB`),
`termini-node-a`, `termini-node-c` RUNNING; `termini-node-b` TERMINATED (the CPU
quota is fully consumed by the running three). Nothing was started, stopped or
recreated by this card; the only writes were the two fault injections through the
operator's own console API, both cleared (§6.3).

Both nodes run daemon md5 `ddc07bd1887d39b7447d928eb32c933a` = commit `7252a0f`
(#184, the revision the demo refresh installed). That is one merge behind
`origin/main` (`1592fff`, #190); `git diff 7252a0f origin/main --
infra/bootstrap/node/termini-bootstrap.py` touches **only** the additive
`§4.12–§4.14` groups (`disk_facts` inode/io, `net_facts` error counters,
`process_facts`, `pressure`/transport scalars, per-instance cgroup counters). The
`lxd_instances[]` collection and the `services[]` emission this card owns are
byte-identical to `main`, and the samples below are the controller's **stored**
samples (ingested and validated), not what the daemon claims it sent.

### 6.1 (a) + (b): node-a, every RUNNING instance with vm_id/state/resources

`sudo lxc list -c nst` and the gate on `termini-node-a`, same SSH session
(2026-10-17T02:45:46Z):

```
ddc07bd1887d39b7447d928eb32c933a  /usr/local/bin/termini-bootstrap
--- lxc list ---
+--------------------------------------+---------+-----------+
|                 NAME                 |  STATE  |   TYPE    |
+--------------------------------------+---------+-----------+
| demo-workload                        | RUNNING | CONTAINER |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 | RUNNING | CONTAINER |
| termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 | RUNNING | CONTAINER |
| termini-01m537g26sm8qgkmztn8stbt-0-0 | STOPPED | CONTAINER |
| termini-01m537km0trb189a6e1gz546-0-0 | RUNNING | CONTAINER |
+--------------------------------------+---------+-----------+
--- gate ---
node_id=01M4TGD31WXH97JAA5S6A4R10D  controller=http://10.10.0.2:8080  daemon=/usr/local/bin/termini-bootstrap

`lxc list` running instances -> derived vm_id (the controller resolves with these):
   7NM4JV8BSD1P8KYBYP8B7BT7CY  termini-01m4wjhwqr3b8pcav2tvqbgg-0-0
   J2DFSZ2VRKD2EVF2ZAJFCE2H8E  termini-01m537km0trb189a6e1gz546-0-0
   NNB9287WRHGFDPEZ995TKCXDNW  demo-workload
   VW4XZD24288MCXGT4557CDYFF9  termini-01m4wjhwqr3b8pcav2tvqbgg-0-1

stored sample seq=26402 ts=2026-10-17T02:45:46Z
   lxd_instances = [{"vm_id": "NNB9287WRHGFDPEZ995TKCXDNW", "state": "running", "ram_used_bytes": 110718976, "ram_limit_bytes": 8319729664, "disk_used_bytes": 0, "cpu_util_pct": 89.04}, {"vm_id": "7NM4JV8BSD1P8KYBYP8B7BT7CY", "state": "running", "ram_used_bytes": 98885632, "ram_limit_bytes": 1073741824, "disk_used_bytes": 0, "cpu_util_pct": 0.06}, {"vm_id": "VW4XZD24288MCXGT4557CDYFF9", "state": "running", "ram_used_bytes": 103026688, "ram_limit_bytes": 1073741824, "disk_used_bytes": 0, "cpu_util_pct": 0.07}, {"vm_id": "J2DFSZ2VRKD2EVF2ZAJFCE2H8E", "state": "running", "ram_used_bytes": 98414592, "ram_limit_bytes": 1073741824, "disk_used_bytes": 0, "cpu_util_pct": 0.03}]
   services      = [{"name": "termini-bootstrap", "status": "running", "state_age_sec": 20452, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 394}, {"name": "lxd", "status": "running", "state_age_sec": 20450, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 0}, {"name": "sshd", "status": "running", "state_age_sec": 20449, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 0}]

PASS: the controller's stored sample lists every running instance on this node (with vm_id/state/resources) and its services.
gate_exit=0
```

Reading: the four `RUNNING` rows on the node are exactly the four entries in the
stored sample, each keyed by the derived `vm_id`, with state and resources; the
one `STOPPED` instance (`…g26sm8…`) is correctly absent; no instance name appears
in the sample (the name → id mapping stays on the node, per C0/§4.6).

### 6.2 node-c: services populated (`llama-cpp` included), no workloads

```
--- lxc list ---
+------+-------+------+
| NAME | STATE | TYPE |
+------+-------+------+
--- gate ---
node_id=01M4ZQ7Q2J80ATY3K32PY3PPJH  controller=http://10.10.0.2:8080  daemon=/usr/local/bin/termini-bootstrap

`lxc list` running instances -> derived vm_id (the controller resolves with these):

stored sample seq=11611 ts=2026-10-17T02:46:24Z
   lxd_instances = []
   services      = [{"name": "termini-bootstrap", "status": "running", "state_age_sec": 20494, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 412}, {"name": "lxd", "status": "running", "state_age_sec": 20482, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 0}, {"name": "sshd", "status": "running", "state_age_sec": 20477, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 0}, {"name": "llama-cpp", "status": "running", "state_age_sec": 20494, "restart_count": 0, "last_exit_code": 0, "journal_errors": 0, "journal_warnings": 0}]

PASS: the controller's stored sample lists every running instance on this node (with vm_id/state/resources) and its services.
gate_exit=0
```

`lxd_instances = []` is the honest "node reported, runs nothing" (the array is
only *absent* when LXD cannot be read — §2 omission rules), and the readiness
list is the node's own: `llama-cpp` appears here because the unit is present on
node-c, and would be omitted on a node that does not host it.

### 6.3 (c): an injected fault, and the guard resolving to a REAL instance

`POST /v1/projects/{p}/demo/faults {"node_id":"01M4TGD31WXH97JAA5S6A4R10D",
"scenario":"oom","target":"real","duration_s":90}` → `201`, injection
`01M53VZ29EQJPBQ28SRYP5ZP78`. The injected series ramps host RAM to 97 % of
`ram_total_bytes` and keeps the nested `lxd_instances[]` consistent; the rule
fired at 02:45:00.539Z (detection latency 2.53 s):

```
=== DECISION 01M53VZATZ66D8N78EESBC3PFK  2026-10-17T02:45:02.721Z fallback schema_violation
    rule=oom-guard-01 action_kind=instance.restart
    action_params = {"force": true, "timeout_s": 30, "instance_name": "demo-workload", "_vm_id": "NNB9287WRHGFDPEZ995TKCXDNW"}
    incident      = [{"action_kind": "instance.restart", "target": "demo-workload"}]

--- guard resolution detail ---
action_params.instance_name = 'demo-workload'
action_params._vm_id        = 'NNB9287WRHGFDPEZ995TKCXDNW'
   threshold_fired      ok        rule oom-guard-01 fired
   evidence_window      ok        evidence window captured
        seq 26370..26389 (20 samples) C0 fields: cpu_core_count, cpu_util_pct, disk, kernel_version, load_avg_1m, lxd_cluster_role, lxd_instances, net, os_name, os_version, processes, ram_total_bytes, ram_used_bytes, seq, services, swap_used_bytes, ts, uptime_sec
   prompt_built         ok        prompt rendered (C0 assertion ok)
   model_call           ok        model answered
   reply_received       rejected  model reply received
   gates                ok        G7 rationale
   gates                rejected  G9 incident record
        rule=G9 field=explanation decision=reject reason=schema_violation (C2 signature in explanation: : phone-number signature)
   action_decided       fallback  fallback to deterministic action
        decided_by=fallback reason=schema_violation action_kind=instance.restart rejecting_gate=G9
   executed             ok        action executed: ok
   audit_recorded       ok        audit row written

--- audit row (remediation_audits) ---
action_kind    instance.restart
result         ok
detail         operation Success: /1.0/operations/3f8d18a4-b223-4780-b9b6-d015f01867c9; action=restart; state Running -> Running
lxd_method     PUT
lxd_path       /1.0/instances/demo-workload/state
lxd_body       {"action": "restart", "force": true, "timeout": 30}
lxd_endpoint   https://10.10.0.11:8443
executed_at    2026-10-17T02:45:02.721Z
```

`NNB9287WRHGFDPEZ995TKCXDNW` is one of the four vm_ids the node reported in §6.1
and hashes from `demo-workload` — a real RUNNING instance on that node — so the
G5 resolution succeeded against real inventory and the executor's LXD call hit the
real instance (`state Running -> Running`, no `404`, no invented name). `lxc list`
on node-a at 02:47 shows `demo-workload` RUNNING again after the restart.

The same path reproduced an hour-earlier run (`01M53V93RXX4KJSZHKRDH3J64P`,
02:33:05Z: `oom-guard-01: instance.restart demo-workload -> ok`).

### 6.4 Finding (out of this card's scope, follow-up raised): G9 rejects the model's explanation

Both live decisions are `decision_source=fallback` with `fallback_reason=schema_violation`:
the model answered, G7 accepted the rationale, and then **G9 rejected the incident
explanation for a "phone-number signature"**. `governance.scan_c2` treats *any*
bare 10–15 digit run as a phone number (`controller/app/governance.py:25`,
`_PHONE = (?<!\d)\+?[0-9]{10,15}(?!\d)`), while the prompt renders the evidence
window with **raw integers** (`termini/decision/prompt.py::compact_json`) and a
real node's window is full of them — host `ram_used_bytes` ≈ 4 119 142 400,
`ram_limit_bytes` 8 319 729 664, `disk.total_bytes` 61 285 326 848, and now the
per-instance `ram_used_bytes`/`ram_limit_bytes` of `lxd_instances[]`. A model that
quotes a metric while explaining itself is therefore rejected, and on a real node
the operator never gets a model-sourced decision (the deterministic playbook still
acts, which is what §6.3 shows).

This is not a defect in the telemetry: the values are exactly what the contract
requires and the controller's own `safe_int_display` groups them for controller-
generated text. It is a redactor/prompt-rendering mismatch on the operator side,
raised as a follow-up card (see the card's completion metadata).

### 6.5 What this run did NOT do

* It did not deploy `origin/main`'s daemon over `7252a0f`: the two revisions are
  identical for this card's code path (§6 preamble) and a service restart on a
  live node is not something this card needs to prove its field is emitted.
* It did not rebake the node image. The image bake still ships whatever commit it
  is built from; the field is in the daemon, and a rebake carries it (§2).
* It started/stopped no GCP compute and created no address or disk. The demo's
  `demo-workload` was left RUNNING on node-a (verified 02:47Z).
