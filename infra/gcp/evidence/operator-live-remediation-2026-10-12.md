# Closing the operator loop against REAL LXD — RAW EVIDENCE

Proof for the claim "a remediation against a real LXD endpoint takes effect and
leaves an audit row". Reproduces the measured defect, shows the fix landing on
real hardware, and pastes the raw output verbatim.

## Provenance

| | |
|---|---|
| Date | 2026-10-12 |
| Controller | `termini-controller`, `asia-south1-a`, `10.10.0.2` / `34.93.190.64`, host `34-93-190-64.sslip.io` |
| Controller code | this branch, deployed with `git archive HEAD controller \| tar -x -C /opt/termini` (a plain copy, not a checkout) |
| Test node | `termini-op-node`, `asia-south1-a`, `n2-standard-2`, internal `10.10.0.8`, from image family `termini-node-os` |
| Instances | `agent-c1` (created STOPPED by hand — the defect's exact shape), `demo-workload` (shipped by the bake) |
| LXD | 5.21.8, API on `:8443` |
| Cost | controller + one `n2-standard-2` for ~30 min ≈ **$0.06**; everything torn down (§6) |

## 1. Root causes established (read from the code, then confirmed live)

1. **The state-change primitives were fire-and-forget.** `restart_instance` /
   `stop_instance` / `delete_instance` returned the write's own result and were
   done (`controller/app/operator/executor.py`). LXD answers a state change with
   `202 + an operation URL`, which means *accepted*, not *done* — `create_instance`
   already waited on the operation, the state-change primitives did not, so a
   no-op restart that LXD accepts was reported `ok`.
2. **A restart of a STOPPED instance is a no-op LXD still accepts.** Reproduced
   live (§3): `202 "Restarting instance"`, instance stays STOPPED. The remediation
   therefore never “did the right thing” for the stopped workload.
3. **The audit row only existed on the loop path and carried no endpoint.** The
   live defect run never reached it: the only rows in `remediation_audits` read
   `"chaos stub operation"` (stub-era, `lxd_endpoint` unknown), so a real
   remediation was indistinguishable from a stub one — or absent entirely.

## 2. Trust + reachability (the precondition the earlier run tripped on)

Authenticated `GET /1.0/instances` from the controller with its minted client
cert (`/data/lxd-client/controller-lxd-client.crt`, fingerprint `a27fd4eb94cb…`),
after `lxc config trust add` on the node:

```
HTTP 200
{"type":"sync","status":"Success","status_code":200,...,"metadata":[
  "/1.0/instances/agent-c1","/1.0/instances/demo-workload"]}
```

## 3. The defect, reproduced live against real LXD

A raw `PUT /1.0/instances/agent-c1/state {"action":"restart"}` against the STOPPED
instance, exactly as the old executor issued it:

```
{"type":"async","status":"Operation created","status_code":100,
 "operation":"/1.0/operations/3ea96494-2d32-4b2f-99b6-44d0d86f2648",
 "metadata":{"class":"task","description":"Restarting instance","status":"Running",...}}
HTTP 202
```

and immediately afterwards, on the node:

```
agent-c1,STOPPED,,,CONTAINER,0
```

**202 "Restarting instance" and the instance is still STOPPED** — the measured
defect, on real hardware.

## 4. The fix landing — the operator loop closes on real LXD

The loop was pointed at the node (`TERMINI_LXD_BASE_URL=https://10.10.0.8:8443`)
and driven by an OOM breach delivered through the node's own telemetry endpoint
(the fault-injection boundary the chaos harness / seeder use; §7 spells out what
is and is not synthetic). The loop's own journal:

```
node 01M4RMWA9TFDQQ3E2D8K00H9S7 rule oom-guard-01 FIRED: detection_latency=-110.634s
    (breach=2026-10-12T18:39:54Z -> fired=2026-10-12T18:38:03.366Z)
node 01M4RMWA9TFDQQ3E2D8K00H9S7 rule oom-guard-01 -> instance.restart agent-c1: ok
    (remediation_latency=1.053s)
```

On the node, before → after:

```
agent-c1,STOPPED,,,CONTAINER,0      # before
agent-c1,RUNNING,,,CONTAINER,0      # after
demo-workload,RUNNING,,,CONTAINER,0
```

The audit row written for the real node (`GET /v1/operator/audits`):

```json
{
  "id": "01M4RPGP44R80DKCAWWSH9S4K5",
  "node_id": "01M4RMWA9TFDQQ3E2D8K00H9S7",
  "rule_id": "oom-guard-01",
  "action_kind": "instance.restart",
  "result": "ok",
  "detail": "operation Success: /1.0/operations/9b76cbe4-251e-4e2d-acc4-5943076fa5ef; action=start; state Stopped -> Running",
  "lxd_method": "PUT",
  "lxd_path": "/1.0/instances/agent-c1/state",
  "lxd_body": "{\"action\": \"start\", \"timeout\": 30}",
  "executed_at": "2026-10-12T18:38:03.366Z",
  "lxd_endpoint": "https://10.10.0.8:8443"
}
```

and the same endpoint right below it, still showing the stub-era rows it is now
distinguishable from:

```json
{"node_id":"01M4NZK474BVARFBXYQBMVA8GV","result":"ok",
 "detail":"{... \"description\": \"chaos stub operation\", ...}",
 "lxd_path":"/1.0/instances/01M4N827979BD9D2762EFE24A0/state",
 "lxd_body":"{\"action\": \"restart\", \"force\": true, \"timeout\": 30}",
 "lxd_endpoint":null}
```

Read the three fixes off that one row:

* `lxd_body` is `{"action":"start"}` — the executor read the state, saw STOPPED,
  and **started** the instance instead of issuing the no-op restart;
* `detail` = `operation Success: …; action=start; state Stopped -> Running` — the
  operation was **waited on** and the resulting state **read back** before `ok`;
* `lxd_endpoint` = the real LXD base URL — the row is unambiguous evidence of a
  real remediation, not a stub one.

## 5. What is verified live vs not

Verified live:

* a remediation fired by the in-process operator loop against a **real node's
  LXD** over mTLS, and changed the instance's real state (STOPPED → RUNNING);
* the audit row for it is readable from `GET /v1/operator/audits`, carrying the
  real endpoint, method/path/body and the operation outcome;
* the exact defect shape (restart of a STOPPED instance → `202`, no effect) is
  reproduced, and the fix (start + wait + confirm) is shown to close it.

Not verified live:

* the fault was injected at the telemetry boundary, not by genuinely driving the
  node host to ≥95 % RAM (a real OOM risks the OOM-killer taking the sampling
  window with it) — everything downstream of ingest is the production path;
* the node's baked daemon reports no `machine.lxd.endpoint` and no
  `lxd_instances` inventory, so the node used the documented global
  `TERMINI_LXD_BASE_URL` fallback and the OOM playbook's `heaviest` target fell
  back to `TERMINI_LXD_DEFAULT_INSTANCE_NAME` (§7);
* the `detection_latency` in §4 is negative because the injected samples were
  stamped ahead of the controller clock to survive a real-node clock skew — an
  artifact of the injection, not of the detector.

## 6. Cost control — stopped, and proven

```
NAME                       ZONE           STATUS
termini-controller         asia-south1-a  TERMINATED
termini-node-a             asia-south1-a  TERMINATED
termini-node-b             asia-south1-a  TERMINATED
termini-node-c             asia-south1-a  TERMINATED
```

`termini-op-node` deleted by name. Controller URL dead:

```
https://34-93-190-64.sslip.io -> 000
http://34.93.190.64           -> 000
```

The temporary controller drop-in was removed (only the standing `lxd.conf` +
`model-mode.conf` remain), so the standing footprint is unchanged apart from the
code deployment.

## 7. Notes on the real node that matter for a live demo

* The node's baked daemon does not report `machine.lxd.endpoint`:
  `nodes.lxd_endpoint` stayed NULL, so the operator loop used the global
  `TERMINI_LXD_BASE_URL` fallback (the documented path for an older node).
* The node's baked daemon does not report `lxd_instances` telemetry either, so
  the OOM playbook's `target: heaviest` resolves to zero candidates and the
  action falls back to `TERMINI_LXD_DEFAULT_INSTANCE_NAME`. On a real,
  single-workload node that default must name the real instance; a node without
  an instance inventory cannot otherwise be targeted.
