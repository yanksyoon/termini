# Baked node image `termini-node-os-5` — bake + on-node verification (RAW EVIDENCE)

This file exists because the node image is the artifact the standing fleet is raised
from and the demo recording depends on, and because a previous bake reported success
while shipping an image that could not launch anything. Everything below is raw output
from a real bake and a real boot.

## Provenance

| | |
|---|---|
| Date | 2026-10-12 |
| Image | **`termini-node-os-5`** (family `termini-node-os`, 20 GB, `READY`) — NEWEST in the family, so `--image-family termini-node-os` boots it |
| Bake route | `image/pipeline/tools/bake-golden-node-image.sh` (golden VM; no `packer`/`distrobuilder` on this host) |
| In-VM bake | `image/pipeline/golden-vm/bake-node-os.sh` (single pass; verify before de-instance-ify) |
| Workload tarball | `agent-vm-2026.10.0-c3d039642a61.tar.gz`, sha256 `c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4` (verified against its sidecar before baking) |
| On-node verification | `image/pipeline/tools/verify-baked-node.sh --image-name termini-node-os-5` |
| Test node | `termini-node-verify` @ `10.10.0.20` (`n2-standard-2`), deleted by the verifier |
| Controller | not reachable during this run (VM `TERMINATED`, port unreachable), so check 4 uses a local recording stub that captures the node's own registration payload |

## 1. Bake — the workload image is placed by the bake's OWN tooling

The image is imported under the contract alias, staged through
`tools/stage_workload_image.sh`, and imported by `provision/10-node-os.sh` from the
manifest under **both** aliases:

```
[provision:import-images] importing agent-vm-2026.10.0-c3d039642a61.tar.gz as [agent-vm/2026.10.0 agent-os/w4]
```

```
[provision:60-node-bootstrap] baked termini-bootstrap (40732 bytes) + termini-bootstrap.service (enabled)
```

## 2. Bake-time verification (inside the golden VM, before de-instancing)

```
===== [bake] 6. verify the bake (postconditions, not file presence) =====
  CHECK api_8443_http_code           PASS  (200)
  CHECK core.https_address           PASS  (:8443)
  CHECK lxc_list_works               PASS  (0)
  CHECK storage_pool_default         PASS  (0)
  CHECK guest_bridge_lxdbr0          PASS  (1)
  CHECK default_profile_root_disk    PASS  (1)
  CHECK alias_agent-vm/2026.10.0     PASS  (c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4)
  CHECK lxc_image_list_has_agent-vm/2026.10.0 PASS  (agent-vm/2026.10.0)
  CHECK lxc_image_list_has_agent-os/w4  PASS  (agent-os/w4)
  CHECK bootstrap_daemon_exec        PASS  (0)
  CHECK bootstrap_unit_enabled       PASS  (enabled)
  CHECK bootstrap_reports_endpoint   PASS  (0)
  CHECK daemon_py_compiles           PASS  (0)
  CHECK baked_offline_sentinel       PASS  (0)
  CHECK image_version_present        PASS  (0)
[bake] all bake checks passed
```

```
===== [bake] 7. de-instance-ify =====
ssh-keygen: generating new host keys: RSA ECDSA ED25519 
[bake] cloud-init clean + host keys regenerated; workload aliases still resolve
BAKE_OK 2026-10-12T19:30:13Z version=2026.10.0 alias=agent-vm/2026.10.0 node_alias=agent-os/w4
```

## 3. On-node verification (booted from `termini-node-os-5`)

```
== host ==
termini-node-verify.asia-south1-a.c.termini-510609.internal
7.0.0-1011-gcp
termini-node-os 2026.10.0
== 1. LXD API on :8443 ==
  curl -sk https://127.0.0.1:8443/1.0 -> 200
  CHECK api_8443_http_code: PASS (200)
  CHECK core_https_address: PASS (:8443)
== 2. lxc list + a USABLE hypervisor ==
  CHECK lxc_list_rc: PASS (0)
  CHECK storage_pool_default: PASS (0)
  CHECK guest_bridge_lxdbr0: PASS (1)
  CHECK default_profile_root_disk: PASS (1)
== 3. workload image in the local store (both aliases) ==
+----------------------+--------------+--------+------------------------------------+--------------+-----------+-----------+------------------------------+
|        ALIAS         | FINGERPRINT  | PUBLIC |            DESCRIPTION             | ARCHITECTURE |   TYPE    |   SIZE    |         UPLOAD DATE          |
+----------------------+--------------+--------+------------------------------------+--------------+-----------+-----------+------------------------------+
| agent-os/w4 (1 more) | c3d039642a61 | no     | Ubuntu 24.04 LTS server (20260926) | x86_64       | CONTAINER | 764.36MiB | Oct 12, 2026 at 7:30pm (UTC) |
+----------------------+--------------+--------+------------------------------------+--------------+-----------+-----------+------------------------------+
  agent-vm/2026.10.0 fingerprint=c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4
  agent-os/w4 fingerprint=c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4
  CHECK alias_agent_vm: PASS (c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4)
== 4. the baked daemon reports its own LXD endpoint ==
  daemon carries the endpoint field: yes
  on-node config.yaml:
    controller_url: http://127.0.0.1:9099
    node_name: termini-node-verify.asia-south1-a.c.termini-510609.internal
    iface: ens4
    image_version: 2026.10.0
    role_hint: compute
  daemon log:
    2026-10-12T19:34:45Z [termini-bootstrap] REGISTERED node_id=01NODEFROMSTUB000000000000 role=compute register_ms=30 boot_to_registered_ms=37000
  registration body captured by the stub:
    machine.lxd.endpoint  = 10.10.0.20:8443
    machine.hostname      = termini-node-verify.asia-south1-a.c.termini-510609.internal
    machine.image_version = 2026.10.0
  CHECK machine_lxd_endpoint: PASS (10.10.0.20:8443)
== 5. offline sentinel + version ==
  CHECK baked_offline_sentinel: PASS (0)
  CHECK image_version_present: PASS (0)
== 6. launch a container from the workload alias ==
  CHECK workload_launchable: PASS (container running)
  verify-container,RUNNING
VERIFY_RESULT=PASS
```

## 4. Teardown proof (after the run)

Instances — every standing instance `TERMINATED`, no golden VM, no test node:

```
=INSTANCES=
NAME                STATUS      ZONE           MACHINE_TYPE
termini-controller  TERMINATED  asia-south1-a  n2-standard-2
termini-node-a      TERMINATED  asia-south1-a  n2-standard-2
termini-node-b      TERMINATED  asia-south1-a  n2-standard-2
termini-node-c      TERMINATED  asia-south1-a  n2-standard-8
=IMAGES=
NAME               FAMILY           STATUS  DISK_SIZE_GB
termini-node-os-1  termini-node-os  READY   20
termini-node-os-3  termini-node-os  READY   20
termini-node-os-5  termini-node-os  READY   20
=DISKS=
NAME                SIZE_GB  STATUS
termini-controller  30       READY
termini-node-a      60       READY
termini-node-b      60       READY
termini-node-c      60       READY
```

Images — `termini-node-os-4` was deleted because it carried the unusable-LXD defect;
`termini-node-os-5` is the newest family member:

```
=IMAGES=
NAME               FAMILY           STATUS  DISK_SIZE_GB
termini-node-os-1  termini-node-os  READY   20
termini-node-os-3  termini-node-os  READY   20
termini-node-os-5  termini-node-os  READY   20
```

Disks — exactly the four pre-existing fleet disks, no new standing resources:

```
NAME                SIZE_GB  STATUS
termini-controller  30       READY
termini-node-a      60       READY
termini-node-b      60       READY
termini-node-c      60       READY
```

## Cost

One `n2-standard-2` golden VM (~8 min) plus one `n2-standard-2` test node (~7 min) in
`asia-south1` at $0.1166/hr ≈ **$0.03** of compute, plus image storage. Order of cents.

## What this verifies, and what it does not

Verified by a real boot: the API answers `200` on `:8443`; the hypervisor is USABLE
(`default` storage pool, `lxdbr0`, `default` profile with a root disk); `lxc image list`
shows `agent-vm/2026.10.0` and `agent-os/w4` at the same fingerprint `c3d039642a61`;
a container LAUNCHES from `agent-os/w4` (the exact call the controller's
`instance.create` makes); the baked daemon registers and reports
`machine.lxd.endpoint = 10.10.0.20:8443`.

Not verified here: registration against the live controller (it was down; check 4 uses a
recording stub instead), and the packer/distrobuilder qcow2 + GPU/CUDA/model payloads
(neither builder is installed — see `image/pipeline/golden-vm/README.md`).
