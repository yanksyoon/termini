# Placing the agent VM image on Termini nodes (`agent-os/w4`)

- Owner: kanban task **`t_7c40f9a3`** (W4 — "Template Agent VM as LXD image and place
  across Nodes A/B").
- Input: the baked guest image alias `agent-vm/2026.10.0` from task `t_d1960fd6`
  (see `image/agent-vm/README.md`). **This directory does not build the image** — it
  distributes and launches it.
- Consumers: `t_fd8eee6c` (cross-node agent→llama.cpp path), `t_cca07879` (W4 root),
  the demo/warm environment (`infra/warm`).
- Contract sources: `docs/contracts/agent-runtime.md` §4 (`vm.launch`, profile
  `termini-agent`, launch-time cloud-init), `docs/contracts/infra-decisions.md` D2/D3
  (per-node guest bridge, offline-preloaded images), `docs/contracts/node-bootstrap.md`
  §3.1/§7 (preloaded workload images, project key → `/root/.ssh/authorized_keys`).

## Deliverables (the card's four)

| Deliverable | Value |
|---|---|
| **Alias name** | `agent-os/w4` |
| **Immutable fingerprint** | `c3d039642a61` (same content as `agent-vm/2026.10.0`, tarball sha256 `c3d039642a619e100ddf680c3d60e4d2641e568f377f5ed9ef420ad886b181e4`) |
| **Launch command** | `lxc launch agent-os/w4 agent-test -p termini-agent -c cloud-init.user-data="$(cat /root/agent-user-data.yaml)"` |
| **cloud-init / profile files** | [`agent-user-data.cloud-init.yaml`](agent-user-data.cloud-init.yaml) (injects the project SSH key, enables sshd), [`termini-agent.profile.yaml`](termini-agent.profile.yaml) (limits + `local` root disk + one `lxdbr0` NIC) |

## Placement note (one paragraph)

Agent VMs run on the **compute nodes — Node A and Node B**; Node C is the inference
node and does not host agent VMs (D5). Placement is a *pre-distribution*, not a
pull: `place-agent-image.sh` exports the baked image on the control host, copies the
tarball to each compute node and imports it into that node's **local LXD image store**
under `agent-os/w4`, so `lxc launch` resolves the alias on-box with zero registry/pull
traffic. Each node backs the alias with its own stock LXD: a `local` (dir) storage
pool and one guest bridge `lxdbr0` on `10.20.<n>.0/24` (`10.20.1.0/24` on A,
`10.20.2.0/24` on B, per D2), which the script creates idempotently if absent. The
agent runtime reaches Node C by the **Node C host's underlay IP** — the frozen value
`INFERENCE_BASE_URL=http://10.10.0.13:8080` (D2/D5) — **never** a guest/overlay
address, because a guest's egress SNATs to its node's underlay IP and Node C's
firewall only admits `tcp/8080` from the underlay `10.10.0.0/24`. In this lab the
nodes are themselves LXD containers on `10.14.100.0/24`, so "Node C's host IP" is
`demo-node-c`'s address on that bridge (`10.14.100.11`); the same placement runs
unchanged on real underlay nodes. **Adding a node later** is one command —
`./place-agent-image.sh --nodes demo-node-d` — which initialises that node's LXD,
imports the alias and creates the profile; bootstrap it with the next free guest
subnet (`10.20.4.0/24`) and no other node is touched.

## Place (once per node, idempotent)

```bash
infra/agent-vm/place-agent-image.sh --nodes "demo-node-a demo-node-b"
# -> exports agent-vm/2026.10.0 on this host, pushes it, imports agent-os/w4 on each
#    node, creates the termini-agent profile, renders /root/agent-user-data.yaml.
```

Re-running is a no-op when the alias fingerprint already matches (safe after a node
rebuild). Options: `--node-alias`, `--host-alias`, `--profile`, `--pool`,
`--project-ssh-key FILE`, `--tarball PATH`, `--no-ensure-lxd`, `--print-launch-cmd`.

## Launch

```bash
lxc exec <node> -- lxc launch agent-os/w4 agent-test -p termini-agent \
  -c cloud-init.user-data="$(cat /root/agent-user-data.yaml)"
```

The instance boots from the local image only; cloud-init writes the project public
key into `/root/.ssh/authorized_keys` and enables `sshd`, so
`ssh root@<instance-ip>` works with the matching project private key (password auth
stays off).

## Verify (the card's acceptance)

```bash
infra/agent-vm/verify-placement.sh --nodes "demo-node-a demo-node-b"
```

Per node it asserts: (1) the alias resolves locally at the expected fingerprint and
no remote pull is used; (2) it **drops the node's default route (egress blocked)**,
launches the image, and confirms the instance is `RUNNING` with
`network_downloads_at_boot=0`; (3) `ssh` into the instance succeeds via the injected
project key; then it restores the route and removes the test key. Emits JSON.
Recorded run: [`evidence/placement-verification.json`](evidence/placement-verification.json).

## Honest scope / coordination

- The lab's `demo-node-a/b/c` containers are **recreated** by the pre-warm task
  (`infra/warm/demo-env.sh restart`). Nested-LXD state on a node is lost when the node
  is recreated; **re-run `place-agent-image.sh` after any node rebuild**. In production
  this pre-distribution is part of the node image / bootstrap (D3: "preloaded workload
  images in the local LXD image store, imported from on-disk tarballs"), so it happens
  once at provisioning.
- **Hotspot:** `demo-node-a` / `demo-node-b` are shared demo fixtures also mutated by
  `t_9fa53ad8` (`infra/warm`). Placement here is additive (a pool/bridge/profile and
  one image) and idempotent, but do not concurrently reconfigure those nodes.
- The image is a container image (`lxc launch`), matching the parent card's acceptance.
  The same provisioning applies to a `--vm` launch; no KVM is available on this host.

## Layout

```
infra/agent-vm/
  README.md                        this file
  place-agent-image.sh             export -> push -> import + profile + user-data
  verify-placement.sh              acceptance: local alias, blocked-egress launch, ssh
  agent-user-data.cloud-init.yaml  launch-time cloud-init (project SSH key, sshd)
  termini-agent.profile.yaml       the termini-agent LXD profile (documentation)
  evidence/placement-verification.json
```
