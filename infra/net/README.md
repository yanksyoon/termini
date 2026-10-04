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

## 7. Bulk transfer under packet loss (`bulk-transfer.py`, kanban `t_942dbaf4`)

The W7 red-team found (`w7/redteam/fan-network-packet-loss.md` G2) that a **64 MiB
single-stream TCP transfer stalls under `tc netem loss 30%`** — 2 003 672 /
67 108 864 bytes (3.0 %) in 45 s, client never finishes — while small
request/response calls survive. Any demo step that ships a model, image, or
artifact over the lossy fan network would appear to hang. `infra/net/bulk-transfer.py`
is the fix.

### Two transports

| `--transport` | How it works | Under 30 % loss |
|---|---|---|
| **`udp`** (default) | Reliable **selective repeat**. The file is cut into MTU-sized datagrams; the receiver keeps a bitmap and answers a query with it; the sender retransmits only the gaps, in rounds. Loss is decoupled from throughput — no congestion window to collapse, no RTO backoff. | completes |
| `tcp` (fallback) | Fixed-size chunks, each over its **own short-lived TCP connection** with an app-level ACK and bounded retries. Robust where UDP is filtered; still slow under heavy loss. | usually stalls |

Both modes verify completion (received byte count, optional SHA-256) so success
is never reported on a partial transfer, and both **resume**: the receiver keeps
a per-unit bitmap, so a re-run sends only what is still missing.

### Usage

```bash
# receiver (Node C)
python3 infra/net/bulk-transfer.py server --port 9200 \
    --size 67108864 --out /root/artifact.bin

# sender (Node A) — UDP, verified
python3 infra/net/bulk-transfer.py client --host 10.10.0.13 --port 9200 \
    --file ./artifact.bin --deadline 30 --verify sha256

# TCP fallback
python3 infra/net/bulk-transfer.py client --host 10.10.0.13 --port 9200 \
    --file ./artifact.bin --transport tcp --chunk 65536 --streams 32 --deadline 30
```

### MTU / payload budget

The UDP payload is chosen below the **smallest path MTU the demo can present**.
The overlay path is 1410 B (GCP: 1460 − 50 B encap, §1); `payload ≤ MTU − 20 (IPv4)
− 8 (UDP) − 8 (seq)` ⇒ **≤ 1374 on the overlay, ≤ 1432 on the direct path**. The
default is **1360** (headroom, works on either path); raise it with `--payload`
only when you have proven the path MTU (`mtu-probe.sh`).

### Measured (this host, N runs)

Acceptance harness: `infra/net/verify-bulk-transfer.sh` (applies `tc netem loss`,
runs the transfer N times, records JSON + SHA-256, removes the qdisc on exit).

| Scenario | Result |
|---|---|
| 64 MiB, clean path, UDP | **3/3 completed**, max **1.04 s**, SHA-256 ok |
| 64 MiB, clean path, TCP | **0.13 s** (no regression vs the 0.033 s reference; the delta is host load) |
| 64 MiB, `loss 30%`, UDP | **5/5 completed**, mean **10.3 s**, max **11.5 s**, SHA-256 ok, 9–10 rounds, 1.44× wire overhead |
| 64 MiB, `loss 30%`, TCP | **stalls** (11.8 MB in 62 s) — the G2 finding; use UDP |
| 64 MiB, `loss 30%`, UDP, two attempts (4 s then resume) | attempt 1 partial (46.8 MB); attempt 2 **resumed 34 447 units** and completed, SHA-256 ok |

At ~6 MB/s sustained under 30 % loss, a single UDP attempt fits roughly
**≥128 MiB inside a 30 s demo budget** (conservative); larger payloads still
finish because each attempt is resumable and bounded — the tool never hangs.

### Reproducing the failure itself

The **red-team** harness is unchanged and is what defines the baseline:
`w7/redteam/net/net_chaos.py --scenario loss` (which drives `traffic_probe.py`,
the single-stream probe). Its recorded stall and the pre-fix numbers are in
`w7/redteam/fan-network-packet-loss.md`; the acceptance evidence for the fix is
under `infra/net/evidence/`.

Re-run here on the dedicated `rt-net-*` underlay (**N=3**): ping loss
29.3–30.3 %, the single-stream 64 MiB TCP probe **stalled on all three runs**
(0.38 / 0.46 / 0.44 MB before `BrokenPipeError`, 9–21 s), while the same
harness's 15/15 `llama.cpp` calls stayed OK; the clean-path baseline moved the
full 64 MiB in **0.048 s** (11 229 Mbps). Raw summaries:
`infra/net/evidence/redteam-repro-loss30/`.

## 8. Honest limitations

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
