"""Data-access layer over SQLite (see app/db.py). All operations take an open
connection and run inside the caller's transaction via `db.session`.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any

from . import fields as F
from . import ulid

GiB = 1024 ** 3


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- metrics registry: name -> (extractor, unit). Mirrors docs/telemetry-schema.md. ---
# Nested -> flat mapping (resolves W1 review A6: the flattening is now defined).
# The node emits *nested* device objects (disk[], net[], gpu[], inference{}); the
# dashboard charts consume *flat* series. Aggregation rules, frozen here:
#
#   * bytes/counter metrics aggregate by SUM across all devices/interfaces
#     (disk_total_bytes = sum of every disk's total_bytes, net_rx_bytes = sum of
#     every interface's rx_bytes, gpu_vram_* = sum across GPUs).
#   * utilization / latency metrics take the MAX across devices (the "worst
#     device drives the chart" convention: gpu_util_pct, disk_write_latency_ms,
#     net_latency_ms).
#   * inference_* passthrough from the single inference{} object.
#
# The demo has one disk / one NIC / one GPU, so SUM == MAX == first for every
# series; the rules above are what keep the contract unambiguous for multi-device
# hosts.
def _arr(sample: dict, key: str) -> list:
    a = sample.get(key)
    return a if isinstance(a, list) else []


def _sum_of(key: str):
    def fn(sample: dict) -> Any:
        vals = [d.get(key) for d in _arr(sample, _CONTAINER_OF[key]) if isinstance(d, dict)]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return sum(nums) if nums else None
    return fn


def _max_of(key: str):
    def fn(sample: dict) -> Any:
        vals = [d.get(key) for d in _arr(sample, _CONTAINER_OF[key]) if isinstance(d, dict)]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return max(nums) if nums else None
    return fn


# Which nested container each flat device metric is drawn from.
_CONTAINER_OF = {
    "util_pct": "gpu",
    "vram_used_bytes": "gpu",
    "vram_total_bytes": "gpu",
    "rx_bytes": "net",
    "tx_bytes": "net",
    "latency_ms": "net",
    "total_bytes": "disk",
    "free_bytes": "disk",
    "write_latency_ms": "disk",
}


def _inf(sample: dict) -> dict:
    return sample.get("inference") if isinstance(sample.get("inference"), dict) else {}


METRICS: dict[str, tuple[Any, str]] = {
    "cpu_util_pct": (lambda s: s.get("cpu_util_pct"), "percent"),
    "cpu_core_count": (lambda s: s.get("cpu_core_count"), "count"),
    "ram_total_bytes": (lambda s: s.get("ram_total_bytes"), "bytes"),
    "ram_used_bytes": (lambda s: s.get("ram_used_bytes"), "bytes"),
    "swap_used_bytes": (lambda s: s.get("swap_used_bytes"), "bytes"),
    "load_avg_1m": (lambda s: s.get("load_avg_1m"), "loadavg"),
    "cpu_temp_celsius": (lambda s: s.get("cpu_temp_celsius"), "celsius"),
    "gpu_util_pct": (_max_of("util_pct"), "percent"),
    "gpu_vram_used_bytes": (_sum_of("vram_used_bytes"), "bytes"),
    "gpu_vram_total_bytes": (_sum_of("vram_total_bytes"), "bytes"),
    "net_rx_bytes": (_sum_of("rx_bytes"), "bytes"),
    "net_tx_bytes": (_sum_of("tx_bytes"), "bytes"),
    "net_latency_ms": (_max_of("latency_ms"), "ms"),
    "disk_total_bytes": (_sum_of("total_bytes"), "bytes"),
    "disk_free_bytes": (_sum_of("free_bytes"), "bytes"),
    "disk_write_latency_ms": (_max_of("write_latency_ms"), "ms"),
    "inference_tokens_per_s": (lambda s: _inf(s).get("tokens_per_s"), "tok/s"),
    "inference_active_requests": (lambda s: _inf(s).get("active_requests"), "count"),
    "inference_vram_used_bytes": (lambda s: _inf(s).get("vram_used_bytes"), "bytes"),
}


def ensure_project(conn, cfg, now: str) -> tuple[dict, str | None]:
    """Get-or-create the single demo project. Returns (project, generated_token)."""
    row = conn.execute("SELECT * FROM projects ORDER BY created_at LIMIT 1").fetchone()
    if row is not None:
        return dict(row), None

    project_id = cfg.project_id or ulid.new_ulid()
    if cfg.project_tokens:
        token = cfg.project_tokens[0]
        generated = None
    else:
        token = ulid.new_ulid() + ulid.new_ulid()  # 52-char opaque token
        generated = token

    conn.execute(
        "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (project_id, "termini-demo", sha256(token), cfg.max_inference_nodes, now),
    )
    return (
        {"id": project_id, "name": "termini-demo", "project_token_hash": sha256(token),
         "max_inference_nodes": cfg.max_inference_nodes, "created_at": now},
        generated,
    )


def project_token_hash(conn) -> str:
    row = conn.execute("SELECT project_token_hash FROM projects LIMIT 1").fetchone()
    return row["project_token_hash"] if row else ""


def count_inference_nodes(conn, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE project_id = ? AND role = 'inference'",
        (project_id,),
    ).fetchone()
    return row["n"]


def find_node_by_dedup(conn, project_id: str, registration_id: str, machine_id: str | None) -> dict | None:
    if machine_id:
        row = conn.execute(
            "SELECT * FROM nodes WHERE project_id = ? AND machine_id = ?",
            (project_id, machine_id),
        ).fetchone()
        if row:
            return dict(row)
    row = conn.execute(
        "SELECT * FROM nodes WHERE project_id = ? AND registration_id = ?",
        (project_id, registration_id),
    ).fetchone()
    return dict(row) if row else None


def upsert_node(
    conn,
    project_id: str,
    reg: dict,
    role: str,
    node_token_hash: str,
    source_ip: str | None,
    now: str,
) -> tuple[str, bool]:
    """Insert or update a node. Returns (node_id, created)."""
    existing = find_node_by_dedup(conn, project_id, reg["registration_id"], reg["machine_id"])
    if existing:
        conn.execute(
            "UPDATE nodes SET hardware_json=?, os_json=?, lxd_json=?, installer_version=?,"
            " hostname_hash=?, role_hint=?, last_seen_at=?, source_ip=? WHERE id=?",
            (
                json.dumps(reg["hardware_json"]),
                json.dumps(reg["os"]) if reg["os"] is not None else None,
                json.dumps(reg["lxd"]) if reg["lxd"] is not None else None,
                reg["installer_version"],
                reg["hostname"],
                reg["role_hint"],
                now,
                source_ip,
                existing["id"],
            ),
        )
        return existing["id"], False

    node_id = ulid.new_ulid()
    conn.execute(
        "INSERT INTO nodes (id, project_id, registration_id, machine_id, hostname_hash, role,"
        " node_token_hash, hardware_json, os_json, lxd_json, role_hint, installer_version,"
        " status, last_seen_at, registered_at, source_ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)",
        (
            node_id, project_id, reg["registration_id"], reg["machine_id"], reg["hostname"],
            role, node_token_hash, json.dumps(reg["hardware_json"]),
            json.dumps(reg["os"]) if reg["os"] is not None else None,
            json.dumps(reg["lxd"]) if reg["lxd"] is not None else None,
            reg["role_hint"], reg["installer_version"], now, now, source_ip,
        ),
    )
    return node_id, True


def get_node(conn, node_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def get_node_by_token_hash(conn, token_hash: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE node_token_hash = ?", (token_hash,)).fetchone()
    return dict(row) if row else None


def list_nodes(conn, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM nodes WHERE project_id = ? ORDER BY registered_at", (project_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def burn_bootstrap_token(conn, token: str, node_id: str | None, now: str) -> bool:
    """Consume a single-use bootstrap token. Returns True if it was valid+unused."""
    h = sha256(token)
    row = conn.execute("SELECT * FROM bootstrap_tokens WHERE token_hash = ?", (h,)).fetchone()
    if row is None or row["used_at"] is not None:
        return False
    conn.execute(
        "UPDATE bootstrap_tokens SET used_at = ?, used_by_node_id = ? WHERE token_hash = ?",
        (now, node_id, h),
    )
    return True


def seed_bootstrap_tokens(conn, tokens: list[str], now: str) -> None:
    for t in tokens:
        h = sha256(t)
        exists = conn.execute(
            "SELECT 1 FROM bootstrap_tokens WHERE token_hash = ?", (h,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO bootstrap_tokens (token_hash, created_at) VALUES (?, ?)", (h, now)
            )


def insert_batch(conn, batch: dict, node_id: str, accepted: int, rejected: int, now: str) -> None:
    red = batch.get("_redaction") or {}
    conn.execute(
        "INSERT INTO telemetry_batches (call_id, node_id, ts_received, schema_version,"
        " governance_chain_tip, redactor_version, redactor_config_hash, accepted_count,"
        " rejected_count, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            batch["call_id"], node_id, now, batch.get("schema_version", 1),
            batch.get("governance_chain_tip"),
            red.get("version"), red.get("config_hash"),
            accepted, rejected, json.dumps(batch),
        ),
    )


def insert_samples(conn, node_id: str, call_id: str, samples: list[dict]) -> tuple[int, int]:
    """Insert samples, dropping out-of-order/duplicate seq. Returns (inserted, dropped)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) AS last_seq FROM telemetry_samples WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    last_seq = row["last_seq"]
    inserted = 0
    dropped = 0
    for s in samples:
        seq = s.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
            dropped += 1
            continue
        ts_epoch = _ts_epoch(s["ts"])
        conn.execute(
            "INSERT OR IGNORE INTO telemetry_samples (call_id, node_id, seq, ts, ts_epoch, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (call_id, node_id, seq, s["ts"], ts_epoch, json.dumps(s)),
        )
        inserted += 1
        last_seq = seq
    return inserted, dropped


def _ts_epoch(ts: str) -> float:
    from .validate import parse_rfc3339_epoch

    return parse_rfc3339_epoch(ts)


def recent_samples(conn, node_id: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM telemetry_samples WHERE node_id = ? ORDER BY ts_epoch DESC, seq DESC LIMIT ?",
        (node_id, limit),
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def latest_sample(conn, node_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM telemetry_samples WHERE node_id = ? ORDER BY ts_epoch DESC, seq DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def metric_series(
    conn, node_id: str, start: float | None, end: float | None,
    metric_names: list[str], resolution: str,
) -> dict:
    """Build downsampled time-series for the requested metrics."""
    q = "SELECT ts_epoch, payload_json FROM telemetry_samples WHERE node_id = ?"
    args: list[Any] = [node_id]
    if start is not None:
        q += " AND ts_epoch >= ?"
        args.append(start)
    if end is not None:
        q += " AND ts_epoch <= ?"
        args.append(end)
    q += " ORDER BY ts_epoch ASC"
    rows = conn.execute(q, args).fetchall()

    bucket = {"raw": 0.0, "5s": 5.0, "1m": 60.0, "5m": 300.0}.get(resolution, 0.0)

    series: dict[str, dict[str, Any]] = {}
    for name in metric_names:
        if name in METRICS:
            series[name] = {"name": name, "unit": METRICS[name][1], "points": []}

    # accumulate (bucket_key -> {metric: [values], ts})
    acc: dict[tuple[str, float], dict] = {}
    for r in rows:
        sample = json.loads(r["payload_json"])
        ts_epoch = r["ts_epoch"]
        key = (sample["ts"], ts_epoch)
        if bucket > 0:
            b = int(ts_epoch // bucket) * bucket
            key = (str(b), float(b))
        slot = acc.setdefault(key, {"vals": {m: [] for m in metric_names}})
        for name in metric_names:
            fn = METRICS[name][0]
            v = fn(sample)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                slot["vals"][name].append(v)

    for (t_label, _), slot in sorted(acc.items(), key=lambda kv: kv[0][1]):
        for name in metric_names:
            vals = slot["vals"][name]
            if vals:
                series[name]["points"].append({"t": t_label, "v": round(sum(vals) / len(vals), 4)})

    return {"node_id": node_id, "resolution": resolution,
            "series": [series[n] for n in metric_names if n in series]}


# ---------------------------------------------------------------------------
# Node SSH / inventory mapping (GET /nodes/{id}/ssh, terminal bridge)
# ---------------------------------------------------------------------------

def set_node_public_ip(conn, node_id: str, public_ip: str | None) -> None:
    conn.execute("UPDATE nodes SET public_ip = ? WHERE id = ?", (public_ip, node_id))


# ---------------------------------------------------------------------------
# Remediation / operation commands (dashboard.md §4.4, controller-api.md §6)
# ---------------------------------------------------------------------------

def insert_command(conn, cmd: dict) -> None:
    conn.execute(
        "INSERT INTO commands (command_id, project_id, node_id, type, payload_json,"
        " approval, state, issued_by, issued_at, timeout_s, trigger_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cmd["command_id"], cmd["project_id"], cmd.get("node_id"), cmd["type"],
            json.dumps(cmd.get("payload") or {}), cmd["approval"], cmd["state"],
            cmd["issued_by"], cmd["issued_at"], cmd.get("timeout_s", 60),
            json.dumps(cmd["trigger"]) if cmd.get("trigger") else None,
        ),
    )


def get_command(conn, command_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
    return _command_row(row) if row else None


def list_commands(conn, project_id: str, state: str | None = None) -> list[dict]:
    q = "SELECT * FROM commands WHERE project_id = ?"
    args: list[Any] = [project_id]
    if state:
        q += " AND state = ?"
        args.append(state)
    q += " ORDER BY issued_at DESC"
    return [_command_row(r) for r in conn.execute(q, args).fetchall()]


def pending_approval_commands(conn, project_id: str) -> list[dict]:
    return list_commands(conn, project_id, state="pending_approval")


def _command_row(row) -> dict:
    c = dict(row)
    c["payload"] = json.loads(c.pop("payload_json") or "{}")
    c["trigger"] = json.loads(c.pop("trigger_json")) if c.get("trigger_json") else None
    c["result"] = json.loads(c.pop("result_json")) if c.get("result_json") else None
    return c


def transition_command(conn, command_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE commands SET {cols} WHERE command_id = ?", (*fields.values(), command_id))


# ---------------------------------------------------------------------------
# Operator log store (SSE/WS backfill tail)
# ---------------------------------------------------------------------------

def insert_log(conn, log: dict) -> None:
    conn.execute(
        "INSERT INTO operator_logs (log_id, project_id, ts, node_id, severity, actor,"
        " message, remediation_json, truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            log["log_id"], log["project_id"], log["ts"], log.get("node_id"),
            log["severity"], log["actor"], log["message"],
            json.dumps(log["remediation"]) if log.get("remediation") else None,
            1 if log.get("truncated") else 0,
        ),
    )


def logs_since(conn, project_id: str, since_log_id: str | None, limit: int = 10000) -> list[dict]:
    q = "SELECT * FROM operator_logs WHERE project_id = ?"
    args: list[Any] = [project_id]
    if since_log_id:
        q += " AND log_id > ?"
        args.append(since_log_id)
    q += " ORDER BY log_id ASC LIMIT ?"
    args.append(limit)
    rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["remediation"] = json.loads(d.pop("remediation_json")) if d.get("remediation_json") else None
        d["truncated"] = bool(d.get("truncated"))
        out.append(d)
    return out


def trim_logs(conn, project_id: str, ring_size: int) -> None:
    """Drop the oldest log rows beyond `ring_size` for the project."""
    conn.execute(
        "DELETE FROM operator_logs WHERE project_id = ? AND log_id NOT IN ("
        " SELECT log_id FROM operator_logs WHERE project_id = ?"
        " ORDER BY log_id DESC LIMIT ?)",
        (project_id, project_id, ring_size),
    )
