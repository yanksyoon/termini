# Termini — Research Stream 1: Node bootstrapping, clustering & networking overlays

- Owner: `infra` (kanban task `t_d1469c94`, W0 deep-research stream 1 of 4)
- Status: research complete — feeds `docs/research/tech-landscape.md` (task `t_8550b53c`)
- Date: 2026-10-04
- Scope: pick technologies to **adopt instead of build** for node provisioning, clustering, and networking on GCP + a real GPU VM, for a 5-day hackathon demo.
- Feeds: `docs/contracts/infra-decisions.md` (D1–D8), `docs/contracts/node-bootstrap.md`
- Requirements source: `~/termini/project.md`
- Method: primary vendor/project docs and repos were retrieved and are cited inline as `[n]`; the full link list is in §9. Every listed area below carries an explicit **adopt / adopt-later / build-nothing-do-not-adopt** verdict with at least one citation.

> **Reading the verdicts.** `ADOPT` = commit to it now for the 5-day build. `ADOPT-LATER` = real technology, correct answer for a production/on-prem day-2, deliberately out of the demo critical path. `DO-NOT-ADOPT (build nothing)` = evaluated, rejected; we neither adopt it nor hand-roll it during the hackathon. "Build-vs-adopt" always resolves to **adopt an existing component or drop the need**, never "write our own" — the only thing we build is the thin `termini-bootstrap` daemon and controller glue that no upstream product provides.

---

## 1. Verdict table (one row per listed area)

| # | Area | What exists | Maturity | License / Cost | Fit for the 5-day demo | Verdict | Integration risk | Cite |
|---|------|-------------|----------|----------------|------------------------|---------|------------------|------|
| B1 | **Canonical MAAS** | Bare-metal-as-a-service: DHCP/DNS/TFTP/PXE/iPXE, IPMI/Redfish power, lifecycle state machine, REST API | Very mature; scales to 1000s of nodes | AGPL-3.0; standalone self-support **free for all machines**; paid support $30/$50/$100 per managed machine·yr | None on GCP (GCP VMs have no BMC/PXE). Only relevant to the on-prem bare-metal story | **ADOPT-LATER** (on-prem production path); do **not** deploy in the hackathon | High: needs own provisioning subnet + BMC access; GCP-incompatible | [1][2][3][5] |
| B2 | **MicroCloud** | Snap that drives LXD + MicroCeph + MicroOVN into a HA cluster | Mature (v3.x); prod needs ≥3 physical nodes, ≤50 | Free (snaps); Ubuntu Pro recommended for prod | Too heavy: bolts Ceph + OVN onto every node, needs ≥3 networks + multiple NICs | **ADOPT-LATER** (day-2 packaging); reject for demo | High: extra distributed systems and NIC prerequisites | [6][7][8][9] |
| B3 | **Foreman** | Lifecycle mgmt + provisioning + config (Puppet/Salt/Ansible), smart proxies | Mature (3.18.x), but Puppet-centric | GPL-3.0; free | Redundant: duplicates provisioning and config we do with images + cloud-init | **DO-NOT-ADOPT** | Medium: Puppet/ENC learning curve, Ruby stack | [10][11] |
| B4 | **Netboot.xyz** | iPXE menu bootloader (Ubuntu installers, live images, utilities) | Mature, widely used | Apache-2.0; free | Useful **only** for on-prem bare-metal netboot of our image; GCP boots a machine image | **ADOPT (optional, on-prem path)** | Low: it is a boot menu; needs a TFTP/HTTP host | [12][13] |
| B5 | **PXE / iPXE** | Standard network boot; MAAS and netboot.xyz both drive it | Mature standard | GPL/iPXE + Apache tooling; free | Not used on GCP; is the on-prem boot mechanism | **ADOPT (interface only)** | Medium: DHCP option wiring, UEFI vs legacy | [2][12] |
| B6 | **cloud-init** | De-facto instance init; NoCloud/cloud metadata; `write_files`, `runcmd`, users/keys | Mature; single-process since 24.3; Ubuntu 24.04 ships 24.1.x/25.x | **GPL-3.0 or Apache-2.0 (dual)**; free | Used **activation-only** (write config + start service); must not install packages at boot | **ADOPT (activation-only)** | Low if kept to `write_files`+`runcmd`; high if used for live installs | [14][15][16] |
| B7 | **Ansible** | Agentless config mgmt/orchestration over SSH | Very mature; `ansible-core` 2.19 (May 2025) / 2.20 (Nov 2025); community pkg 12.x/13.x | GPL-3.0+; free | Adopt for **image build + CI + day-2 runbooks**, not the live control plane | **ADOPT (build/runbooks)** | Low | [17][18][19] |
| B8 | **Terraform / OpenTofu** | IaC; GCP provider provisions VPC + VMs | Very mature | Terraform **BUSL-1.1** (source-available, IBM-owned since Feb 2025); OpenTofu **MPL-2.0** (Linux Foundation/CNCF sandbox) | Adopt for GCP infra (VPC, firewall, VMs, machine images) | **ADOPT** (prefer **OpenTofu** for license purity) | Low | [20] |
| C1 | **LXD** | System container + VM manager, single REST API, clusters, live migration | Very mature; LTS **5.21** supported to Jun 2029; feature track 6.x | **AGPL-3.0-only**, free; Ubuntu Pro adds support | The core: hosts agent VMs, forms the 3-member cluster | **ADOPT (core)** | Low–medium: join must be version-consistent | [21][22][23][24] |
| C2 | **Incus** | Community fork of LXD by the original LXD team; same CLI/API | Mature; 6.0 LTS supported to Jun 2029; v6.2x line in 2026 | **Apache-2.0**, no CLA | Technically interchangeable with LXD; LXD chosen for Canonical/Ubuntu alignment | **ADOPT-LATER** (drop-in alternative; keep migration path) | Low: `lxd-to-incus` migration tool | [28][29][30] |
| C3 | **OpenStack (DevStack/Kolla)** | Full IaaS; Kolla-Ansible containerized deploy | Mature; release 2025.1 "Epoxy" | Apache-2.0; free | Days to stand up, heavy daemons, solves problems we don't have | **DO-NOT-ADOPT** | Very high | [31] |
| C4 | **k8s — k3s** | CNCF-certified lightweight Kubernetes, single binary | Very mature; tracks upstream (v1.31.x–v1.36.x) | Apache-2.0; free | No k8s requirement in the demo; adds a scheduler we'd have to justify | **DO-NOT-ADOPT** (ADOPT-LATER only if agent scheduling moves to k8s) | Medium–high | [32][33] |
| C5 | **k8s — Talos Linux** | Immutable Kubernetes OS, API-only, no shell | Mature; free + enterprise | **MPL-2.0**; Enterprise **$1,000/node·yr**, 10-node min | Replaces the OS; conflicts with the "customer brings Ubuntu/our image" story | **DO-NOT-ADOPT** | High paradigm shift | [33][34] |
| C6 | **Proxmox VE** | KVM/LXC platform, HA, live migration | Mature | **AGPLv3**; subs €120–€1,100 per socket·yr (all features included) | Not chosen: LXD already covers containers+VMs with one API on Ubuntu | **DO-NOT-ADOPT** | Medium: different OS/boot model | [35] |
| C7 | **Live migration / node evacuation** | LXD stateful VM live migration + cluster evacuation | Mature | Included in LXD | **Evacuation** is used for the disk-failure scenario; live migration is achievable but copies root disk on local pools | **ADOPT (evacuation)**; live migration = optional | Medium: needs `migration.stateful` pre-set + `size.state` | [24][25][26][27] |
| N1 | **OVN** | SDN for OVS: logical switches/routers, ACLs, DHCP/DNS | Mature | Apache-2.0; free | Correct day-2 overlay; needs OVN controllers + Geneve tunnels per node = 1–2 days + MTU risk | **ADOPT-LATER** (day-2) | High for a 5-day build | [36][37] |
| N2 | **VXLAN** | Kernel L2-over-UDP encapsulation (RFC 7348) | Mature, in-kernel standard | GPL-2.0 kernel | A primitive inside OVN/fan; not something to hand-roll | **DO-NOT-ADOPT (build nothing)** | Medium — MTU-sensitive | [38] |
| N3 | **LXD fan** | `/8` address expansion over the underlay; `fanctl`/bridge fan mode | Legacy; `fan.type=ipip` removed; known regressions | Free (in LXD/Ubuntu) | Fragile: MTU pitfalls + a real 6.14-kernel regression reported for MicroCloud | **DO-NOT-ADOPT** | High (silent drops) | [39][40][41] |
| N4 | **Tailscale** | WireGuard mesh + identity + ACLs; Tailscale SSH removes key management | Mature | Free **≤6 users** (unlimited devices); Standard **$8/user·mo**; Premium $18 | Optional operator path: reach fleet nodes + GPU box from anywhere without exposing SSH | **ADOPT (optional, operator access)** | Low | [42][43] |
| N5 | **WireGuard** | In-kernel, fixed-cipher VPN; the transport under Tailscale | Mature, upstream Linux | **GPL-2.0** | Adopt **only if** a cross-node tunnel is strictly required; demo deliberately uses the underlay | **ADOPT (primitive, if needed)** | Low–medium (kernel) | [44] |
| N6 | **Nebula** | Slack's overlay: mutual TLS, lighthouses, groups | Mature (v1.11.x) | **MIT**; free (managed option available) | Redundant with WireGuard/Tailscale; needs a lighthouse + own PKI | **DO-NOT-ADOPT** | Low–medium | [45] |
| N7 | **Cilium** | eBPF CNI/policy for Kubernetes | Mature (v1.17–v1.20 in 2026) | Apache-2.0 (BPF templates GPL-2.0/BSD-2) | Kubernetes-only; we are not running k8s in the demo | **DO-NOT-ADOPT** | N/A for demo | [46] |

---

## 2. Bootstrapping & provisioning — detail

### B1. Canonical MAAS — ADOPT-LATER (on-prem), do not deploy in the demo
- **What exists.** MAAS turns bare metal into a cloud-like pool: DHCP/DNS/TFTP/PXE/iPXE network boot, IPMI/Redfish/AMT power control, hardware discovery/commissioning, a `new→commissioning→ready→deployed→released` lifecycle, and a REST API/CLI designed to be driven by Terraform/Ansible/Juju [1][2].
- **Maturity.** The most "platform" of the bare-metal tools; designed for hundreds-to-thousands of nodes. Region controller + rack controllers, roughly ~2 GB RAM for the controller; 10–20 min provisioning per machine [5].
- **License / cost.** AGPL-3.0. Canonical's MAAS page states **self-support is free for all machines**; paid tiers are per *managed machine·yr* ($30 Essential / $50 Standard / $100 Advanced) [3][4][5].
- **Fit.** Zero fit for the GCP demo: GCP VMs cannot PXE-boot and have no BMC, which is exactly what MAAS drives. It is, however, the honest production answer for "customer brings their own hardware".
- **Adopt-vs-build verdict.** **ADOPT-LATER**: reference it in the on-prem day-2 story; **do not** stand up a region/rack controller during the hackathon and do **not** build a bespoke bare-metal provisioner.
- **Integration risk.** High (own provisioning subnet, DHCP authority, BMC credentials, GCP-incompatible).

### B2. Canonical MicroCloud — ADOPT-LATER (day-2), reject for demo
- **What exists.** A single snap that automates LXD + MicroCeph (storage) + MicroOVN (network) into a HA cluster; init scrapes members, offers disks to Ceph, and configures OVN [8][9].
- **Maturity / prerequisites.** Current snap channels are `lxd 6/stable`, `microceph squid/stable`, `microovn 24.03/stable`, `microcloud 3/stable`. **Production requires a minimum of 3 physical machines** (VMs explicitly not recommended), ≤50 members, ≥2 dedicated NICs per member (one intra-cluster, one uplink), an uplink that supports broadcast+multicast, and snapd ≥2.59 [6][7][9].
- **License / cost.** Free snaps; Ubuntu Pro recommended for production.
- **Fit.** Wrong trade for a 3-node, single-operator demo: it adds Ceph quorum and OVN as live-failure surfaces and imposes NIC prerequisites.
- **Adopt-vs-build verdict.** **ADOPT-LATER**. MicroCloud is the production packaging path once HA shared storage + multi-tenant flat networking matter; **reject** for the demo (this matches frozen decision **D1**, which chose LXD directly).
- **Integration risk.** High — three extra distributed systems and networking prerequisites.

### B3. Foreman — DO-NOT-ADOPT
- **What exists.** Full lifecycle/provisioning/config management with smart proxies; provisions bare metal and clouds; Puppet-centric ENC with Ansible/Salt/Chef plugins [10][11].
- **Maturity / license.** Mature (site shows 3.18.1 / 3.17.2); **GPL-3.0** [10][11].
- **Fit.** Duplicates everything we already do with pre-baked images + cloud-init + a thin daemon, and drags a Ruby/Puppet stack into a 5-day build.
- **Adopt-vs-build verdict.** **DO-NOT-ADOPT** and do not build an equivalent — the need is already covered by B6/B7.

### B4. Netboot.xyz — ADOPT (optional, on-prem path)
- **What exists.** An iPXE bootloader that chains to a menu of network installers, live distros, and rescue tools; bootable via USB/floppy/DHCP/UEFI, or self-hosted [12][13].
- **Maturity / license.** Mature and widely used; **Apache-2.0** [12].
- **Fit.** On-prem bare-metal nodes need *some* way to netboot our image. netboot.xyz gives a menu; we would chain it to our own image, so it is a convenience, not a requirement.
- **Adopt-vs-build verdict.** **ADOPT (optional)** for the on-prem netboot path; on GCP it is unused (machine images). Do not build a custom iPXE menu when the project already maintains one.
- **Integration risk.** Low (needs a TFTP/HTTP host on the provisioning subnet).

### B5. PXE / iPXE — ADOPT (interface only)
- **What exists.** The standard network-boot path; MAAS drives both PXE/iPXE and UEFI HTTP boot [2], and netboot.xyz is an iPXE payload [12].
- **Verdict.** **ADOPT as the on-prem boot interface.** No custom bootloader work. On GCP the equivalent interface is the machine image + instance metadata.
- **Integration risk.** Medium (DHCP `next-server`/`bootfile-name`, UEFI vs legacy, secure boot). Out of the demo critical path.

### B6. cloud-init — ADOPT (activation-only)
- **What exists.** The de-facto instance initialization standard, pre-installed on essentially every mainstream cloud image; `write_files`, `runcmd`, users/SSH-key modules, NoCloud + cloud metadata (GCP) datasources [14][15][16].
- **Maturity / notes.** Ubuntu 24.04 (Noble) ships cloud-init 24.1.x with 25.1.4 security updates available; since 24.3 it runs as a single process and the service was renamed `cloud-init.service → cloud-init-network.service` — external units that ordered on the old name should target `cloud-config.target` [14][15].
- **License / cost.** **Dual GPL-3.0 / Apache-2.0** [16].
- **Fit.** We use cloud-init **only to write `/etc/termini/config.yaml`, the CA pubkey/token files, and to enable+start `termini-bootstrap.service`** — never to install packages. This is the activation-only strategy of frozen decision **D3**.
- **Adopt-vs-build verdict.** **ADOPT (activation-only).** Reinvents nothing; it is the single supported way to inject per-node config on both GCP and on-prem.
- **Integration risk.** Low if restricted to `write_files`+`runcmd`; the 3–7 min "install packages in cloud-init" failure mode is exactly what D3 forbids.

### B7. Ansible — ADOPT (image build, CI, day-2 runbooks)
- **What exists.** Agentless config management/orchestration over SSH [17][18][19].
- **Maturity / version.** Very mature. `ansible-core` 2.19 (May 2025) and 2.20 (Nov 2025); the community bundle is 12.x→13.x; only the latest major community version is maintained, `ansible-core` maintains latest + 2 [18][19].
- **License / cost.** GPL-3.0+; free [17].
- **Fit.** Excellent for **building the Termini Node OS image** (the Packer/CI provisioner) and for operator **runbooks**. It should **not** be the runtime control plane — the live path is the node's outbound mTLS channel to the controller (D6), which must not depend on inbound SSH.
- **Adopt-vs-build verdict.** **ADOPT** for build + runbooks; do not put Ansible in the boot path.
- **Integration risk.** Low.

### B8. Terraform vs OpenTofu — ADOPT (prefer OpenTofu)
- **What exists.** IaC for the GCP side (VPC, firewall, instances, machine images) [20].
- **License / governance.** HashiCorp relicensed Terraform to **BUSL-1.1** on 2023-08-10; IBM's acquisition of HashiCorp closed **2025-02-27**; the community fork **OpenTofu** (from Terraform 1.5.x) keeps **MPL-2.0** under the Linux Foundation and joined CNCF as a sandbox project on 2025-04-23 [20].
- **Fit.** Internal use of either binary is unaffected by BUSL; for a product that may later be hosted/multi-tenant, OpenTofu's OSI license removes ambiguity.
- **Adopt-vs-build verdict.** **ADOPT** IaC for GCP; **prefer OpenTofu** on license grounds. Do not hand-roll `gcloud` shell scripts when a provider module exists.
- **Integration risk.** Low.

---

## 3. VM/container runtimes & clustering — detail

### C1. LXD — ADOPT (core)
- **What exists.** A single manager for system containers **and** VMs with one REST API and one CLI, across a cluster; role-based member roles; instance moves/evacuation [24].
- **Maturity / versions.** Very mature. The snap's **default track is 5.21 (LTS)**, supported until **June 2029**; the feature track is 6.x (docs currently 6.9). Cluster members must run the **same snap revision**; Canonical instructs installing members with `--cohort="+"` to keep them synchronized [21][22][23].
- **License / cost.** **AGPL-3.0-only**; free; Ubuntu Pro adds support ([23] shows 5.21.8 published Sep 2026).
- **Fit.** The core of the demo — it hosts the agent VMs, forms the 3-member cluster `termini`, and one API drives placement/moves. Matches frozen **D1**.
- **Adopt-vs-build verdict.** **ADOPT.** This is the concrete ranking of the best options (see the k3s/Talos comparison and the OpenStack Kolla quickstart for the alternatives we rejected) [31][33].
- **Integration risk.** Low–medium; the #1 real-world failure is a snap-revision mismatch between members — mitigate with `--cohort="+"` and the pre-join check the contract already specifies.

### C2. Incus — ADOPT-LATER (drop-in alternative)
- **What exists.** The fork of LXD maintained by the original LXD team; same design, `incus` CLI/API; scales a single instance up to a full rack cluster (up to ~50 servers); live migration of VMs with remote-storage disks [28][29][30].
- **Maturity / license.** 6.0 LTS (Apr 2024), supported to June 2029; **Apache-2.0**, no CLA [28][30].
- **Fit.** Functionally interchangeable with LXD; LXD wins only on Canonical/Ubuntu alignment and the pre-baked-image story. The migration tool `lxd-to-incus` de-risks any later swap [30].
- **Adopt-vs-build verdict.** **ADOPT-LATER.** No functional gap forces it for the demo; keep it documented as the escape hatch.

### C3. OpenStack (DevStack/Kolla) — DO-NOT-ADOPT
- **What exists.** Full IaaS; Kolla-Ansible deploys containerized services [31].
- **Maturity / license.** Mature; release 2025.1 "Epoxy"; **Apache-2.0** [31].
- **Even the "quick" path is heavy.** Kolla-Ansible's all-in-one minimal host is 2 NICs / 8 GB RAM / 40 GB disk, and current Kolla requires Ansible 10–11 (ansible-core 2.17–2.18) with a full `bootstrap-servers → prechecks → deploy` run over a container fleet (MariaDB/Galera, RabbitMQ, etcd, HAProxy, …) [31].
- **Adopt-vs-build verdict.** **DO-NOT-ADOPT.** Days to stand up, solves multi-tenant quota/volume/UI problems the demo does not have.

### C4. k3s — DO-NOT-ADOPT (k8s not required)
- **What exists.** CNCF-certified, single-binary lightweight Kubernetes; Apache-2.0; tracks upstream rapidly (v1.31.x in 2025, v1.36.x in 2026) [32][33].
- **Fit.** There is no Kubernetes requirement in `project.md`. Adding k3s introduces a scheduler, CNI, and ingress layer whose value the demo cannot show.
- **Adopt-vs-build verdict.** **DO-NOT-ADOPT**; **ADOPT-LATER** only if agent scheduling is later moved to Kubernetes — and even then k3s over Talos given our Ubuntu host image.

### C5. Talos Linux — DO-NOT-ADOPT
- **What exists.** An immutable, API-only Kubernetes OS: no shell, no SSH, no package manager; managed via `talosctl` and machine-config YAML [33][34].
- **License / cost.** **MPL-2.0** free; **Talos Enterprise $1,000/node·yr with a 10-node minimum** [34].
- **Fit.** Talos *is* the operating system — it would replace the customer's Ubuntu host image, which breaks the "bring your own Ubuntu hardware / our pre-baked image" story and the SSH contract (D4).
- **Adopt-vs-build verdict.** **DO-NOT-ADOPT.**

### C6. Proxmox VE — DO-NOT-ADOPT
- **What exists.** KVM + LXC platform with HA, snapshots, live migration, firewall, clustering [35].
- **License / cost.** **AGPLv3**, all features free; optional subscription per occupied CPU socket·yr: Community €120, Basic €370, Standard €550, Premium €1,100 [35].
- **Fit.** A credible LXD alternative, but it is a different OS/boot model and offers nothing LXD + our image pipeline does not already give us.
- **Adopt-vs-build verdict.** **DO-NOT-ADOPT.**

### C7. Live migration & node evacuation — ADOPT (evacuation); live migration optional
- **What exists.** LXD supports **live migration of VMs** and **cluster-member evacuation** (including live migration for VMs) [24][25]. Live migration requires `migration.stateful=true` on the VM (sett only while stopped) and, **on a local storage pool, `size.state` ≥ `limits.memory`** on the root disk; with a remote pool (Ceph RBD) no `size.state` is needed because the disk is not copied [25][26][27].
- **Finding that sharpens frozen decision D7.** `infra-decisions.md` D7 states that with per-member local storage "live migration of a running VM is not available". Upstream LXD is more capable than that: live migration **is** supported for local pools via stateful migration — it simply transfers the root disk over an NBD socket (slower) instead of skipping it as shared Ceph storage does [26]. The demo choice (resize + **evacuation**) is still the right one for reliability, but the contract should say "live migration is available but copies the disk and requires `migration.stateful`+`size.state` pre-set" rather than "not available".
- **Adopt-vs-build verdict.** **ADOPT** LXD's built-in evacuation for the disk-failure scenario; set `migration.stateful=true` + `size.state` on agent VMs so a live move is possible if time allows. Day-2 shared storage = MicroCeph (B2).
- **Integration risk.** Medium (pre-configuration is mandatory and can only happen while a VM is stopped).

---

## 4. Networking overlays — detail

### N1. OVN — ADOPT-LATER (day-2 overlay)
- **What exists.** OVN translates logical switches/routers into OpenFlow on OVS; distributed routers/switches, ACLs, DHCP, DNS [36]. LXD exposes it as the `ovn` network type with `security.acls` and default NAT to an uplink [37].
- **Maturity / license.** Mature; **Apache-2.0** [36].
- **Fit.** The correct answer for a real multi-tenant product (flat logical subnets, ACLs, floating IPs). It requires OVN central/controller (or MicroOVN) on every node plus Geneve tunnels and MTU tuning — 1–2 days of integration and a live failure surface.
- **Adopt-vs-build verdict.** **ADOPT-LATER.** Matches frozen **D2** (underlay routing for the demo, OVN documented as day-2).

### N2. VXLAN — DO-NOT-ADOPT (build nothing)
- **What exists.** Kernel L2-in-UDP encapsulation (RFC 7348), the primitive under fan/OVN encap [38].
- **Fit / verdict.** It is a building block, not a product. **DO-NOT-ADOPT** as a hand-rolled overlay; if we ever need an overlay it rides OVN, not hand-written `ip link` VXLAN.

### N3. LXD fan networking — DO-NOT-ADOPT
- **What exists.** Ubuntu's `/8` address-expansion overlay mapping underlay subnets onto a larger space via `fanctl`/bridge fan mode [39].
- **Status / risk.** It is a legacy mechanism: LXD removed `fan.type=ipip` networks [40], and there is a real regression where `ip link add … fan-map` fails on the 6.14 kernel with "binary attribute size out of range" (MicroCloud had to advise the 6.8 GA kernel) [41]. LXD's own guidance now prefers a plain bridge on a single host/cloud and OVN for private clouds [41].
- **Adopt-vs-build verdict.** **DO-NOT-ADOPT** (also ruled out by **D2**). Note the concrete regression as evidence that this is not a demo-safe choice.

### N4. Tailscale — ADOPT (optional, operator access)
- **What exists.** A WireGuard-based mesh with identity + ACLs; **Tailscale SSH** claims port 22 on the tailnet IP and authenticates by node identity/ACLs instead of SSH keys [42][43].
- **Maturity / cost.** Mature. Free plan **≤6 users with unlimited user devices** (50 tagged resources, 3 ACL groups); Standard **$8/user·mo**; Premium $18 [42].
- **Fit.** Optional operator path during the hackathon: reach all three nodes and the GPU box from a laptop without exposing inbound SSH or distributing keys. It is **not** the node↔controller path (that is D6) and not the guest-to-`llama.cpp` path (that is D2 underlay).
- **Adopt-vs-build verdict.** **ADOPT (optional).** If used, it complements — never replaces — the CA-signed SSH contract in D4.

### N5. WireGuard — ADOPT (primitive, only if a tunnel is strictly needed)
- **What exists.** The in-kernel, fixed-cipher-secure tunnel; the transport under Tailscale [44].
- **License / cost.** **GPL-2.0** (kernel); free [44].
- **Fit.** The demo deliberately has no cross-node tunnel (D2). Keep WireGuard as the chosen primitive if one is ever required, rather than a bespoke tunnel.
- **Adopt-vs-build verdict.** **ADOPT (primitive).** Do not build a tunnel with raw crypto/sockets.

### N6. Nebula — DO-NOT-ADOPT
- **What exists.** Slack's overlay: mutually-authenticated peers, lighthouse discovery, certificate-based groups [45].
- **License / cost.** **MIT**; free; a managed option exists [45].
- **Fit / verdict.** Redundant with WireGuard/Tailscale for a 3-node demo, and it adds a lighthouse + its own PKI to operate. **DO-NOT-ADOPT** for W0–W6.

### N7. Cilium — DO-NOT-ADOPT
- **What exists.** eBPF-based networking/security/observability, primarily a Kubernetes CNI, with L3–L7 policy and cluster mesh [46].
- **Maturity / license.** Mature (v1.17.0 in Feb 2025; v1.18–v1.20 lines in 2026); **Apache-2.0**, BPF templates GPL-2.0/BSD-2 [46].
- **Fit / verdict.** Kubernetes-only; we run no Kubernetes in the demo. **DO-NOT-ADOPT** for W0–W6.

---

## 5. Pinned versions & cost snapshot (for the W1 contracts)

| Component | Version to pin / commit | License | Cost at demo scale | Notes |
|-----------|-------------------------|---------|--------------------|-------|
| LXD snap | `5.21/stable` (currently 5.21.8), same revision on all members, `--cohort="+"` | AGPL-3.0-only | $0 | LTS supported to Jun 2029 [22][23] |
| cloud-init | whatever ships in Ubuntu 24.04 (≥24.1; 25.1.4 security update available) | GPL-3.0 / Apache-2.0 | $0 | activation-only [14][15] |
| OpenTofu | current 1.12.x line | MPL-2.0 | $0 | Terraform BUSL-1.1 alternative [20] |
| Ansible (build/CI) | ansible-core 2.19+ (community 12.x/13.x) | GPL-3.0+ | $0 | not in boot path [18][19] |
| Tailscale (optional) | latest client | proprietary daemon | Free ≤6 users; then $8/user·mo | operator access only [42][43] |
| GCP GPU node (Node C) | **g2-standard-8** (1× L4 24 GB, 8 vCPU, 32 GB) | n/a | **≈$0.84/hr on-demand**; cheaper on Spot/Flex — g2-standard-4 ≈$0.71/hr; N1+T4 ≈$0.39–0.64/hr | G2 = L4, best fit for `llama.cpp` inference [47][48][49] |
| MicroCloud / OVN / Ceph | day-2 only | free (snaps) | — | not in demo [6][7] |
| Proxmox | rejected | AGPLv3 | €120–€1,100/socket·yr | reference only [35] |

> Prices are list, retrieved 2026-10-04, and move; treat the GPU line as the one to re-check when Node C is provisioned. Proxmox's own price list was mid-transition (Community €115 → €120, Premium €1,060 → €1,100 between the Jan-2025 and current sheets) [35].

---

## 6. Short list of concrete tools/libraries to commit to for the 5-day demo

**Commit (adopt):**
1. **LXD snap, channel `5.21/stable`, `--cohort="+"`, one identical revision per member** — hypervisor + 3-member cluster. [21][22][23]
2. **cloud-init, activation-only** — per-node `config.yaml` + CA pubkey + enrollment token + `systemctl enable --now termini-bootstrap`. [14][15][16]
3. **OpenTofu (MPL-2.0) + `hashicorp/google` provider** — GCP VPC, firewall rules, the three node VMs, and the GPU Node C. [20][47]
4. **Packer + `virt-customize` (or Ansible as the provisioner inside a Packer build)** — produce the immutable "Termini Node OS" GCP machine image and the on-prem `qcow2`. [17][19]
5. **LXD cluster evacuation** (built-in) as the node-evacuation primitive; set `migration.stateful=true` + `size.state` on agent VMs so a live move is possible but not depended on. [24][25][26]
6. **Underlay routing + per-node NAT bridge** (no overlay) — the D2 networking model, backed by the fan/OVN risk evidence here. [37][39][41]

**Adopt optionally (cheap, low risk):**
7. **Tailscale (free ≤6 users) + Tailscale SSH** — operator access to the fleet during the demo; complements, never replaces, the SSH-CA contract. [42][43]
8. **netboot.xyz + iPXE** — on-prem bare-metal netboot of the `qcow2` (GCP uses the machine image). [12][13]

**Explicitly do not adopt (and do not build equivalents):** MAAS (B1, day-2 only), MicroCloud (B2, day-2 only), Foreman (B3), OpenStack (C3), k3s/Talos (C4/C5), Proxmox (C6), OVN in-demo (N1, day-2 only), VXLAN hand-roll (N2), LXD fan (N3), Nebula (N6), Cilium (N7).

---

## 7. Implications for the W1 contracts

These are the changes/confirmations the W1 contract authors (node registration, SSH key injection, GPU node lifecycle) should act on. They are consistent with `docs/contracts/node-bootstrap.md` and `docs/contracts/infra-decisions.md` unless flagged.

### 7.1 Node registration
- **Transport unchanged and confirmed.** Registration rides the outbound mTLS channel to the controller; enable `--cohort="+"` on the LXD snap so the "identical snap revision across members" precondition is actually satisfiable, and keep the pre-join `lxd_version_mismatch` guard. [21][22][24]
- **Add a `machine.provisioning` descriptor** to `POST /v1/nodes/register` — e.g. `{ "method": "gcp-image" | "ipxe-netboot" | "manual", "image_version": "…" }`. The controller currently has `machine.image_version` but no way to distinguish a GCP-image boot from an on-prem netboot; this field makes the lifecycle branchable and is cheap to add now.
- **`machine_id` fallback for GCP.** `machine_id = sha256(dmi.product_uuid + ":" + /etc/machine-id)` depends on a DMI product UUID that is not guaranteed on every GCP VM shape; specify a fallback (e.g. GCP instance ID from the metadata server, or `/etc/machine-id` alone) so registration cannot stall on a missing UUID.
- **cloud-init datasource note.** The same `user-data` must work via NoCloud (on-prem) and the GCP metadata datasource; keep the injected fields to `write_files` + `runcmd` and do not rely on package installs. [14][15]
- **Idempotency/upsert unchanged** (see `node-bootstrap.md` §10).

### 7.2 SSH key injection
- **The CA + short-lived-cert model in D4 is confirmed as the right call**; the research surfaces two hardening details and one alternative:
  1. Inject the CA public key via cloud-init `write_files` → `/etc/ssh/termini_ca.pub` and set `TrustedUserCAKeys`; keep `PasswordAuthentication no` and bind sshd to the underlay interface (already in `node-bootstrap.md` §7). [14]
  2. Do **not** use cloud-init's `ssh_authorized_keys`/`users` module for standing access — that is the long-lived-shared-key model D4 rejects. Use it only for the one-time break-glass key, which the daemon removes after registration (already specified).
  3. If Tailscale SSH is used for operator access, it removes key distribution entirely for that path (identity + ACLs over WireGuard) [43] — document it as an **operator convenience**, not a replacement for CA-signed SSH, so the governance/audit story stays intact.
- **Version note for the pre-baked image:** Ubuntu 24.04's cloud-init is ≥24.1 and runs single-process since 24.3 (service renamed to `cloud-init-network.service`); if any unit orders on cloud-init, target `cloud-config.target`. [14]

### 7.3 GPU node lifecycle (Node C)
- **Instance choice:** use **GCP `g2-standard-8` (1× NVIDIA L4 24 GB, 32 GB RAM, ≈$0.84/hr on-demand)**; `g2-standard-4` (≈$0.71/hr) if RAM is less critical, or an **N1 + attached T4** (≈$0.39–0.64/hr) for the cheapest inference. G2/L4 is GCP's purpose-built inference shape. [47][48][49]
- **Keep D5 (host-exposed `llama.cpp`, no passthrough).** The G2 series' L4 is the right accelerator for `llama.cpp`; VFIO passthrough is not the 5-day path. Registration must still succeed when the GPU is absent: `gpus: []` and `health: gpu_unavailable` (see `node-bootstrap.md` §9 #10). [47]
- **Bake the driver, not the download.** The NVIDIA driver stack + `nvidia-smi` belong in the Node C image variant (already in §3.1); no boot-time driver install, matching D3.
- **GPU telemetry → controller remediation.** `nvidia-smi` output feeds the Node C telemetry stream; a driver/GPU failure should mark the node `gpu_unavailable` and let the controller re-route or alert — never fail registration.
- **Networking for Node C is unchanged by this study:** `llama.cpp` is reached over the **underlay** at `http://10.10.0.13:8080` (D2/D5), not a guest/overlay address.

### 7.4 One correction to record
- **D7 wording.** Change "with per-member local storage, live migration of a running VM is not available" to "live migration is available but, on a local pool, copies the root disk and requires `migration.stateful=true` and `size.state ≥ limits.memory`; the demo uses resize + evacuation, and shared Ceph (day-2) removes the disk copy." Upstream supports local-pool live migration [25][26]; the demo plan stays the same, but the contract should be accurate.

---

## 8. Open / day-2 items (none blocking W1)

| Item | Why it can wait | Day-2 trigger |
|------|-----------------|---------------|
| MicroCloud + MicroCeph + MicroOVN | Needs ≥3 physical nodes + multiple NICs; demo is 3 VMs | When HA shared storage + flat multi-tenant networking are required [6][7] |
| OVN overlay + ACLs | 1–2 days of integration; MTU-sensitive | When the underlay is trusted and tenants need isolation [36][37] |
| MAAS | GCP can't PXE/BMC; needs its own provisioning subnet | When real customer bare metal is onboarded [1][2] |
| LXD → Incus evaluation | Functionally equivalent; `lxd-to-incus` de-risks swap | If AGPL-3.0-only or Canonical-governance becomes a problem [28][30] |
| Kubernetes (k3s) | No k8s requirement in the demo | If agent scheduling moves to Kubernetes [32] |
| Proxmox reference | Not chosen | If a non-Ubuntu host platform is ever required [35] |

---

## 9. References

Retrieved 2026-10-04.

1. Canonical — *What MAAS can do* — https://canonical.com/maas/docs/what-maas-can-do
2. Canonical — *MAAS networking* — https://canonical.com/maas/docs/about-maas-networking
3. Canonical — *MAAS Support* (self-support free; per-managed-machine tiers) — https://canonical.com/maas/support
4. Canonical — *MAAS* product page — https://canonical.com/maas
5. pistack — *MAAS vs Cobbler vs Tinkerbell (2026)* (AGPL-3.0; PXE/iPXE; controller sizing) — https://pistack.xyz/posts/maas-vs-cobbler-vs-tinkerbell-bare-metal-provisioning-guide-2026
6. Canonical — *MicroCloud Requirements* — https://canonical.com/microcloud/docs/latest/reference/requirements/
7. Canonical — *How to install MicroCloud* (snap channels) — https://canonical.com/microcloud/docs/latest/how-to/install/
8. Canonical — *About MicroCloud* — https://canonical.com/microcloud/docs/latest/explanation/microcloud/
9. GitHub — *canonical/microcloud* README — https://github.com/canonical/microcloud/blob/main/README.md
10. GitHub — *theforeman/foreman* (GPL-3.0) — https://github.com/theforeman/foreman
11. Foreman project site (3.18.1 / 3.17.2) — http://theforeman.org/
12. GitHub — *netbootxyz/netboot.xyz* (Apache-2.0) — https://github.com/netbootxyz/netboot.xyz
13. netboot.xyz project site — https://netboot.xyz/
14. cloud-init docs — *Breaking changes / 24.3 single-process* — https://docs.cloud-init.io/en/24.4/reference/breaking_changes.html
15. Ubuntu — `cloud-init` changelog, Noble 24.04 — https://launchpad.net/ubuntu/noble/+source/cloud-init/+changelog
16. cloud-init LICENSE (GPL-3.0 / Apache-2.0 dual) — https://github.com/SadeghHayeri/cloud-init/blob/main/LICENSE
17. GitHub — *ansible/ansible* (GPL-3.0+) — https://github.com/ansible/ansible
18. Ansible — *Releases and maintenance* — https://docs.ansible.com/projects/ansible/latest/reference_appendices/release_and_maintenance.html
19. Ansible version mapping (ansible-core 2.19/2.20) — https://ansiblebyexample.com/articles/ansible-terminology-ansible-vs-ansible-core-packages
20. Terraform vs OpenTofu in 2026 (BUSL-1.1, IBM close 2025-02-27, OpenTofu CNCF sandbox 2025-04-23) — https://dev.to/mr_manushukla/terraform-vs-opentofu-in-2026-the-fork-has-diverged-so-which-do-you-standardize-on-14bm
21. Canonical — *How to install LXD* (default track 5.21, `--cohort`) — https://canonical.com/lxd/docs/latest/installing
22. LXD — *Releases and snap* (5.21 LTS to Jun 2029) — https://documentation.ubuntu.com/lxd/latest/reference/releases-snap
23. Snapcraft — *LXD* (AGPL-3.0-only; 5.21.8) — https://snapcraft.io/lxd
24. LXD — *Clusters* (member roles, evacuation) — https://canonical.com/lxd/docs/latest/explanation/clusters/
25. LXD — *How to migrate LXD instances between servers* (live-migration criteria) — https://canonical.com/lxd/docs/latest/howto/instances_migrate/
26. LXD — *VM live migration implementation* (local vs shared storage) — https://canonical.com/lxd/docs/latest/reference/vm_live_migration_internals/
27. LXD — *Ceph RBD storage driver* — https://canonical.com/lxd/docs/latest/reference/storage_ceph/
28. Linux Containers — *Incus 6.0 LTS released* (Apache-2.0; live migration) — https://linuxcontainers.org/incus/news/2024_04_04_03_04.html
29. Incus documentation home — https://linuxcontainers.org/incus/docs/main
30. pkg.go.dev — *github.com/lxc/incus/v6* (Apache-2.0; `lxd-to-incus`) — https://pkg.go.dev/github.com/lxc/incus/v6
31. OpenStack — *Kolla-Ansible quickstart 2025.1* (min host, Apache-2.0) — https://docs.openstack.org/kolla-ansible/2025.1/user/quickstart.html
32. GitHub — *k3s-io/k3s* releases (Apache-2.0) — https://github.com/k3s-io/k3s/releases
33. *k3s, k0s, RKE2 or Talos compared (2026)* (licenses; Talos has no shell) — https://wz-it.com/en/blog/k3s-k0s-rke2-talos-kubernetes-distributions-comparison
34. Talos — *Linux OS for Kubernetes* (MPL-2.0; Enterprise $1,000/node·yr) — https://talos.dev
35. Proxmox — *Pricing for Subscription Plans* (AGPLv3; €120/€370/€550/€1,100 per socket·yr) — https://proxmox.com/en/products/proxmox-virtual-environment/pricing
36. OVN project (Apache-2.0; ACLs, distributed routers) — https://www.ovn.org/
37. LXD — *OVN network* (`security.acls`, uplink NAT) — https://canonical.com/lxd/docs/latest/reference/network_ovn/
38. Linux kernel docs — *VXLAN* — https://docs.kernel.org/networking/vxlan.html
39. Ubuntu Wiki — *FanNetworking* — https://wiki.ubuntu.com/FanNetworking
40. GitHub — *canonical/lxd* releases (removed `fan.type=ipip`) — https://github.com/canonical/lxd/releases
41. Ubuntu Discourse — *Fan regression "binary attribute size out of range" (6.14 kernel)* — https://discourse.ubuntu.com/t/error-binary-attribute-size-out-of-range-when-adding-a-node-without-uplink-nic/70122
42. Tailscale — *Pricing* (free ≤6 users; $8/$18 per user·mo) — https://tailscale.com/pricing
43. Tailscale — *Tailscale SSH* (identity/ACL auth over WireGuard) — https://tailscale.com/docs/features/tailscale-ssh
44. WireGuard for the Linux kernel (GPL-2.0) — https://git.zx2c4.com/wireguard-linux/
45. GitHub — *slackhq/nebula* (MIT; lighthouses) — https://github.com/slackhq/nebula
46. GitHub — *cilium/cilium* (Apache-2.0; v1.17.0 Feb 2025) — https://github.com/cilium/cilium
47. Google Cloud — *GPU machine types* (G2 = L4; N1 + T4) — https://docs.cloud.google.com/compute/docs/gpus
48. Google Cloud — *Accelerator-optimized VM pricing* (g2-standard-4 $0.7068/hr) — https://cloud.google.com/products/compute/pricing/accelerator-optimized
49. Thunder Compute — *GCP GPU instances 2026* (L4 ≈$0.70–1.12/hr; T4 ≈$0.39–0.64/hr) — https://www.thundercompute.com/blog/google-cloud-gpu-instances
