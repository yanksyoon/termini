# Placing the agent VM image on Termini nodes (`agent-os/w5`)

- Owner: kanban task **`t_7c40f9a3`** (W4 — "Template Agent VM as LXD image and place
  across Nodes A/B"); **alias refreshed by `t_94303203`** (rebuild + republish as
  `agent-os/w5`, see [§ Republished as `agent-os/w5`](#republished-as-agent-osw5--t_94303203)).
- Input: the baked guest image alias `agent-vm/2026.10.0` from task `t_d1960fd6`
  (see `image/agent-vm/README.md`), rebuilt 2026-10-16 by `t_94303203`. **This
  directory does not build the image** — it distributes and launches it.
- Consumers: `t_fd8eee6c` (cross-node agent→llama.cpp path), `t_cca07879` (W4 root),
  `t_1a2c913c` (agent task surface), `t_44a2b87b` (client Agents panel), the
  demo/warm environment (`infra/warm`).
- Contract sources: `docs/contracts/agent-runtime.md` §4 (`vm.launch`, profile
  `termini-agent`, launch-time cloud-init), §4.4 (the rendered `/etc/termini/agent.yaml`),
  §5.6 (the in-VM task surface) `docs/contracts/infra-decisions.md` D2/D3
  (per-node guest bridge, offline-preloaded images), `docs/contracts/node-bootstrap.md`
  §3.1/§7 (preloaded workload images, project key → `/root/.ssh/authorized_keys`).

## Deliverables (the card's four)

| Deliverable | Value |
|---|---|
| **Alias name** | `agent-os/w5` |
| **Immutable fingerprint** | `eb7806889646` (same content as the 2026-10-16 bake of `agent-vm/2026.10.0`, tarball sha256 `eb7806889646eb7b59bcd590b4d1ceae5c3b2ce211490426054e981381af12b1`) |
| **Launch command** | `lxc launch agent-os/w5 agent-test -p termini-agent -c cloud-init.user-data="$(cat /root/agent-user-data.yaml)"` |
| **cloud-init / profile files** | [`agent-user-data.cloud-init.yaml`](agent-user-data.cloud-init.yaml) (injects the project SSH key, enables sshd), [`termini-agent.profile.yaml`](termini-agent.profile.yaml) (limits + `local` root disk + one `lxdbr0` NIC) |

`agent-os/w4` (fingerprint `c3d039642a61`) is **deliberately left in place** on the
nodes — it is the one-flag rollback (`place-agent-image.sh --node-alias agent-os/w4`).

## Placement note (one paragraph)

Agent VMs run on the **compute nodes — Node A and Node B**; Node C is the inference
node and does not host agent VMs (D5). Placement is a *pre-distribution*, not a
pull: `place-agent-image.sh` exports the baked image on the control host, copies the
tarball to each compute node and imports it into that node's **local LXD image store**
under `agent-os/w5`, so `lxc launch` resolves the alias on-box with zero registry/pull
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
# -> exports agent-vm/2026.10.0 on this host, pushes it, imports agent-os/w5 on each
#    node, creates the termini-agent profile, renders /root/agent-user-data.yaml.
```

Re-running is a no-op when the alias fingerprint already matches (safe after a node
rebuild). Options: `--node-alias`, `--host-alias`, `--profile`, `--pool`,
`--project-ssh-key FILE`, `--tarball PATH`, `--no-ensure-lxd`, `--print-launch-cmd`.

## Launch

```bash
lxc exec <node> -- lxc launch agent-os/w5 agent-test -p termini-agent \
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

## Republished as `agent-os/w5` (t_94303203)

`agent-os/w4` (uploaded 2026-10-12) predated two landed contract changes, so an
instance launched from it could not be given a task at all:

1. `agent-runtime.md` §5.6/§7.4 (PR #170) — the runtime exposes `POST /v1/tasks` on
   its loopback listener (`127.0.0.1:9101`) with a per-instance `tasks.max_concurrent`
   bound. w4 has no such surface (`GET /v1/tasks` → `404`).
2. PR #164 — the wired model alias is `gemma`; `deepseek-flash` / `deepseek-v4-pro`
   are retired and refused with `ModelNotAllowed` instead of being substituted.
   w4's baked `/etc/termini/agent.yaml` still names `deepseek-flash`.

The image was rebuilt from the same recipe (`image/agent-vm/build-agent-image.sh
--build`) and placed on A/B under the **new** alias `agent-os/w5`, with `w4` kept so
the change is reversible. The fleet tooling that names the per-node alias now
defaults to `agent-os/w5`: this directory's `place-agent-image.sh`,
`verify-placement.sh`, `verify-callpath.sh`, plus `infra/images/prewarm-images.sh`,
`infra/images/verify-image-residency.sh` and `infra/warm/warm_cluster.sh`. (The
controller's compiled `DEFAULT_LXD_WORKLOAD_IMAGE` stays `agent-vm/2026.10.0`, pinned
by `tests/test_workload_image_default.py`; a controller that launches from a node's
local store must carry `TERMINI_LXD_WORKLOAD_IMAGE=agent-os/w5`.)

Acceptance on a **fresh launch** (no files copied into the instance), run on both
nodes: [`evidence/w5-republish-evidence.json`](evidence/w5-republish-evidence.json)
(structured) with the raw captures under
[`evidence/w5-live/`](evidence/w5-live/) — including
[`before-w4.txt`](evidence/w5-live/before-w4.txt) (the same probes against a fresh
`w4` instance: `deepseek-flash`, no `tasks:` block, `/v1/tasks` → `404`), the
`lxc launch` output, the config dump, the in-instance loopback probe, the
outside-the-instance refusal, and a live `POST /v1/tasks` that fails with the
distinct `INFERENCE_UNREACHABLE` (there is no on-prem inference in the lab) rather
than a timeout that looks like success. Reproduce with
`tools/run-w5-republish.sh` (drives place → fresh launch on A/B →
`tools/accept-w5.sh`).

The published identity of `agent-vm/2026.10.0` in the repo is refreshed in the same
change (`image/agent-vm/evidence/image-manifest.json` →
`agent-vm-2026.10.0+g68763f60ef6d`, sha256 `eb7806…`), so the manifest cannot silently
describe a different image than the alias the nodes import.

## Cross-node agent → llama.cpp call path (`t_fd8eee6c`)

The card above places the image; this one **wires it to inference**. One command
launches a fresh Agent VM on Node A from the local alias, installs the current
tree's `termini-agent`, runs a multi-turn **ADK** session whose every turn is served
by llama.cpp on Node C over the frozen direct host route, and asserts the acceptance:

```bash
infra/agent-vm/verify-callpath.sh            # demo-node-a -> demo-node-c:8080
infra/agent-vm/verify-callpath.sh --node-a node-a --node-c node-c --turns 3
```

It asserts, from raw evidence, that **every turn streamed** (`>= 2` discrete SSE
deltas arriving at distinct times — not one buffered blob) and that **first-token +
per-turn + run latency were recorded**, then writes
`evidence/callpath-evidence.json` (`callpath-report.json`,
`callpath-env.json`, `callpath-probe.json`, `callpath-run.txt` alongside).
Exit 0 = PASS. Files: `tools/callpath-runner.sh` (in-VM half),
`tools/check_callpath.py` (host-side gate).

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
- **`t_94303203` scope:** the bake ran on this host's LXD and the placement + acceptance
  ran on the **lab footprint** (`demo-node-a` / `demo-node-b`) — no GCP compute was
  started or created. The built w5 content is origin/main plus the §5.6 change set of
  PR #170 (`t_1a2c913c`), which is what the card asked the image to ship; re-run
  `image/agent-vm/build-agent-image.sh --build` from `main` after #170 lands if that
  branch is modified before merging (the build records `gitsha` in
  `/etc/termini/agent-image`, so the provenance of a bake is readable in the guest).
- **Call-path acceptance ran on the lab footprint** (`demo-node-a` → `demo-node-c`,
  10.14.100.11), which is the frozen `http://<nodeC>:8080` role realised as nested
  LXD on this host — not the GCP 3-node underlay (unreachable from this host). On
  the frozen Node C config (`--reasoning-format none`, `t_95528ae8`) the R1-distill
  `` block is **inline in `delta.content`**; the lab `llama-cpp.service`
  (no such flag) splits it into `delta.reasoning_content`, so the lab report shows
  `reasoning_deltas` > 0 with `content_deltas` for the final answer. The client
  counts both as streamed tokens; the GCP run would show them all as `content`.
- **Hotspot:** the lab `llama-cpp.service` on `demo-node-c` (infra/warm) does not
  set `--reasoning-format none`, unlike the W3 endpoint contract; flagged for the
  verifier (t_9549b286).

## Layout

```
infra/agent-vm/
  README.md                        this file
  place-agent-image.sh             export -> push -> import + profile + user-data
  verify-placement.sh              acceptance: local alias, blocked-egress launch, ssh
  verify-callpath.sh               acceptance: cross-node multi-turn ADK streaming + latency
  agent-user-data.cloud-init.yaml  launch-time cloud-init (project SSH key, sshd)
  termini-agent.profile.yaml       the termini-agent LXD profile (documentation)
  tools/callpath-runner.sh         in-VM half of the call-path acceptance
  tools/check_callpath.py          host-side acceptance gate + evidence writer
  tools/run-w5-republish.sh        w5 rebuild acceptance driver (place + fresh launch)
  tools/accept-w5.sh               w5 in-instance acceptance (service, config, task surface)
  evidence/placement-verification.json
  evidence/callpath-evidence.json  cross-node call-path acceptance evidence
  evidence/w5-republish-evidence.json   w5 rebuild + republish evidence (t_94303203)
  evidence/w5-live/                raw captures behind the JSON above
```
