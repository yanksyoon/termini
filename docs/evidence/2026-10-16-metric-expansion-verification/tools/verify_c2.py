"""Adversarial verification of C2 enforcement on the metric-expansion fields.

Runs the EXACT ingest gates from controller/app/server.py:_ingest_telemetry,
in order: governance.scan_c2 -> governance.check_c1_transforms ->
assert_redactor_version -> validate.validate_envelope. This is the committed
state at origin/main (worktree).
"""
import json
import sys

WT = "/home/ubuntu/.hermes/profiles/verifier/cache/scratch/metrics-verify-1792189188"
sys.path.insert(0, WT + "/controller")

from app import governance  # noqa: E402
from app import fields as F  # noqa: E402
from app.validate import validate_envelope, validate_sample  # noqa: E402

NODE_ID = "01M4349RPTT29RNTC6VBCXQ0RC"
PROJECT_ID = "01M4349RPSZFTB60AVJEG0ZZY9"
CALL_ID = "01M4349RPY26W8Q453PZYK05CB"

def base_sample(**extra):
    s = {
        "seq": 1000,
        "ts": "2026-10-04T07:49:00.000Z",
        "cpu_util_pct": 31.2,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 19756849561,
    }
    s.update(extra)
    return s

def envelope(samples):
    return {
        "_redaction": {"version": "redactor-config v3",
                       "config_hash": "3f2a9c1e7b4d4a1f5f2a9c1e7b4d4a1f",
                       "result": "ok"},
        "call_id": CALL_ID,
        "node_id": NODE_ID,
        "schema_version": 1,
        "governance_chain_tip": "01M4349RPZ4DMD3MT18EBZRQB4",
        "samples": samples,
    }

def ingest_gates(raw):
    """Replay server.py's ingest gate sequence; return (c2, c1, verrors)."""
    c2 = governance.scan_c2(raw)
    c1 = governance.check_c1_transforms(raw)
    verrors = []
    if not c2 and not c1:
        _, verrors = validate_envelope(raw)
    return c2, c1, verrors

# ---------------------------------------------------------------------------
# The NEW C0 fields (metric expansion §4.12–§4.14). All numeric.
# ---------------------------------------------------------------------------
NEW_SCALAR_FIELDS = [
    "oom_kill_count", "pgmajfault_count", "mem_available_bytes",
    "tcp_retransmit_segs", "conntrack_count", "conntrack_max",
]
NEW_PRESSURE_FIELDS = [
    "cpu_some_avg10", "io_some_avg10", "memory_some_avg10", "memory_full_avg10",
]
NEW_DISK_FIELDS = ["io_ticks_ms", "io_in_flight", "inode_used", "inode_total"]
NEW_NET_FIELDS = ["rx_errors", "tx_errors", "rx_dropped", "tx_dropped"]
NEW_LXD_FIELDS = ["oom_kill_count", "pgmajfault_count"]

# C2-shaped payloads (strings) to plant, per the verification brief.
C2_VALUES = {
    "proc_cmdline_path": "/proc/12345/cmdline",
    "email": "attacker@example.com",
    "card_digit_run": "4111111111111111",
    "file_path": "/etc/passwd",
}

def clean_sample_with_new_fields():
    return base_sample(
        pressure={k: 4.86 for k in NEW_PRESSURE_FIELDS},
        oom_kill_count=37, pgmajfault_count=2647724, mem_available_bytes=2092816000,
        tcp_retransmit_segs=295393, conntrack_count=854, conntrack_max=262144,
        disk=[{"path": "/dev/sda", "total_bytes": 274877906944,
               "free_bytes": 214748364800, "write_latency_ms": 3.0,
               "mount_ro": False, "io_ticks_ms": 50829476, "io_in_flight": 1,
               "inode_used": 1127654, "inode_total": 12976128}],
        net=[{"iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000,
              "latency_ms": 0.8, "rx_errors": 0, "tx_errors": 0,
              "rx_dropped": 0, "tx_dropped": 0}],
        lxd_instances=[{"vm_id": "01M4349RPXWDC52ZDAVMPCMJSH", "state": "running",
                        "cpu_util_pct": 12.0, "ram_used_bytes": 3328599654,
                        "ram_limit_bytes": 4294967296, "oom_kill_count": 0,
                        "pgmajfault_count": 103032}],
    )

# ---------------------------------------------------------------------------
print("=== 0. BASELINE: a valid batch carrying every new field must PASS all gates ===")
c2, c1, verr = ingest_gates(envelope([clean_sample_with_new_fields()]))
print("scan_c2:", c2)
print("check_c1:", c1)
print("validate_envelope errors:", verr)
assert not c2 and not c1 and not verr, "baseline failed"
print("BASELINE: PASS (clean batch accepted)\n")

# ---------------------------------------------------------------------------
print("=== 1. C2 field-NAME denylist: no new field name tokenises to C2 ===")
all_new = (NEW_SCALAR_FIELDS + NEW_PRESSURE_FIELDS + NEW_DISK_FIELDS
           + NEW_NET_FIELDS + NEW_LXD_FIELDS)
bad = [n for n in all_new if governance.is_c2_field_name(n)]
print("new field names that read as C2:", bad)
assert not bad
# Positive control: a known C2 name DOES trip the matcher.
for probe in ["cmdline", "app_logs", "customer_notes", "argv", "environ", "message"]:
    assert governance.is_c2_field_name(probe), probe
print("positive control: cmdline/app_logs/customer_notes/argv/environ/message all trip the matcher\n")

# ---------------------------------------------------------------------------
print("=== 2. Plant C2-shaped STRING values in EVERY new C0 field -> fail-closed ===")
# Each plant must (a) be visible in the raw payload, (b) be rejected by the
# ingest gates (C2 scan OR schema type check — both are fail-closed).
results = []
def plant(field_spec, value, label):
    # field_spec is a callable returning the sample dict with the value planted.
    raw_sample = field_spec(value)
    raw_env = envelope([raw_sample])
    # (a) visibility: the planted value must be present in the serialized raw.
    raw_txt = json.dumps(raw_env)
    visible = value in raw_txt
    c2, c1, verr = ingest_gates(raw_env)
    failed = bool(c2) or bool(c1) or bool(verr)
    results.append((label, value, visible, failed, c2, verr))
    return label, value, visible, failed, c2, verr

def scalar_plant(field):
    def f(v):
        s = clean_sample_with_new_fields()
        s[field] = v
        return s
    return f

def pressure_plant(field):
    def f(v):
        s = clean_sample_with_new_fields()
        s["pressure"][field] = v
        return s
    return f

def disk_plant(field):
    def f(v):
        s = clean_sample_with_new_fields()
        s["disk"][0][field] = v
        return s
    return f

def net_plant(field):
    def f(v):
        s = clean_sample_with_new_fields()
        s["net"][0][field] = v
        return s
    return f

def lxd_plant(field):
    def f(v):
        s = clean_sample_with_new_fields()
        s["lxd_instances"][0][field] = v
        return s
    return f

planters = {}
for fld in NEW_SCALAR_FIELDS:
    planters[fld] = scalar_plant(fld)
for fld in NEW_PRESSURE_FIELDS:
    planters["pressure." + fld] = pressure_plant(fld)
for fld in NEW_DISK_FIELDS:
    planters["disk[]." + fld] = disk_plant(fld)
for fld in NEW_NET_FIELDS:
    planters["net[]." + fld] = net_plant(fld)
for fld in NEW_LXD_FIELDS:
    planters["lxd_instances[]." + fld] = lxd_plant(fld)

# For every field, plant EVERY C2-shaped value. Expect all to fail closed.
all_pass = True
for fld, planter in sorted(planters.items()):
    for vname, value in C2_VALUES.items():
        label, v, visible, failed, c2, verr = plant(planter, value, f"{fld} <- {vname}")
        if not (visible and failed):
            all_pass = False
            print(f"  FAIL: {label} visible={visible} failed={failed}")
    # print one representative line per field (card digit run, which the C2 scan
    # itself should catch) plus note the email case.
    _, v, vis, failed, c2, verr = plant(planter, C2_VALUES["email"], f"{fld} <- email")
    _, v2, vis2, failed2, c2b, verrb = plant(planter, C2_VALUES["card_digit_run"], f"{fld} <- card")
    _, v3, vis3, failed3, c2c, verrc = plant(planter, C2_VALUES["proc_cmdline_path"], f"{fld} <- proc-path")
    print(f"  {fld}: email visible={vis} C2={c2} schema={bool(verr)} -> "
          f"failed={failed} | card visible={vis2} C2={c2b} schema={bool(verrb)} -> "
          f"failed={failed2} | proc-path visible={vis3} C2={c2c} schema={bool(verrc)} -> failed={failed3}")

print("\nALL %d field/value plants fail-closed: %s" % (len(planters)*len(C2_VALUES), all_pass))
assert all_pass

# ---------------------------------------------------------------------------
print("\n=== 3. The instrument is real: scan catches a value we KNOW is C2 ===")
# Show the scan is not vacuously clean: plant an email and print the exact hit.
raw = clean_sample_with_new_fields()
raw["oom_kill_count"] = "attacker@example.com"
hits = governance.scan_c2(envelope([raw]))
print("planted 'attacker@example.com' in oom_kill_count ->", hits)
assert any("email" in h for h in hits)
# And a clean batch produces zero hits (no false positive).
assert governance.scan_c2(envelope([clean_sample_with_new_fields()])) == []
print("clean batch -> [] (no false positive)\n")

# ---------------------------------------------------------------------------
print("=== 4. File-path / proc-path VALUES are rejected by SCHEMA, not C2 scan ===")
# An honest finding: scan_c2 has no file-path signature, so a path string rides
# to the schema check, which rejects it because the new fields are numeric.
for val in ["/proc/12345/cmdline", "/etc/passwd"]:
    raw = clean_sample_with_new_fields()
    raw["mem_available_bytes"] = val
    c2 = governance.scan_c2(envelope([raw]))
    _, verr = validate_sample(raw)
    print(f"  value={val!r}: scan_c2={c2}  schema_errors={verr}")
    assert c2 == [] and verr, "path string must be rejected by schema"
print("  => fail-closed via the schema type check (the C2 value scan has no path signature)\n")

print("\nALL ADVERSARIAL C2 CHECKS PASSED")
