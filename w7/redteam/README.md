# W7 red-team — cloud-init boot latency & fan-network packet loss

Owner: kanban **t_8d243ee1** (SRE). Parent: W7 independent verification (t_5f60a4bf).

Adversarial test of the two infrastructure failure modes flagged in
`termini/project.md`:

1. **cloud-init slowness** — *"Installing packages, running system updates, and
   setting up LXD cluster overlays over cloud-init takes 3 to 7 minutes on fresh
   hardware … a live 4 GB model download … will crash the presentation"*.
2. **fan-network packet drops** — *"if the demo setup loses connectivity, or if
   MTU sizes on the host physical network mismatch, cross-node VM traffic
   silently drops packets … routing overhead … can add unpredictable latency"*.

The two per-failure-mode reports are **[`cloud-init-boot-latency.md`](cloud-init-boot-latency.md)**
and **[`fan-network-packet-loss.md`](fan-network-packet-loss.md)**; raw evidence
is under `evidence/`.

> **Resolution (t_ca7fc132, owner infra).** The cloud-init report's F1/F2/F3 are
> fixed in `infra/bootstrap/node/termini-bootstrap.py`: a consumed/invalid token
> (401/403), a DNS failure and a connection-refused now fail fast to terminal
> `REGISTRATION_FAILED` after ≤3 backoff retries (no more silent `REGISTERING`
> hang), and cold boot is within the 30 s demo timeout on an unloaded footprint.
> See [`cloud-init-boot-latency.md` §Resolution](cloud-init-boot-latency.md) and
> the re-run evidence under `evidence/boot/*-fixed/`.

## What was tested, against what

| Failure mode | Target | Harness |
|---|---|---|
| cloud-init boot latency | fresh node → `ONLINE` at the controller, over N≥5 runs, under 5 boot-time degradations | `boot/boot_latency.py` + `boot/fault_proxy.py` |
| the pre-hardening cloud-init | the apt-at-boot path D3 removed (break-the-demo repro) | `boot/legacy_apt_repro.sh` |
| fan-network packet loss | 3-node underlay: ping-flood, TCP throughput, real llama.cpp call; `tc netem` latency/loss; kill-node-mid-stream | `net/net_chaos.py` + `net/traffic_probe.py` + `net/service_probe.py` |

**Reliability budget used** (from the frozen contracts):

- `docs/contracts/node-bootstrap.md` §12.5: `systemctl start termini-bootstrap` →
  `ONLINE` target **< 5 s** (activation-only boot, D3).
- W6 warm layer (`infra/warm`): a snapshot-restored fresh node reaches
  *registered + inference responding* **< 30 s** — used as the **demo timeout**.
- `controller/app/health.py` (dashboard.md §1.1): a node's heartbeat is
  `degraded` after **15 s** and `offline` after **45 s** of no telemetry.
- `controller/app/operator/detect.py`: the latency rule (`latency-01`) fires on
  `net[].latency_ms ≥ 250 ms` for **3 consecutive samples** (demo cadence 1 s).

## How to run

```bash
# cloud-init boot latency (own controller on a free port, own LXD copies)
bash w7/redteam/run.sh /tmp/redteam-evidence

# or individually
python3 w7/redteam/boot/boot_latency.py --scenario all --runs 5 --out /tmp/rt/boot
python3 w7/redteam/boot/boot_latency.py --scenario badtoken --out /tmp/rt/badtoken   # -> terminal REGISTRATION_FAILED
python3 w7/redteam/boot/boot_latency.py --baked --out /tmp/rt/baked                  # test the snapshot's baked daemon
bash    w7/redteam/boot/legacy_apt_repro.sh /tmp/rt/legacy-apt 90
bash    w7/redteam/net/env_up.sh /tmp/rt/net            # dedicated 3-node underlay
python3 w7/redteam/net/net_chaos.py --scenario all --env /tmp/rt/net/env.json --out /tmp/rt/net
```

The boot harness pushes the repo's canonical daemon (`infra/bootstrap/node/termini-bootstrap.py`)
into each node before it starts, so a run verifies the committed code rather than
the copy baked into the snapshot; `--baked` restores the baked-daemon behaviour.

Requirements: `lxc`, `jq`, the `agentic-os/w3` node image (host snapshot
`demo-node-a/unregistered`), and the repo `controller/` (stdlib-only). The boot
harness needs **no** network beyond the host bridge. The net harness needs a GPU-less
host CPU to run the resident `llama.cpp` (as the emulated demo does).

## Environment / honesty notes

- The demo runs **emulated on one host**: `demo-node-a/b/c` are LXD *containers*
  (not VMs) on `lxdbr0` (`10.14.100.0/24`), controller on `10.14.100.1:8085`,
  `llama.cpp` CPU-only on Node C (`:8080`). The on-prem/GCP 3-member LXD cluster
  of `infra-decisions.md` is not provisioned here; the underlay path behaviour
  tested is the same L3 routing the frozen **D2** decision specifies.
- The net harness provisions its **own** `rt-net-a/b/c` nodes + private controller
  (`net/env_up.sh`) so the shared demo nodes (which other workers start/stop) do
  not corrupt a run.
- Every `tc` qdisc is removed in a `finally` block; every ephemeral LXD copy is
  deleted after its run.
