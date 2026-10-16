# Termini — Infrastructure Decisions (W1)

- Owner: `infra` (kanban task `t_134e2f24`)
- Status: **FROZEN** for the 5-day build. Changes require a comment on the task and an update here plus in `docs/architecture.md`.
- Date: 2026-10-04
- Audience: controller, agents, frontend, governance, verifier workstreams.
- Related: `docs/contracts/node-bootstrap.md` (this workstream), `docs/contracts/controller-api.md`, `docs/contracts/node-telemetry.md`, `docs/contracts/vm-allocation.schema.json` (controller), `docs/contracts/agent-runtime.md` (agents), `docs/contracts/governance.md` (governance), `docs/architecture.md` (architect).
- Source of truth for requirements: `~/termini/project.md`.

This document answers every open **infrastructure** question in `project.md` and freezes the choices the rest of the build depends on. Each decision states the choice, the rationale, the rejected alternatives, and the consequences other workstreams must honour.

---

## 0. Decision summary

| # | Question | Decision | Rejected alternatives |
|---|----------|----------|-----------------------|
| D1 | Cloud system: LXD vs MicroCloud vs OpenStack | **LXD snap, single 3-member cluster** (pinned channel) | MicroCloud, OpenStack |
| D2 | Overlay networking for a shared logical subnet | **Underlay-routed services; no LXD overlay for the demo.** Per-node NAT bridges; cross-node traffic uses the VPC/LAN underlay | OVN, Fan networking, hand-rolled VXLAN/Geneve |
| D3 | Pre-baked image strategy | **Immutable pre-baked host image + offline-preloaded workload images + activation-only cloud-init.** No package installs at boot | Live `cloud-init` package install, per-boot model download, single "mega-image" |
| D4 | SSH key custody & rotation | **Controller-held SSH CA** signing short-lived (≤8 h), principal-bound user certificates from browser-local public keys; access surfaced as a one-click web terminal + `ssh` string (`auth_model: ssh_ca`); node identity via mTLS from a node-local key + controller-signed cert | Shared project private key in the UI, static passwords, long-lived per-node keys, agent-managed keys |
| D5 | GPU access on Node C | **Host-exposed `llama.cpp` on Node C's underlay IP (OpenAI-compatible API)**; GPU passthrough is a day-2 path only | Full PCI/VFIO passthrough into a VM |
| D6 | Node ↔ controller control topology | **Outbound-only control channel from each node** (mTLS WebSocket); push telemetry; no inbound SSH required | Controller pulls/scrapes nodes, inbound SSH from internet |
| D7 | Cluster storage | **Local `zfs` pool per member** (`local`); no shared storage in the demo | MicroCeph/Ceph, NFS shared pool |
| D8 | Telemetry transport & cadence | **Push over the D6 channel**, demo cadence **1000 ms**, default 15000 ms, bounded local spool | Prometheus scrape, fixed slow polling |
| D9 | One inference address, two surfaces | **Split them: agents are served by LOCAL weights on Node C (`10.10.0.13:8080`, `backend=local`); the operator's diagnosis is served by Gemini from the CONTROLLER only (`127.0.0.1:8085`, `backend=gemini-proxy`)** — ⚠️ **SUPERSEDED by `t_957956e5` / `t_3fcee90b`: Node C's `:8080` ships `backend=gemini-proxy` (managed model) again; `local` is the one-variable fallback. Read the Supersession block in D9 before relying on this row.** | One endpoint with a backend flag, a forwarded managed model on the agent port, a key file on either host |

These choices share one bias: **maximise demo reliability and minimise the number of distributed systems that can fail live**, while keeping the architecture honest enough that the "your hardware, your data, our operations" story survives an adversarial read.

---

## D1 — Cloud system: LXD, not MicroCloud, not OpenStack

**Decision.** Use **LXD from the snap** as the single node-hypervisor and cluster manager, formed into **one 3-member cluster (`termini`)** spanning Nodes A, B and C. Pin the snap to the **`5.21/stable` LTS channel** and record the exact revision in the baked image; every member must run the **same snap revision**.

**Rationale.**
- `project.md` requires a real cluster, and the demo scenarios require cluster features: role-based placement, cross-member instance moves, and a single API across nodes. LXD gives one REST API and one CLI for containers **and** VMs.
- Time-to-first-cluster is minutes (`snap install lxd && lxd init --preseed`), versus hours/days for OpenStack. On a 5-day build this is the dominant factor.
- LXD is already named as the intended path in `project.md`; the only open sub-question was whether to add MicroCloud/OpenStack.
- LXD VMs (QEMU) give the isolation the spec asks for ("small, isolated execution environments"), while remaining light enough to run a fan-out of agent VMs on commodity compute nodes.

**Rejected alternatives.**
- **OpenStack** — requires Keystone/Nova/Neutron/Glance/placement daemons, message queues, and a real control plane; days to stand up and heavy per-node overhead. It solves problems (multi-tenant quotas, Cinder volumes, Horizon) that this demo does not have. Rejected on time and operational cost.
- **MicroCloud** — automates LXD + MicroCeph + MicroOVN, but that convenience is exactly the wrong trade here: it bolts **Ceph** and **OVN** onto every node, wants ≥3 systems for quorum, and its opinionated network/storage defaults fight the "keep it small and single-operator" constraint. It also raises the live-failure surface for a 3-node demo. **MicroCloud is the day-2/production packaging path**, not the demo path; revisit only if we later need HA shared storage and flat multi-tenant networking.

**Consequences.**
- `docs/contracts/node-bootstrap.md` specifies the exact `lxd init --preseed` seed/join sequence, token issuance, and join-failure fallback.
- The controller mints cluster join tokens at registration via the seed's LXD REST API (`10.10.0.11:8443`) — **cluster administration only**. VM placement via a typed `vm.launch` command to the node daemon (which calls its **local** LXD API, AR-2) is **day-2 — not wired in the demo** (`_apply_plan` returns `vm_ids: []`); the controller never drives LXD placement directly. See D2 for addressing and D7 for storage.

---

## D2 — Overlay networking: route over the underlay, do **not** run an LXD overlay in the demo

**Decision.**
1. Each node runs an LXD-managed bridge (`lxdbr0`) on a **distinct, non-overlapping** guest subnet with **NAT/masquerade** enabled. Guest VMs get addresses only on their own node's bridge.
2. **Cross-node communication uses the physical underlay network directly** (the demo VPC on GCP; the customer LAN on-prem). Nodes address each other by their real underlay IPs.
3. Every **cross-node service is exposed on the node's underlay IP** with a firewall allow-list — there is no logical switch stretched across nodes for the demo. In particular `llama.cpp` on Node C is reachable at `http://<node-c-underlay-ip>:8080`.

**Rationale.**
- The requirement is "Agent VMs on Node A can reach `llama.cpp` on Node C". Underlay routing satisfies this with **one static route that either works or doesn't**, instead of a tunnel stack (OVN/OVS/Geneve) that can silently drop packets on MTU mismatch — the exact failure mode called out in `project.md` (adversarial note 3).
- Guest VMs egress through their node's NAT, so **Node C sees the guest's traffic sourced from Node A's underlay IP**. A single underlay-CIDR firewall rule on Node C is enough to authorise all agent VMs on Node A/B.
- One less distributed system to debug live. The overlay is not where the demo value is.

**Addressing plan (frozen).**

| Scope | CIDR | Notes |
|-------|------|-------|
| Underlay (nodes + controller) | `10.10.0.0/24` | On GCP this maps onto the VPC subnet; on-prem it is the customer LAN. Static addresses. |
| Controller | `10.10.0.10` | HTTPS 443; LXD client cert used to reach nodes. |
| Node A (seed) | `10.10.0.11` | LXD cluster seed; LXD API :8443. |
| Node B | `10.10.0.12` | LXD cluster member. |
| Node C (GPU) | `10.10.0.13` | LXD cluster member; `llama.cpp` :8080. |
| Guest bridge on A | `10.20.1.0/24` (gw `10.20.1.1`) | DHCP pool `10.20.1.100–200`. |
| Guest bridge on B | `10.20.2.0/24` (gw `10.20.2.1`) | DHCP pool `10.20.2.100–200`. |
| Guest bridge on C | `10.20.3.0/24` (gw `10.20.3.1`) | DHCP pool `10.20.3.100–200`. |

**Port map (frozen).**

| Port | Proto | Where | Who may reach it |
|------|-------|-------|------------------|
| 443 | TCP | Controller public IP | Frontend/browser (public); nodes (outbound target) |
| 8443 | TCP | Every node (`core.https_address`) | LXD cluster peers `10.10.0.0/24`; controller `10.10.0.10` for join-token minting only (cluster admin, not placement) |
| 8080 | TCP | Node C | Underlay `10.10.0.0/24` (i.e. nodes A/B and their guest NAT traffic). Not public. |
| 22 | TCP | Every node | Local admin CIDR only; **CA-signed short-lived operator cert, no passwords** (D4). No inbound SSH is required for platform operation (D6). |

**Cross-node reachability worked example.**
`agent VM (10.20.1.x on Node A) → 10.20.1.1 (bridge gw) → SNAT → 10.10.0.11 → underlay → 10.10.0.13:8080 (llama.cpp on Node C)`.
Node C firewall: `allow tcp/8080 from 10.10.0.0/24`. No MTU-sensitive tunnel is involved; the guest MTU is clamped to the underlay MTU in the bridge config (`mtu 1460` on GCP, `1500` on-prem) to avoid PMTU black holes.

**Rejected alternatives.**
- **OVN (LXD `ovn` network type).** Gives a flat L2/L3 logical network across members, native ACLs, and floating IPs — genuinely the "right" answer for a multi-tenant product. Rejected for the demo because it needs `ovn-central`/`ovn-controller` (or MicroOVN) on every node, `ovn-encap-ip` wiring, and Geneve tunnels whose MTU behaviour must be tuned; that is 1–2 days of integration and a live failure surface. **Documented as the day-2 path** once the underlay is trusted and multi-tenancy matters.
- **LXD Fan networking.** Advertised as zero-config, but fan/`fan` overlay support is legacy in modern LXD snaps, and it depends on encapsulation with the same MTU pitfalls plus a fragile `/8` remap. Rejected as deprecated and fragile.
- **Hand-rolled VXLAN/Geneve or a WireGuard mesh.** Same class of complexity as OVN with none of the management/ACL features. Rejected.
- **Single flat guest bridge across the underlay.** Would require an L2 bridge extension over the WAN/VPC and would expose guest L2 to the underlay. Rejected on security and feasibility.

**Consequences.**
- Agents must reach inference via the **underlay address**, not a guest-network address: base URL `http://10.10.0.13:8080`. This is the value the controller hands to agent VMs as `INFERENCE_BASE_URL`.
- Agent VMs on the same node as Node C are not a case we rely on; Node C's own guest bridge exists only for parity and local tooling.

---

## D3 — Pre-baked image strategy: immutable images + offline-preloaded workloads + activation-only cloud-init

**Decision.** Three image artifacts, all built in CI and versioned immutably. **At demo time nothing is installed, downloaded, or upgraded during boot.**

1. **Host image — "Termini Node OS".** Ubuntu Server **24.04 LTS** minimal, plus, already on disk:
   - LXD **snap** pre-installed and pinned (`5.21/stable`, revision recorded);
   - the **`termini-bootstrap` daemon** installed as a `systemd` service (enabled, not started until config is present);
   - `chrony` (time sync), `nftables` (default-deny firewall), `openssh-server` (key-based access, D4), and the NVIDIA **driver stack on the Node C image variant**.
   - **The workload images are preloaded** into the local LXD image store from tarballs (see 3).
2. **Guest image — "agent-vm"** (LXD VM, `agent-vm/<version>`): Python 3.12, Node.js, agent runtime deps, and a pre-warmed event/log pipeline. No model weights.
3. **Guest image — "inference"** (LXD VM/container variant `inference/<version>`): the pinned `llama.cpp` runtime and any weights, present for completeness; **the demo does not launch it** (D5).

Images are tagged `termini-node-<YYYY.MM.DD>-<gitsha>` and `agent-vm-<version>`; the host records its image tag in registration telemetry (`machine.image_version`).

**Rationale.**
- `project.md` adversarial note 2 is dispositive: live `cloud-init` provisioning takes 3–7 min and can fail on network; a live 4 GB model download can crash the demo. Pre-baking turns "cold boot" into "activate a pre-installed daemon and register", which the bootstrap contract targets at **< 5 s to registration**.
- Immutable, content-tagged images make the build reproducible and give the controller something meaningful to report (everything running is version-pinned).
- Preloading workload images **on every host** removes the container/VM image registry from the critical path at demo time; `lxc launch` resolves locally with zero network.

**Rejected alternatives.**
- **Live `cloud-init` package install / `apt upgrade`.** Slow, network-dependent, non-deterministic. Rejected outright.
- **Pull guest images from a registry at deploy time.** Adds a network dependency and a latency spike at the exact moment we want instant response. Rejected; images are preloaded instead. (A registry remains the day-2 distribution mechanism for fleet updates.)
- **One giant image containing host + all guests + all models.** Wastes disk on compute-only nodes and couples release cadences; also makes the "small Node A/B vs GPU Node C" distinction invisible. Rejected in favour of base + role-specific variants.
- **Docker-only (no LXD).** Rejected: loses VM-grade isolation, GPU/device semantics, and cluster migration that the spec depends on.

**Consequences.**
- `node-bootstrap.md` §3 lists the exact image contents and cloud-init fields; cloud-init only writes config and starts a service.
- Build-time CI produces: GCP machine images (for the demo nodes) and `qcow2` (for on-prem packaging). The build pipeline is out of scope for W1 docs but the **contract** is the immutable tag + on-disk preload requirement.

---

## D4 — SSH key custody & rotation: controller-held SSH CA + short-lived certs

**Decision.**
- The **controller holds an Ed25519 SSH certificate authority** (CA), not a long-lived
  user key. The operator (or dashboard) generates a keypair **locally in the
  browser/on their own machine**; only the **public** half is uploaded, and the
  controller signs it into a **short-lived (≤8 h default, ≤24 h max),
  principal-bound OpenSSH certificate** via `ssh-keygen -s`
  (`POST /v1/projects/{project_id}/ssh-key`, `controller-api.md` §4.3). The CA private
  key never leaves the controller; the user's private key never transits it.
- **Revocation is first-class:** `POST /v1/projects/{project_id}/ssh-key/revoke`
  revokes a certificate before its TTL; every issuance and revocation is recorded in
  the `ssh_certificates` audit ledger (migration `0006_ssh_ca.sql`).
- User access is surfaced in the dashboard as a **one-click web terminal** plus a
  copy-paste **`ssh` string**
  (`GET /v1/projects/{project_id}/nodes/{node_id}/ssh`, `dashboard.md` §3.2) that
  returns `auth_model: "ssh_ca"` and references the operator's browser-local key. No
  passwords and no project private key displayed in the UI.
- **No inbound SSH from the internet is required.** The web terminal is
  controller-brokered over the node's outbound mTLS control channel (D6); the demo
  terminal bridge authenticates with a **short-lived CA-signed operator cert**, not a
  long-lived break-glass key.
- **Node identity** is separate from user access: at first boot the bootstrap daemon
  generates a **node-local Ed25519 keypair**, sends a **CSR** in registration, and
  receives a controller-signed **client certificate**. This cert drives the outbound
  mTLS control channel (D6); the controller's LXD API access is authorised separately
  by an LXD trust client cert that the controller holds.
- **Host keys are pinned on first registration** (TOFU on the node→controller
  direction): the daemon reports its host public key; the controller records it and
  verifies it on every later SSH. No `StrictHostKeyChecking=no` in the fleet.
- **The CA is optional on the controller at boot:** with no CA key configured,
  `POST /v1/projects/{project_id}/ssh-key` returns `503` and the SSH terminal backend
  errors cleanly; the controller still runs and the node still registers without it.

**Rationale.**
- `project.md` proposed generating "an SSH keypair for the project" and displaying the
  private key on the frontend. That is a long-lived, fleet-wide, non-revocable secret.
  The shipped model avoids it entirely: the controller holds only the CA, user private
  keys stay on the operator's machine, and access is short-lived, principal-bound, and
  revocable.
- The CA model is cheap to run (`ssh-keygen -s` signing, one `TrustedUserCAKeys` line
  on nodes) and is what the demo actually ships: real, honest, scoped, time-bounded,
  audited access.

**Rejected alternatives.**
- **Shared project private key shown in the UI.** Rejected (see above).
- **Static root password or a shared password.** Rejected — no rotation, no attribution, no revocation.
- **Long-lived per-node keypairs distributed by the controller.** Rejected — the controller would hold N long-lived private keys; blast radius and rotation cost scale with the fleet.

**Consequences.**
- `node-bootstrap.md` §7 injects the CA trust anchor (`ca_pubkey` →
  `TrustedUserCAKeys /etc/ssh/termini_ca.pub`) and pins host keys; issued certs are
  accepted only against that trust anchor.
- The controller workstream owns the CA, the `ssh-key` sign/revoke endpoints, the
  `ssh`-string endpoint, and the terminal bridge; the audit trail of every issued and
  revoked cert is the `ssh_certificates` ledger (migration `0006_ssh_ca.sql`).

---

## D5 — GPU access on Node C: host-exposed `llama.cpp`, no passthrough in the demo

**Decision.** Run `llama.cpp` **directly on the Node C host** as a `systemd` unit, bound to the underlay IP, exposing the OpenAI-compatible API:
`http://10.10.0.13:8080` — endpoints `/v1/models`, `/v1/chat/completions` (streaming supported), `/health`.
The GPU is consumed by the host process; the model IDs and request/response shape are owned by `docs/contracts/agent-runtime.md`. **GPU passthrough (VFIO) is documented as the day-2 path, not built.**

**Realization note (reconciled with `agent-runtime.md` §8).** The `:8080` listener is
implemented as an **OpenAI-compatible router** (`termini-infer-router`) fronting one
or more loopback-bound `llama.cpp` workers (one per model alias). A single-model
deployment collapses the router to a transparent passthrough. The endpoint, port,
and firewall scope are unchanged.
**Superseded in part by D9:** `:8080` is now the **agent-facing** surface and is
served from the node's own weights (`backend=local`); the forwarded managed model
moved to the controller's loopback provider listener. See D9.
**D9 is itself superseded for this surface (kanban `t_957956e5` / `t_3fcee90b`):**
`:8080` still fronts the loopback worker, but a FRESH Node C now ships
`backend=gemini-proxy` — the managed model — and `local` is the one-variable
fallback. The address, the port and the firewall scope are unchanged.

**Rationale.**
- `project.md` adversarial note 3 explicitly recommends "expose `llama.cpp` directly on Node C's host IP ... without relying on complex OVN/Fan virtual bridges during a live presentation." Host exposure removes both the overlay AND the passthrough variables at once.
- VFIO passthrough of a single GPU into an LXD VM requires IOMMU/VT-d, binding the device to `vfio-pci` at boot, the host GPU driver not claiming it, and LXD `gpu`/`raw.qemu` configuration. On a **GCP T4/L4 instance** this is at best fragile and at worst not permitted (the GPU is attached in a way that does not present a clean, bound-able PCI function for guest assignment). It is not a 5-day-build item.
- Running on the host also removes a virtualisation hop from the streaming path, which matters for the latency-sensitive demo (multi-turn agent ↔ LLM calls).
- If the "inference workload must be a managed LXD workload" story needs to be shown, an **LXD container with a GPU device** (`nvidia.runtime=true`, shared `dev/nvidia*`) is an acceptable middle ground and far simpler than passthrough — but it is optional, and the default is the host service.

**Rejected alternatives.**
- **Full PCI passthrough into an inference VM.** Rejected for the demo (complexity, GCP feasibility, time). Preserved as a day-2 option on bare metal.
- **No GPU / CPU-only inference.** Rejected — Node C is specified as the GPU node and GPU acceleration is part of the value proposition.
- **Expose `llama.cpp` on a **guest** address and rely on overlay routing.** Rejected — that is exactly the D2 overlay dependency we removed.

**Consequences.**
- The controller models this as a workload of type `inference` on Node C, but the *implementation* is a host service; `docs/contracts/agent-runtime.md` must describe the endpoint as a host-underlay URL, not a guest URL.
- Node C firewall allows `tcp/8080` from `10.10.0.0/24` only (see D2).
- GPU metrics (`nvidia-smi`) feed telemetry; a GPU/driver failure marks Node C `gpu_unavailable` (see `node-bootstrap.md` §9) rather than failing registration.

---

## D6 — Node ↔ controller topology: outbound-only control channel

**Decision.** Every node maintains a **single outbound mTLS connection** to the controller (WebSocket over 443, reverse request/response). Registration, heartbeat/telemetry, command delivery, and log streaming all ride this channel. **No inbound port to a node is required** for the platform to operate.

**Rationale.**
- Customer-owned nodes on-prem are frequently behind NAT/firewalls; an outbound-only model is the only one that works everywhere without inbound firewall changes.
- It gives the governance story a single, auditable egress point on the node (the bootstrap daemon), which is what `docs/contracts/governance.md` needs to enforce "telemetry only".
- It removes the need for the controller to hold inbound SSH to nodes (D4).

**Rejected alternatives.**
- **Controller scrapes/polls nodes (Prometheus-style).** Requires inbound reachability; breaks behind NAT; rejected.
- **Inbound SSH from the controller to nodes.** Conflicts with D4's removal of inbound SSH; rejected as the primary path (retained only as an optional LAN operator path).

**Consequences.**
- Controller endpoint path is defined in `docs/contracts/controller-api.md`; the node-side view is in `node-bootstrap.md` §5/§8. Both must agree on the registration/heartbeat wire format (see the reconciliation note in `node-bootstrap.md`).

---

## D7 — Cluster storage: local ZFS per member

**Decision.** The LXD cluster's storage pool `local` uses the **`zfs` driver, per member** (a dedicated disk or a loop-backed file per GCP VM). No shared storage pool in the demo.

**Rationale.**
- ZFS gives cheap snapshots (handy for the "resize / roll back a bad remediation" stories) and is first-class in LXD.
- A shared pool (Ceph/MicroCeph) is the thing that would let us do **live** migration, but it is a heavy, multi-node distributed system that is out of scope for 5 days and adds its own failure modes.

**Consequence / honest limitation.** With per-member local storage, in-cluster movement
of a running VM is **stop → transfer → start (evacuation)** — an LXD primitive available
but not driven by the demo's AI operator. (Upstream LXD does support *stateful* live
migration on a local pool — `migration.stateful=true` + `size.state ≥ limits.memory` —
but it copies the root disk and is not on the demo critical path.) The demo's AI operator
remediates via `instance.restart` (OOM) and `instance.stop` (disk failure/degradation) —
see `node-telemetry.md` §5 and `controller/playbooks/*.yaml`. Hotplug CPU/RAM resize and
evacuation are LXD primitives noted here as day-2 options, not demo actions. Day-2 adds
MicroCeph for cheap true live migration.

**Rejected alternatives.** MicroCeph/Ceph (heavy, multi-node quorum), a shared NFS pool (single point of failure, not cluster-native), `btrfs` (weaker tooling for loop devices), `dir` (no snapshots).

---

## D8 — Telemetry transport & cadence

**Decision.** Telemetry is **pushed by the node** over the D6 channel: cadence **1000 ms during the demo**, default **15000 ms**, configurable per node via the registration response. The daemon keeps a **bounded local spool** so short controller outages don't lose the decisive sample.

**Rationale.**
- Push matches D6 (no inbound scraping) and gives the AI operator a fast enough signal to act before the kernel OOM-killer wins — `project.md` adversarial note 4.
- A bounded spool lets the controller reconcile a node that was briefly partitioned.

**Rejected alternatives.** Pull/scrape (D6), fixed slow cadence (note 4), unbounded buffering (memory risk on constrained nodes).

---

## D9 — One inference address, two surfaces: LOCAL weights for agents, Gemini for the diagnosis

- Owner: kanban **`t_aa7a70f9`** (infra), encoding the user's decisions of 2026-10-15.
- Amends: D5 (which describes one inference address) and `docs/contracts/agent-runtime.md` AR-4/AR-6.

> ### ⚠️ SUPERSEDED — `t_957956e5`, REQUIRED WORK 3 (kanban `t_3fcee90b`), 2026-10-16
>
> **The text below is kept as history. It is no longer the shipped configuration.**
>
> Node C's `:8080` (the agent surface) **ships `backend=gemini-proxy` again**: a
> request to the frozen agent address is answered by the **managed model** (Google
> AI — Gemini, or Gemma on Google Cloud — through the proxy's `generateContent`
> translation) and the answer is labelled in-band. The reason is measured, not
> stylistic: the live-GPU path is blocked *independently* of this decision (the L4
> quota is 0 in `asia-south1` and the Cloud Quotas API refuses the increase —
> `t_21ac1bd8`, `t_294d2159`), and the local path this section chose is slow **on
> this footprint** — the operator's 20-sample prompt measured **402 s** on
> `n2-standard-8` (see the cost note below). The proxy is the path that works today.
>
> **What the split got right is kept.** There are still two listeners, separately
> labelled, and neither may stand in for the other:
> - the **agent address is unchanged** — `http://10.10.0.13:8080`, the value the
>   controller distributes to agents (invariant 1);
> - the **provider-only diagnosis listener** on the controller
>   (`http://127.0.0.1:8085`, `gemini-proxy` → `aiplatform.googleapis.com`) is
>   still loopback-only, still not an agent destination, still never advertised to
>   an agent (`infra/inference/setup-inference-surfaces.sh`);
> - the pre-split script still refuses to run without `--allow-unsplit`.
>
> **What changed is which backend answers the agent surface, and that it says so.**
> `/health` names it in-band (`backend`, `answered_by`, `served_model`, `egress`,
> `destination`, `note`), every response carries the `X-Termini-*` labels, and
> nothing in the dashboard, the audit row or the deck may claim a local GPU or
> local weights for a forwarded call (`verify-proxy.sh` asserts the word `GPU` is
> absent from the whole response).
>
> **Consequences worth stating plainly:**
> - **Agent traffic now leaves the premises by default.** That is the honest
>   reading of the pivot, and it is why `docs/contracts/governance.md` §7.1 was
>   updated in the same change. The on-prem guarantee is now **opt-in**, not the
>   default: `inference_backend = "local"` (Terraform) / `INFERENCE_BACKEND=local`
>   (provision.sh, raise-standing-fleet.sh) at render time, or
>   `termini-inference-switch local` on a running node — one variable, one command,
>   no rebuild. This is the D9 configuration preserved as a switch.
> - The D9-era `--allow-unsplit` gate on `infra/cloudrun/setup-operator-node-c.sh`
>   no longer protects the same thing (that script points the diagnosis at Node C,
>   which now forwards); see that script's own header.
> - The first-boot pins flipped with this change: `infra/gcp/variables.tf`
>   (`inference_backend`, now `gemini-proxy`), `infra/gcp/userdata/variables.tf`,
>   `infra/gcp/provision.sh`, `infra/gcp/tools/raise-standing-fleet.sh`,
>   `infra/gcp/cloud-init/node-c-serve-block.sh`, `infra/inference/install-inference-proxy.sh`,
>   and the expectations that pinned `local` (`infra/gcp/userdata/verify/main.tf`,
>   `infra/gcp/verify-cloud-init.sh` §8, `tests/test_node_c_first_boot.py`,
>   `tests/test_inference_surfaces.py`) — inverted, not deleted.

**Decision.** The footprint runs **two inference listeners with different
guarantees**, and neither may stand in for the other:

| | Agent-facing surface | Provider-only surface |
|---|---|---|
| Address | `http://10.10.0.13:8080` (Node C, underlay) | `http://127.0.0.1:8085` (the CONTROLLER, loopback) |
| Backend | `local` — llama.cpp worker on `127.0.0.1:8081` | `gemini-proxy` → `aiplatform.googleapis.com` |
| Who calls it | agent VMs, the controller's intent extraction (`:8090`), any OpenAI client on the underlay | the operator's DIAGNOSIS call, and nothing else |
| Answers bytes from | **this node's own weights** (alias `gemma`) | `gemini-2.5-flash` (forwarded managed model) |
| Egress class | `in-vpc` — nothing leaves the premises | `external` — the request leaves for Google |
| Identity | Node C's least-privilege SA (`t_5a4a0f73`), `cloud-platform` scope | the controller's dedicated provider SA (`roles/aiplatform.user`), `cloud-platform` scope |
| Reachability | firewall: `tcp/8080` from `10.10.0.0/24` only | loopback only; the fleet has no route to it |
| Credential | metadata identity, **no key file** | metadata identity, **no key file** |

The agent port **offers no forwarded backend**: a request to `10.10.0.13:8080`
can only be answered by that node's weights. The provider listener **offers no
local backend** (there is no worker on the controller) and `/health` reports that
by measuring it, not by asserting it.

**Rationale.**
- The client's agents must run on **local weights**: that is the whole on-prem
  story, and agent traffic is the traffic the client cares about. A forwarded
  managed model on that port would make the claim false.
- The operator's **diagnosis** is a provider-side function: it runs over client
  C0 telemetry (metrics + instance inventory, no workload payloads), and the user
  decided on 2026-10-15 (Q1/Q1b) that this prompt **may leave the premises** as
  part of the managed service and may involve Google's Gemini.
- The two wants are about *different* traffic, so one address cannot satisfy both
  without quietly downgrading one of them. Splitting is what makes each claim
  separately true — and separately checkable.
- Keeping the forwarded path on the **controller** rather than on a node also
  puts the egress where the telemetry already lands: the box that decides is the
  box that may leave.
- Independence is a property, not a hope: separate listener, separate env file,
  separate unit, separate address. Flipping one leaves the other's `/health`
  byte-identical.
- **No key file anywhere** (same rule as `t_5a4a0f73`): each host mints its token
  per request from its own metadata server. A key file would be a long-lived,
  copyable, unrevocable credential on a box we already trust less than the
  metadata service.

**Cost / performance note (stated, not hidden).** The local agent surface is
**CPU-only** in this footprint (free-tier billing rejects the L4), and the local
model is slow: the operator's 20-sample decision prompt measured **402 s** on
`n2-standard-8` (prefill ≈33 tok/s, generation ≈8–10 tok/s; see
`infra/cloudrun/setup-operator-node-c.sh` and
`infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md`), against
**≈1.8 s** for the same prompt served by the provider surface. **Agent tasks
served locally are therefore not interactive until the GPU quota lands** — short
turns are seconds-to-minutes, not milliseconds. The diagnosis is fast *because*
it is the provider surface; the two facts belong in the same paragraph.

**Rejected alternatives.**
- **One endpoint with a backend flag** (the pre-split shape): the flag is set
  once, per node, by whoever provisioned it, and it silently decides whether
  agent traffic is answered locally. The demo would ship whichever value happened
  to be set — the failure mode this decision exists to prevent.
- **The forwarded managed model on Node C's `:8080`**: makes the agent-facing
  endpoint forward while still *presenting* as a local inference server, i.e. the
  one arrangement in which a client cannot tell where its tokens came from.
- **The diagnosis on Node C**: correct on-prem behaviour, but it is what makes
  the demo's model decision take minutes instead of seconds (above), and it
  contradicts the user's Q1 (provider diagnosis may use Gemini).
- **A service-account key file** on the controller or a node: rejected in
  `t_5a4a0f73` and again here — the metadata identity is the only credential.
- **A second address for the provider surface on a node**: needs a firewall
  change and puts external-egress capability back on the client's fleet.

**Consequences.**
- Node C's first-boot default is `TERMINI_INFERENCE_BACKEND=local`
  (`infra/gcp/variables.tf` `inference_backend`, `infra/gcp/provision.sh`,
  `infra/gcp/cloud-init/node-c-serve-block.sh`,
  `infra/inference/install-inference-proxy.sh`). One command on a running node
  still moves it (`termini-inference-switch`), but a rebuilt node serves local.
- The controller runs the provider listener under systemd from
  `infra/inference/termini-inference-provider.service`, installed by
  `infra/inference/setup-inference-surfaces.sh`.
- `controller-api.md` §5.2 step 5: the controller's `TERMINI_INFERENCE_BASE_URL`
  is now loopback, so it **cannot** identify the model host; the model host must
  be named explicitly in `TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS` or Node C
  becomes eligible for auto agent placement.
- `agent-runtime.md` AR-4/AR-6: the agent's endpoint is the local surface; an
  agent never speaks to the provider surface and is never told about it.
- The pre-split script `infra/cloudrun/setup-operator-node-c.sh` refuses to run
  without `--allow-unsplit`, because running it would move the diagnosis back onto
  the agent surface.
- The `externalModelCall()` audit row keeps saying **which** surface answered:
  `egress=external destination=aiplatform.googleapis.com` for a diagnosis
  (`external_call.py` + `egress.py` read the listener's own `/health`), so a
  forwarded answer can never be read as a local one.

---

## Crosswalk: every open infra question → answer

| `project.md` open question | Answer |
|----------------------------|--------|
| LXD, OpenStack, or MicroCloud? | **LXD** (D1). MicroCloud and OpenStack rejected with rationale. |
| How do nodes share a network under that cloud technology? | **Underlay routing + per-node NAT bridges** (D2); no OVN/fan/VXLAN in the demo. |
| What should be pre-installed in the OS? How is the OS served? | **Immutable pre-baked host image + offline-preloaded workload images + activation-only cloud-init** (D3). |
| SSH key custody / how customers get access | **Controller-held SSH CA + web terminal + `ssh` string** (D4): browser-local keys signed into short-lived (≤8 h) principal-bound certs. |
| GPU passthrough vs host-exposed `llama.cpp` on Node C | **Host-exposed `llama.cpp`** on `10.10.0.13:8080`; passthrough deferred to day-2 (D5). |
| How does the controller reach nodes? | **Outbound-only mTLS control channel** (D6), push telemetry (D8). |
| Storage for the cluster / migration semantics | **Local ZFS per member** (D7); remediation via `instance.restart`/`instance.stop`, live migration deferred. |

## Cross-component invariants (other workstreams rely on these)

1. **Addresses are static and frozen** per the D2 table. `INFERENCE_BASE_URL=http://10.10.0.13:8080` is the value the controller distributes to agents — it is the **agent-facing** surface, and since the pivot (`t_957956e5` / `t_3fcee90b`) it **ships `backend=gemini-proxy`**: the managed model answers through the front door and the answer is labelled in-band (`backend`, `answered_by`, `served_model`), so an agent can always tell where its tokens came from and nothing may read a forwarded answer as a local one. The **on-prem** configuration (`backend=local`, that node's own weights) is one variable / one command away and is **not** the default (D9, superseded). The **provider-only** diagnosis surface is loopback on the controller (`http://127.0.0.1:8085`) and is never handed to an agent.
2. **Node identity is the machine ID** (DMI UUID + `/etc/machine-id`); re-registration is an upsert (see `node-bootstrap.md` §10).
3. **Only hardware/OS telemetry leaves a node**, always through the bootstrap daemon (governance boundary; enforced on the node side).
4. **No private keys transit the controller** (user keys stay in the browser; node keys stay on the node; only CSRs and public keys move).
5. **LXD snap revision is identical across all members**; a mismatched member will fail its join and degrade (see `node-bootstrap.md` §9).
6. **Every cross-node service is reached by underlay IP:port**, never a guest/overlay address.

## Risks & day-2

| Risk | Mitigation in the demo | Day-2 path |
|------|------------------------|------------|
| Cluster join fails live | Pre-baked identical image + static IPs; fallback = standalone LXD nodes, degraded banner (`node-bootstrap.md` §9) | MicroCloud / hardened cluster |
| No live migration with local ZFS | Use resize + evacuation | MicroCeph shared pool |
| Underlay firewall misconfig | Explicit allow-list table (D2); health check from a guest | OVN + ACLs |
| GCP GPU driver/passthrough limits | Host process, no passthrough | Bare-metal VFIO passthrough |
