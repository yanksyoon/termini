# Pre-baked OS image residency on the demo nodes (`infra/images`)

Owner: kanban task **`t_f0017269`** — *Pre-warm pre-baked OS images on nodes A/B/C
(zero network boot)*. Parent workstream: `t_5630daf9` (W6 demo reliability).

Goal: every demo node carries the pre-baked OS images **on its own disk**, so a
node boots and launches an agent VM with **zero network fetches**. Scope is
images only — the model cache (`t_5968666d`) and the live warm cluster
(`t_ef5ef65c`) are separate cards.

There are two pre-baked images:

| Role | Host alias | Per-node alias | What it is |
|---|---|---|---|
| host node image | `agentic-os/w3` | — (the node *is* it) | the OS a node boots (image/ pipeline) |
| agent VM image | `agent-vm/2026.10.0` | `agent-os/w5` | the workload image a node launches agent VMs from (image/agent-vm) |

---

## 1. Acceptance → command

| Card acceptance criterion | How it is met | Command |
|---|---|---|
| the required image artifacts are present on disk **on each node** (A/B/C) | `prewarm-images.sh` caches both image tarballs under `/var/lib/termini/images/` on every node (sha256 sidecars) and imports `agent-os/w5` into each node's local LXD store from that on-disk tarball | `infra/images/prewarm-images.sh` |
| a fresh agent VM **launches from the local image without egress** (egress blocked / zero downloads) | `verify-image-residency.sh` drops the node's default route + off-lab egress, launches a fresh VM from the local alias, and proves the boot needed no network (egress still blocked after launch **and** the image's own first-boot evidence reports `network_downloads_at_boot: 0`) | `infra/images/verify-image-residency.sh` |

## 2. What was found (the gap this closes)

Before this card the images were **not** resident on every node:

- `demo-node-a` / `demo-node-b` had the agent alias `agent-os/w4` in their nested
  LXD store (from `t_7c40f9a3`), but **no on-disk tarball cache**;
- `demo-node-c` had an **empty nested LXD store** — no agent image at all.

`prewarm-images.sh` closes both: it caches both tarballs on all three nodes and
imports the agent alias on `demo-node-c` (idempotent on A/B).

## 3. Commands

```bash
infra/images/prewarm-images.sh                     # place + cache on A/B/C (idempotent)
infra/images/prewarm-images.sh --nodes "demo-node-c"
infra/images/verify-image-residency.sh             # THE assertion (exit 0 = warm)
infra/images/verify-image-residency.sh --no-launch # residency checks only (no egress block)
```

`prewarm-images.sh` exports each host image **once** to
`~/.cache/termini/images/` and pushes the tarball to every node's
`/var/lib/termini/images/`, importing the agent alias from the cached tarball
rather than re-exporting. It writes a machine-readable manifest to each node at
`/var/lib/termini/images/cache.json`:

```json
{"kind":"termini.image_cache","node":"demo-node-c",
 "host_image":{"alias":"agentic-os/w3","fingerprint":"f999…","file":"agentic-os-w3-f999….tar.gz","sha256":"…"},
 "agent_image":{"alias":"agent-vm/2026.10.0","version":"2026.10.0","fingerprint":"c3d0…","node_alias":"agent-os/w4","file":"agent-vm-2026.10.0-c3d0….tar.gz","sha256":"…"}}
```

## 4. What `verify-image-residency.sh` asserts, per node

- `host_image_baked` — `/var/lib/termini/.baked-offline` sentinel present;
- `host_image_version` — `/etc/termini/image-version` matches the expected version;
- `host_image_cached` — the host image tarball is on the node's disk and its
  sha256 matches its sidecar (and the host export cache, when present);
- `agent_image_cached` — same for the agent image tarball;
- `agent_alias_local` — `agent-os/w4` resolves in the node's **local** LXD store
  at the published fingerprint;
- `egress_blocked` / `egress_after_launch` — off-lab TCP is unreachable before
  and after the launch, so nothing could have been pulled;
- `offline_launch_ok` — `lxc launch agent-os/w4 <throwaway>` on the node succeeds;
- `zero_network_downloads` / `verified_offline` — the launched image's own
  first-boot evidence reports `network_downloads_at_boot: 0` and
  `verified_offline: true`.

Egress is dropped with the node's default route **and** an nftables output rule,
and is restored after each node (a `trap` covers failures). Emits
`evidence/image-residency-<ts>.json` (+ a `.txt` transcript) and exits non-zero
if any check fails.

## 5. Honest scope

- The demo cluster is **emulated on this single host** as LXD containers
  (`demo-node-a/b/c`) — the three GCP nodes of `infra/gcp/` are not provisioned
  here (same caveat as `infra/warm`). Placement of the agent image runs on those
  containers; the same commands apply unchanged to real underlay nodes.
- **Placement policy is unchanged:** per `infra-decisions.md` D5 agent VMs run on
  the compute nodes A/B; `demo-node-c` is the inference node. `verify-image-
  residency.sh` proves node C can also *boot the image offline* (image
  self-containment), and tears the throwaway instance down immediately — it does
  not change where the demo places agent VMs.
- No multi-GB artifact was rebuilt here: `prewarm-images.sh` **re-uses the images
  already published** on this host (`agentic-os/w3`, `agent-vm/2026.10.0`); the
  real bake lives in `image/` and `image/agent-vm/`.
- `lxc launch` on the nodes runs **containers** (no KVM/GPU on this host); the
  production path launches VMs from the same tarballs.
- **Hotspot / coordination:** the egress-block phase briefly drops each node's
  default route (a few seconds per node, restored per node). Do not run it
  concurrently with a task that depends on a node's outbound connectivity.
  `demo-node-a/b` are shared fixtures also used by `infra/agent-vm` and
  `infra/warm`; the placement here is additive and idempotent.

## 6. Files

```
infra/images/
  README.md                    this file
  prewarm-images.sh            cache both tarballs + import agent alias on each node
  verify-image-residency.sh    acceptance: residency + blocked-egress zero-download boot
  evidence/                    captured run (json + txt)
```
