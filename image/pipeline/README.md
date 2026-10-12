# Termini node image pipeline (`agentic-os/w3`)

This directory bakes the **immutable pre-baked node image**: base OS + tooling +
LXD + llama.cpp + CUDA (gpu variant) + the pinned GGUF weights **already on local
disk**, plus **the workload image the demo launches** — so a fresh node's first
boot installs nothing and downloads nothing (D3, `docs/contracts/infra-decisions.md`).

- Recipe check (no build host, no network, no multi-GB downloads):

  ```bash
  bash image/pipeline/build-image.sh --check      # must print RECIPE OK
  bash image/pipeline/verify-pipeline.sh          # recipe check + bake rehearsal + GPU verifier
  ```

- Real bake (needs `packer` + KVM, or `distrobuilder`):

  ```bash
  bash image/pipeline/build-image.sh --variant compute   # Node A/B
  bash image/pipeline/build-image.sh --variant gpu       # Node C
  ```

- Golden-VM bake (when neither `packer` nor `distrobuilder` is installed — the
  route that actually produced the deployed family members, now committed):

  ```bash
  export PATH="$HOME/google-cloud-sdk/bin:$PATH"
  image/pipeline/tools/bake-golden-node-image.sh \
    --workload-tarball ~/.cache/termini/images/agent-vm-2026.10.0-c3d039642a61.tar.gz
  image/pipeline/tools/verify-baked-node.sh --image-name termini-node-os-4
  ```

  See [`golden-vm/README.md`](golden-vm/README.md). It creates a throwaway VM,
  runs `golden-vm/bake-node-os.sh` as a single pass (verify before
  de-instancing), captures a GCE image into the `termini-node-os` family, and
  deletes the VM. `verify-baked-node.sh` boots a node from the family and proves
  the image ON the node, then deletes it — the behaviour test `--check` cannot be.

## Image identities (do not conflate them)

| Image | Where it is authored | Alias it ships under | Launched by |
|-------|----------------------|----------------------|-------------|
| **Host node image** | this pipeline | `agentic-os/w3` | the node *is* it (first boot) |
| **Workload (agent VM) image** | `image/agent-vm/` | `agent-vm/<version>` (contract) **and** `agent-os/w4` (demo per-node) | the controller's `instance.create` and the warm/placement layer |

The workload image is the guest the controller materialises agent VMs from. Its
authoritative alias is **`agent-vm/<version>`** — `docs/contracts/agent-runtime.md`
§3.1 and §4.2 fix `source.alias = "agent-vm/2026.10.0"` for the LXD launch the
controller drives. The demo's warm/placement tooling (`infra/agent-vm/`,
`infra/images/`, `infra/warm/`) launches the *same content* under the per-node
alias **`agent-os/w4`**. The bake therefore registers **both** aliases so either
caller resolves on a fresh node.

## The workload-image stage (D3)

Because first boot never contacts a registry, the workload image must be
**imported into the image's local LXD image store at bake time**. That happens
through a staging directory:

```
$IMAGE_STAGE/images.json          the stage manifest (names the aliases)
$IMAGE_STAGE/<tarball>.tar.gz     the exported LXD image
```

Flow:

1. **Populate** — `image/pipeline/tools/stage_workload_image.sh` exports the
   workload image from the build host's local LXD store (no registry) and writes
   the manifest + tarball + digest into `$IMAGE_STAGE` (default
   `/var/lib/termini/images-staged`). `build-image.sh` runs it automatically
   before a bake.

   ```bash
   image/pipeline/tools/stage_workload_image.sh --stage /var/lib/termini/images-staged
   ```

2. **Deliver** — the Packer recipe uploads the stage into the build VM
   (`/tmp/termini-images-staged`); the distrobuilder recipe expects it at the
   canonical path. Both pass `IMAGE_STAGE` to the import step.

3. **Import** — `image/pipeline/provision/import-staged-images.sh` (invoked by
   `provision/10-node-os.sh`) verifies each tarball's digest and imports it under
   **every alias the manifest declares** (`lxc image import <tarball> --alias …`).
   LXD is initialised first (`lxd init --preseed`, API on `:8443`) so the import
   has a daemon to talk to.

### Why the import is manifest-driven and loud

The alias cannot be derived from the tarball's file name (it contains a slash).
An earlier revision keyed the alias off the basename inside an `if [ -d … ]`
guard, so an **unpopulated stage imported zero images and still exited 0** — the
bake reported success while shipping an image with nothing to launch. The import
now **fails** on a missing/empty stage or a digest mismatch, with a message that
says what to stage. Set `TERMINI_ALLOW_MISSING_WORKLOAD_IMAGE=1` only for a
deliberate non-demo bake that ships no workload image.

`image/pipeline/tools/check_image_stage.py` validates a stage (manifest shape,
tarball presence, digests, required alias). `build-image.sh --check` runs its
`--selftest` **and** asserts the stage wiring end-to-end, so a regression that
reintroduces the silent-zero-import bug fails the recipe check.

## Honest scope

`build-image.sh --check` and `verify-pipeline.sh` validate the recipe and the
stage/import logic on any checkout, without a build host. Confirming the image on
a **real node** — that `lxc image list` shows the workload image under
`agent-vm/2026.10.0` and `agent-os/w4` after boot — is a BEHAVIOUR test no linter
can perform, but it no longer needs `packer`: the golden-VM route
([`golden-vm/`](golden-vm/README.md)) bakes a GCE image from a throwaway VM and
[`tools/verify-baked-node.sh`](tools/verify-baked-node.sh) boots a node from the
family and asserts it there. What still requires `packer`/`distrobuilder` is the
qcow2 / distrobuilder container artifact and the GPU-CUDA-model payloads — see the
honest-scope section of [`golden-vm/README.md`](golden-vm/README.md).
