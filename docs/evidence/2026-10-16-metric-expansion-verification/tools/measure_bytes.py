import json, sys
sys.path.insert(0, "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/metrics-verify-1792189188/controller")
from app import fields as F
U64 = 18446744073709551615

new_only = {
    "pressure": {k: 100.0 for k in F.PRESSURE_FIELDS},
    "oom_kill_count": U64, "pgmajfault_count": U64, "mem_available_bytes": U64,
    "tcp_retransmit_segs": U64, "conntrack_count": U64, "conntrack_max": U64,
    "disk": [{"io_ticks_ms": U64, "io_in_flight": U64, "inode_used": U64, "inode_total": U64}],
    "net": [{"rx_errors": U64, "tx_errors": U64, "rx_dropped": U64, "tx_dropped": U64}],
    "lxd_instances": [{"oom_kill_count": U64, "pgmajfault_count": U64}],
}
print("new-fields-only (compact):", len(json.dumps(new_only, separators=(",", ":"))))

full = dict(new_only)
full.update({"seq": 1000, "ts": "2026-10-04T07:49:00.000Z", "cpu_util_pct": 31.2,
             "ram_total_bytes": 34359738368, "ram_used_bytes": 19756849561})
print("full sample (base + new, compact):", len(json.dumps(full, separators=(",", ":"))))

p = {"pressure": {k: 100.0 for k in F.PRESSURE_FIELDS}}
print("pressure block alone:", len(json.dumps(p, separators=(",", ":"))))
c = {"oom_kill_count": U64, "pgmajfault_count": U64, "mem_available_bytes": U64}
print("three 20-digit counters alone:", len(json.dumps(c, separators=(",", ":"))))
print("METRIC_EXPANSION_MAX_BYTES =", F.METRIC_EXPANSION_MAX_BYTES)
