"""Bounds + cost verification for the metric-expansion fields.

1. Over-long array: REJECTED (not truncated) where a cap exists; and where NO
   cap exists (disk[]/net[]/lxd_instances[]) the over-long array is ACCEPTED —
   an honest finding about the producer-only bounds.
2. Worst-case bytes: re-measure the new-field sample against the stated budget.
3. Token delta: re-measure from MY OWN captured request, not the implementer's.
"""
import json
import sys

WT = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/metrics-verify-1792189188"
LIVE = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/verify-diagnosis-1792189616/live"
sys.path.insert(0, WT + "/controller")

from app import fields as F  # noqa: E402
from app.validate import validate_sample  # noqa: E402

U64 = 18446744073709551615

def base(**extra):
    s = {"seq": 1000, "ts": "2026-10-04T07:49:00.000Z", "cpu_util_pct": 31.2,
         "ram_total_bytes": 34359738368, "ram_used_bytes": 19756849561}
    s.update(extra)
    return s

print("=== 1. Over-long array: REJECTED where capped, ACCEPTED where uncapped ===")
proc = {"pid": 1, "name": "x", "state": "running", "cpu_util_pct": 1.0,
        "ram_used_bytes": 1, "threads": 1, "age_sec": 1}

# processes[]: capped at 10 -> 11 entries REJECTED.
rows = [dict(proc, pid=1000 + i) for i in range(11)]
_, err = validate_sample(base(processes=rows))
print(f"processes[] 11 entries -> errors={err}")
assert any("exceeds the contract maximum" in e for e in err)

# disk[]/net[]/lxd_instances[]: NO max_items in fields.py -> 50 entries ACCEPTED.
disk_row = {"path": "/dev/sda", "total_bytes": 274877906944,
            "free_bytes": 214748364800, "write_latency_ms": 3.0, "mount_ro": False,
            "io_ticks_ms": 1, "io_in_flight": 0, "inode_used": 1, "inode_total": 2}
clean, err = validate_sample(base(disk=[dict(disk_row)] * 50))
print(f"disk[] 50 entries -> accepted={not err} err={err[:2]} (len err={len(err)})")
net_row = {"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8,
           "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}
clean, err = validate_sample(base(net=[dict(net_row)] * 50))
print(f"net[] 50 entries -> accepted={not err}")
lxd_row = {"vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "state": "running",
           "ram_used_bytes": 1, "oom_kill_count": 0, "pgmajfault_count": 0}
clean, err = validate_sample(base(lxd_instances=[dict(lxd_row)] * 50))
print(f"lxd_instances[] 50 entries -> accepted={not err}")
print("=> disk[]/net[]/lxd_instances[] carry NO ingest-enforced max_items; their\n"
      "   bounds are producer-side only (DISK_MAX=8, NET_MAX=1, running instances).\n"
      "   The controller backstop is MAX_BODY=1 MiB, not a per-array cap.")

print("\n=== 2. Worst-case bytes of the new fields ===")
wc = base(
    pressure={k: 100.0 for k in F.PRESSURE_FIELDS},
    oom_kill_count=U64, pgmajfault_count=U64, mem_available_bytes=U64,
    tcp_retransmit_segs=U64, conntrack_count=U64, conntrack_max=U64,
    disk=[{"io_ticks_ms": U64, "io_in_flight": U64, "inode_used": U64, "inode_total": U64}],
    net=[{"rx_errors": U64, "tx_errors": U64, "rx_dropped": U64, "tx_dropped": U64}],
    lxd_instances=[{"oom_kill_count": U64, "pgmajfault_count": U64}],
)
n = len(json.dumps(wc, separators=(",", ":")))
print(f"worst-case new-field sample (compact JSON): {n} bytes")
print(f"  contract budget METRIC_EXPANSION_MAX_BYTES={F.METRIC_EXPANSION_MAX_BYTES}")
print(f"  node-telemetry.md §4.15 stated 744 bytes")
assert n <= F.METRIC_EXPANSION_MAX_BYTES
print(f"  -> {n} <= {F.METRIC_EXPANSION_MAX_BYTES} (within the code budget)")

print("\n=== 3. Token delta from MY OWN captured request ===")
reqs = [json.loads(l) for l in open(LIVE + "/stub-model-requests.jsonl")]
last = reqs[-1]
msgs = last["body"]["messages"]
user_content = msgs[-1]["content"]
decision_json = user_content.split("input:\n\n", 1)[1].split("\n\nReturn", 1)[0]
payload = json.loads(decision_json)

# Full user message (the DecisionInput JSON is embedded in the user template).
full_user_len = len(user_content)
# The DecisionInput compact JSON alone.
din_len = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

# Recompute: strip the new fields from state+evidence+derived, re-serialize.
NEW_SCALARS = {"oom_kill_count", "pgmajfault_count", "mem_available_bytes",
               "tcp_retransmit_segs", "conntrack_count", "conntrack_max"}
NEW_PRESSURE = set(F.PRESSURE_FIELDS)
NEW_DISK = {"io_ticks_ms", "io_in_flight", "inode_used", "inode_total"}
NEW_NET = {"rx_errors", "tx_errors", "rx_dropped", "tx_dropped"}
NEW_LXD = {"oom_kill_count", "pgmajfault_count"}

def strip_new(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in NEW_SCALARS or k in NEW_PRESSURE:
                continue
            if k == "disk" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in e.items() if kk not in NEW_DISK} for e in v]
            elif k == "net" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in e.items() if kk not in NEW_NET} for e in v]
            elif k == "lxd_instances" and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in e.items() if kk not in NEW_LXD} for e in v]
            else:
                out[k] = strip_new(v)
        return out
    if isinstance(obj, list):
        return [strip_new(e) for e in obj]
    return obj

stripped = strip_new(payload)
stripped.pop("derived", None)  # derived is entirely new
din_stripped_len = len(json.dumps(stripped, separators=(",", ":"), ensure_ascii=False))
dynamic_delta_chars = din_len - din_stripped_len
# The static part (system prompt + few-shot) is identical in both; measure the
# system prompt length directly and compare to the v1 length is not available
# here — so the dynamic (per-request) delta is what I can measure from capture.
print(f"captured DecisionInput JSON: {din_len} chars")
print(f"with new fields + derived stripped: {din_stripped_len} chars")
print(f"dynamic delta (new fields + derived, {payload['derived']['window_samples']}-sample window): "
      f"{dynamic_delta_chars} chars")
print(f"  @ 4 chars/token = {dynamic_delta_chars/4:.1f} tokens")
# Static: system prompt length.
sys_len = len(msgs[0]["content"])
print(f"system prompt (v2): {sys_len} chars")
fewshot_len = sum(len(m["content"]) for m in msgs[1:-1])
print(f"few-shot messages: {fewshot_len} chars")

print("\nDONE")
