# Termini — LXD cluster + single-CIDR overlay

Owner: `infra` (kanban task **t_58a1fed1**). Companion to
`docs/contracts/infra-decisions.md` (D1 cluster, D2 networking, D7 storage) and
`infra/gcp` (which provisions the three nodes this builds on).

Goal: join Nodes A/B/C into **one LXD cluster** and give instances on **every**
member an address in a **single logical subnet**, using a VXLAN overlay.

## TL;DR — verified live

| Check | Result |
|-------|--------|
| `lxc cluster list` | **3/3 ONLINE** (node-a `database-leader`, node-b, node-c) |
| Overlay subnet | **10.99.0.0/24** on all members (VXLAN VNI 100 over the underlay) |
| Instance on Node A → instance on Node C | **ping both ways, 0% loss**, HTTP `overlay-ok` |
| Full mesh | a↔b, a↔c, b↔c all reachable in the one subnet |
| Storage | local **ZFS** pool `local`, per member (D7) |
| LXD | snap `5.21/stable` rev `40958`, identical on all members |

Evidence: `evidence/overlay-run-*.txt` (live state) and `evidence/verify-*.txt`
(acceptance run, ends `RESULT: PASS`).

## Cluster

- Formed with `lxd init --preseed`: node-a is the **seed**, node-b/node-c join
  with single-use tokens from `lxc cluster add` (see `cluster-up.sh`).
- One local **ZFS** storage pool `local` per member (`docs/contracts/infra-decisions.md` D7).
- All members run the same snap (`5.21/stable`, rev `40958`) — D1 invariant #5.

## Overlay network (mode: `vxlan-bridge`)

```
instance on node-a (10.99.0.x) ── veth ── termini-overlay bridge ── vxlan-termini
                                                                     │  (VNI 100, UDP 4789, encap over the 10.10.0.0/24 underlay)
instance on node-c (10.99.0.y) ── veth ── termini-overlay bridge ── vxlan-termini
```

1. **VXLAN transit** on every member: `vxlan-termini` (VNI `100`, `dstport 4789`,
   `nolearning`) with static FDB flood entries pointing at the other members'
   underlay addresses. MTU `1410` (underlay `1460` − 50 B VXLAN).
2. **LXD bridge network `termini-overlay`** (cluster-wide; member-specific
   `bridge.external_interfaces=vxlan-termini`), MTU `1400`. It is deliberately
   pure L2 (`ipv4.address=none`) because LXD keys like `ipv4.address`/`ipv4.dhcp`
   are cluster-wide, not per-member, so three bridge IPs/DHCP servers on one L2
   would collide.
3. **One authoritative DHCP/gateway service** on the seed: `10.99.0.1/24` on the
   bridge + `dnsmasq` serving the pool. Instances on **any** member DHCP an
   address from the same subnet. This is why `verify.sh` sees a Node-A instance
   and a Node-C instance land in the same `/24`.

### Addressing plan

| Scope | Value | Notes |
|-------|-------|-------|
| Overlay CIDR | `10.99.0.0/24` | one subnet for instances on all members |
| Gateway / DHCP / DNS | `10.99.0.1` | on **node-a** only (single authority) |
| DHCP pool | `10.99.0.100 – 10.99.0.254` | served to instances across the VXLAN |
| VXLAN VNI / port | `100` / `4789` | port already allowed by `infra/gcp` firewall (`allow-vxlan`) |
| Snapshot allocations | ovl-a `10.99.0.131` (node-a), ovl-b `10.99.0.187` (node-b), ovl-c `10.99.0.156` (node-c) | DHCP leases (observed) |

The CIDR and the per-instance/node allocations are written into the **shared
inventory** (`~/.termini/inventory.json`, new `overlay` object) by
`record-inventory.sh`, which then validates the file against
`infra/gcp/inventory.schema.json`.

## Why VXLAN (and not OVN / fan)

The card preferred OVN, with LXD fan as the fallback. On this footprint neither
is usable as-is, and the card explicitly allows a "OVN/VXLAN overlay", so the
overlay is built with **VXLAN** — the VLAN/Geneve family already anticipated by
the footprint's firewall rule:

- **OVN** — `lxc network create --type=ovn` fails (`Failed to get OVN client …
  ovs-vsctl … database connection failed`): the OVS/OVN stack is not installed,
  and OVN would carry Geneve on **UDP 6081**, which the frozen firewall does not
  open (it allows **UDP 4789** — VXLAN — only). Standing up `ovn-central` on
  every member for a 3-node demo is exactly the live failure surface D2 rejected.
- **fan** — LXD 5.21 rejects it: `lxc network create fanprobe --type=fan` →
  `Error: Unknown driver`. Fan networking is legacy/removed in modern LXD snaps.

Tradeoff, stated plainly: VXLAN costs a 50-byte header (path MTU `1460 → 1410`),
so the bridge/instance MTU is pinned to `1400` to avoid PMTU black holes. Cross
node traffic rides the plain underlay between the three members; the
`llama.cpp` data path stays on the direct underlay route (D2/D5) — the overlay
is for agent-workload addressing, not the latency-critical inference leg.

## Usage

```bash
# 1. form the cluster (idempotent; skips members already ONLINE)
infra/cluster/cluster-up.sh

# 2. build the single-CIDR overlay (idempotent)
infra/cluster/overlay-up.sh

# 3. record the CIDR + allocations in the shared inventory
infra/cluster/record-inventory.sh

# 4. acceptance: cluster online + same-CIDR + cross-member ping/HTTP
infra/cluster/verify.sh          # ends "RESULT: PASS"
```

All scripts read the node list from `~/.termini/inventory.json` and SSH with
`~/.ssh/id_ed25519` (override with `--inventory` / `--key` / `INVENTORY` /
`SSH_KEY`). No credentials are stored in the repo.

### Teardown

```bash
# remove overlay instances + network and the VXLAN transit (keeps the cluster)
for n in ovl-a ovl-b ovl-c; do sudo lxc delete -f "$n"; done   # on the seed
sudo lxc network delete termini-overlay                        # on the seed
# per member: sudo ip link del vxlan-termini
```

## Known limitations (stated, not hidden)

1. **Instances are LXD containers, not VMs.** LXD VMs need `/dev/kvm`; this GCP
   footprint exposes no nested virtualization (`cpu vmx/svm flags: 0`,
   `modprobe kvm_intel → Operation not supported`, no `/dev/kvm`). The overlay is
   instance-type agnostic: an LXD VM attaches to `termini-overlay` exactly like a
   container. To run real VMs, re-create the instances with
   `advanced_machine_features { enable_nested_virtualization = true }` (n2
   supports it) before the cluster is formed. `verify.sh` labels the instance
   type in its output so this is never mistaken for a VM proof.
2. **One DHCP authority.** The gateway/DHCP lives on the seed only; if node-a is
   down, new leases stop (existing instances keep working). Acceptable for the
   demo; a second dnsmasq with a split pool is the HA path.
3. **The bridge IP is added outside LXD** (`ip addr`) because `ipv4.address` is
   cluster-wide. It is re-applied by `overlay-up.sh`, not persisted by LXD.
4. **Relationship to D2.** D2 froze "underlay routing, no overlay in the demo"
   for the *inference* path and named OVN as the day-2 alternative. This card
   asks for the cluster + a single-CIDR overlay, so the overlay is built and
   verified here while the direct underlay inference route is unchanged. If the
   overlay becomes the demo's default guest network, D2 should be amended to
   record `vxlan-bridge` as the chosen mechanism.

## Files

| File | Purpose |
|------|---------|
| `cluster-up.sh` | form the 3-member LXD cluster (seed + join) |
| `overlay-up.sh` | VXLAN transit + LXD bridge network + overlay DHCP/gateway |
| `verify.sh` | acceptance verifier (cluster online, same CIDR, cross-member ping/HTTP) |
| `record-inventory.sh` | write the overlay section into the shared inventory + schema-validate |
| `evidence/` | captured live run + acceptance output |
