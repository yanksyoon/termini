# Direct inference route — runbook (kanban `t_bcae0231`)

Removes OVN/fan MTU overhead from the llama.cpp data path used during the demo,
makes the path switchable, and proves the path with DF-bit probes and a sustained
streaming load test. Implements the networking half of W6 ("demo reliability"),
consistent with the frozen decision `docs/contracts/infra-decisions.md` **D2**
(underlay routing, no overlay in the demo; OVN is the documented day-2 path) and
**D5** (host-exposed llama.cpp on Node C, `:8080`).

## TL;DR

```bash
# on the node that hosts the agent VM
sudo infra/net/direct-route.sh init          # install /etc/termini/network.json
sudo infra/net/direct-route.sh up            # pin llama.cpp to the direct route
   infra/net/direct-route.sh verify          # DF probes: direct passes, overlay fails
infra/net/inference-loadtest.sh --path direct --duration 180   # zero dropped connections
```

## 1. What "the direct route" is

`llama.cpp` runs **on the Node C host**, bound to the underlay IP `10.10.0.13:8080`
(D5). Agent VMs live on their node's guest bridge (`10.20.x.0/24`), get SNATed
onto the underlay, and reach Node C over the **plain underlay** — no VXLAN, no
Geneve, no OVN logical switch. That is the direct path, and it is frozen by D2.

The overlay path (`ovn`) is the fallback: traffic would instead traverse an OVN
logical router carried over Geneve (UDP 6081), which costs 50 bytes of outer
headers per packet and therefore has a **smaller path MTU than the underlay**.

| Path | Carrier | Encap overhead | Path MTU (on-prem LAN) | Path MTU (GCP VPC) |
|------|---------|----------------|------------------------|--------------------|
| `direct` | underlay L3 route | **0 B** | **1500** | **1460** |
| `ovn` | VXLAN/Geneve overlay | **50 B** = IPv4 20 + UDP 8 + Geneve/VXLAN 8 + inner Eth 14 | **1450** | **1410** |

The GCP figure is the VPC default MTU of **1460** (Google Cloud, *Maximum
transmission unit* — https://docs.cloud.google.com/vpc/docs/mtu); the demo runs on
that VPC, so on the demo footprint the direct path is 1460 and the overlay path is
1410. The lab in §5 runs on native Ethernet and therefore measures 1500 / 1450;
the arithmetic (50-byte walk-off) is identical.

**Why it matters.** If an endpoint believes the path MTU is 1500 but the overlay
can only carry 1450, every full-size segment (an agent prompt, a streamed
response) is dropped in the tunnel. When ICMP `fragmentation-needed` is filtered
— common in cloud networks and in the overlay itself — the sender never learns,
and the prompt **stalls until it times out**. That is the on-stage failure this
card removes.

## 2. The toggle (`--direct` / `--ovn`)

Two independent switches, highest precedence first:

1. **Per-process env var** — `TERMINI_INFERENCE_PATH=direct|ovn` (wins over the file,
   ideal for a one-off `source` before the demo).
2. **Persisted config** — `/etc/termini/network.json` field `.active`
   (`sudo infra/net/direct-route.sh select ovn`).

```bash
infra/net/direct-route.sh status      # active path, base URL, both path MTUs, iface MTU
infra/net/direct-route.sh env         # export TERMINI_INFERENCE_PATH / INFERENCE_BASE_URL
infra/net/direct-route.sh select ovn  # persist the fallback
TERMINI_INFERENCE_PATH=direct infra/net/direct-route.sh env   # one-shot override
```

`INFERENCE_BASE_URL` (what the controller hands an agent VM, D2) is derived from
the active path: `http://10.10.0.13:8080` in the demo. Because D2 requires every
cross-node service to be reached by its **underlay** address, both paths use the
same URL — the toggle changes the **route** (egress device and therefore the path
MTU), not the address the agent talks to. `TERMINI_MTU_PROFILE=onprem|gcp`
selects the MTU profile if a node is not the default.

## 3. Provisioning (`direct-route.sh`)

| Command | Effect | Needs root |
|---------|--------|-----------|
| `init` | install `infra/net/network.example.json` → `/etc/termini/network.json` | yes |
| `up` | `ip route replace 10.10.0.13/32 dev <iface> [via <nh>]` and clamp `lxdbr0` MTU to the path MTU | yes |
| `down` | remove that route | yes |
| `status` | print active path / profile / URL / both MTUs and the live NIC MTU | no |
| `env` | print the `export` lines for the demo shell | no |
| `select <direct\|ovn>` | persist `.active` | yes when `/etc/termini/network.json` is root-owned |
| `verify` | run the three acceptance probes below | no |

Add `--dry-run` to `up`/`down`/`init` to print the exact commands without applying
them. The route is idempotent (`ip route replace`) and scoped to the inference
host `/32`, so it cannot capture other traffic. The firewall side is already
covered by the D2 port map (`tcp/8080` from `10.10.0.0/24` only, terraform
`google_compute_firewall.inference`).

## 4. Verification

**DF-bit path-MTU probe** — `mtu-probe.sh` sends ICMP with `Don't Fragment` set, so
an oversized packet is dropped rather than silently fragmented:

```bash
infra/net/mtu-probe.sh --path direct --expect pass   # payload 1472 (MTU 1500)
infra/net/mtu-probe.sh --path ovn    --mtu 1500 --expect fail   # same payload, overlay
infra/net/mtu-probe.sh --path ovn    --mtu 1450 --expect pass   # tunnel size
infra/net/mtu-probe.sh --path ovn --sweep            # binary-search the real path MTU
```

`direct-route.sh verify` runs the acceptance triple: **direct passes at the
direct-path MTU, the overlay fails at that MTU, and the overlay passes at its own
(tunnel-size) MTU** — which is what proves the direct path is genuinely bypassing
the overlay rather than being a second name for it. Exit status is 0 only when all
three match.

**Sustained streaming load** — `inference-loadtest.sh` drives repeated
`POST /v1/chat/completions` (`stream: true`, SSE) with a large prompt body for the
whole window and reports first-token latency, inter-token jitter, and the number
of **dropped connections**. It exits non-zero if any connection dropped.

```bash
infra/net/inference-loadtest.sh --path direct --duration 180 --prompt-bytes 65536 --out report.json
```

Acceptance: **zero dropped connections** and stable latency. A request cut short
only because the load window ended is reported separately as `aborted` and is not
a drop.

## 5. Reproducible lab (`lab/overlay-vs-direct-lab.sh`)

The demo nodes are provisioned by `infra/gcp`, but the MTU behaviour can be proven
without them on any Linux host with `sudo`:

```bash
sudo infra/net/lab/overlay-vs-direct-lab.sh --out infra/net/evidence
sudo infra/net/lab/overlay-vs-direct-lab.sh --quick     # ~60 s smoke version
```

It builds three topologies in network namespaces and runs the card's acceptance:

1. **direct** — two endpoints on a routed veth at the underlay MTU (1500).
2. **overlay** — the same endpoints joined by a **real VXLAN tunnel** whose device
   MTU the kernel derives as 1450 (1500 − 50).
3. **black hole** — endpoints at 1500, a middle hop limited to 1450, and ICMP
   `fragmentation-needed` filtered on the client: the exact stall an overlay MTU
   mismatch causes, and the reason the direct route is required.

It then runs the four probes, a PMTU sweep on each path, a 3-minute sustained
streaming load on the direct path, a shorter overlay load, and a black-hole load
showing the dropped connections the direct path avoids. Anything unexpected
fails the run.

### Evidence

Committed under `infra/net/evidence/` from a real run of the script above:

- `lab-run-<ts>.txt` — full stdout, including the interface MTUs, the 50-byte
  observed encap overhead, and the pass/fail summary.
- `sweep-<ts>.txt` — binary-search path-MTU sweeps: **1500 direct, 1450 overlay**.
- `loadtest-direct-<ts>.json`, `loadtest-overlay-<ts>.json`,
  `loadtest-direct-control-<ts>.json`, `loadtest-blackhole-<ts>.json` — the load
  reports (dropped-connection counts and latency percentiles).

The committed run result:

| Check | Result |
|-------|--------|
| DF probe direct @ 1500 | pass |
| DF probe overlay @ 1500 | **fail** (proves the overlay path is a different, smaller-MTU path) |
| DF probe overlay @ 1450 | pass |
| PMTU sweep | direct **1500**, overlay **1450** |
| Direct load, 180 s, 2×64 KB prompts | **334/334 completed, 0 dropped**, first-token p50 2.23 ms / p95 4.92 ms |
| Overlay load, 60 s | 114/114 completed, 0 dropped (correctly sized) |
| Black-hole overlay load, 30 s | **6 dropped** — the stall the direct route removes |
| Direct-path control, 30 s | 60/60 completed, 0 dropped |

## 6. Fallback runbook (demo day)

| Symptom | Do this |
|---------|---------|
| Direct path misbehaves (prompts stall, connection errors) | `TERMINI_INFERENCE_PATH=ovn` in the demo shell, or `direct-route.sh select ovn`; then `inference-loadtest.sh --path ovn --duration 30` to confirm |
| Direct path MTU looks wrong after a host change | `direct-route.sh verify`; if the direct probe no longer passes, `direct-route.sh up` to re-pin the route, then re-verify |
| Suspect an overlay MTU regression | `mtu-probe.sh --path ovn --sweep` — a path MTU below 1450 (on-prem) / 1410 (GCP) means the overlay needs its tunnel MTU corrected |
| Need to prove the path on stage | `direct-route.sh verify` — three lines, ends in `VERIFY: PASS` |

Switch back to `direct` (the default) once the overlay is healthy again; it is the
frozen demo path.

## 7. Honest limitations

- `direct-route.sh up`/`down` are validated by `--dry-run` and by the lab, **not**
  against a live 3-node cluster — the GCP footprint (`infra/gcp`) is not
  provisioned in this environment, and no OVN overlay exists to exercise the `ovn`
  leg end to end. The `ovn` path is modelled (VXLAN; Geneve adds the same 8-byte
  header) rather than run against OVS/OVN.
- No `llama-server`/GGUF is present on this host, so the load test runs against
  `lab/mock_llama_server.py`, which speaks the real OpenAI SSE wire protocol and
  reads the full request body before responding (so a stalled upload is visible as
  a dropped connection). Pointing `inference-loadtest.sh` at the baked
  `llama-server` requires no change.
- The lab needs root (namespaces, veth, VXLAN, iptables) and Linux; it is
  destructive only to its own `tnlab-*` namespaces.
