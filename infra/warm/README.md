# Termini demo warm layer (`infra/warm`)

Owner: kanban task **`t_9fa53ad8`** — *Pre-warm pre-baked cluster so the demo
never cold-provisions*. Parent workstream: `t_5630daf9` (W6 demo reliability).

The live demo must never install, download, build, or cold-load anything. This
directory makes that true and *provable*: it bakes the node image, stands up the
demo cluster, warms every slow path, and ships **one command** —
`warm_check.sh` — that fails loudly if any cold path is still cold.

---

## 1. Acceptance, mapped to commands

| Task acceptance criterion | How it is met | Command |
|---|---|---|
| pre-baked images with all runtime deps present at boot; pinned alias | `bake-image.sh` bakes deps into one LXD image and publishes `agentic-os/w3` + an immutable tag | `demo-env.sh bake` |
| demo nodes created, started, registered with the controller | `demo-env.sh up` creates A/B/C from the alias and boots the real bootstrap agent | `demo-env.sh up` |
| every slow path warm (model resident, layers pre-pulled, Scenario-B scratch volumes) | `demo-env.sh warm` (called by `up`) | `demo-env.sh warm` |
| `warm_check.sh` single command, specific failure reason | `warm_check.sh` (32 checks on this demo cluster, one reason per failure) | `warm_check.sh` |
| green after a **full stop/start with no network fetch** | `demo-env.sh restart` → `warm_check.sh` | `demo-env.sh restart && warm_check.sh` |
| fresh node reaches *registered + inference responding* **< 30 s** from snapshot restore | `demo-env.sh fresh` restores a node from the `unregistered` snapshot, preloads the GGUF into the host page cache, starts it, and times `ready` (both conditions first true) | `demo-env.sh fresh` |

## 2. What actually runs here

The demo environment is **emulated on this single host** (8 cores, 19 GiB, no
GPU, LXD snap 5.21 single-node) because the three GCP nodes A/B/C of
`infra/gcp/` are not provisioned in this build environment:

```
controller/  ──►  host, systemd transient unit `termini-controller`  :8085
                   the REAL controller core (registration + telemetry ingest)
demo-node-a  ──►  LXD container from alias agentic-os/w3   role=compute
demo-node-b  ──►  LXD container from alias agentic-os/w3   role=compute
demo-node-c  ──►  LXD container, role=inference, host model dir bind-mounted,
                   resident llama.cpp server on :8080 (OpenAI-compatible)
```

Each node runs `termini-bootstrap` (a minimal node-side implementation of
`docs/contracts/node-bootstrap.md` §5/§8): it gathers machine facts, registers
over `/v1/nodes/register`, persists identity so a restart reconnects instead of
re-enrolling, and streams C1-transformed telemetry at the demo cadence
(1000 ms). Controllers and agents are the real code in this repo, not stubs.

### Honest scope (emulated vs. specified)

| Aspect | This environment | Production path (`image/`, `infra/gcp/`, contracts) |
|---|---|---|
| Node hypervisor | LXD **containers** (instant boot, no KVM/GPU here) | LXD **VMs** on GCP nodes A/B/C (`image/pipeline/packer`) |
| LXD cluster | single-node LXD; nodes are separate instances | 3-member `termini` cluster (`infra-decisions.md` D1) |
| Inference | host-exposed `llama.cpp`, **CPU** (`--n-gpu-layers 0`) — D5 topology, no GPU on this host | same host-exposed service on Node C **GPU** |
| Model | the **real pinned** `deepseek-flash` GGUF (4.58 GiB) resident in RAM | same, plus `deepseek-v4-pro` lazy |
| Guest `agent-vm` image | pinned alias preloaded in the host store; no nested LXD to preload into containers | preloaded into each node's local LXD store (D3) |
| Transport | plain HTTP on the demo underlay | mTLS + LXD join tokens (`# not in controller core`) |

Everything else — the bake, the alias pinning, the registration handshake, the
telemetry schema/governance gate, the residency proof — is executed for real.

## 3. Commands

```bash
infra/warm/bake-image.sh                 # build + publish the pre-baked image (once)
infra/warm/demo-env.sh up                # controller + nodes + warm + snapshots
infra/warm/demo-env.sh status            # node/role/state/ip
infra/warm/warm_check.sh                 # THE assertion (exit 0 = warm)
infra/warm/demo-env.sh down              # full stop (nodes + controller)
infra/warm/demo-env.sh restart           # down + up, prints the timing
infra/warm/demo-env.sh fresh             # snapshot-restore a fresh node, timed
```

`bake-image.sh` builds `llama.cpp` at the pinned commit
(`image/models.lock.json::llamacpp.commit`) unless `TERMINI_LLAMA_BIN_SRC`
points at a prebuilt pair — that is the CI fast path. It installs the canonical
node bootstrap daemon + unit by running the image pipeline's provision step
(`image/pipeline/provision/60-node-bootstrap.sh`), so the alias and the image
recipe share one implementation. The one-time model fetch and bake may use the
network; **everything after that is offline**. On a build host whose containers
have no working DNS, set `TERMINI_BAKE_BASE=<image>` to a base whose runtime
packages are already present — the package step then installs nothing.

Restore points: `demo-env.sh up` records `unregistered` on each node **before it
first boots** (the pre-enrollment point `fresh` restores from) and `warm` after
the model is resident. Both are created once and never overwritten by a later
`up`/`restart`, so a restart cannot capture (and later restore) an enrolled or
degraded state. `demo-env.sh fresh` times the window from `lxc start` of the
restored node to *registered **and** inference responding* and fails if it
exceeds `REGISTER_BUDGET_S` (30 s).

## 4. What `warm_check.sh` asserts

One `PASS`/`FAIL` line per check; any `FAIL` prints a specific reason and the
script exits non-zero.

- `image_alias_pinned`, `image_immutable_tag`, `image_fingerprint`, `image_manifest`
- `controller_reachable`, `controller_project`
- per node: `.running`, `.agent_online`, `.no_error`, `.registered`,
  `.baked_offline`, `.scratch`, `.telemetry`
- inference node: `.inference_health`, `.inference_models`, `.model_file`,
  `.model_sha_sidecar`, `.model_fs_local`, `.model_resident`

`--json` emits a machine-readable report on stdout only (implies `--quiet`);
`--quiet` prints only failures.

## 5. Files

```
infra/warm/
  lib.sh                    shared config (alias, nodes, controller, model pins)
  bake-image.sh             bake + publish the pre-baked node image
  demo-env.sh               bake | up | warm | down | restart | fresh | reset | status
  warm_check.sh             the single warm assertion
  node/termini-bootstrap.py node agent: register + telemetry
  node/termini-llama.sh     resident llama.cpp launcher
  node/termini-model-present.sh  ExecCondition: only run where the GGUF is present
  node/termini-bootstrap.service, node/llama-cpp.service  systemd units
  evidence/                 captured proof (see below)
```

## 6. Evidence (`infra/warm/evidence/`)

| File | Shows |
|---|---|
| `warm_check-after-restart.txt` | `warm_check.sh` green (32 checks) after a full `demo-env.sh restart` |
| `restart-timing.txt` | the stop/start transcript + proof the `unregistered` restore point is unchanged by the restart |
| `warm_check.json` | machine-readable warm report (`ok:true`, `failed:0`) |
| `fresh-timing.txt` | a snapshot-restored node reaching registered + inference within the 30 s budget |
| `rebuild-and-warmcheck.txt` | a clean `reset → up → warm_check` from the baked alias |
| `offline-egress.txt` | baked deps present, bake sentinel set, node peers are the local controller only |

These were captured on the shared build host, which runs several workers at
once; the numbers include that contention. The functional claims (green
`warm_check`, registration, residency, alias/tag pinning, offline boot) do not.

