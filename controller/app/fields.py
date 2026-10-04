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
    "disk", "net", "gpu", "lxd_instances", "inference", "services",
}

DISK_FIELDS: dict[str, dict] = {
    "path": {"kind": "str", "unit": None, "cclass": "C0"},
    "total_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 1},
    "free_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "write_latency_ms": {"kind": "float", "unit": "ms", "cclass": "C0", "min": 0},
    "mount_ro": {"kind": "bool", "unit": None, "cclass": "C0"},
}

NET_FIELDS: dict[str, dict] = {
    "iface": {"kind": "str", "unit": None, "cclass": "C0"},
    "rx_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "tx_bytes": {"kind": "int", "unit": "bytes", "cclass": "C0", "min": 0},
    "latency_ms": {"kind": "float", "unit": "ms", "cclass": "C0", "min": 0},
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
}

INFERENCE_FIELDS: dict[str, dict] = {
    "server_up": {"kind": "bool", "unit": None, "cclass": "C0"},
    "model": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["deepseek-flash", "deepseek-v4-pro"]},
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
}

# Top-level sample container keys (list/object fields).
SAMPLE_CONTAINERS: dict[str, dict] = {
    "disk": {"kind": "list", "cclass": "C0", "fields": DISK_FIELDS},
    "net": {"kind": "list", "cclass": "C0", "fields": NET_FIELDS},
    "gpu": {"kind": "list", "cclass": "C0", "fields": GPU_FIELDS},
    "lxd_instances": {"kind": "list", "cclass": "C0", "fields": LXD_INSTANCE_FIELDS},
    "services": {"kind": "list", "cclass": "C0", "fields": SERVICE_FIELDS},
    "inference": {"kind": "obj", "cclass": "C0", "fields": INFERENCE_FIELDS},
}

ALL_SAMPLE_KEYS = set(SAMPLE_FIELDS) | set(SAMPLE_CONTAINERS)

# ---------------------------------------------------------------------------
# Batch envelope
# ---------------------------------------------------------------------------
REDACTION_FIELDS: dict[str, dict] = {
    "version": {"kind": "str", "unit": None, "cclass": "C0"},
    "config_hash": {"kind": "str", "unit": None, "cclass": "C0"},
    "result": {"kind": "str", "unit": None, "cclass": "C0", "enum": ["ok"]},
}

ENVELOPE_REQUIRED = {"_redaction", "call_id", "node_id", "schema_version", "governance_chain_tip", "samples"}
ENVELOPE_OPTIONAL = set()
