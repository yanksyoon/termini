# Live plan-materialisation on a real node — RAW EVIDENCE

Proves the claim behind #128/#129: **applying a Termini allocation plan now creates
real LXD instances on a real node** — the demo no longer acts on the local stub
(`chaos/lxd_stub.py` on `127.0.0.1:18443`). Everything below is a real `gcloud`/
`ssh`/`curl`/`lxc` run against GCP; outputs are pasted verbatim, not paraphrased.

**Headline: the core claim is PROVEN** (2 instances created on a real node, listed by
both `lxc list` on the node and an authenticated `GET /1.0/instances` from the
controller). Four things did **not** work as shipped or could not be verified — see
§9. Read that section before quoting any of this.

## Provenance

| | |
|---|---|
| Date | 2026-10-12 |
| Controller | `termini-controller`, `asia-south1-a`, internal `10.10.0.2` / external `34.93.190.64`, host `34-93-190-64.sslip.io` |
| Controller code | landed `main` @ `1cbb78a` (`git archive HEAD controller`), deployed to `/opt/termini` (a plain copy — **not** a git checkout) |
| Test node | `termini-live-test`, `asia-south1-a`, `n2-standard-2`, internal `10.10.0.7`, created from image family `termini-node-os` (→ `termini-node-os-3`) |
| Project / token | `01J0DEM0WARM000000000000AB` / `demo-project-token` |
| Allocation plan | `01M4RK2TED98YTJA3W98WZBC9V` (after the fix: `01M4RKB0CXK92ZCE8VWESQJW36`) |
| Cost | controller + one `n2-standard-2` for ~20 min ≈ **$0.05**; all VMs stopped/deleted at the end (§8) |

## 1. Deploy the landed code to the controller

`/opt/termini` is a **copy**, not a git clone (`fatal: not a git repository`), and it
predated the landed change: `lxdtrust.py`, `operator/target.py` and migration `0010`
were all absent. Deployed with `git archive HEAD controller | tar -x -C /opt/termini`.

```
-rw-rw-r-- 1 ubuntu ubuntu 19546 Oct 12 17:19 /opt/termini/controller/app/lxdtrust.py
-rw-rw-r-- 1 ubuntu ubuntu   997 Oct 12 17:19 /opt/termini/controller/app/migrations/0010_node_lxd_endpoint.sql
-rw-rw-r-- 1 ubuntu ubuntu  3804 Oct 12 17:19 /opt/termini/controller/app/operator/target.py
c9b8a5963ac679f9f5a16f5192d70d287bf77b0a855b915d56943176c0379d92  /opt/termini/controller/app/lxdtrust.py
```

After `systemctl restart termini-controller`:

```
applied migrations: 0010_node_lxd_endpoint
LXD client identity ready: /data/lxd-client/controller-lxd-client.crt (fingerprint=a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5)
controller listening on 127.0.0.1:8080 (db=/data/controller.db)
```

`PRAGMA table_info(nodes)` gained `lxd_endpoint, lxd_client_cert, lxd_client_key`.
The controller has **no `/healthz`** route (that belongs to `console/serve.py`); the
liveness probe used is an authenticated `GET /v1/projects/<pid>/nodes` → `200`.

## 2. Create one real node from the image family

```
termini-live-test  asia-south1-a  n2-standard-2  10.10.0.7  8.234.80.88  RUNNING
```

Cloud-init (rendered from `infra/bootstrap/render-cloud-init.sh`) wrote the config +
enrollment token and started the baked daemon. Node side:

```
GET /1.0 -> 200                                  # LXD serving on :8443
core.https_address = :8443
lxc list: demo-workload RUNNING CONTAINER        # the one container the image ships
```

Registration, from the node's own journal:

```
2026-10-12T17:26:24Z [termini-bootstrap] REGISTERED node_id=01M4RJDET6M7VY3Q72AH9C722F role=compute register_ms=772 boot_to_registered_ms=16170
```

## 3. Workload image alias (the #127 gap)

The pre-built image predates #127, so the `agent-vm` alias the controller launches by
default was **missing** — exactly one image and no alias:

```
| ALIAS | FINGERPRINT  | ... | ubuntu 24.04 LTS amd64 (release) (20260926) | CONTAINER | 267.06MiB |
```

Cheapest correct fix (no rebuild — packer/distrobuilder absent): alias the cached image.

```
sudo lxc image alias create agent-vm ee3016f85cc4
| agent-vm | ee3016f85cc4 | no | ubuntu 24.04 LTS amd64 (release) (20260926) | CONTAINER | 267.06MiB |
```

**This is a stand-in for what the #127 bake now does** (guarantee the workload alias).
It proves materialisation, not the bake.

## 4. Node registers as ONLINE

The controller records the node `online` with `lxd_endpoint = NULL`:

```
node_id     = 01M4RJDET6M7VY3Q72AH9C722F
reg/role/st = 2e9f264ab8bb267ecbcc5702057a0dec / compute / online    # registration_id is C1-hashed
lxd_endpoint= None
lxd_json    = {"version": "5.21.8", "snap_revision": "40958"}
cores/mem   = 2 / 8319729664
```

`lxd_endpoint = NULL` because the **baked** daemon predates #128's
`machine.lxd.endpoint` reporting, so the controller uses the documented fallback:
`TERMINI_LXD_BASE_URL`. That global was set to the real node
(`https://10.10.0.7:8443`) in place of the stub `http://127.0.0.1:18443`.

## 5. Trust flow (#129) — observable before/after

**BEFORE** (controller presents no trusted cert):

```
https://10.10.0.7:8443: NOT TRUSTED — GET /1.0/instances -> HTTP 403 (controller cert not trusted)   [exit 3]
curl --cert controller-lxd-client.crt --key ... -k https://10.10.0.7:8443/1.0/instances
HTTP 403
{"type":"error","status":"","status_code":0,"operation":"","error_code":403,"error":"Forbidden","metadata":null}
```

Mint the node-side token, then install it (controller side):

```
sudo /usr/local/bin/termini-bootstrap mint-trust-token --name termini-controller
minted LXD trust token name=termini-controller -> /var/lib/termini/lxd-trust-token

python3 -m app.lxdtrust trust --node https://10.10.0.7:8443 --token "$(cat /tmp/tt.txt)"
https://10.10.0.7:8443 [token]: trusted — certificate added to trust store; authenticated GET /1.0/instances -> 200   [exit 0]
```

**AFTER**:

```
python3 -m app.lxdtrust verify --node https://10.10.0.7:8443
https://10.10.0.7:8443: TRUSTED — authenticated GET /1.0/instances -> 200   [exit 0]

curl --cert controller-lxd-client.crt --key ... -k https://10.10.0.7:8443/1.0/instances
HTTP 200
{"type":"sync","status":"Success","status_code":200,...,"metadata":["/1.0/instances/demo-workload"]}
```

Corroboration — the controller's cert is in the node's trust store, fingerprint matches:

```
client  termini-controller  termini-controller  a27fd4eb94cb
  -> a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5
```

**403 → 200 is the whole point of #129, shown live.**

## 6. Apply a plan end to end

Intent (structured spec; `target_node` is explicit — see §9.6):

```
POST /v1/projects/01J0DEM0WARM000000000000AB/intents
{"spec":{"allocations":[{"type":"agent_vm","count":2,"vcpus":1,"ram_gb":1,"disk_gb":8,"target_node":"01M4RJDET6M7VY3Q72AH9C722F"}]}}
-> 200 accepted, plan_id 01M4RK2TED98YTJA3W98WZBC9V
```

**First apply, exactly as landed (no client-cert wiring): FAILED.**

```
POST /v1/projects/.../vms/apply  {"plan_id":"01M4RK1E50NXHRA0MXVCY9JPH8"}
{"state":"failed",
 "allocations":[{"status":"failed","detail":"create termini-01m4rk1e...-0-0 failed: URLError:
   <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate (_ssl.c:1000)>",
   "rolled_back":0,"capacity_released":true}]}
```

After wiring the documented `TERMINI_LXD_CLIENT_CERT`/`_KEY` knobs to the minted
identity and restarting, the same plan shape succeeds:

```
POST /v1/projects/.../vms/apply  {"plan_id":"01M4RK2TED98YTJA3W98WZBC9V"}
{"state":"ready",
 "vm_ids":["termini-01m4rk2ted98ytja3w98wzbc-0-0","termini-01m4rk2ted98ytja3w98wzbc-0-1"],
 "allocations":[{"status":"ready","node_id":"01M4RJDET6M7VY3Q72AH9C722F","realized_as":"vm",
   "vm_ids":["...-0-0","...-0-1"],"created":2,"already_applied":0}]}
```

### VERIFIED ON THE NODE (not just in the response)

```
+--------------------------------------+---------+------+------+-----------+
|                 NAME                 |  STATE  | ...  |   TYPE    |
+--------------------------------------+---------+------+------+-----------+
| demo-workload                        | RUNNING | ...  | CONTAINER |
| termini-01m4rk2ted98ytja3w98wzbc-0-0 | STOPPED | ...  | CONTAINER |
| termini-01m4rk2ted98ytja3w98wzbc-0-1 | STOPPED | ...  | CONTAINER |
```

```
sudo lxc info termini-01m4rk2ted98ytja3w98wzbc-0-0
Name: termini-01m4rk2ted98ytja3w98wzbc-0-0
Status: STOPPED
Created: 2026/10/12 17:38 UTC
config:
  limits.cpu: "1"          <-- exactly what the controller requested
  limits.memory: 1GiB      <-- exactly what the controller requested
```

### and from the CONTROLLER's own authenticated LXD client

```
GET https://10.10.0.7:8443/1.0/instances        HTTP 200
{"type":"sync","status":"Success","status_code":200,...,"metadata":[
  "/1.0/instances/demo-workload",
  "/1.0/instances/termini-01m4rk2ted98ytja3w98wzbc-0-0",
  "/1.0/instances/termini-01m4rk2ted98ytja3w98wzbc-0-1"]}
```

### committed capacity (controller DB)

```
plan_id 01M4RK2TED98YTJA3W98WZBC9V  node 01M4RJDET6M7VY3Q72AH9C722F  cpu=2 ram_gb=2 disk_gb=16
```

Both halves agree: the controller claims two instances **and they exist on the node**.

## 7. Bonus — remediation through the real LXD API (PARTIAL)

Driving the operator's own executor primitive (`instance.restart`) against the real node:

```
PUT https://10.10.0.7:8443/1.0/instances/termini-01m4rk2ted98ytja3w98wzbc-0-0/state
status : 202
result : ok
detail : {"type":"async","status":"Operation created","status_code":100,
          "operation":"/1.0/operations/71f31a94-...","metadata":{"description":"Restarting instance", ...}}
```

But `lxc list` afterwards still showed the instance **STOPPED**, and
`GET /v1/operator/audits` returned only **stub-era** rows (`"detail":"...chaos stub
operation..."`, node ids of the synthetic `demo-node-*`). **No new audit row exists for
the real node.** See §9.3–9.4.

## 8. Cost control — stopped, and proven

```
NAME                ZONE           STATUS
termini-controller  asia-south1-a  TERMINATED
termini-node-a      asia-south1-a  TERMINATED
termini-node-b      asia-south1-a  TERMINATED
termini-node-c      asia-south1-a  TERMINATED
```

`termini-live-test` deleted by name. The controller's public URL is dead:

```
https://34-93-190-64.sslip.io -> 000
http://34.93.190.64:80        -> 000
```

The controller's config drop-ins were restored to their pre-test contents (the stub
`lxd.conf`, and the ad-hoc test token/cert drop-ins removed), so the standing
footprint is unchanged apart from the code deployment.

## 9. What did NOT work / could not be verified

1. **The bake's guarantees were bypassed, not exercised.** The node image's baked
   daemon is older than the landed code: it reports no `machine.lxd.endpoint` and has
   no `mint-trust-token` subcommand. I refreshed `/usr/local/bin/termini-bootstrap`
   from the repo (the `REFRESH_RUNTIME` path in `provision-node.sh`) and created the
   `agent-vm` alias by hand (§3). So "a node baked today ships the alias + the
   endpoint/trust-token support" is **assumed, not proven** here.

2. **Landed `main`, as deployed, cannot materialise on a real node.** The first apply
   failed with `CERTIFICATE_VERIFY_FAILED` before trust was consulted: `main.py` mints
   the LXD client identity and hands it to the operator loop, but **never writes it
   into `cfg`**, and the apply path (`op_target.executor_for_node`) reads only
   `cfg.lxd_client_cert`/`_key`. With no cert and no `TERMINI_LXD_SERVER_CA`,
   `LxdExecutor._make_ssl_ctx` returns `None` and urllib verifies LXD's self-signed
   cert → failure. Worked around with the documented env knobs; a **minimal code fix**
   (`dataclasses.replace(cfg, lxd_client_cert=…, lxd_client_key=…)` in `main.py`) was
   written and **verified live** (apply → `state: ready`, node showed 4 instances, and
   the full 439-test suite passes) — see the separate fix PR.

3. **Instances are created but left STOPPED.** `create_instance` never starts them
   (LXD's `POST /1.0/instances` does not auto-start). "An instance exists on the node"
   is proven; **"a running agent VM" is NOT.** The demo's `vm` state would read
   `stopped`/`error`, not `running`.

4. **Remediation audit row against the real endpoint is NOT proven.** The restart
   returned `202 ok` from the real LXD API, but (a) `restart` on a *stopped* container
   is a no-op, and (b) no `operator_audits` row was written, because the in-process
   operator loop never fired for the real node — the only rows present are from the
   stub era. The standalone operator CLI cannot help either: it also builds its
   `LxdExecutor` with no client cert, so it would hit the same TLS failure.

5. **Per-node LXD addressing (#128's headline) was not exercised live.** Because the
   baked node reported no endpoint, the run used the global `TERMINI_LXD_BASE_URL`
   fallback — the documented path for an older node. The node-reported-endpoint branch
   is covered only by unit tests (`tests/test_apply_materialisation.py`), not live.

6. **"auto" target would have missed the real node.** The DB still holds synthetic
   seeder nodes (`demo-node-a`, 8 cores / 64 GiB) whose free RAM beats the test node's,
   so `target_node: "auto"` would have selected a synthetic node — there is no LXD
   there, so materialisation would silently target the wrong host (or fail). The intent
   therefore named the real `node_id` explicitly. Cleaning the seeder's synthetic rows
   (or gating `auto` on a reported endpoint) is worth doing before a live demo.

7. Minor: the node's registration reported `disks[0].size_bytes = 1` (a placeholder),
   so the controller computed `disk_gb = 0` and **skipped** the disk capacity clamp
   entirely (`health.clamp_to_node_capacity` skips when `disk_gb <= 0`). Disk capacity
   accounting was therefore not exercised.
