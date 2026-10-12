# Golden-VM bake of the node OS image (`termini-node-os` family)

This directory is the **golden-VM route**: the reproducible, committed bake that
produces a new member of the `termini-node-os` family when `packer` /
`distrobuilder` are not available on the build host. It is the route that actually
produced the deployed family members, and it used to exist only in a previous
session's scratch work — hand-assembled and unrepeatable. It is now one command.

Two files carry it:

| File | Runs where | Role |
|------|-----------|------|
| [`bake-node-os.sh`](bake-node-os.sh) | **on the golden VM** (root, single pass) | LXD → workload image → stage → import → daemon → **verify** → de-instance-ify |
| [`../tools/bake-golden-node-image.sh`](../tools/bake-golden-node-image.sh) | on the operator's host | create VM, upload tree + workload tarball, run the bake, stop, capture the image, delete the VM |
| [`../tools/verify-baked-node.sh`](../tools/verify-baked-node.sh) | on the operator's host | boot a node from the family and prove the image on the node, then delete it |

## Run it

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

# 1. bake -> publishes termini-node-os-4 into the family
image/pipeline/tools/bake-golden-node-image.sh \
  --workload-tarball ~/.cache/termini/images/agent-vm-2026.10.0-c3d039642a61.tar.gz

# 2. prove it on a real node (boots, checks, deletes the node)
image/pipeline/tools/verify-baked-node.sh --image-name termini-node-os-4
```

The workload tarball is the **published** workload image (`lxc image export
agent-vm/2026.10.0`, `image/agent-vm/build-agent-image.sh`). Pass `--dry-run` to
either script to print the plan without touching GCP.

## What the image must contain, and under which alias

The repo used to disagree about the workload alias, so the bake registers **both**
and the on-node verification asserts both resolve to the same fingerprint:

| Alias | Who launches it | Authority |
|-------|-----------------|-----------|
| `agent-vm/<version>` (e.g. `agent-vm/2026.10.0`) | the controller's `instance.create` | **frozen contract** — `docs/contracts/agent-runtime.md` §3.1/§4.2 `source.alias` |
| `agent-os/w4` | the demo warm/placement layer (`infra/agent-vm/`, `infra/warm/`) | per-node demo alias over the same content |

Beyond the aliases, the image must carry:

- **LXD installed, initialised AND usable**, API serving on `:8443` — installing
  the snap is not initialising it, and *listening is not usable*. The image must
  have a `default` storage pool, the `lxdbr0` guest bridge, and a `default`
  profile carrying a root disk + NIC, because the controller's `instance.create`
  sends `profiles: ["default"]` with no devices. Without the profile devices a
  node answers `200` on `:8443` and still cannot launch anything
  (`Failed getting root disk: No root device could be found`).
- **The current `termini-bootstrap`** (`infra/bootstrap/node/termini-bootstrap.py`
  + its unit), which reports the node's **own LXD endpoint** at registration
  (`machine.lxd.endpoint`) so the controller can address one node among a fleet.
- The `.baked-offline` sentinel and `/etc/termini/image-version` (D3 — first boot
  installs/downloads nothing).

## The bake pass, in order (each step depends on the previous)

1. **LXD up** — `snap install lxd`, `lxd init --preseed`, `lxc config set
   core.https_address ":8443"`, restart, wait for `200` on `/1.0`.
2. **Workload image into the local store** under `agent-vm/<version>`.
3. **Stage** it with `image/pipeline/tools/stage_workload_image.sh` (export +
   manifest — the alias cannot be derived from a tarball file name; it contains a
   slash).
4. **Import** — run the real `image/pipeline/provision/10-node-os.sh`, which
   re-asserts LXD and imports the stage under *every* alias the manifest declares.
   This is the bake's own code path, not a hand-created alias, and it **fails
   loudly on an empty stage**.
5. **Daemon** — `image/pipeline/provision/60-node-bootstrap.sh` (current daemon +
   unit, enabled).
6. **VERIFY** — postconditions, not file presence (see below).
7. **De-instance-ify LAST** — `cloud-init clean`, drop SSH host keys, empty
   `/etc/machine-id`, then `ssh-keygen -A`.

## Traps this script exists to avoid

- **`snap set lxd core.https_address` is REJECTED** (`invalid option name`). The
  snap form is a silent no-op under `|| true`; the LXD-native
  `lxc config set core.https_address ":8443"` is what binds the API.
- **`lxc info` is not an "is LXD initialised" probe.** It returns 0 on a daemon
  that is running but has no storage pool and a device-less `default` profile —
  the state a freshly-started snap is in. Using it as the init guard SKIPS the
  preseed, so the image ships a hypervisor that answers `200` on `:8443` and
  cannot launch anything. `provision/10-node-os.sh` now probes the postcondition
  (a storage pool) and then *ensures* the pool, bridge and default-profile devices
  idempotently, asserting each — the fix belongs there because packer and
  distrobuilder share that provisioner.
- **Initialise before importing.** `lxc image import` needs a live daemon; an
  import guarded by `if [ -d "$STAGE" ]` over an empty stage imports *zero* images
  and exits 0 — the silent-success bug the manifest-driven import replaces.
- **De-instance-ify destroys your own access** (it removes the host keys), so the
  whole bake-and-verify is ONE pass and every diagnostic runs before cleanup.
- **A recipe linter is not a behaviour test.** `bash
  image/pipeline/build-image.sh --check` proves syntax/JSON/manifest wiring and
  nothing about runtime. The behaviour test is `verify-baked-node.sh`, which boots
  a node and probes it — including a real `lxc launch` from the workload alias.
- **De-instancing a golden VM is a whole pass.** `lxc launch` under `bash -s` will
  steal the rest of a script from stdin (surfacing as a nonsense LXD parse error);
  deliver on-node check scripts as FILES.

## What this route cannot prove without packer

`packer`/`distrobuilder` are absent, so this route does **not**:

- build a qcow2 from `packer/termini-node.pkr.hcl` or the distrobuilder container
  variant, or exercise those provisioners' own ordering;
- bake the GPU/CUDA/model payloads (`15-nvidia-driver.sh`, `20-gpu-cuda.sh`,
  `30-llamacpp.sh`, `40-models.sh`) — this bake runs the always-on subset
  (`10-node-os.sh`, `60-node-bootstrap.sh`) that the compute node needs;
- validate `image-manifest.json` against the schema (that is `build-image.sh`'s
  job when a real builder runs).

The golden-VM image is GCE-specific (a boot disk), so it is captured as a GCE
image rather than a distributable LXD/qcow2 artifact. `build-image.sh --check` is
still the recipe gate: run it after any change to these files.
