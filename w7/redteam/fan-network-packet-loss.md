# Failure mode 2 — fan-network packet loss / latency (W7 red-team, t_8d243ee1)

**Verdict: BREAK-THE-DEMO, reproducible — (a) a bulk cross-node transfer stalls
completely under 30 % packet loss, and (b) the control plane's latency health
signal is inert: the node reports a hardcoded `0.3 ms` regardless of the real
path, so `latency-01` can never fire and fan-network degradation is undetectable.**

| | |
|---|---|
| Harness | `net/net_chaos.py`, `net/traffic_probe.py`, `net/service_probe.py`, `net/env_up.sh` |
| Raw evidence | `evidence/net/{baseline,latency,loss,kill,agent-constants}/` |
| Underlay | dedicated 3-node set `rt-net-a/b/c` on `lxdbr0` + private controller (isolated from the shared demo) |

## Reliability budget used

- `controller/app/health.py` (dashboard.md §1.1): `DEGRADED_AFTER_S = 15`, `OFFLINE_AFTER_S = 45`.
- `controller/app/operator/detect.py`: `latency-01` fires on `net[].latency_ms ≥ 250 ms` for `latency_consecutive = 3` samples.
- `docs/contracts/agent-runtime.md`: inter-node service call — connect timeout 2 s, ≤ 2 retries with jitter.
- Demo timeout (W6 warm layer): 30 s.

## Method

```bash
bash    w7/redteam/net/env_up.sh evidence/net/env            # 3 dedicated nodes + private controller
python3 w7/redteam/net/net_chaos.py --scenario baseline --env evidence/net/env/env.json --out evidence/net/baseline
python3 w7/redteam/net/net_chaos.py --scenario  latency --env evidence/net/env/env.json --out evidence/net/latency   # tc netem delay 300ms 50ms
python3 w7/redteam/net/net_chaos.py --scenario     loss --env evidence/net/env/env.json --out evidence/net/loss      # tc netem loss 30%
python3 w7/redteam/net/net_chaos.py --scenario     kill --env evidence/net/env/env.json --out evidence/net/kill      # stop node C mid-stream
```

`ping -f` (raw loss/RTT), a 64 MB TCP transfer A→C, and a real inter-node call
(node A → host-exposed `llama.cpp` on node C, per agent-runtime.md, with the
documented retry policy). Every `tc` qdisc is removed in a `finally` block.

## Results

| scenario | ping loss | ping RTT avg | TCP 64 MB A→C | service call | telemetry `net[].latency_ms` |
|---|---:|---:|---|---|---|
| baseline | 0.0 % (3000/3000) | 0.008–0.010 ms | **0.033 s, 16 184 Mbps, completed** | 20/20 ok, 0 retried, mean 5.51 s | 0.3 |
| latency (delay 300 ms ± 50) | 0.0 % (600/600) | **298.6 ms** | — | 5/5 ok, 0 retried, mean **8.75 s** | **0.3** |
| loss (30 %) | **31.2 %** (2064/3000) | 0.08 ms | **STALLED: 2 003 672 / 67 108 864 bytes (3.0 %) in 45 s; client did not finish in 70 s** | 15/15 ok, 0 retried, mean 8.52 s | **0.3** |
| kill (stop C mid-stream) | — | — | — | stream cut at 20 chunks; client exited **2.39 s** after kill | offline detected **44.41 s**; recover **40.5 s** |

Raw excerpts:

```
# baseline (evidence/net/baseline/summary.json)
ping a->c: 3000 packets transmitted, 3000 received, 0% packet loss, rtt min/avg/max/mdev = 0.005/0.008/0.155/0.005 ms
tcp a->c: {"sent_bytes":67108864,"elapsed_s":0.033,"mbps":16184.05,"completed":true}

# latency (evidence/net/latency/summary.json)
netem: delay 300ms 50ms distribution normal
ping a->c: 600 packets transmitted, 600 received, 0% packet loss, rtt ... = 127.292/298.613/452.003/49.754 ms
telemetry_net_latency_ms: {"a":[0.3,0.3,0.3,0.3,0.3,0.3],"c":[0.3,0.3,0.3,0.3,0.3,0.3]}

# loss (evidence/net/loss/summary.json)
netem: loss 30%
ping a->c: 3000 packets transmitted, 2064 received, 31.2% packet loss, time 11929ms
tcp_a_to_c: {"stalled":true,"client_exec_error":"client exec timed out after 70s...",
             "server":{"role":"server","port":9099,"connections":1,"bytes":2003672}}
```

## Findings

### G1 — [CRITICAL] The control plane's latency health signal is a hardcoded constant

The node agent (`/usr/local/bin/termini-bootstrap`, a Python script baked into the
node image) builds its telemetry facts with literal values — raw
`evidence/net/agent-constants/node-agent-telemetry-facts.txt`:

```python
"net_facts":  return [{"iface": ifname, "rx_bytes": rx, "tx_bytes": tx, "latency_ms": 0.3}]
"disk_facts": {"path": dev, "total_bytes": total, "free_bytes": free,
               "write_latency_ms": 0.5, "mount_ro": False}
```

Only `rx_bytes`/`tx_bytes` are live; `latency_ms` and `write_latency_ms` are literals.
Consequently, with a **real RTT of 298.6 ms** injected by `tc netem`, the node still
reported `net[].latency_ms = 0.3` on every sample (latency *and* loss runs).
`detect.py::latency-01` thresholds `net[].latency_ms ≥ 250 ms` for 3 samples, so it
**can never fire from real network degradation** — the operator is structurally blind
to the fan-network failure mode. **Severity: CRITICAL.**

### G2 — [HIGH / reproducible break-the-demo] Bulk cross-node transfer stalls under 30 % loss

Under `tc netem loss 30%` a 64 MB TCP transfer from node A to node C **never
completed**: the server received **2 003 672 of 67 108 864 bytes (3.0 %)** in its
45 s window, and the client probe could not make progress inside its 70 s outer
timeout (vs **0.033 s** on the clean path). **Repro:**
`python3 w7/redteam/net/net_chaos.py --scenario loss --env …/env.json --out /tmp/loss`.
Any demo step that ships a model/artifact/image (the `project.md` "4 GB model
download" concern) over a lossy fan network will appear to hang. **Severity: HIGH.**

### G3 — [MEDIUM] A node killed mid-stream is detected only at the 45 s edge and the signal is masked

`lxc stop --force` of node C during a streaming call:

- client noticed the cut in **2.39 s** (stream cut after 20 chunks);
- controller marked the node `offline` at **44.41 s** — i.e. at the documented 45 s
  threshold, so detection works but gives the operator essentially no margin;
- the node was **already `degraded`** before the kill (`degraded_reasons: ["gpu_unavailable"]`),
  so the "time-to-degraded 2.64 s" is **not** a network signal — the real state
  change is the 44.41 s `offline`;
- recovery (restart → `ONLINE` + inference healthy) took **40.5 s > 30 s demo budget**.

**Severity: MEDIUM** (detection exists but is late/masked; recovery exceeds the demo budget).

### G4 — [MEDIUM] Operator false positive under latency

In the latency run the only operator event fired was **`disk-degrade-01`**
(`metric: disk_free_bytes`, `value: -6 589 529.04`, threshold slope ≤ −5 000 000 B/s,
`fired_at …11:26:21Z`) — an unrelated disk rule triggered while `latency-01` stayed
silent. The AI operator would alert on the wrong subsystem during a network fault.
**Severity: MEDIUM.**

### G5 — [LOW / positive] Small request/response service calls survive loss and latency

The real inter-node `llama.cpp` call was resilient: 15/15 ok under 30 % loss and
5/5 ok under 300 ms delay, **0 retries** in both (TCP retransmits transparently at
this payload size). Call latency rose 5.51 s → 8.75 s under delay (+59 %) but
stayed inside the client timeout, so the documented retry policy never engaged.
The failure is specific to **bulk** transfers, not to request/response traffic.

## Honesty / environment notes

- Emulated on one host: `rt-net-*` are LXD **containers** on `lxdbr0`; the
  on-prem/GCP 3-member LXD cluster is not provisioned here. The path exercised is
  the same L3 routing the frozen **D2** decision specifies; absolute Mbps numbers
  are host-local and not representative of a real WAN.
- `loss` was re-run after a first attempt whose TCP sub-probe timed out before
  writing its JSON; the re-run (recorded above) captures the partial-transfer
  number from the *server* socket plus the client stall, which is the datum.
- `kill` recovery (40.5 s) includes container restart + bootstrap re-registration,
  not a VM migration.
- `evidence/net/env/controller.db`, `env.json` and `finish.log` are kept out of git
  (single-use bootstrap + project tokens); committed JSON contains request ids only.
