"""Canonical telemetry field allowlist — the single source of truth for the
enforced schema.

This mirrors `docs/contracts/node-telemetry.md` §2–§4 exactly (field names,
types, units, ranges, enums, and C-classes). The ingest validator (`validate.py`)
and the governance gate (`governance.py`) both consume this table, and
`docs/contracts/node-telemetry.md` is written from it, so the enforced schema and
the published schema cannot drift.

Field kinds:
  int   — JSON integer (bool rejected)
  float — JSON number (int accepted, bool rejected)
  str   — JSON string
  bool  — JSON boolean
  list  — JSON array of objects (validated per-element via the `element` spec)
  obj   — JSON object (validated per-field via the `fields` spec)

Range/enum/pattern constraints are checked exactly. Unknown fields are stripped
(deny-by-default); a *present* field that violates its constraint rejects the
sample.
"""
from __future__ import annotations

# A present field that matches one of these shapes is a C2 signature and
# fail-closes the batch. Field-name denylist lives in governance.py.
C2_FIELD_NAMES = {
    "log", "logs", "log_line", "syslog", "message", "msg", "prompt", "content",
    "text", "body", "payload", "args", "argv", "cmdline", "cmd", "command",
    "env", "environ", "environment", "file", "files", "token", "secret",
    "password", "credential", "ssh_key", "private_key", "serial", "asset_tag",
    "user", "username",
    # Added: the remaining governance.md §4.3 classes that had no field-name
    # term. Matching is token-based (see governance.is_c2_field_name), so these
    # also catch `customer_notes`, `conversation_log`, `doc_content`, etc.
    "conversation", "transcript", "document", "doc", "customer", "certificate",
    # Abbreviations for the same classes: `cert_pem` is certificate material just
    # as `certificate` is (caught by the test suite, not by inspection).
    "cert", "pem",
    # Process *paths* — the executable the process runs and its working
    # directory. Both are file paths, which §4.3 lists as C2 ("file contents or
    # paths under customer data dirs"): `/proc/<pid>/exe` and `cwd` name customer
    # locations just as a command line does. Added with `processes[]` (t_957956e5)
    # — not because that field carries them (it does not), but so a node that
    # tries anyway is FAIL-CLOSED with a 422 instead of being silently stripped.
    # No allowlisted field tokenises to either (pinned in tests).
    "exe", "cwd",
}

ULID_PATTERN = "^[0-9A-HJKMNP-TV-Z]{26}$"
RFC3339_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)

# ---------------------------------------------------------------------------
# Sample scalar fields
# ---------------------------------------------------------------------------
SAMPLE_FIELDS: dict[str, dict] = {
    # --- frame fields (required) ---
    "ts": {"kind": "str", "unit": None, "cclass": "C0", "rfc3339": True},
    "seq": {"kind": "int", "unit": None, "cclass": "C0", "min": 0},
    # --- host metrics (C0) ---
    "uptime_sec": {"kind": "int", "unit": "s", "cclass": "C0", "min": 0},
    "cpu_util_pct": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "cpu_core_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 1},
    "ram_total_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 1},
    "ram_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "swap_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "load_avg_1m": {"kind": "float", "unit": "loadavg", "cclass": "C0", "min": 0},
    "cpu_temp_celsius": {"kind": "float", "unit": "celsius", "cclass": "C0", "min": 0},
    "os_name": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["Ubuntu"]},
    "os_version": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["24.04"]},
    "kernel_version": {"kind": "str", "unit": None, "cclass": "C0"},
    "lxd_cluster_role": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["member"]},
    # --- §4.12 pressure + memory-pressure scalars (C0, research t_cae0dfa8) ---
    # Cumulative counters from /proc/vmstat and /proc/meminfo. Omitted, never 0,
    # when the source is unreadable (node-telemetry.md §4.12).
    "oom_kill_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "pgmajfault_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "mem_available_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    # --- §4.13 transport / conntrack scalars (C0, research t_cae0dfa8) ---
    "tcp_retransmit_segs": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    # Count-only: the conntrack *table* (/proc/net/nf_conntrack) is C2 and must
    # never be read for telemetry — only these two count files are C0 (governance
    # §4.1.1). `conntrack_max` is a table capacity, so it is never 0.
    "conntrack_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "conntrack_max": {"kind": "int", "unit": "count", "cclass": "C0", "min": 1},
    # --- identifiers (C1, must arrive already transformed) ---
    "hostname": {"kind": "str", "unit": None, "cclass": "C1", "pattern": "^[0-9a-f]{16}$"},
    "ip_local": {"kind": "str", "unit": None, "cclass": "C1", "c1_ip": True},
    "mac_address": {"kind": "str", "unit": None, "cclass": "C1", "pattern": "^[0-9a-f]{16}$"},
    "project_id": {"kind": "str", "unit": None, "cclass": "C1", "ulid": True},
}

# Fields that count toward the "at least one metric field" requirement.
METRIC_FIELDS = {
    "uptime_sec", "cpu_util_pct", "cpu_core_count", "ram_total_bytes",
    "ram_used_bytes", "swap_used_bytes", "load_avg_1m", "cpu_temp_celsius",
    "disk", "net", "gpu", "lxd_instances", "inference", "services", "processes",
    # §4.12–§4.14 (research t_cae0dfa8): pressure, vmstat/meminfo scalars, and
    # the transport/conntrack scalars are metrics in their own right.
    "pressure", "oom_kill_count", "pgmajfault_count", "mem_available_bytes",
    "tcp_retransmit_segs", "conntrack_count", "conntrack_max",
}

DISK_FIELDS: dict[str, dict] = {
    "path": {"kind": "str", "unit": None, "cclass": "C0"},
    "total_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 1},
    "free_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "write_latency_ms": {"kind": "float", "unit": "ms", "cclass": "C0", "min": 0},
    "mount_ro": {"kind": "bool", "unit": None, "cclass": "C0"},
    # §4.13 disk I/O + inode extension (C0, research t_cae0dfa8): the raw
    # saturation signal behind write_latency_ms, plus inode exhaustion (a full
    # inode table fails writes while free_bytes looks healthy). Omitted, never
    # 0, when /sys/block/<dev>/stat or statvfs is unreadable.
    "io_ticks_ms": {"kind": "int", "unit": "ms", "cclass": "C0", "min": 0},
    "io_in_flight": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "inode_used": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "inode_total": {"kind": "int", "unit": "count", "cclass": "C0", "min": 1},
}

NET_FIELDS: dict[str, dict] = {
    "iface": {"kind": "str", "unit": None, "cclass": "C0"},
    "rx_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "tx_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "latency_ms": {"kind": "float", "unit": "ms", "cclass": "C0", "min": 0},
    # §4.13 NIC error/drop counters (C0, research t_cae0dfa8): explains
    # latency-01 — high latency with rising drops is an underlay fault, high
    # latency with clean counters is application-level. Omitted, never 0, when
    # /sys/class/net/<iface>/statistics is unreadable.
    "rx_errors": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "tx_errors": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "rx_dropped": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "tx_dropped": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
}

GPU_FIELDS: dict[str, dict] = {
    "model": {"kind": "str", "unit": None, "cclass": "C0"},
    "util_pct": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "vram_total_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 1},
    "vram_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "temp_celsius": {"kind": "float", "unit": "celsius", "cclass": "C0", "min": 0},
}

LXD_INSTANCE_FIELDS: dict[str, dict] = {
    "vm_id": {"kind": "str", "unit": None, "cclass": "C0", "ulid": True},
    "state": {
        "kind": "str", "unit": None, "cclass": "C0",
        "enum": ["running", "stopped", "error", "starting", "paused", "frozen",
                 "migrating", "idle"],
    },
    "cpu_util_pct": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "ram_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "ram_limit_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "disk_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    # Operational fields an incident needs (node-telemetry.md §4.6, t_957956e5).
    # `last_exit_code` is deliberately absent: LXD exposes no exit status for an
    # instance, and a field that could only ever be invented is worse than none.
    "state_age_sec": {"kind": "int", "unit": "s", "cclass": "C0", "min": 0},
    "restart_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    # §4.14 per-instance cgroup counters (C0, research t_cae0dfa8): OOM and
    # major-fault attribution per instance, sourced from the instance cgroup
    # (/sys/fs/cgroup/lxc.payload.<name>/memory.events + memory.stat) — NOT the
    # LXD API, which does not expose them. Omitted, never 0, when the cgroup
    # file is unreadable.
    "oom_kill_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "pgmajfault_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
}

INFERENCE_FIELDS: dict[str, dict] = {
    "server_up": {"kind": "bool", "unit": None, "cclass": "C0"},
    "model": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["gemma", "deepseek-flash", "deepseek-v4-pro"]},
    "active_requests": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "queue_depth": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "tokens_per_s": {"kind": "float", "unit": "tok/s", "cclass": "C0", "min": 0},
    "vram_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
}

SERVICE_FIELDS: dict[str, dict] = {
    "name": {
        "kind": "str", "unit": None, "cclass": "C0",
        "enum": ["termini-bootstrap", "lxd", "sshd", "llama-cpp"],
    },
    "status": {
        "kind": "str", "unit": None, "cclass": "C0",
        "enum": ["running", "failed", "stopped", "exited"],
    },
    # Operational fields (§4.8, t_957956e5): systemd reports these, so they are
    # real measurements rather than inferences. Exactly one service per
    # allowlisted name, matching the rest of §4.8.
    "state_age_sec": {"kind": "int", "unit": "s", "cclass": "C0", "min": 0},
    "restart_count": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    # ExecMainStatus can legitimately be -1 (unit exec not started / signal).
    "last_exit_code": {"kind": "int", "unit": None, "cclass": "C0", "min": -1, "max": 255},
    # Counts, never lines (§4.11). The messages behind them stay C2.
    "journal_errors": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
    "journal_warnings": {"kind": "int", "unit": "count", "cclass": "C0", "min": 0},
}

# `processes[]` — node-telemetry.md §4.10, C0, metrics only.
#
# Deny-by-default review (t_957956e5) admitted process METRICS and nothing else:
# the kernel task name (`comm`), the kernel state, and the counters. `argv`,
# `cmdline`, `exe`, `environ`, `cwd` and any file path are NOT fields here — they
# are C2 by name (governance.md §4.3) and are rejected at ingest, at this depth,
# by the C2 field-name scan.
#
# `name` is the only free-form string in the group, so it carries a pattern that
# makes prose/PII structurally impossible: printable ASCII, ≤15 chars — exactly
# the kernel's TASK_COMM_LEN. The C2 value scan also applies to it (it is not in
# `_SKIP_STRING_SCAN`), so an email/IP/MAC/card/KEY string cannot ride along.
PROCESS_FIELDS: dict[str, dict] = {
    "pid": {"kind": "int", "unit": None, "cclass": "C0", "min": 1},
    "name": {"kind": "str", "unit": None, "cclass": "C0", "pattern": r"^[\x20-\x7e]{1,15}$"},
    "state": {
        "kind": "str", "unit": None, "cclass": "C0",
        "enum": ["running", "sleeping", "disk_sleep", "zombie", "stopped",
                 "tracing", "dead"],
    },
    "cpu_util_pct": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "ram_used_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "threads": {"kind": "int", "unit": "count", "cclass": "C0", "min": 1},
    "age_sec": {"kind": "int", "unit": "s", "cclass": "C0", "min": 0},
}

# Top-N bound for `processes[]` (§4.10). The owner's note of 2026-10-16 set the
# window at **top-10 per sample, ranked by RSS**, on the existing cadence and with
# no new retention tier; §4.10 records that decision and the objection to it. The
# bound is not a target the collector may undershoot silently: ingest rejects an
# over-long array rather than truncating it, so an over-eager collector is a loud
# defect instead of a quiet data loss.
PROCESSES_MAX = 10

# `pressure{}` — node-telemetry.md §4.12, C0. The four PSI "some/full × avg10"
# scalars from /proc/pressure/{cpu,io,memory}. Fixed four-key object (research
# card rows 1+12): no array, no free-form string, each value a percentage 0–100.
# The whole object is omitted (never an empty `{}`, never 0s) when /proc/pressure
# is unreadable — that is the collector's job, and contract §4.12 pins it.
PRESSURE_FIELDS: dict[str, dict] = {
    "cpu_some_avg10": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "io_some_avg10": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "memory_some_avg10": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
    "memory_full_avg10": {"kind": "float", "unit": "percent", "cclass": "C0", "min": 0, "max": 100},
}

# Cost/bounds honesty (node-telemetry.md §4.12–§4.14): the worst case for the
# new fields is a sample carrying every one of them at its maximum representable
# magnitude (64-bit counters at 20 digits, PSI at 100.0, one worst-case entry
# each for disk[]/net[]/lxd_instances[]). Measured serialized size (compact
# separators): 744 bytes. This budget is the ceiling a test pins, so a 1 s
# cadence cannot silently grow the ingest payload (it stays far under the 16 KiB
# whole-batch cap of governance.md §5.2). Pinned by
# tests/test_telemetry_metric_expansion.py.
METRIC_EXPANSION_MAX_BYTES = 1024

# Top-level sample container keys (list/object fields).
SAMPLE_CONTAINERS: dict[str, dict] = {
    "disk": {"kind": "list", "cclass": "C0", "fields": DISK_FIELDS},
    "net": {"kind": "list", "cclass": "C0", "fields": NET_FIELDS},
    "gpu": {"kind": "list", "cclass": "C0", "fields": GPU_FIELDS},
    "lxd_instances": {"kind": "list", "cclass": "C0", "fields": LXD_INSTANCE_FIELDS},
    "services": {"kind": "list", "cclass": "C0", "fields": SERVICE_FIELDS},
    "inference": {"kind": "obj", "cclass": "C0", "fields": INFERENCE_FIELDS},
    "pressure": {"kind": "obj", "cclass": "C0", "fields": PRESSURE_FIELDS},
    "processes": {"kind": "list", "cclass": "C0", "fields": PROCESS_FIELDS,
                  "max_items": PROCESSES_MAX},
}

ALL_SAMPLE_KEYS = set(SAMPLE_FIELDS) | set(SAMPLE_CONTAINERS)

# ---------------------------------------------------------------------------
# Batch envelope
# ---------------------------------------------------------------------------
REDACTION_FIELDS: dict[str, dict] = {
    "version": {"kind": "str", "unit": None, "cclass": "C0"},
    # A redactor config digest. Hex, but an all-digit string of 13-16 characters is
    # rejected: that is exactly a card-number (PAN) shape, and it is indistinguishable
    # from a digest. Found by testing rather than reading — a plain hex pattern let
    # `4111111111111111` through and it was STORED on a live controller. The scan
    # cannot cover this field (it is exempt, and scanning it false-positives real
    # digests as phone numbers), so the constraint has to live in the format.
    "config_hash": {
        "kind": "str", "unit": None, "cclass": "C0",
        "pattern": r"^(?![0-9]{13,16}$)[0-9a-fA-F]{4,128}$",
    },
    "result": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["ok"]},
}

ENVELOPE_REQUIRED = {"_redaction", "call_id", "node_id", "schema_version", "governance_chain_tip", "samples"}
ENVELOPE_OPTIONAL = set()
