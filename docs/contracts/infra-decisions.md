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
| D4 | SSH key custody & rotation | **Controller-held SSH CA + short-lived, principal- and source-bound user certificates**; node identity via mTLS from a node-local key + controller-signed cert | Shared project private key in the UI, static passwords, long-lived per-node keys, agent-managed keys |
| D5 | GPU access on Node C | **Host-exposed `llama.cpp` on Node C's underlay IP (OpenAI-compatible API)**; GPU passthrough is a day-2 path only | Full PCI/VFIO passthrough into a VM |
| D6 | Node ↔ controller control topology | **Outbound-only control channel from each node** (mTLS WebSocket); push telemetry; no inbound SSH required | Controller pulls/scrapes nodes, inbound SSH from internet |
| D7 | Cluster storage | **Local `zfs` pool per member** (`local`); no shared storage in the demo | MicroCeph/Ceph, NFS shared pool |
| D8 | Telemetry transport & cadence | **Push over the D6 channel**, demo cadence **1000 ms**, default 15000 ms, bounded local spool | Prometheus scrape, fixed slow polling |

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
- The controller owns the cluster: it addresses the seed's LXD REST API at `10.10.0.11:8443` and distributes join tokens. See D2 for addressing and D7 for storage.

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
| 8443 | TCP | Every node (`core.https_address`) | LXD cluster peers `10.10.0.0/24`; controller `10.10.0.10` |
| 8080 | TCP | Node C | Underlay `10.10.0.0/24` (i.e. nodes A/B and their guest NAT traffic). Not public. |
| 22 | TCP | Every node | Local admin CIDR only; **CA-signed certs only, no passwords** (D4). No inbound SSH is required for platform operation (D6). |

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
   - `chrony` (time sync), `nftables` (default-deny firewall), `openssh-server` (CA-only, D4), and the NVIDIA **driver stack on the Node C image variant**.
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

## D4 — SSH key custody & rotation: controller CA + short-lived certificates + mTLS node identity

**Decision.**
- The **controller holds an SSH certificate authority** (Ed25519). The **private CA key never leaves the controller's secret store** (GCP Secret Manager / KMS-backed). Nodes trust only the **CA public key**, injected at provisioning.
- **Users never receive a private key from us.** The browser generates a keypair locally (or the user uploads a public key); the **private key never transits the controller**. The controller signs the user's public key into a **short-lived certificate**:
  `ssh-keygen -s ca -I <user> -n <principal> -V +8h -O source-address=<cidr> <user.pub>`
  bound to a principal (`root` for the demo), a TTL (default **8 h**), and optionally a source address.
- **Node identity** is separate from user access: at first boot the bootstrap daemon generates a **node-local Ed25519 keypair**, sends a **CSR** in registration, and receives a controller-signed **client certificate**. This cert drives the outbound mTLS control channel (D6) and the controller's LXD API access is authorised separately by an LXD trust client cert that the controller holds.
- **Host keys are pinned on first registration** (TOFU on the node→controller direction): the daemon reports its host public key; the controller records it and verifies it on every later SSH. No `StrictHostKeyChecking=no` in the fleet.
- **Rotation.** The CA is rotatable without re-provisioning: nodes trust a **CA set** (`TrustedUserCAKeys` file with >1 key during a rollover window). Enrollment tokens are single-use and short-TTL. User certs are ≤8 h by construction. Node client certs have a bounded lifetime and are re-issued over the authenticated channel.
- **No inbound SSH from the internet is required.** Interactive access (xterm.js) is brokered by the controller: it either opens the session over the node's outbound control channel (D6) or issues a short-lived operator certificate for LAN access. This is the governance-friendly default and it survives NAT.

**Rationale.**
- `project.md` proposed generating "an SSH keypair for the project" and displaying the private key on the frontend. That is a long-lived, fleet-wide, non-revocable secret: one leak compromises every node and there is no rotation story. It fails the governance bar the same document sets.
- A CA model is the standard way to get **revocable, scoped, time-bounded** access, and it is cheap: `ssh-keygen` does the signing; nodes need one line of config.
- Keeping the user's private key in the browser means the controller is never a custody point for user secrets — a much cleaner answer to "your data, our operations".

**Rejected alternatives.**
- **Shared project private key shown in the UI.** Rejected (see above).
- **Static root password or a shared password.** Rejected — no rotation, no attribution, no revocation.
- **Long-lived per-node keypairs distributed by the controller.** Rejected — the controller would hold N long-lived private keys; blast radius and rotation cost scale with the fleet.
- **Agent-managed SSH with no CA.** Rejected — unscoped, unauditable, unrevocable.

**Consequences.**
- `node-bootstrap.md` §7 defines the injected items (`termini.ca_pubkey`, break-glass key lifecycle) and host-key pinning.
- The controller workstream owns CA storage, signing endpoints, and cert TTL policy; governance owns the audit trail of every issued cert.

---

## D5 — GPU access on Node C: host-exposed `llama.cpp`, no passthrough in the demo

**Decision.** Run `llama.cpp` **directly on the Node C host** as a `systemd` unit, bound to the underlay IP, exposing the OpenAI-compatible API:
`http://10.10.0.13:8080` — endpoints `/v1/models`, `/v1/chat/completions` (streaming supported), `/health`.
The GPU is consumed by the host process; the model IDs and request/response shape are owned by `docs/contracts/agent-runtime.md`. **GPU passthrough (VFIO) is documented as the day-2 path, not built.**

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

**Consequence / honest limitation.** With per-member local storage, **live migration of a running VM** is not available; in-cluster movement is **stop → transfer → start (evacuation)**, and the fleet-remediation primitives that work identically on one node are **hotplug CPU/RAM resize** (Scenario A's first option) and **evacuation** (Scenario B). `project.md` Scenario A offers resize *or* live-migrate; the demo uses **resize** as the primary remediation and **evacuation** for the disk-failure scenario. Day-2 adds MicroCeph for true live migration.

**Rejected alternatives.** MicroCeph/Ceph (heavy, multi-node quorum), a shared NFS pool (single point of failure, not cluster-native), `btrfs` (weaker tooling for loop devices), `dir` (no snapshots).

---

## D8 — Telemetry transport & cadence

**Decision.** Telemetry is **pushed by the node** over the D6 channel: cadence **1000 ms during the demo**, default **15000 ms**, configurable per node via the registration response. The daemon keeps a **bounded local spool** so short controller outages don't lose the decisive sample.

**Rationale.**
- Push matches D6 (no inbound scraping) and gives the AI operator a fast enough signal to act before the kernel OOM-killer wins — `project.md` adversarial note 4.
- A bounded spool lets the controller reconcile a node that was briefly partitioned.

**Rejected alternatives.** Pull/scrape (D6), fixed slow cadence (note 4), unbounded buffering (memory risk on constrained nodes).

---

## Crosswalk: every open infra question → answer

| `project.md` open question | Answer |
|----------------------------|--------|
| LXD, OpenStack, or MicroCloud? | **LXD** (D1). MicroCloud and OpenStack rejected with rationale. |
| How do nodes share a network under that cloud technology? | **Underlay routing + per-node NAT bridges** (D2); no OVN/fan/VXLAN in the demo. |
| What should be pre-installed in the OS? How is the OS served? | **Immutable pre-baked host image + offline-preloaded workload images + activation-only cloud-init** (D3). |
| SSH key custody / how customers get access | **Controller SSH CA + short-lived, scoped user certs; browser-local private keys** (D4). |
| GPU passthrough vs host-exposed `llama.cpp` on Node C | **Host-exposed `llama.cpp`** on `10.10.0.13:8080`; passthrough deferred to day-2 (D5). |
| How does the controller reach nodes? | **Outbound-only mTLS control channel** (D6), push telemetry (D8). |
| Storage for the cluster / migration semantics | **Local ZFS per member** (D7); resize + evacuation for the demo, live migration deferred. |

## Cross-component invariants (other workstreams rely on these)

1. **Addresses are static and frozen** per the D2 table. `INFERENCE_BASE_URL=http://10.10.0.13:8080` is the value the controller distributes to agents.
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
