# Node-c was not a trusted LXD target — installed, proven, and made part of the raise path

- Task: `t_d31b8e02` (found live by `t_ece9d14f`, evidence §7 F2)
- Date: 2026-10-17, standing fleet window (controller + node-a + node-c RUNNING, node-b TERMINATED)
- Fleet: controller `10.10.0.2`, node-a `10.10.0.11`, node-c `10.10.0.13` (project `termini-510609`, zone `asia-south1-a`)

## 1. The defect, reproduced before the fix

The controller's LXD client identity was installed in node-a's trust store by
hand (one-off, alongside #129) and **never on node-c**. From the controller:

```
$ cd /opt/termini/controller && PYTHONPATH=/opt/termini/controller:/opt/termini \
    TERMINI_DB_PATH=/data/controller.db python3 -m app.lxdtrust verify \
      --node https://termini-node-a.asia-south1-a.c.termini-510609.internal:8443 \
      --node https://termini-node-c.asia-south1-a.c.termini-510609.internal:8443

https://termini-node-a…:8443: TRUSTED — authenticated GET /1.0/instances -> 200
https://termini-node-c…:8443: NOT TRUSTED — GET /1.0/instances -> HTTP 403 (controller cert not trusted)
rc=3
```

node-c was otherwise healthy: `GET /1.0` answered 200, it registered, it reported
telemetry, and its LXD trust store was **empty**:

```
$ gcloud compute ssh termini-node-c --command="lxc config trust list"
+------+------+-------------+-------------+------------+-------------+
| TYPE | NAME | COMMON NAME | FINGERPRINT | ISSUE DATE | EXPIRY DATE |
+------+------+-------------+-------------+------------+-------------+
```

## 2. The controller identity (the one value that must appear everywhere)

```
$ python3 -m app.lxdtrust cert
cert=/data/lxd-client/controller-lxd-client.crt
key=/data/lxd-client/controller-lxd-client.key
fingerprint=a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5
```

## 3. Installing it with the repo's own tool (node half + controller half)

Node half — the node mints its own one-time certificate-add trust token over its
local LXD socket (no operator secret involved):

```
$ gcloud compute ssh termini-node-c --command="sudo termini-bootstrap mint-trust-token --name termini-controller; sudo cat /var/lib/termini/lxd-trust-token"
-rw------- 1 root root 445 /var/lib/termini/lxd-trust-token
eyJjbG…MmY4            (445-byte CertificateAddToken; client_name=termini-controller,
                        addresses 10.10.0.13:8443, expires_at 2026-11-01T03:21:55Z)
```

Controller half — consume it (single-use):

```
$ python3 -m app.lxdtrust trust --node https://termini-node-c…:8443 --token '<token>'
https://termini-node-c…:8443 [token]: trusted — certificate added to trust store;
                                   authenticated GET /1.0/instances -> 200
trust_rc=0
```

Idempotent by construction — the same call again is a no-op, not a second entry:

```
$ python3 -m app.lxdtrust trust --node https://termini-node-c…:8443 --token '<same token>'
https://termini-node-c…:8443 [token]: already_trusted — authenticated GET /1.0/instances -> 200
```

Both nodes, after the install:

```
$ python3 -m app.lxdtrust verify --node <node-a> --node <node-c>
https://termini-node-a…:8443: TRUSTED — authenticated GET /1.0/instances -> 200
https://termini-node-c…:8443: TRUSTED — authenticated GET /1.0/instances -> 200
verify_rc=0
```

The fingerprint in **each** node's trust store equals the controller's identity:

```
$ gcloud compute ssh termini-node-a --command="lxc config trust list"
| client | termini-controller | termini-controller | a27fd4eb94cb | Oct 12, 2026 at 5:25pm (UTC) | Oct 9, 2036 at 5:25pm (UTC) |

$ gcloud compute ssh termini-node-c --command="lxc config trust list"
| client | termini-controller | termini-controller | a27fd4eb94cb | Oct 12, 2026 at 5:25pm (UTC) | Oct 9, 2036 at 5:25pm (UTC) |

$ … --format=json | grep -o '"fingerprint": *"[a-f0-9]*"'
node-a: "fingerprint":"a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5"
node-c: "fingerprint":"a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5"
```

## 4. The consequence is gone: a plan naming node-c materialises a RUNNING instance

Exactly the case that used to fail (§7 F2): an `agent_vm` with an **explicit**
`target_node` naming node-c (`01M4ZQ7Q2J80ATY3K32PY3PPJH`), which was at 0
committed vCPU / 0 GiB / 0 GB.

```
POST /v1/projects/01J0DEM0WARM000000000000AB/intents
{"spec":{"allocations":[{"type":"agent_vm","count":1,"vcpus":1,"ram_gb":1,"disk_gb":4,
                         "target_node":"01M4ZQ7Q2J80ATY3K32PY3PPJH"}]}}

HTTP 200 in 0.007910s
{"plan_id":"01M53Y85M5WZZ81S3GKMZZSZ88","outcome":"accepted",
 "allocations":[{"index":0,"status":"accepted","node_id":"01M4ZQ7Q2J80ATY3K32PY3PPJH",
                 "realized_as":"vm","vm_ids":[]}]}
```

```
POST /v1/projects/01J0DEM0WARM000000000000AB/vms/apply {"plan_id":"01M53Y85M5WZZ81S3GKMZZSZ88"}

HTTP 202 in 29.409344s
{"job_id":"01M53Y8E93PHXKEQY5BTRRYRX6","plan_id":"01M53Y85M5WZZ81S3GKMZZSZ88",
 "state":"ready","vm_ids":["termini-01m53y85m5wzz81s3gkmzzsz-0-0"],
 "allocations":[{"index":0,"status":"ready","node_id":"01M4ZQ7Q2J80ATY3K32PY3PPJH",
                 "realized_as":"vm","vm_ids":["termini-01m53y85m5wzz81s3gkmzzsz-0-0"],
                 "created":1,"already_applied":0}]}
```

On the node (`gcloud compute ssh termini-node-c`):

```
$ lxc list -c n,s,t,f
+--------------------------------------+---------+-----------+--------------+
|                 NAME                 |  STATE  |   TYPE    |  BASE IMAGE  |
+--------------------------------------+---------+-----------+--------------+
| termini-01m53y85m5wzz81s3gkmzzsz-0-0 | RUNNING | CONTAINER | c3d039642a61 |
+--------------------------------------+---------+-----------+--------------+

$ lxc config show termini-01m53y85m5wzz81s3gkmzzsz-0-0 | grep -E 'limits|volatile.base_image'
  limits.cpu: "1"
  limits.memory: 1GiB
  volatile.base_image: c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4
$ lxc info termini-01m53y85m5wzz81s3gkmzzsz-0-0 | grep Status
Status: RUNNING
```

Capacity moved on node-c, and only on node-c:

```
{"cpu_cores":8,"ram_gb":31,"disk_gb":57,"cpu_allocated":1,"ram_allocated_gb":1,
 "disk_allocated_gb":4,"cpu_free":15,"ram_free_gb":28,"disk_free_gb":48}
```

## 5. Cleanup — released, deleted, and proven empty

Release returns the commit, and the instance is deleted explicitly — the
**deployed** controller build at run time predates the release-teardown fix
(#203 / `t_28ab1be5`, merged to main during this task), so on that build release
left the workload running and the instances were removed by hand:

```
POST …/vms/release {"plan_id":"01M53Y85M5WZZ81S3GKMZZSZ88","reason":"node-c trust + materialisation verification t_d31b8e02"}
HTTP 200
{"status":"released","released":{"cpu_allocated":1,"ram_allocated_gb":1,"disk_allocated_gb":4},
 "rows_released":1,"released_at":"2026-10-17T03:25:52Z"}
```

```
$ gcloud compute ssh termini-node-c --command="lxc delete --force termini-01m53y85m5wzz81s3gkmzzsz-0-0; lxc list -c n,s"
delete_rc=0
+------+-------+
| NAME | STATE |
+------+-------+
```

Back to the baseline — 0 committed, empty instance list, trust still installed:

```
node 01M4ZQ7Q2J80ATY3K32PY3PPJH: status online, cpu_allocated 0, ram_allocated_gb 0,
disk_allocated_gb 0, cpu_free 16, ram_free_gb 29

$ python3 -m app.lxdtrust verify --node https://termini-node-c…:8443
https://termini-node-c…:8443: TRUSTED — authenticated GET /1.0/instances -> 200
```

## 6. The fix is in the raise path, not a one-off

A one-off install would be undone by the next raise: recreating an instance
replaces its boot disk, so its trust store comes back **empty**. The repo now
carries the step.

- **`infra/gcp/tools/install-lxd-trust.sh`** (new) — probes from the controller
  (`app.lxdtrust verify`), then, only if the node answers 403, mints the token on
  the node (`termini-bootstrap mint-trust-token`) and consumes it on the
  controller (`app.lxdtrust trust --token`), then re-probes and exits non-zero if
  the node is not proven trusted. `--node a|c|b` (repeatable), `--all` (skips a
  missing or non-RUNNING instance — node-b is TERMINATED today), `--timeout`,
  `--dry-run`. The token is single-use, never printed, and transits a 0600 temp
  file removed on exit.
- **`infra/gcp/tools/raise-standing-fleet.sh`** — `--raise` now installs the
  controller identity into **every node it raised** (idempotent; `--no-trust-install`
  opts out and says loudly that the nodes are then untrusted; `--trust-timeout`
  bounds the wait for a freshly booted node).
- **`docs/contracts/node-bootstrap.md` §6.4** — the step is contractual: the 200/403
  signature, why an untrusted node is invisible to the guardrail, and the explicit
  note that **node-b has never had it** (TERMINATED, 12/12 vCPU) and needs the same
  step when it is raised.
- **`infra/gcp/README.md`** — raise-path section + the offline `bash -n` list.
- **`tests/test_raise_lxd_trust.py`** (new, 14 tests) — pins that the raise path
  calls the install per raised node, that an already-trusted node is a no-op (no
  token, no POST), that an untrusted node gets the identity and is re-proven, that
  the token never reaches the output, that a trust install which cannot prove
  itself fails, and that a TERMINATED node is skipped by `--all` but is an error
  when named explicitly. Run against a scripted `gcloud`.

### Live proof of the raise-path tool itself (not just the manual flow)

Section 3's install was first done by hand with the same two commands. To prove
the **committed helper** performs the real install, node-c's trust entry was
removed again (back to the defect state), and then:

```
$ infra/gcp/tools/install-lxd-trust.sh --node c --timeout 120
[lxd-trust] c: NOT TRUSTED (403) — installing the controller identity
[lxd-trust] c: minted a 445-byte one-time trust token on termini-node-c
https://termini-node-c…:8443 [token]: trusted — certificate added to trust store; authenticated GET /1.0/instances -> 200
[lxd-trust] c: TRUSTED — controller identity installed and proven (fingerprint below)
[lxd-trust]   fingerprint=a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5
[lxd-trust] done — every requested node is trusted by the controller identity
RC=0
```

`--all` on the whole fleet (idempotent on the two RUNNING nodes, honest about
node-b):

```
$ infra/gcp/tools/install-lxd-trust.sh --all --timeout 60
[lxd-trust] b: termini-node-b is TERMINATED — skipped (only a RUNNING node can be trusted)
[lxd-trust] a: already trusted — no mutation (idempotent no-op)
[lxd-trust] c: already trusted — no mutation (idempotent no-op)
[lxd-trust] done — every requested node is trusted by the controller identity
ALL_RC=0
```

Offline suite:

```
$ python3 -m unittest tests.test_raise_lxd_trust
Ran 14 tests in 0.450s
OK
```

## 7. Fleet state left behind

- controller, node-a, node-c **RUNNING** (unchanged); node-b **TERMINATED** (untouched).
- node-c: trust store holds exactly the controller identity; **0** committed vCPU/GiB/GB,
  and `lxc list` is empty. Nothing from this run remains.
- node-a: not touched by this run. It still carries other workstreams' instances
  (`verif-fail`, `verif-proof`, and the pre-existing leaked `termini-…` containers
  from released plans) — those belong to `t_28ab1be5` / the other live workers, not
  to this card.
- No GCP instance was created, started or stopped by this run.

## 8. What this does not fix

- **Node-b has never had the trust installed** (it is TERMINATED; the project's
  `CPUS_ALL_REGIONS` is 12, consumed exactly by controller(2) + node-a(2) +
  node-c(8)). When it is ever raised it needs the same step — now one command
  (`install-lxd-trust.sh --node b`) and nothing else; that is why the step is in
  the raise path rather than in a per-node manual note.
- **Release teardown of the materialised workloads** is F3 (`t_28ab1be5`). The
  controller deployed at run time predated that fix, so this run released its
  commit *and* deleted its instance by hand; with #203 on main the release itself
  now tears the workload down, and node-c ends empty either way.
- The controller still runs with `TERMINI_LXD_SERVER_CA` unset, so node server
  certificates are not pinned (the `DISABLED (no server_ca pinned)` warnings in
  the captures above). Trust is unaffected; pinning is a separate hardening item.
