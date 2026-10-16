# Infrastructure metric inventory and telemetry recommendation

Research card `t_cae0dfa8` — architect, 2026-10-16.

**Purpose.** Enumerate every infrastructure metric this host can actually produce,
classify each against `docs/contracts/governance.md` §4 (C0/C1/C2), and reduce it to a
bounded, governed set worth shipping into node telemetry and the AI diagnosis. This
document is the scope source for four follow-up cards (contract+ingest, node collector,
diagnosis, dashboard) — their field names are fixed in §5 and must appear verbatim.

**Method.** Every availability claim below was established by running the command on
this host and capturing the raw output into `docs/research/fixtures/` (one file per
source family, listed in §6). A metric I did not observe is marked
`COULD NOT VERIFY (needs a live node)` — nothing is asserted from memory. "Absent"
claims carry a **positive control**: the same probe that found a source missing is shown
finding a sibling source on the same host, so a zero-hit probe is never assumed to work.

**Host under test.** `hack` — the shared build/demo host, an Ubuntu 24.04.5 VM
(`uname -r` = `6.8.0-142-generic`, 8 logical cores, 20 GiB RAM, no swap). DMI reports
`QEMU` / `Standard PC (Q35 + ICH9, 2009)` — **this is not a GCE instance** (see §1.7).
LXD 5.21.8 LTS runs as a **snap** (`snap.lxd.daemon.service`, not a native `lxd.service`)
and hosts the demo fleet as containers: `demo-node-a/b/c`, `rt-net-a/b/c`, `bsp-verify`.
The GCP fleet is TERMINATED; nothing here raised compute. The node-side collector runs
as root on a real node, but this host's probes ran as the unprivileged `ubuntu` user —
every "denied" below is a *this-host, this-user* observation and is stated as such.

---

## 1. Table 1 — the inventory

One row per candidate metric/source, grouped by source family. Columns: metric; what it
measures; exact source; availability on this host (command + fixture reference); collection
cost; volatility; C class. `C0/C1/C2` are `governance.md` §4; *undecidable → C2*.

### 1.1 `/proc` family

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| CPU time breakdown | user/nice/system/idle/iowait/irq/softirq/steal, aggregate + per-core | `/proc/stat` line 1 (field 2..11) | OK — `head -n3 /proc/stat` → `cpu 23485375 30499 9629463 851348373 7264530 0 1527252 746832 0 0` (fixture `proc-probe.txt`) | one file read | per-sample | C0 |
| Load average (1/5/15 m) | runnable + uninterruptible tasks | `/proc/loadavg` fields 1–3 | OK — `3.84 2.72 1.84 6/1044 98373` | one file read | per-sample | C0 (1 m already ships; 5/15 m are candidates) |
| Memory totals | MemTotal/Free/Available/Buffers/Cached/Active/Inactive | `/proc/meminfo` | OK — `MemTotal: 20457208 kB`, `MemAvailable: 2092816 kB` | one file read | per-sample | C0 (`MemAvailable` is the honest "free" — candidate, see §2) |
| **PSI (pressure stall)** | % time tasks stalled on CPU / IO / memory, some vs full | `/proc/pressure/{cpu,io,memory}` (`some`/`full` × `avg10/60/300`) | OK — `cpu some avg10=4.86`, `io some avg10=0.39`, `memory some avg10=0.00 full avg10=0.00` | three file reads | per-sample | C0 |
| **OOM kills** | node-wide processes killed by the OOM killer | `/proc/vmstat` key `oom_kill` | OK — `oom_kill 37` (fixture `vmstat-keys-probe.txt`) | one file read | cumulative counter | C0 |
| **Major page faults** | page faults needing disk I/O (thrashing) | `/proc/vmstat` key `pgmajfault` | OK — `pgmajfault 2647724` | one file read | cumulative counter | C0 |
| VM pressure extras | `pswpin/pswpout` (swap), `pgpgin/pgpgout`, `compact_stall/compact_fail`, `nr_dirty/nr_writeback` | `/proc/vmstat` | OK — `pswpin 0`, `compact_stall 1360`, `compact_fail 1280` | one file read | cumulative | C0 (compaction = SHIP-LATER) |
| Per-device I/O | reads/writes/sectors/ticks per block device | `/proc/diskstats` (or `/sys/block/<dev>/stat`) | OK — `loop0 …`, `sda` via `/sys/block/sda/stat` → `… 2098074812 … 4035234316 … 50829476 …` (fixture `sys-probe.txt`) | one file read | cumulative counter | C0 |
| NIC byte/packet/err/drop | per-interface rx/tx counters | `/proc/net/dev` | OK — `enp5s0: 49765821675 67456739 0 0 0 0 …` | one file read | cumulative counter | C0 |
| TCP/IP/UDP SNMP counters | `RetransSegs`, `CurrEstab`, `ActiveOpens`, `InErrors` … | `/proc/net/snmp` (Tcp/Ip/Udp rows) | OK — `Tcp: … RetransSegs 295393 … CurrEstab 28` | one file read | cumulative counter | C0 |
| TCP extended counters | `TCPLostRetransmit`, `TCPFastRetrans`, `TCPSynRetrans` … | `/proc/net/netstat` `TcpExt` row | OK — labels present (`proc-probe.txt`); values on the second line | one file read | cumulative counter | C0 |
| Per-CPU scheduler stats | runqueue/waittime per cpu | `/proc/schedstat` | OK — `version 15 … cpu0 0 0 0 …` | one file read | per-sample, high-cardinality | C0 (REJECT — see §3) |
| IRQ / softirq counts | per-CPU interrupt/softirq counters | `/proc/interrupts`, `/proc/softirqs` | OK — 8-column per-CPU tables | one file read each | per-sample, high-cardinality | C0 (REJECT — §3) |
| Buddy allocator fragmentation | free blocks per order per zone | `/proc/buddyinfo` | OK — `Node 0, zone Normal 807 1453 1985 …` | one file read | slowly-moving | C0 (REJECT — §3) |
| Zone memory detail | per-zone anon/file/page counters | `/proc/zoneinfo` | OK — `Node 0, zone DMA …` | one file read | slowly-moving | C0 (REJECT — §3) |
| Page-type fragmentation | per-migratetype free page counts | `/proc/pagetypeinfo` | **DENIED** — `Permission denied` (needs root); positive control: `/proc/buddyinfo` (sibling) is readable | — | slowly-moving | C0 (REJECT — §3) |
| Swap devices | swap file/partition usage | `/proc/swaps` | OK — empty (no swap configured on this host) | one file read | slowly-moving | C0 (already ships `swap_used_bytes`) |
| Uptime | seconds up + idle | `/proc/uptime` | OK — `1123080.33 8513483.91` | one file read | per-sample | C0 (already ships `uptime_sec`) |
| Crypto capabilities | cipher/driver list | `/proc/crypto` | OK — `curve25519 …` | one file read | static | C0 (REJECT — no diagnostic value) |
| Open file descriptors | system-wide allocated/unused/max fds | `/proc/sys/fs/file-nr` | OK — `7424 0 9223372036854775807` (`/proc/fs/file-nr` does NOT exist) | one file read | slowly-moving | C0 (SHIP-LATER — §3) |
| Loaded modules | kernel module list | `/proc/modules` | OK | one file read | static | C1 (module names can fingerprint a build; no diagnostic value — REJECT) |
| CPU identity | model / cores / flags | `/proc/cpuinfo` | OK — `12th Gen Intel(R) Core(TM) i7-1260P`, 8 siblings | one file read | static | C0 (already ships `cpu_core_count`) |
| md RAID state | assembled arrays / personalities | `/proc/mdstat` | OK — `Personalities: [raid0] [raid1] …`, no active arrays | one file read | slowly-moving | C0 (REJECT — no md on this fleet) |

### 1.2 `/sys` family

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| **Per-device queue/IO stats** | reads/writes/sectors, ms spent in I/O, in-flight ops | `/sys/block/<dev>/stat` (fields 1,3,5,7,9,10) | OK — `/sys/block/sda/stat` → `… sectors_read 2098074812 … sectors_written 4035234316 … ios_in_progress 0 … ms_doing_io 50829476` | one file read/device | cumulative counter | C0 |
| Block devices | device inventory | `/sys/block/` | OK — `sda` + `loop0..7` | one dir read | static | C0 |
| NVMe SMART/health | media errors, wear, temp | `/sys/class/nvme/` | **ABSENT** — no NVMe device; `nvme`/`smartctl` CLIs both ABSENT (`cloud-probe.txt`) | — | slowly-moving | C0 (SHIP-LATER — needs NVMe + live node) |
| Thermal zones | CPU/board temperature | `/sys/class/thermal/thermal_zone*/` | **ABSENT** — only `cooling_device0..7`, no `thermal_zone*`; positive control: `cooling_device*` entries DO list | — | per-sample | C0 (already ships `cpu_temp_celsius`, omitted when no sensor) |
| EDAC memory errors | ECC corrected/uncorrected counts | `/sys/devices/system/edac/mc/mc*/ce_count,ue_count` | **EMPTY** — `edac/mc/` exists but has no `mc0/mc1` controller nodes | — | cumulative | C0 (SHIP-LATER — needs ECC hardware) |
| Power/energy (RAPL) | CPU/DRAM energy in µJ | `/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj` | **ABSENT** — no `intel-rapl` (VM) | — | cumulative | C0 (REJECT — no RAPL on a VM) |
| **NIC error/drop counters** | per-interface `rx_errors/tx_errors/rx_dropped/tx_dropped` … | `/sys/class/net/<iface>/statistics/*` | OK — `enp5s0 rx_errors: 0 tx_errors: 0 rx_dropped: 0 tx_dropped: 0 …` (all zero on this host) | one file read/counter | cumulative counter | C0 |
| RAID state | md arrays via sysfs | `/sys/block/md*` | **ABSENT** — no md devices | — | slowly-moving | C0 (REJECT — no md) |
| **Mount usage** | per-filesystem used/free bytes | `statvfs` / `df` (`/dev/sda1 96G 56G 41G 58%`) | OK | one syscall per mount | slowly-moving | C0 (already ships `disk[]`) |
| **Inode usage** | inodes used/total per filesystem | `statvfs` `f_files`/`f_ffree` (`df -i`: `sda1 IUsed 1127654, IFree 11848474`) | OK | one syscall per mount | slowly-moving | C0 (SHIP — §2) |

### 1.3 cgroup v2

Unified hierarchy (`cgroup2 on /sys/fs/cgroup`). Self cgroup = the worker's systemd scope;
LXD instances live under `/sys/fs/cgroup/lxc.payload.<name>/`.

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| cgroup memory usage | current memory of a cgroup | `memory.current` | OK — self `419835904`; `demo-node-a` `224927744`; `demo-node-c` `6710243328` | one file read | per-sample | C0 |
| cgroup memory stat | anon/file/slab + `pgfault`/`pgmajfault` | `memory.stat` | OK — self `pgmajfault 735`; `demo-node-a` `pgmajfault 103032` | one file read | cumulative | C0 |
| **cgroup OOM events** | `oom` / `oom_kill` / `oom_group_kill` + `max` (limit hit) | `memory.events` | OK — self `oom 0 oom_kill 0`; `demo-node-a` `max 32932 oom 0 oom_kill 0` | one file read | cumulative counter | C0 |
| cgroup CPU usage | `usage_usec`, user/system split | `cpu.stat` | OK — `demo-node-a usage_usec 4163450661` | one file read | cumulative | C0 |
| **cgroup CPU throttling** | `nr_periods/nr_throttled/throttled_usec` | `cpu.stat` | OK — `demo-node-a nr_periods 0 nr_throttled 0 throttled_usec 0` (no throttling: quota not exhausted) | one file read | cumulative | C0 (SHIP-LATER — §3) |
| cgroup I/O | per-device `rbytes/wbytes/rios/wios` | `io.stat` | **Present on LXD cgroups, absent on the user slice** — `demo-node-a io.stat: 8:0 rbytes=14272143360 wbytes=12659527680 …`; self `io.stat` does not exist (io controller not enabled on the user slice) | one file read | cumulative | C0 |
| cgroup I/O PSI | io pressure per cgroup | `io.pressure` | OK — `demo-node-a some avg10=0.00 …` | one file read | per-sample | C0 |
| cgroup pids | current pid count + limit events | `pids.current`, `pids.events`, `pids.max` | OK — `demo-node-a pids.current 91 pids.events max 0` | one file read | per-sample | C0 |
| CPU quota | `cpu.max` (quota/period) | `cpu.max` | OK — `demo-node-a max 100000` | one file read | static | C0 |
| Memory limit | `memory.max` | `memory.max` | OK — `demo-node-a 1073741824` (= `limits.memory 1GiB`) | one file read | static | C0 (already ships `ram_limit_bytes`) |

### 1.4 LXD API

`lxc query` over the local LXD socket (`/var/snap/lxd/common/lxd/unix.socket`).

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| Instance state | `status`/`status_code`/`state.processes` | `/1.0/instances/<name>?recursion=1` → `state` | OK — `state.status Running`, `status_code 103`, `processes 91` | one LXD call (subprocess) | per-sample | C0 |
| Instance CPU | `state.cpu.usage` (ns) | same | OK — `state.cpu.usage` | one LXD call | per-sample | C0 (already ships `cpu_util_pct` derived from this) |
| Instance memory | `state.memory.usage/total/swap_usage` | same | OK — `state.memory.usage`, `total`, `swap_usage`, `swap_usage_peak`, `usage_peak` | one LXD call | per-sample | C0 (already ships `ram_used_bytes`) |
| Instance disk | `state.disk` per volume | same | OK — `root {total:0, usage:0}` (dir driver reports 0), `scratch {total:2147483648, usage:0}` | one LXD call | slowly-moving | C0 (already ships `disk_used_bytes`, 0 on dir) |
| Instance network | `state.network` per NIC | same | OK — keys `eth0, lo, lxdbr0` | one LXD call | per-sample | C0 (already ships `net[]`) |
| **`state.started_at`** | seconds since instance entered current state | `state.started_at` | **ABSENT** — `started_at present: False`; positive control: the same probe found `state.processes 91`, `state.cpu.usage`, `state.memory.usage`, `state.status Running` (fixture `lxd-api-probe.txt`) | — | per-sample | C0 (absent — confirmed; the contract already says `state_age_sec` is omitted when LXD cannot say) |
| Per-instance limits | `limits.memory`, `limits.cpu` | `expanded_config` | OK — `limits.memory 1GiB`, `limits.cpu 2` | one LXD call | static | C0 (already ships `ram_limit_bytes`) |
| Instance events | start/stop/error lifecycle events | `/1.0/events` (`lxc monitor --type=instance`) | OK — endpoint works (no event fired in a 4 s window) | long-lived socket | event-driven | C0 (SHIP-LATER — §3) |
| Storage pool usage | pool bytes + inodes used/total | `/1.0/storage-pools/<pool>/resources` | OK — `space total 102888095744 used 59390136320`; `inodes total 12976128 used 1127663` | one LXD call | slowly-moving | C0 |
| **Instance `config` object** | `cloud-init.user-data` (contains an **enrollment token**, file paths, `project_id`), `volatile.eth0.hwaddr` (raw MAC), `volatile.uuid` (UUID) | `/1.0/instances/<name>` → `config` / `expanded_config` | **PRESENT AND C2** — the raw object carries `term-demo-boot-a`, `00:16:3e:44:f8:98`, `f1de7de0-…` (fixture `lxd-api-probe.txt`) | — | — | **C2 — never egress** (headline result, §3) |
| Node resource inventory | sockets/cores/memory/gpu | `/1.0/resources` | OK — cpu sockets, `total 8`, gpu `virtio-pci` (no NVIDIA) | one LXD call | static | C0 (REJECT for telemetry — already derivable from shipped fields) |

### 1.5 systemd

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| Unit restart count | `NRestarts` | `systemctl show <unit> -p NRestarts` | OK — `lxd` → `NRestarts=0` | one subprocess (batch all props in one call) | slowly-moving | C0 (already ships `services[].restart_count`) |
| Unit exit status | `ExecMainStatus` | `systemctl show -p ExecMainStatus` | OK — `lxd` → `ExecMainStatus=0` | one subprocess | slowly-moving | C0 (already ships `services[].last_exit_code`) |
| Unit active-since | `ActiveEnterTimestamp` | `systemctl show -p ActiveEnterTimestamp` | OK — running unit returns a timestamp; `lxd.service` (dead) returns empty | one subprocess | slowly-moving | C0 (already ships `services[].state_age_sec`) |
| **Unit cgroup accounting** | `MemoryCurrent`, `CPUUsageNSec`, `IOReadBytes`, `TasksCurrent` | `systemctl show -p MemoryCurrent …` | **Populates only when the unit runs** — `lxd.service` (inactive/dead) → `MemoryCurrent=[not set]`, `CPUUsageNSec=[not set]`; **positive control**: `snap.lxd.daemon` (running) → `MemoryCurrent=315392 CPUUsageNSec=996417000` (fixture `systemd-control-probe.txt`). `IOReadBytes` was `[not set]` even for the running unit (IO accounting off by default) | one subprocess | per-sample | C0 |
| Failed units | units in failed state | `systemctl --failed` | OK — empty (no failed units) | one subprocess | slowly-moving | C0 (REJECT — already implied by `services[]`) |
| Boot timing | kernel + userspace boot duration | `systemd-analyze` | OK — `10.547s` total | one subprocess | static per boot | C0 (REJECT — a boot-time one-shot, not per-sample telemetry) |
| Journald rate | `RuntimeMaxUse`/`SystemMaxUse` | `systemctl show systemd-journald -p …` | **ABSENT** — `[not set]` (defaults; not a live rate counter) | one subprocess | static | C0 (REJECT — §3) |
| Journal entry counts | err/warn entries per unit per window | `journalctl -u <unit> --since=-5m -p err \| wc -l` | OK — `snap.lxd.daemon` → `1` (exit 0, readable unprivileged) | one subprocess (cache with a refresh interval) | per-sample | C0 (already ships `services[].journal_errors/warnings`, §4.11) |

### 1.6 Network fabric

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| **Conntrack count** | active connection-tracked flows | `/proc/sys/net/netfilter/nf_conntrack_count` | OK — `854` (readable unprivileged) | one file read | per-sample | **C0 with a C-class caveat — see §3** |
| Conntrack max | table capacity | `/proc/sys/net/netfilter/nf_conntrack_max` | OK — `262144` | one file read | static | C0 |
| Conntrack *table* | per-flow 5-tuples (IPs, ports) | `/proc/net/nf_conntrack` | readable only as root; **contents are C1/C2** | — | per-sample | **C2 — never egress the table, only the count** |
| Socket summary | per-protocol socket counts | `ss -s` | OK — `TCP: 250 (estab 22, closed 215, timewait 181)` | one subprocess | per-sample | C0 (SHIP-LATER — low value, §3) |
| Interface MTU | per-iface MTU + flags | `ip -o link show` | OK — `enp5s0 mtu 1442`, `tailscale0 mtu 1280`, `lxdbr0 mtu 1500` | one subprocess | static | C0 (REJECT — static config, not a fault signal) |
| DNS resolver | active resolver + latency | `resolvectl status` (systemd-resolved) | OK — DNS server `10.200.0.1`, domain `lxd` | one subprocess | slowly-moving | C0 (SHIP-LATER — §3; latency needs a target) |
| NTP drift / sync | clock sync state, offset | `timedatectl` (`chronyc` ABSENT) | OK — `System clock synchronized: yes`, `NTP service: active` | one subprocess | slowly-moving | C0 (SHIP-LATER — §3) |
| OVS/OVN flow counters | `ovs-vsctl`, `ovn-nbctl/sbctl` flow/session counters | `ovs-vsctl`, `ovn-*` | **ABSENT** — binaries not installed; the fleet uses the Linux bridge (`lxdbr0`), not OVS | — | — | C0 (REJECT — §3) |

### 1.7 Cloud layer (GCE)

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| Instance id / zone / machine type | GCE instance identity | `metadata.google.internal/computeMetadata/v1/instance/{id,zone,machine-type}` | **NOT REACHABLE** — `getent hosts metadata.google.internal` → exit 2 (no resolution); DMI is `QEMU`, not Google. Positive control: the host's own resolver works (`resolvectl` → `10.200.0.1`) | HTTP GET (only on a GCE node) | static | C1 (identifiers) — SHIP-LATER, needs live window |
| Preemptible / spot signal | `instance/scheduling/preemptible` | same host | NOT REACHABLE here; GCE-only | HTTP GET | static | C0 — SHIP-LATER, needs live window |
| Live-migration signal | `maintenance-event` | `metadata.google.internal/.../maintenance-event` | NOT REACHABLE here; GCE-only | HTTP GET | event-driven | C0 — SHIP-LATER, needs live window |
| Cloud Monitoring / Ops agent | agent presence | `google-cloud-ops-agent`, `stackdriver-agent`, `google-guest-agent` units | **ABSENT** — no Google units, no Google dpkg packages (fixture `cloud-probe.txt`) | — | — | n/a (agent absent) — REJECT, see §3 |
| Disk/PD performance limits | GCE PD IOPS/throughput caps | Cloud Monitoring API | **NOT REACHABLE** — this is a QEMU VM with Private Google Access OFF-equivalent (no metadata/API path); a GCE node could query the API only with the monitoring scope | API call | static | C1 — REJECT (unreachable + no diagnostic value for the 4 frozen rules) |

### 1.8 Kernel / observability

| Metric | Measures | Exact source | Availability (this host) | Cost | Volatility | Class |
|---|---|---|---|---|---|---|
| Recent OOM / `dmesg` events (counts) | count of OOM/hung-task lines | `dmesg \| grep -c` | **DENIED unprivileged** — `dmesg_restrict=1`, `dmesg` exit 1; positive control: `journalctl` (same host) returned a count (`1`), so "denied" is about `dmesg`, not a broken instrument. The node daemon runs as root, so this is obtainable *on the node*, but it is **superseded** by the cgroup `memory.events oom_kill` (§1.3) which is cleaner and per-cgroup | root-only | cumulative | counts = C0, **message text = C2** (never the text) |
| Hung-task / soft-lockup counters | watchdog timeouts | `dmesg \| grep -c 'hung_task\|soft lockup'` | DENIED unprivileged (same as above) | root-only | cumulative | C0 counts (REJECT — superseded) |
| `kdump` state | crash-dump service armed? | `/sys/kernel/kexec_crash_loaded`, `systemctl is-active kdump` | OK — `0`, `inactive` (kdump not configured) | one file read | static | C0 (REJECT — §3: a crash-dump facility, not a metric) |
| eBPF / tracepoints | `bpftrace -l` probe list | `bpftrace -l` | **root-only** — `ERROR: bpftrace currently only supports running as the root user`; `kernel.unprivileged_bpf_disabled=2` | root-only, expensive | — | **C2** — tracepoint output is function names/args/paths (REJECT — §3) |
| Kernel pointer visibility | `kptr_restrict` | `/proc/sys/kernel/kptr_restrict` | OK — `1` | one file read | static | C0 (context, not a metric) |

---

## 2. Table 2 — the recommendation (ranked)

What is already shipped is **not** re-proposed: host scalars (`cpu_util_pct`, `ram_total/used`,
`swap_used`, `load_avg_1m`, `cpu_temp_celsius`, `os/kernel`, `lxd_cluster_role`), `disk[]`,
`net[]`, `gpu[]`, `inference{}`, `lxd_instances[]`, `services[]` (+ journal counts),
`processes[]`. The table below is the *new* set, ranked by diagnostic value.

Ranking logic: a metric ranks high if it changes the remediation the model would choose
(not just its confidence), makes the incident explainable, and is a leading indicator for a
frozen rule (`oom-guard-01`, `disk-fail-01`, `disk-degrade-01`, `latency-01`).

Token-cost model (stated assumption): the diagnosis runs on the paid managed model
(Gemini via `gemini-proxy`, `governance.md` §7.1). Each new field adds ≈25 chars in compact
JSON per sample. The evidence window is `EVIDENCE_SAMPLE_CAP = 20` samples plus the latest
`state` (1) plus `target_resource.instances` (1 copy of `lxd_instances[]`) — ≈21
sample-equivalents per diagnosis. I use **4 chars/token** (Gemini-class tokenizer; a
conservative, token-heavy estimate) and **100 model diagnoses/day** as the planning bound
(the deterministic fallback path never bills the model). Tokens/diagnosis ≈
`chars/sample × 21 ÷ 4`.

| # | Group / field(s) | Frozen rule (leading indicator) | AI-diagnosis value | Cadence | Bound | Tokens/diagnosis (≈) |
|---|---|---|---|---|---|---|
| 1 | `pressure{}` — `cpu_some_avg10`, `io_some_avg10`, `memory_some_avg10`, `memory_full_avg10` (float, 0–100) | `oom-guard-01` (memory PSI), `disk-degrade-01` & `latency-01` (io PSI) | **Changes remediation.** Today the model sees `ram_used_bytes` high and answers "restart heaviest"; PSI tells it *where the stall is* — memory PSI high → memory-bound (restart is right), io PSI high + RAM fine → I/O-bound (restart is wrong, shed I/O instead). The single highest-value addition. | every sample | fixed 4 scalars | ~500 |
| 2 | `oom_kill_count` (int, cumulative) | `oom-guard-01` | **Ground truth.** Distinguishes "RAM is filling" from "the kernel already killed something". `oom_kill>0` flips the decision to urgent and makes the incident explainable as a *real* OOM, not a projected one. | every sample | fixed 1 scalar | ~120 |
| 3 | `pgmajfault_count` (int, cumulative) | `oom-guard-01` | **Changes remediation.** Rising major faults = working-set thrash; the fix is memory, not CPU. A flat fault count alongside high `ram_used_bytes` means "used but not thrashing". | every sample | fixed 1 scalar | ~120 |
| 4 | `disk[].io_ticks_ms`, `disk[].io_in_flight` (int) | `disk-degrade-01` | **Replaces an opaque derived number with the raw signal.** `write_latency_ms` is node-derived and unexplained; `io_ticks_ms` (ms spent in I/O) + `io_in_flight` (queue depth) let the model *see* saturation and say *why* latency is rising. | every sample | 2 fields/device, `disk[]` already bounded | ~500 |
| 5 | `disk[].inode_used`, `disk[].inode_total` (int) | `disk-degrade-01` (new failure mode) | **New failure mode the current rule misses.** A full inode table fails writes while `free_bytes` looks healthy; `mount_ro`/`free_bytes` never catch it. Makes "disk full of tiny files" explainable. | every sample | 2 fields/device | ~300 |
| 6 | `net[].rx_errors`, `net[].tx_errors`, `net[].rx_dropped`, `net[].tx_dropped` (int) | `latency-01` | **Explains latency.** High `latency_ms` with rising drops = NIC/underlay fault (surface, no restart); high latency with clean counters = application-level. Changes the decision from "restart something" to "surface the network fault". | every sample | 4 fields/iface, `net[]` already bounded | ~800 |
| 7 | `tcp_retransmit_segs` (int, cumulative, node-level) | `latency-01` | **Transport-layer ground truth.** Retransmit delta is the canonical congestion/loss signal; pairs with `net[].latency_ms` to split "slow network" from "lossy network". | every sample | fixed 1 scalar | ~120 |
| 8 | `conntrack_count`, `conntrack_max` (int) | `latency-01` (new failure mode) | **New failure mode.** Conntrack-table exhaustion drops new connections while existing ones look fine — invisible to `net[]` bytes/latency. `count/max` near 1.0 makes it explainable. | every sample | fixed 2 scalars | ~200 |
| 9 | `lxd_instances[].oom_kill_count`, `lxd_instances[].pgmajfault_count` (int, cumulative) | `oom-guard-01` (instance-level attribution) | **Changes the target.** Instead of "restart heaviest by RSS", restart the instance whose `oom_kill`/`pgmajfault` is rising. Turns a node-level OOM into a *named instance* incident. | every sample | 2 fields/instance, `lxd_instances[]` already bounded | ~1,300 (6 running instances) |
| 10 | `lxd_instances[].cpu_throttled_usec` (int, cumulative) | (none today — a cpu-limit precursor) | **Weak today.** Only non-zero when an instance is CPU-quota'd (`cpu.stat throttled_usec`); `demo-node-a` shows `0`. Ship later, when CPU limits become a remediation axis. | every sample | 1 field/instance | — (SHIP-LATER) |
| 11 | `mem_available_bytes` (int) | `oom-guard-01` (cleaner than `free`) | **Corrects a known overcount.** `MemFree` understates usable memory (page cache is reclaimable); `MemAvailable` is the kernel's own "would not swap" number, so the OOM rule stops false-firing on cache-heavy nodes. | every sample | fixed 1 scalar | ~120 |
| 12 | `pressure[].avg60` (the 60 s window) | (smoothing for all three PSI-backed rules) | **Smooths noisy samples** so a single spike doesn't flip the decision; complements avg10. Cheap since it's in the same file read. | every sample | +4 scalars | ~500 |
| 13 | `compact_stall` / `compact_fail` (int, cumulative) | (advanced memory fragmentation) | **Niche.** Fragmentation pressure (`compact_fail 1280` on this host) is real but its remediation is "nothing autonomous is safe". Documented, deferred. | every sample | 2 scalars | — (SHIP-LATER) |
| 14 | `fd_count` / `fd_max` (int) | (fd exhaustion) | **Marginal.** System-wide fd exhaustion is better surfaced per-process than node-wide; deferred until a real incident shows it. | every sample | 2 scalars | — (SHIP-LATER) |
| 15 | GCE preemptible / maintenance-event | (new: node-vanish class) | **High value on GCE, zero value here.** "Why did my node disappear" needs the metadata server, which does not resolve on this host and needs a live GCE window to confirm. Deferred with the exact command for the live card (§5). | on metadata poll | 2 booleans | — (SHIP-LATER, needs live window) |
| 16 | SMART media health (`smartctl -a`) | `disk-fail-01` (predict before `mount_ro`) | **Real predictive value** — reallocated sectors/pending sectors precede a remount-ro — but `smartctl` is not installed here and needs root + a real SATA/NVMe node. Deferred with the exact command (§5). | slow poll (60 s) | bounded attribute subset | — (SHIP-LATER, needs live node) |

**Aggregate token cost.** Rows 1–9 + 11–12 (the SHIP set) sum to ≈ **4,600 tokens added
per diagnosis** (each ≈25-char field × 21 sample-equivalents ÷ 4 chars/token; the
`lxd_instances[]` two fields × six running instances is the largest single contributor at
≈1,300). At 100 diagnoses/day that is ≈ **460,000 tokens/day** against the managed model.
That is the *full, every-group, six-running-instances* worst case; a 1–2-instance node with a
quiet NIC is closer to ≈ 2–3 k tokens/diagnosis. The cost is real and is why the bound column
is non-negotiable: every field above is a fixed scalar or rides an already-bounded array, and
the collector must omit-not-fabricate (§5) so a quiet node ships less, not more.

*(Assumption recap, for the cost-honesty rule: 4 chars/token; 21 sample-equivalents per
diagnosis; 100 model diagnoses/day. Price is not asserted here — the managed-model tier is an
ops decision — the token figure is the invariant and the number the diagnosis card must
re-measure from its own captured request.)*

---

## 3. Decision — SHIP / SHIP-LATER / REJECT

### SHIP (register in `governance.md` §4.1.1 as C0 groups; no contract amendment needed)

Every shipped field is a plain C0 counter/percentage with no identifier, no string, no path.
None needs C1 in the prompt, so **no `governance.md` §7.2 amendment is triggered** — these are
§4.1.1 registrations, which is exactly what the contract card (`t_98a685c1`) owns.

1. **`pressure{}`** — the four PSI scalars (rows 1, 12). *Reason:* highest diagnostic ROI,
   cheap, C0.
2. **`oom_kill_count`, `pgmajfault_count`, `mem_available_bytes`** — flat C0 scalars
   (rows 2, 3, 11) from `/proc/vmstat` + `/proc/meminfo`.
3. **`disk[]` extension** — `io_ticks_ms`, `io_in_flight`, `inode_used`, `inode_total`
   (rows 4, 5). *Reason:* makes `disk-degrade-01` diagnosable and adds inode exhaustion.
4. **`net[]` extension + `tcp_retransmit_segs`** — four error/drop counters + one node-level
   retransmit (rows 6, 7). *Reason:* explains `latency-01`.
5. **`conntrack_count`, `conntrack_max`** — count only (row 8). *Reason:* new failure mode.
   **C-class caveat (flagged, as required):** the *count* is C0 (an integer aggregate, the same
   reasoning as journal counts §4.11); the *table* (`/proc/net/nf_conntrack`) is C1/C2 and must
   never be read for telemetry. The collector must read only the two `nf_conntrack_count/_max`
   files, and the contract card must write that constraint into §4.1.1 so a future collector
   cannot drift to the table.
6. **`lxd_instances[]` extension** — `oom_kill_count`, `pgmajfault_count` (row 9), sourced
   from the instance cgroup (`/sys/fs/cgroup/lxc.payload.<name>/memory.events` +
   `memory.stat`), **not** from the LXD API (which does not expose them). *Reason:* per-instance
   attribution.

**Failure mode for every SHIP field (already the contract's rule, restated per field):** when
the source is unreadable the field is **omitted, never `0`** — `pressure` omitted if
`/proc/pressure` is gone; `oom_kill_count`/`pgmajfault_count` omitted if `/proc/vmstat` is
gone; the `disk[]`/`net[]`/`lxd_instances[]` extension fields omitted per-entry if the
`/sys` or cgroup file is gone. No new array is introduced, so no new top-N bound is needed;
the existing `disk[]`/`net[]`/`lxd_instances[]` bounds carry the volume.

### SHIP-LATER (real value, but blocked by availability or a missing prerequisite)

| Item | Reason |
|---|---|
| SMART media health (`smartctl -a -A`, reallocated/pending sectors) | `smartctl`/`nvme` not installed on this host; needs root + a real SATA/NVMe node. **needs live window** — exact command: `smartctl -a /dev/sda` and `smartctl -a /dev/nvme0n1` (or `nvme smart-log /dev/nvme0`). |
| GCE instance id/zone/preemptible/maintenance-event | metadata server does not resolve here; C1 identifiers. **needs live window** — exact command: `curl -sH 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/scheduling/preemptible`. |
| EDAC memory errors (`ce_count`/`ue_count`) | `edac/mc/` empty on this host (no ECC controllers); needs server hardware. |
| `cpu_throttled_usec` (per-instance) | zero until CPU limits become a remediation axis; the source is confirmed present. |
| `compact_stall`/`compact_fail` | real fragmentation signal, but no safe autonomous remediation; document-and-wait. |
| `fd_count`/`fd_max` | node-wide fd exhaustion is better surfaced per-process later. |
| DNS resolver latency / NTP drift | need a measured target (`resolvectl` latency, `timedatectl` offset); low value for the 4 frozen rules. |
| `ss -s` socket summary | aggregates of things `net[]` already carries; low marginal value. |
| LXD `/1.0/events` instance lifecycle | event-driven restart attribution; only valuable once the diagnosis wants restart *history*, not current state. |

### REJECT (a rejected row with a reason is a result)

| Item | Class / reason |
|---|---|
| **LXD `config` / `expanded_config` / `devices` objects** | **C2 — headline result.** The raw LXD API object carries `cloud-init.user-data` (an enrollment token, file paths, `project_id`), a raw MAC (`volatile.eth0.hwaddr`), and a UUID (`volatile.uuid`). Fixture `lxd-api-probe.txt` shows them verbatim. The existing contract is already correct to key `lxd_instances[]` by `vm_id` and ship only `state.*`/`expanded_config.limits.*` scalars; this inventory confirms that defense is load-bearing, not paranoia. |
| **Conntrack table** (`/proc/net/nf_conntrack`) | **C2** — per-flow 5-tuples (IPs/ports). Only the two count files are C0. |
| **`dmesg` message text** | **C2** — log text never egresses. Counts-only is the §4.11 precedent, but cgroup `memory.events oom_kill` supersedes even the count, and `dmesg_restrict=1` makes it root-only anyway. |
| **eBPF / `bpftrace` tracepoints** | **C2 + cost** — root-only (`unprivileged_bpf_disabled=2`, `bpftrace` refuses non-root), expensive to instrument, and probe output is function names/arguments/paths. |
| **OVS/OVN flow counters** | unavailable — `ovs-vsctl`/`ovn-*` not installed; the fleet uses the Linux bridge (`lxdbr0`), not OVS. Revisit only if a fleet adopts OVS. |
| **`/proc/pagetypeinfo`** | root-only (`Permission denied`) + marginal value (superseded by `compact_stall`). |
| **`/proc/schedstat`, `/proc/interrupts`, `/proc/softirqs`** | high-cardinality per-CPU tables, low diagnostic value for the four frozen rules; cost exceeds value. |
| **`/proc/buddyinfo`, `/proc/zoneinfo`** | readable but root-level fragmentation detail with no actionable remediation. |
| **`/proc/crypto`, `/proc/modules`, `/proc/mdstat`, full `/proc/cpuinfo`** | no diagnostic value for the four rules (`modules` is also C1 as a build fingerprint). |
| **`kdump` state** | not a metric — a crash-dump facility (`kexec_crash_loaded=0`, `kdump inactive` here); dumps are C2-sized content. |
| **`systemd-analyze` boot timing** | a boot-time one-shot, not per-sample telemetry; belongs in the bake/CI surface, not the telemetry stream. |
| **journald rate (`RuntimeMaxUse`)** | `[not set]` (defaults), not a live rate counter; journal *counts* already ship (§4.11). |
| **GCE Cloud Monitoring / Ops agent** | absent on this host, and with Private Google Access OFF-equivalent (no metadata/API path) the monitoring-API metrics are unreachable from a node; PD performance limits belong to the controller's GCP side, not node telemetry. |
| **RAPL energy, thermal zones** | absent on a QEMU VM (no `intel-rapl`, no `thermal_zone*`); `cpu_temp_celsius` already ships and is omitted when no sensor exists. |

---

## 4. Hand-off block (the four follow-up cards)

The field names below are canonical and **must appear verbatim** in the contract, `fields.py`,
the collector, the prompt, and the dashboard. A rename is a divergence to reconcile, not a
style choice.

**To `t_98a685c1` (contract + ingest — controller).** Own the *contract half*: add
`node-telemetry.md` §4.12–§4.14 and register every group in `governance.md` §4.1.1, plus
`fields.py`/`validate.py` enforcement. Field names and types:

- §4.12 `pressure{}` — object, C0: `cpu_some_avg10`, `io_some_avg10`, `memory_some_avg10`,
  `memory_full_avg10` (float, 0–100, omitted if `/proc/pressure` unreadable).
- §4.12 flat scalars — C0 ints, cumulative, omitted-not-0: `oom_kill_count`, `pgmajfault_count`
  (from `/proc/vmstat`); `mem_available_bytes` (from `/proc/meminfo`).
- §4.13 `disk[]` extension — C0 ints: `io_ticks_ms`, `io_in_flight`, `inode_used`, `inode_total`.
- §4.13 `net[]` extension — C0 ints: `rx_errors`, `tx_errors`, `rx_dropped`, `tx_dropped`; plus
  flat `tcp_retransmit_segs` (int, cumulative).
- §4.13 flat scalars — C0 ints: `conntrack_count`, `conntrack_max`. **Write the count-only
  constraint into §4.1.1** (the conntrack *table* is C2 and must never be read).
- §4.14 `lxd_instances[]` extension — C0 ints, cumulative: `oom_kill_count`, `pgmajfault_count`
  (sourced from the instance cgroup, not the LXD API).

Assert worst-case bytes/sample in a test (the token table in §2 is your size budget), and
reject-not-truncate on any array that would exceed its bound (mirror `PROCESSES_MAX`).

**To `t_92aac3c8` (node collector — infra).** Own `infra/bootstrap/node/termini-bootstrap.py`.
Collect the §4.12–§4.14 fields from the exact sources in §1, one function per group returning
`None`/`{}` on unreadable source. Fold systemd into the existing single `systemctl show`; cache
journal/LXD sweeps. Per-instance cgroup metrics come from `/sys/fs/cgroup/lxc.payload.<name>/`
(you already map instance name → cgroup path for `ram_limit_bytes`). **Never read the LXD
`config` object** (C2, §3); read only `state.*` and `expanded_config.limits.*`. The sources
that need a live node (SMART, GCE metadata, EDAC) are marked in §3 with their exact commands —
emit nothing and say so in your PR.

**To `t_922b3268` (diagnosis — controller).** Own the evidence window + prompt + incident
evidence quoting. Carry the §4.12–§4.14 C0 fields into the evidence window under the existing
`EVIDENCE_SAMPLE_CAP` discipline; register them in `governance.md` §4.1.1 *before* substitution
(`externalModelCall()` asserts per-field C0). Bump the static template version and re-measure
the token delta from your own captured request — my §2 estimate is ≈4,600 tokens/diagnosis
worst-case (4 chars/token, 21 sample-equivalents, 6 instances); verify and state your figure.
The PSI group is the one that changes remediation, so it must be quoted in `evidence[]` first.
No new guardrail gate is required by any of these metrics — none introduces a new precondition.

**To `t_8b60acb2` (dashboard — frontend).** Own the charts. Admit the §4.12–§4.14 fields into
`METRIC_UNITS`/`CHART_GROUPS` with their units (percent for PSI, count for OOM/faults, bytes for
`mem_available_bytes`/inode-derived, ms for `io_ticks_ms`). Label derived-vs-raw: `oom_kill_count`
and `pgmajfault_count` are raw cumulative counters — present the controller-computed *delta* as
derived, never the raw counter as a rate. Absent ≠ zero: a node that omits `pressure{}` renders
"not reported", not a flat zero. Wire health flags **only** where the contract gives a threshold
(none of the new fields has one yet) — comment on the diagnosis card if you want a threshold
added rather than inventing a client-side one.

---

## 5. Fixture index

Every availability claim in §1 traces to one of these committed files (raw command output):

| Fixture | Source families covered |
|---|---|
| `docs/research/fixtures/proc-probe.txt` | `/proc` presence + `stat`, `loadavg`, `meminfo`, `pressure/*`, `vmstat`, `diskstats`, `net/dev`, `net/snmp`, `net/netstat`, `schedstat`, `interrupts`, `softirqs`, `buddyinfo`, `zoneinfo`, `pagetypeinfo` (denied), `swaps`, `uptime`, `crypto`, `file-nr`, `mdstat`, `cpuinfo` |
| `docs/research/fixtures/sys-probe.txt` | `/sys/block`, thermal (absent), nvme (absent), edac (empty), rapl (absent), `block/*/stat`, net error/drop counters, `df -hT`/`df -i` |
| `docs/research/fixtures/cgroup-lxd-probe.txt` | cgroup v2 self + LXD instance cgroups (`memory.current/stat/events`, `cpu.stat`, `io.stat`, `pids`, `cpu.max`, `memory.max`) |
| `docs/research/fixtures/lxd-api-probe.txt` | LXD `/1.0/instances` state (incl. **`started_at` absent**), `config` (C2), storage resources, `/1.0/resources` |
| `docs/research/fixtures/systemd-net-kernel-probe.txt` | `systemctl show`, `systemd-analyze`, `ss -s`, conntrack, MTU, DNS, NTP, dmesg (denied), kdump, bpftrace (root-only), unprivileged-bpf |
| `docs/research/fixtures/systemd-control-probe.txt` | positive control: running-unit `MemoryCurrent`/`CPUUsageNSec`, `journalctl` counts, `lxc info` |
| `docs/research/fixtures/cloud-probe.txt` | DMI (QEMU, not GCE), metadata DNS (unresolvable), Google agents (absent), smartctl/nvme/sensors (absent), `dmesg_restrict`, `kptr_restrict`, lxd-as-snap |
| `docs/research/fixtures/vmstat-keys-probe.txt` | `/proc/vmstat` OOM/fault/compaction keys, root cgroup `memory.stat`/`cpu.stat`, PSI full structure, TcpExt retransmit labels |
