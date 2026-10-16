# Node telemetry fixtures — metric expansion (§4.12–§4.14)

Raw, **real-shaped** captures — not hand-written samples — of the sources the
metric-expansion collectors read. They were copied verbatim from this fleet's
build host (`hack`, Ubuntu 24.04, kernel 6.8.0-142, LXD-as-snap with the demo
containers running) on 2026-10-16 by the collector card `t_92aac3c8`:

```
cp /proc/pressure/{cpu,io,memory}   tests/fixtures/telemetry/proc/pressure/
cp /proc/meminfo /proc/vmstat       tests/fixtures/telemetry/proc/
cp /proc/net/snmp                   tests/fixtures/telemetry/proc/net/
cp /proc/self/mountinfo             tests/fixtures/telemetry/proc/self/
cp /proc/sys/net/netfilter/nf_conntrack_{count,max} \
                                    tests/fixtures/telemetry/proc/sys/net/netfilter/
cp /sys/class/net/enp5s0/statistics/{rx_bytes,tx_bytes,rx_errors,tx_errors,rx_dropped,tx_dropped} \
                                    tests/fixtures/telemetry/sys/class/net/enp5s0/statistics/
cp /sys/dev/block/8:1/stat          tests/fixtures/telemetry/sys/dev/block/8:1/stat
cp /sys/fs/cgroup/lxc.payload.demo-node-a/memory.{events,stat} \
                                    tests/fixtures/telemetry/sys/fs/cgroup/lxc.payload.demo-node-a/
```

The tree mirrors the real paths (`PROC_ROOT`/`SYS_ROOT`/`CGROUP_ROOT` are module
constants in `infra/bootstrap/node/termini-bootstrap.py`), so
`tests/test_bootstrap_telemetry.py` points the collectors at it and exercises the
real shapes: PSI's two line kinds (`some`/`full`), the two-line `/proc/net/snmp`
label/value table, a 17-field partition `stat`, and an LXD instance cgroup.

Why the whole file rather than the interesting lines: a fixture trimmed to the
keys under test cannot catch a parser that reads the wrong column or a key that
moved, and the committed command above reproduces it byte-for-byte.

Nothing here is C2: these files are counters and device/interface names — the
conntrack **table** (`/proc/net/nf_conntrack`) is deliberately absent, because the
collector never reads it (contract §4.13, `governance.md` §4.1.1).
