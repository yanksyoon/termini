# Termini pre-baked image pipeline (`agentic-os/w3`)

- Owner: kanban task **`t_ab18d582`** (workstream: agents / Node-C bake).
- Status: **shipped** for W3 (see "Verification" and "Honest scope" below).
- Consumers: `t_066b6e3d` (cloud-init bootstrap — references the alias), `t_95528ae8`
  (Node C inference over HTTP), `t_58a1fed1` (LXD cluster bring-up).
- Contract sources: `docs/contracts/infra-decisions.md` (D3, D5),
  `docs/contracts/node-bootstrap.md` (§3.1), `docs/contracts/agent-runtime.md`
  (§3, §8, §11).

This directory builds the **immutable pre-baked node image**: base OS + tooling +
LXD + llama.cpp + CUDA runtime + the pinned GGUF weights **already on local disk**,
so a fresh node's first boot installs nothing and downloads nothing.

---

## 1. Published identity (for downstream tasks)

| Field | Value |
|-------|-------|
| **Stable alias** | `agentic-os/w3` |
| **Version** | `2026.10.0` |
| **Immutable tag** | `agentic-os-w3-2026.10.0+<12-hex-gitsha>` (`agentic-os/w3-<tag>`) |
| **Checksum** | `sha256` recorded per artifact in `image-manifest.json` + a sibling `<artifact>.sha256` |
| **Variants** | `compute` (Nodes A/B) · `gpu` (Node C — CUDA runtime + resident weights) |
| **Artifact formats** | `qcow2` (Packer/QEMU → GCP machine image or on-prem) · `lxd-image` tar (distrobuilder) |

### Approximate size (from the recipe; measured at build time into the manifest)

| Variant | Alias | Approx. on-disk size | Composition |
|---------|-------|----------------------|-------------|
| `compute` | `agentic-os/w3` (compute) | **~2.2 GiB** | Ubuntu 24.04 base (~0.6) + base pkgs (~0.6) + LXD snap (~0.25) + llama.cpp CPU (~0.03) + python/node/ruby deps (~0.7) |
| `gpu` | `agentic-os/w3` (gpu, Node C) | **~20 GiB** | compute base + NVIDIA driver (~0.5) + CUDA/cuDNN runtime (~3.5) + llama.cpp CUDA build (~0.2) + **model weights 12.95 GiB** |

The "~4 GiB GGUF" the task called for is the `deepseek-flash` weight (4.58 GiB);
`deepseek-v4-pro` (8.37 GiB) is also resident on disk but **lazy-loaded** per
`agent-runtime.md` §11.2. A compute-only node carries **no** weights.

### What is baked in

| Layer | Contents |
|-------|----------|
| Base | Ubuntu Server 24.04 LTS, `chrony`, `nftables`, `openssh-server`, python3/ruby, cmake/gcc/git |
| Hypervisor | LXD snap, channel `5.21/stable`, revision recorded to `/etc/termini/lxd-snap.txt`, `snap refresh --hold` so boot never refreshes |
| Inference runtime | `llama.cpp` @ commit `7fe450e19305b828c199d602c23a8337aaa1f03b` (release `v0.5.0`) built to `/opt/termini/llama.cpp/bin/{llama-server,llama-cli}`; `llama-cpp@<alias>.service` template |
| GPU (gpu variant) | NVIDIA driver stack (`cuda-drivers-550` + `nvidia-smi`, step `15-nvidia-driver.sh`) + CUDA runtime 12.4 (`cudart`, `cublas`, `nvrtc`) + cuDNN 9. Nouveau is blacklisted and `nvidia`/`nvidia_uvm` load at boot; nothing is installed at boot. |
| Weights | `deepseek-flash` + `deepseek-v4-pro` GGUF under `/var/lib/termini/models/`, sha256-pinned |
| Agent deps | `/opt/termini/venv` (openai, httpx, pydantic, tenacity, structlog, langchain-core, langgraph), Node.js 20, ruby gems |
| Offline proof | `/var/lib/termini/.baked-offline` sentinel + `/etc/termini/image-version` + `termini-firstboot-preflight.service` |
| Workload images | guest `agent-vm/<version>` tarballs preloaded into the local LXD image store (no registry pull at boot) |

**Never installed at boot:** any `apt`/snap/registry fetch, any model download.
The first-boot unit actively forbids it (see §4).

---

## 2. Model pins — resolves BQ-1

`models.lock.json` pins the exact Hugging Face GGUF distills behind the two
aliases. The build fails if a downloaded file's `sha256` does not match the lock.

| Alias | Repo | File | Bytes | Residency |
|-------|------|------|-------|-----------|
| `deepseek-flash` | `bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF` | `...-Q4_K_M.gguf` | 4,920,736,608 (4.58 GiB) | resident |
| `deepseek-v4-pro` | `bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF` | `...-Q4_K_M.gguf` | 8,988,110,240 (8.37 GiB) | lazy |

`sha256` values were read from the Hugging Face "blobs" API (`lfs.sha256`, which
is the file-content digest); the `llama.cpp` commit is the **peeled** commit of
tag `v0.5.0` (`git ls-remote refs/tags/v0.5.0^{}`).

> **Cross-document flag (for the architect, `t_5fb81d14`).** `agent-runtime.md`
> §11.1 estimates flash ≈ 6 GiB and pro ≈ 18 GiB of weights, giving budgets of
> ~9 GiB / ~22 GiB and making `single-heavy` the default residency. The **actual**
> pinned weights are 4.58 GiB and 8.37 GiB, so the two aliases together need
> ~20.5 GiB with KV cache — which **fits an L4 24 GiB** and makes the `dual`
> policy viable. §11.1/§11.2 should be re-derived from these real sizes.

---

## 3. Build (reproducible from a clean checkout)

```bash
cd <repo>
image/pipeline/build-image.sh --variant gpu        # or --variant compute
```

Requirements on the **build host**: `packer` (or `distrobuilder`), `qemu`/KVM,
`jq`, `python3`, network access to `cloud-images.ubuntu.com`,
`developer.download.nvidia.com`, `huggingface.co`, `github.com`/`nodejs.org`.

Reproducibility is enforced by **pins**, not by "latest":

| Input | Pinned where |
|-------|--------------|
| Ubuntu base image | `build-image.sh` (`BASE_URL` + `BASE_SHA256`, dated release `20260911`) |
| llama.cpp | `build-image.sh` → `models.lock.json::llamacpp.commit` |
| Model weights | `models.lock.json::aliases.*.sha256` |
| CUDA runtime | `CUDA_PKG_SUFFIX` (default `12-4`) |
| Node.js | `NODE_VERSION` (default `20.18.0`) |
| LXD snap | channel `5.21/stable`; exact revision recorded in the image + manifest |

The builder emits `image-manifest.json` (schema: `manifests/image-manifest.schema.json`)
with the base digest, every pin, and the produced artifact's `size_bytes` + `sha256`.
Nothing is resolved from a moving tag at build time.

Publish under the stable alias:

```bash
image/pipeline/publish-image.sh <artifact.qcow2> <image-manifest.json>
# -> lxc image import <artifact> --alias agentic-os/w3 --alias <immutable-tag>
# -> writes published.json (alias, immutable_tag, version, sha256, size_bytes)
```

---

## 4. First boot: proving no network download happens

`pipeline/first-boot-preflight.sh` runs before `termini-bootstrap.service`
(`systemd/termini-firstboot-preflight.service`, ordered `Before=`;
`termini-bootstrap.service` adds `Requisite=termini-firstboot-preflight.service`).
**`termini-bootstrap.service` is not shipped yet** — it is the planned on-node
daemon (`docs/contracts/node-bootstrap.md`), which this image pipeline does not
build; the ordering above is forward-looking and is a no-op until that unit
exists. The preflight still runs and writes its boot log. It:

1. asserts the `llama-server` binary exists, is executable, and records its size + `sha256`;
2. asserts every pinned GGUF is present at its exact expected size, with a
   `.sha256` sidecar (full re-hash optional via `--verify-sha`);
3. asserts the model root is on a **local** filesystem (fails on `nfs/cifs/smb2/…`);
4. asserts the bake sentinel `/var/lib/termini/.baked-offline` exists;
5. writes `/var/log/termini/first-boot.json` and exits non-zero on any failure.

Because the unit is `Requisite=` for the bootstrap daemon, a node with a missing
or mismatched baked artifact **fails loudly instead of downloading** — the
`<5 s to registration` guarantee in `node-bootstrap.md` cannot be met by cheating.

`t_066b6e3d` (cloud-init) references the alias with the same string:
`image: agentic-os/w3` (variant `gpu` for Node C, `compute` for A/B).

---

## 5. Verification

One command, no build host required:

```bash
image/pipeline/verify-pipeline.sh            # recipe check + bake rehearsal + GPU verifier rehearsal
```

It runs three stages:

1. **Recipe check** (`build-image.sh --check`) — required files present, `bash -n`
   on every script, JSON well-formed, `python3 -m py_compile`, `shellcheck -S warning`
   on every script (CI parity), and the model/schema cross-check.
2. **Bake rehearsal** (`tools/rehearse_bake.py`) — builds a stub image root, runs
   the real `first-boot-preflight.sh` against it, and asserts:
   - the preflight **passes** and emits a boot log with `verdict: pass`,
     `network_downloads_at_boot: 0`, both aliases resolved from local disk;
   - deleting one model file makes the preflight **fail** with
     `model_files_present`/`model_sizes_match`/`model_sha256_sidecars` false;
   - the emitted `image-manifest.json` validates against the schema.
3. **GPU verifier rehearsal** (`tools/rehearse_gpu.py`) — drives `tools/gpu_check.py`
   against stub fixtures (fake `nvidia-smi`, fake `/dev` nodes, a stub `libcudart`
   built with `cc`) and asserts each verdict / failure reason.

The committed evidence is in `examples/` (`boot-log.rehearsal.json`,
`image-manifest.rehearsal.json`, `gpu-check.rehearsal.json`, `gpu-check.absent.json`).

### Node C GPU enablement

The gpu variant bakes the driver (`15-nvidia-driver.sh`) and the CUDA runtime
(`20-gpu-cuda.sh`). Acceptance for the GPU node — *nvidia-smi reports the GPU and
the device is usable from the host process* — is a standalone verifier:

```bash
image/pipeline/verify-gpu.sh                 # on live Node C: smi + host-process CUDA probe
image/pipeline/verify-gpu.sh --rehearse      # offline: stub-fixture rehearsal
```

`tools/gpu_check.py` writes a JSON report (`kind: termini.gpu_check`) and exits
non-zero with a stable `reason` (`nvidia_smi_absent`, `no_gpu_reported`,
`device_nodes_missing`, `device_not_openable`, `cuda_runtime_unusable`). It proves
usability at three levels: the driver answers (`nvidia-smi -L`), the device nodes
open (`/dev/nvidiactl`, `/dev/nvidia0`), and a real runtime call succeeds from the
checking process (`cudaGetDeviceCount`/`cudaMemGetInfo`/`cudaStreamCreate` via
`libcudart` with `ctypes`).

It is a **diagnostic**: per `node-bootstrap.md` §9 a missing driver/GPU must **not**
block registration (the node registers with `machine.gpus: []` and DEGRADED), so
nothing gates the first boot on it — `first-boot-preflight.sh` deliberately makes
no GPU assertion. Driver selection is a build-time switch:
`TERMINI_DRIVER_SOURCE=baked` (default; installs the pinned `cuda-drivers-<branch>`
from NVIDIA's CUDA apt repo) or `image` (assert the base image already ships the
stack and install nothing).

---

## 6. Honest scope — what was *not* executed here

This workstream ran inside a plain CI/dev host (no KVM, no GPU, no LXD, no Packer),
so the following are **specified and locally rehearsed, but not executed end-to-end**:

- **No multi-GB image was produced here.** `build-image.sh` (Packer/distrobuilder,
  the real bake) requires a build host; `--check` and the rehearsal are what ran.
- **No real node was booted** from the produced image. The first-boot preflight was
  exercised against a stub root (`examples/boot-log.rehearsal.json`), not a GCP VM.
- **No GPU/CUDA was exercised on real hardware.** The `gpu` variant's driver
  (`15-nvidia-driver.sh`) and CUDA steps are recipe-only here, and `verify-gpu.sh`
  ran only in `--rehearse` mode against stub fixtures (`examples/gpu-check.*.json`).
  The live `verify-gpu.sh` run — the actual `nvidia-smi` + `libcudart` probe against
  the Node C GPU — needs the real accelerator.
- **The alias was not published to a registry.** `publish-image.sh` is written and
  the alias string is frozen; publishing needs a built artifact (needs the bake).
- **`llama-server` model loading** was proven for real on the dev host: a CPU build
  of the **pinned** llama.cpp commit loaded a pre-warmed GGUF and served a
  completion with the network namespace stripped to loopback
  (`evidence/offline-load-proof.txt`). That proves the local-load mechanism, not
  the GPU variant.

Everything executed vs. specified is itemised in `evidence/README.md`.

The task verifier (`t_2e7ebf9d`) should treat §1–§5 as the reviewable contract and
plan a build-host run to convert the recipe into a bootable artifact.

---

## 7. Layout

```
image/
  README.md                         this file
  models.lock.json                  pinned aliases, sha256, llama.cpp commit
  manifests/image-manifest.schema.json
  pipeline/
    build-image.sh                  reproducible builder (+ --check)
    publish-image.sh                alias + version tag + checksum
    first-boot-preflight.sh         no-network boot assertion + boot log
    verify-pipeline.sh              clean-checkout verifier
    verify-gpu.sh                   Node C GPU acceptance (live, or --rehearse)
    provision/00..50*.sh            bake steps (base, LXD, NVIDIA driver, CUDA, llama.cpp, models, agent deps)
    packer/termini-node.pkr.hcl     VM/QCow2 recipe
    distrobuilder/termini-node.yaml container recipe
    systemd/termini-firstboot-preflight.service
    tools/verify_manifest.py        pin + manifest validator
    tools/rehearse_bake.py          local bake rehearsal
    tools/gpu_check.py              nvidia-smi + host-process CUDA probe
    tools/rehearse_gpu.py           GPU verifier rehearsal
  examples/                         committed rehearsal evidence
  evidence/                         what was actually executed (+ offline-load proof)
```
