# Evidence — node-side metric expansion (`t_92aac3c8`)

The collector card's local proof: the node daemon's own collector functions run
against **this host** (`hack`, the shared build host — a QEMU VM, not a GCE
instance), with the resulting payload and its C2 self-check committed beside the
tool that produced them.

## Reproduce

```
python3 docs/evidence/2026-10-16-metric-expansion/tools/collect_node_payload.py
```

The tool loads `infra/bootstrap/node/termini-bootstrap.py` itself (no copy, no
stub), builds one real sample with `build_sample()` — real `/proc`, `/sys`,
cgroup hierarchy, systemd and the local LXD — and writes:

| File | What it is |
|------|------------|
| `node-payload.json` | the raw sample exactly as the daemon built it |
| `c2-scan.txt` | contract validator + `governance.scan_c2` + the §4.3 signature grep |
| stdout | the same report, plus which groups were and were not collected |

`net[].latency_ms` is a real measurement: the tool binds a loopback listener and
points `controller_url` at it, so the RTT probe runs against a live socket
instead of the `0.0` first-sample fallback.

## Result (captured 2026-10-16 on this host)

* **validator:** `app.validate.validate_sample(sample)` → `errors: []`
* **C2 scan:** `app.governance.scan_c2(sample)` → `violations: []`
* **§4.3 signature grep:** one hit, `ip_local: raw IPv4: '10.201.0.0/24'` — the
  contract's C1 *transformed* form (last octet replaced with `.0/24`,
  `node-telemetry.md` §4.2), which `scan_c2` exempts by pattern. Nothing else
  matched: no `/proc`, `/sys`, `/etc` or `/var/log` path, no command line, no
  environment, no `exe`/`cwd`, no credential, no raw MAC, no UUID, no
  `nf_conntrack`. (The conntrack *table* is never read; only the two count
  files.)
* **groups collected on this host:** `pressure{}` (4 scalars), `oom_kill_count`,
  `pgmajfault_count`, `mem_available_bytes`, `tcp_retransmit_segs`,
  `conntrack_count`, `conntrack_max`; `disk[]` rows carry
  `io_ticks_ms`/`io_in_flight`/`inode_used`/`inode_total`; `net[]` carries the
  four `rx_errors`/`tx_errors`/`rx_dropped`/`tx_dropped`; all seven running
  `lxd_instances[]` carry `oom_kill_count` + `pgmajfault_count` from their cgroups.

## Honest negatives — groups this host CANNOT produce

Emitted as nothing (no field, not a `0`). The research card flagged these as
`needs a live node` / absent; the exact command that closes each gap on a live
node is named.

| Group | Why not here | Command a live node must run |
|-------|--------------|------------------------------|
| SMART media health (`disk-fail-01` predictor) | `smartctl`/`nvme` not installed; no SATA/NVMe device on this QEMU VM | `smartctl -a /dev/sda` (and `smartctl -a /dev/nvme0n1` / `nvme smart-log /dev/nvme0`) |
| EDAC memory errors (`ce_count`/`ue_count`) | `/sys/devices/system/edac/mc/` has no controller nodes on a VM | `cat /sys/devices/system/edac/mc/mc*/ce_count /sys/devices/system/edac/mc/mc*/ue_count` |
| GCE preemptible / maintenance-event | `metadata.google.internal` does not resolve off-GCE (DMI is `QEMU`) | `curl -sH 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/scheduling/preemptible` |
| OVS/OVN flow counters | `ovs-vsctl`/`ovn-*` not installed — the fleet uses the Linux bridge (`lxdbr0`) | n/a — rejected in the research card, not deferred |

None of these is a `§4.12–§4.14` field: they are the research card's
`SHIP-LATER` rows, so the collector ships nothing for them by design.

## Note on a pre-existing field (not part of this card)

`disk[/dev/root].write_latency_ms` reads `0.0` in the captured payload because
the fsync'd probe cannot write into `/` as the unprivileged probe user and falls
back to the last measured value — the same behaviour §4.3 has had since
`t_159722ab`, and it is *outside* the metric-expansion groups. Unlike the new
fields, `write_latency_ms` is a required numeric in §4.3 that the
`disk-degrade-01` rule consumes, so making it omittable is a contract change, not
a collector fix; recorded here so the residual is visible rather than silent.
