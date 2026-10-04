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
def _first(arr: Any) -> dict:
    return arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], dict) else {}


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
    "gpu_util_pct": (lambda s: _first(s.get("gpu")).get("util_pct"), "percent"),
    "gpu_vram_used_bytes": (lambda s: _first(s.get("gpu")).get("vram_used_bytes"), "bytes"),
    "gpu_vram_total_bytes": (lambda s: _first(s.get("gpu")).get("vram_total_bytes"), "bytes"),
    "net_rx_bytes": (lambda s: _first(s.get("net")).get("rx_bytes"), "bytes"),
    "net_tx_bytes": (lambda s: _first(s.get("net")).get("tx_bytes"), "bytes"),
    "net_latency_ms": (lambda s: _first(s.get("net")).get("latency_ms"), "ms"),
    "disk_total_bytes": (lambda s: _first(s.get("disk")).get("total_bytes"), "bytes"),
    "disk_free_bytes": (lambda s: _first(s.get("disk")).get("free_bytes"), "bytes"),
    "disk_write_latency_ms": (lambda s: _first(s.get("disk")).get("write_latency_ms"), "ms"),
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
