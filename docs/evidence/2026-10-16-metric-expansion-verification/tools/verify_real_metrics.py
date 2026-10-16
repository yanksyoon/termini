"""Real-metrics verification: re-derive the new metric-expansion fields from the
host's own /proc, /sys and cgroup files BY HAND, then call the committed
collector functions and compare. A payload that matches no reality is worse than
an empty one.
"""
import os
import re
import sys

WT = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/metrics-verify-1792189188"
sys.path.insert(0, WT + "/infra/bootstrap/node")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "termini_bootstrap", WT + "/infra/bootstrap/node/termini-bootstrap.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

def read(p):
    try:
        with open(p) as fh:
            return fh.read()
    except OSError as e:
        return f"<unreadable: {e}>"

ok = True
def check(label, by_hand, daemon, note=""):
    global ok
    match = "MATCH" if by_hand == daemon else "DIFF"
    if by_hand != daemon:
        ok = False
    print(f"  {label:28s} by-hand={by_hand!r:>20}  daemon={daemon!r:>20}  {match} {note}")

print("=== §4.12 pressure{} (PSI) ===")
# By hand: parse /proc/pressure/{cpu,io,memory} avg10 on some/full lines.
def psi_by_hand(res, kind):
    txt = read(f"/proc/pressure/{res}")
    for line in txt.splitlines():
        parts = line.split()
        if parts and parts[0] == kind:
            for tok in parts[1:]:
                if tok.startswith("avg10="):
                    return round(float(tok.split("=",1)[1]), 2)
    return None
for field, res, kind in (("cpu_some_avg10","cpu","some"),
                          ("io_some_avg10","io","some"),
                          ("memory_some_avg10","memory","some"),
                          ("memory_full_avg10","memory","full")):
    by_hand = psi_by_hand(res, kind)
    daemon = tb.pressure_facts()
    check(f"pressure.{field}", by_hand, daemon.get(field) if daemon else None)
print("  raw /proc/pressure/memory:", repr(read('/proc/pressure/memory').strip()))

print("\n=== §4.12 flat scalars ===")
vmstat = read("/proc/vmstat")
meminfo = read("/proc/meminfo")
def vm(key):
    for line in vmstat.splitlines():
        p = line.split()
        if p and p[0] == key:
            return int(p[1])
    return None
oom_by_hand = vm("oom_kill")
pgm_by_hand = vm("pgmajfault")
memavail_by_hand = None
for line in meminfo.splitlines():
    if line.startswith("MemAvailable:"):
        memavail_by_hand = int(line.split()[1]) * 1024
daemon = tb.memory_pressure_scalars()
check("oom_kill_count", oom_by_hand, daemon.get("oom_kill_count"))
check("pgmajfault_count", pgm_by_hand, daemon.get("pgmajfault_count"))
check("mem_available_bytes", memavail_by_hand, daemon.get("mem_available_bytes"))

print("\n=== §4.13 transport scalars ===")
snmp = read("/proc/net/snmp")
rows = [l.split() for l in snmp.splitlines() if l.startswith("Tcp:")]
labels, values = rows[0][1:], rows[1][1:]
retr_by_hand = int(values[labels.index("RetransSegs")])
ct_count = int(read("/proc/sys/net/netfilter/nf_conntrack_count").strip())
ct_max = int(read("/proc/sys/net/netfilter/nf_conntrack_max").strip())
daemon = tb.transport_scalars()
check("tcp_retransmit_segs", retr_by_hand, daemon.get("tcp_retransmit_segs"))
check("conntrack_count", ct_count, daemon.get("conntrack_count"))
check("conntrack_max", ct_max, daemon.get("conntrack_max"))

print("\n=== §4.13 disk[] extension (io_ticks_ms / io_in_flight / inode_*) ===")
# Resolve the block device behind "/" the same way the collector does.
mountinfo = read("/proc/self/mountinfo")
majmin = {}
for line in mountinfo.splitlines():
    f = line.split()
    if len(f) >= 5 and re.match(r"^\d+:\d+$", f[2]):
        majmin[f[4]] = f[2]
root_mm = majmin.get("/")
stat = read(f"/sys/dev/block/{root_mm}/stat").split()
in_flight_by_hand = int(stat[8])
ticks_by_hand = int(stat[9])
st = os.statvfs("/")
inode_total_by_hand = st.f_files
inode_used_by_hand = max(0, st.f_files - st.f_ffree)
daemon_disk = tb.disk_facts()
d0 = daemon_disk[0] if daemon_disk else {}
check("disk[0].io_in_flight", in_flight_by_hand, d0.get("io_in_flight"))
check("disk[0].io_ticks_ms", ticks_by_hand, d0.get("io_ticks_ms"))
check("disk[0].inode_total", inode_total_by_hand, d0.get("inode_total"))
check("disk[0].inode_used", inode_used_by_hand, d0.get("inode_used"))
print(f"  (block device for '/': {root_mm})")

print("\n=== §4.13 net[] extension (NIC error/drop counters) ===")
ifname = "enp5s0"  # the research card's probed interface
def ifc(counter):
    return int(read(f"/sys/class/net/{ifname}/statistics/{counter}").strip())
cfg = {"iface": ifname, "controller_url": "http://127.0.0.1:9999"}
daemon_net = tb.net_facts(cfg, ifname)[0]
for counter in ("rx_errors","tx_errors","rx_dropped","tx_dropped"):
    check(f"net[0].{counter}", ifc(counter), daemon_net.get(counter))

print("\n=== §4.14 lxd_instances[] extension (per-instance cgroup) ===")
# Find a live lxc.payload.<name> cgroup on this host.
cgroup_root = "/sys/fs/cgroup"
live_names = []
try:
    for e in sorted(os.listdir(cgroup_root)):
        if e.startswith("lxc.payload."):
            name = e[len("lxc.payload."):]
            live_names.append(name)
except OSError as e:
    print("  cgroup unreadable:", e)
print("  lxc.payload.* cgroups on this host:", live_names)
if live_names:
    name = live_names[0]
    base = os.path.join(cgroup_root, f"lxc.payload.{name}")
    def cg(file, key):
        txt = read(os.path.join(base, file))
        for line in txt.splitlines():
            p = line.split()
            if p and p[0] == key:
                return int(p[1])
        return None
    oom_by_hand = cg("memory.events", "oom_kill")
    pgm_by_hand = cg("memory.stat", "pgmajfault")
    daemon_lxd = tb.instance_cgroup_facts(name)
    check(f"lxd_instances[{name!r}].oom_kill_count", oom_by_hand, daemon_lxd.get("oom_kill_count"))
    check(f"lxd_instances[{name!r}].pgmajfault_count", pgm_by_hand, daemon_lxd.get("pgmajfault_count"))
else:
    print("  NO lxc.payload.* cgroup -> per-instance fields COULD NOT VERIFY on this host")

print("\nRESULT:", "ALL REAL-METRICS CHECKS MATCH" if ok else "MISMATCH FOUND")
