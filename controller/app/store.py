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


# --- metrics registry: name -> (extractor, unit). Mirrors docs/contracts/node-telemetry.md. ---
# Nested -> flat mapping (frozen in docs/contracts/dashboard.md §1.4): the wire
# telemetry is nested (disk[], net[], gpu[], inference{}) and the controller
# derives flat chart series by taking the FIRST element of each device array.
# The demo has exactly one disk/NIC/GPU per node, so "first element" is the only
# element; day-2 defines per-device series + sum/max aggregation for multi-device
# hosts. `store.py METRICS` is the reference implementation.
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
    # Decommissioned nodes do not count against the inference cap: a retired node
    # must not keep occupying the (single, in the demo) inference slot.
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE project_id = ? AND role = 'inference'"
        " AND decommissioned_at IS NULL",
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
    """Insert or update a node. Returns (node_id, created).

    `reg["lxd_endpoint"]` (optional) is the node's own LXD REST address (host:port
    or a full URL), used by the per-node executor resolver. It is written with
    COALESCE semantics on the UPDATE branch: a re-registration that omits the
    endpoint keeps the previously recorded one rather than clearing it (an older
    node must not silently drop a fleet node back to the global fallback).

    `reg["synthetic"]` (optional, default False) records provenance: True only for
    rows fabricated by the demo seeder (`console/seed_demo.py`), which registers
    through this same endpoint. It is persisted on BOTH branches and is always
    written from the request — a re-registration that stops declaring itself
    synthetic (a real node taking over the record) must clear the flag, otherwise a
    deprioritised row could never come back as real hardware.
    """
    endpoint = reg.get("lxd_endpoint")
    synthetic = 1 if reg.get("synthetic") else 0
    existing = find_node_by_dedup(conn, project_id, reg["registration_id"], reg["machine_id"])
    if existing:
        # node_token_hash and status MUST be updated here too. The controller mints a
        # fresh node token on every registration (server.py) and returns it to the
        # node, so re-registration has to persist its hash — otherwise the token the
        # controller just handed out can never authenticate, and every re-registering
        # node is 403'd on telemetry. `status='online'` for the same reason: a node
        # that just contacted us is online, and leaving the stale value made a
        # re-registered node keep showing OFFLINE on the dashboard.
        # This is why setup-seeder-vm.sh deletes the DB to "resume": only the INSERT
        # path persisted the token, so re-running against a populated DB could not work.
        conn.execute(
            "UPDATE nodes SET hardware_json=?, os_json=?, lxd_json=?, installer_version=?,"
            " hostname_hash=?, role_hint=?, last_seen_at=?, source_ip=?,"
            " node_token_hash=?, synthetic=?, lxd_endpoint=COALESCE(?, lxd_endpoint),"
            " status='online', decommissioned_at=NULL, decommission_reason=NULL"
            " WHERE id=?",
            (
                json.dumps(reg["hardware_json"]),
                json.dumps(reg["os"]) if reg["os"] is not None else None,
                json.dumps(reg["lxd"]) if reg["lxd"] is not None else None,
                reg["installer_version"],
                reg["hostname"],
                reg["role_hint"],
                now,
                source_ip,
                node_token_hash,
                synthetic,
                endpoint,
                existing["id"],
            ),
        )
        return existing["id"], False

    node_id = ulid.new_ulid()
    conn.execute(
        "INSERT INTO nodes (id, project_id, registration_id, machine_id, hostname_hash, role,"
        " node_token_hash, hardware_json, os_json, lxd_json, role_hint, installer_version,"
        " lxd_endpoint, synthetic, status, last_seen_at, registered_at, source_ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)",
        (
            node_id, project_id, reg["registration_id"], reg["machine_id"], reg["hostname"],
            role, node_token_hash, json.dumps(reg["hardware_json"]),
            json.dumps(reg["os"]) if reg["os"] is not None else None,
            json.dumps(reg["lxd"]) if reg["lxd"] is not None else None,
            reg["role_hint"], reg["installer_version"], endpoint, synthetic, now, now, source_ip,
        ),
    )
    return node_id, True


def get_node(conn, node_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def set_node_lxd_endpoint(conn, node_id: str, endpoint: str) -> None:
    """Record the LXD endpoint a node reported on a heartbeat.

    Unlike the registration path this does NOT use COALESCE semantics: the node
    only reports an endpoint while it is listening, so a reported value is the
    current truth and must replace the stored one (a rebuilt node with a new
    address would otherwise keep the old endpoint forever). Callers only pass a
    validated, verified value (`registration.endpoint_from_heartbeat`).
    """
    conn.execute("UPDATE nodes SET lxd_endpoint = ? WHERE id = ?", (endpoint, node_id))


def get_node_by_token_hash(conn, token_hash: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE node_token_hash = ?", (token_hash,)).fetchone()
    return dict(row) if row else None


def list_nodes(conn, project_id: str) -> list[dict]:
    """Live fleet nodes only: a decommissioned node is retired from the listing.

    The row itself survives (telemetry/audit still resolve it), but so the
    dashboard's fleet cannot contradict the deck, a retired node is excluded here
    — this is the ONLY reader that hides them, and every placement/capacity path
    (``_resolve_target``, ``_host_caps``) goes through it, so a retired node is
    never auto-selected or counted again.
    """
    rows = conn.execute(
        "SELECT * FROM nodes WHERE project_id = ? AND decommissioned_at IS NULL"
        " ORDER BY registered_at", (project_id,)
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


# Telemetry provenance channels (`telemetry_samples.source`). A node's own agent
# writes to NODE_SOURCE; the operator's demo fault console writes its injected
# series to DEMO_FAULT_SOURCE. They share the table and the read paths (charts,
# rules, operator loop) but each has its OWN `seq` namespace, so a 1 Hz injected
# series can never occupy a `seq` the node has not sent yet — see
# `insert_samples` and migration 0021.
NODE_SOURCE = "node"
DEMO_FAULT_SOURCE = "demo_fault"


def insert_samples(conn, node_id: str, call_id: str, samples: list[dict],
                   *, source: str = NODE_SOURCE) -> tuple[int, int]:
    """Insert samples, dropping out-of-order/duplicate seq. Returns (inserted, dropped).

    `seq` is compared against the highest stored `seq` for the SAME
    (node, source) channel only. The head is per channel on purpose: the demo
    fault console writes at 1 Hz while the node's agent reports at ~0.3 Hz from
    its own counter, so a shared head meant the injector's seq range silently
    swallowed the node's real samples for minutes after a clear (t_c37aa0dc —
    measured 2 m 19 s on node-c). With separate channels the node's own stream
    resumes on its very next sample.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) AS last_seq FROM telemetry_samples"
        " WHERE node_id = ? AND source = ?",
        (node_id, source),
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
        cur = conn.execute(
            "INSERT OR IGNORE INTO telemetry_samples"
            " (call_id, node_id, source, seq, ts, ts_epoch, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (call_id, node_id, source, seq, s["ts"], ts_epoch, json.dumps(s)),
        )
        if cur.rowcount <= 0:  # a duplicate inside the same channel/batch
            dropped += 1
            continue
        inserted += 1
        last_seq = seq
    return inserted, dropped


def max_seq(conn, node_id: str, source: str = NODE_SOURCE) -> int:
    """Highest `seq` stored for one (node, source) channel; -1 when empty."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) AS last_seq FROM telemetry_samples"
        " WHERE node_id = ? AND source = ?",
        (node_id, source),
    ).fetchone()
    return int(row["last_seq"])



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


def _epoch_to_rfc3339(ts: float) -> str:
    """Format an epoch-second bucket start as RFC3339 with millis (dashboard.md §1.4)."""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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

    for (t_label, t_epoch), slot in sorted(acc.items(), key=lambda kv: kv[0][1]):
        # Contract §1.4: point timestamps are RFC3339. Raw points carry the sample
        # ts verbatim; downsampled buckets are formatted from their epoch second so
        # the dashboard's Date.parse() keeps every point (was epoch seconds).
        ts_iso = t_label if bucket <= 0 else _epoch_to_rfc3339(t_epoch)
        for name in metric_names:
            vals = slot["vals"][name]
            if vals:
                series[name]["points"].append({"t": ts_iso, "v": round(sum(vals) / len(vals), 4)})

    return {"node_id": node_id, "resolution": resolution,
            "series": [series[n] for n in metric_names if n in series]}


# ---------------------------------------------------------------------------
# Node SSH / inventory mapping (GET /nodes/{id}/ssh, terminal bridge)
# ---------------------------------------------------------------------------

def set_node_public_ip(conn, node_id: str, public_ip: str | None) -> None:
    conn.execute("UPDATE nodes SET public_ip = ? WHERE id = ?", (public_ip, node_id))


# ---------------------------------------------------------------------------
# SSH CA certificate ledger (POST /v1/projects/{pid}/ssh-key, D4)
# ---------------------------------------------------------------------------

def insert_ssh_certificate(conn, cert: dict) -> None:
    conn.execute(
        "INSERT INTO ssh_certificates (cert_id, project_id, principal, key_id,"
        " key_algorithm, key_fingerprint, valid_from, valid_until, issued_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cert["cert_id"], cert["project_id"], cert["principal"], cert["key_id"],
            cert["key_algorithm"], cert["key_fingerprint"], cert["valid_from"],
            cert["valid_until"], cert["issued_at"],
        ),
    )


def get_ssh_certificate(conn, fingerprint: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ssh_certificates WHERE key_fingerprint = ?", (fingerprint,)
    ).fetchone()
    return dict(row) if row else None


def revoke_ssh_certificate(conn, fingerprint: str, now: str) -> str | None:
    """Revoke the active cert matching `fingerprint`.

    Returns "revoked" when an active cert was revoked, "already_revoked" when it
    was already revoked, or ``None`` when no such fingerprint was ever issued.
    """
    row = conn.execute(
        "SELECT cert_id, revoked_at FROM ssh_certificates WHERE key_fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return "already_revoked"
    conn.execute(
        "UPDATE ssh_certificates SET revoked_at = ? WHERE cert_id = ?",
        (now, row["cert_id"]),
    )
    return "revoked"


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


# ---------------------------------------------------------------------------
# VM allocation commit (controller-api.md §5.2 step 7)
# ---------------------------------------------------------------------------

def allocated_capacity(conn, node_id: str) -> dict:
    """Committed `agent_vm` capacity for one node, summed across LIVE plans.

    Only rows whose instances are still claimed count: a released allocation
    (`released_at` set) has given its capacity back to the pool, so a node whose
    stale plans were released reports the smaller committed total. This is the
    subtraction the §5.2 step 6 clamp reads (controller-api.md §5.2 step 7).
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cpu_allocated), 0) AS cpu,"
        " COALESCE(SUM(ram_allocated_gb), 0) AS ram,"
        " COALESCE(SUM(disk_allocated_gb), 0) AS disk"
        " FROM vm_allocations WHERE node_id = ? AND released_at IS NULL",
        (node_id,),
    ).fetchone()
    return {
        "cpu_allocated": int(row["cpu"]),
        "ram_allocated_gb": int(row["ram"]),
        "disk_allocated_gb": int(row["disk"]),
    }


def commit_allocation(conn, project_id: str, plan_id: str, node_id: str,
                      cpu_allocated: int, ram_allocated_gb: int,
                      disk_allocated_gb: int, now: str) -> None:
    """Persist one accepted/clamped `agent_vm` class's committed capacity.

    The row starts LIVE (`released_at IS NULL`) with no recorded instances: the
    commit is a reservation, not a materialisation. `_apply_plan` records the
    instances it creates (`set_allocation_instances`) and releases the reservation
    if it cannot create them (`release_allocation`).
    """
    conn.execute(
        "INSERT INTO vm_allocations (id, project_id, plan_id, node_id,"
        " cpu_allocated, ram_allocated_gb, disk_allocated_gb, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ulid.new_ulid(), project_id, plan_id, node_id,
         cpu_allocated, ram_allocated_gb, disk_allocated_gb, now),
    )


def release_allocation(conn, project_id: str, plan_id: str, node_id: str,
                       cpu_allocated: int, ram_allocated_gb: int,
                       disk_allocated_gb: int, now: str | None = None) -> bool:
    """Release the committed capacity of one plan class on a node.

    Failure semantics for apply-plan materialisation: a create that fails or times
    out must not leave committed capacity claiming an instance that does not
    exist, so the failing class's commit is decremented here.

    `vm_allocations` holds ONE row per (project, plan, node) whose columns are the
    SUM across that plan's classes on that node (see `_build_plan`), so this
    decrements the row by the class's own delta. When nothing is left the row is
    SOFT-released (`released_at` set) rather than deleted — the commit and the
    reason stay auditable, and the row no longer counts against capacity. Returns
    True when a LIVE committed row was found and adjusted.
    """
    rows = conn.execute(
        "SELECT id, cpu_allocated, ram_allocated_gb, disk_allocated_gb"
        " FROM vm_allocations WHERE project_id = ? AND plan_id = ? AND node_id = ?"
        " AND released_at IS NULL",
        (project_id, plan_id, node_id),
    ).fetchall()
    if not rows:
        return False
    at = now or now_iso()
    for row in rows:
        cpu = int(row["cpu_allocated"]) - max(0, int(cpu_allocated))
        ram = int(row["ram_allocated_gb"]) - max(0, int(ram_allocated_gb))
        disk = int(row["disk_allocated_gb"]) - max(0, int(disk_allocated_gb))
        if cpu <= 0 and ram <= 0 and disk <= 0:
            conn.execute(
                "UPDATE vm_allocations SET released_at = ?,"
                " release_reason = COALESCE(release_reason, 'materialisation_failed')"
                " WHERE id = ?",
                (at, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE vm_allocations SET cpu_allocated = ?, ram_allocated_gb = ?,"
                " disk_allocated_gb = ? WHERE id = ?",
                (max(0, cpu), max(0, ram), max(0, disk), row["id"]),
            )
    return True


def set_allocation_instances(conn, project_id: str, plan_id: str, node_id: str,
                             vm_ids: list[str]) -> bool:
    """Record the LXD instance names a plan's commit materialised on a node.

    Reconciliation may only release a commit whose instances were materialised and
    are now absent, so the success path of `_apply_plan` records them here. A NULL
    `vm_ids_json` (never applied) is never reconciled — its reservation stands.
    """
    cur = conn.execute(
        "UPDATE vm_allocations SET vm_ids_json = ?"
        " WHERE project_id = ? AND plan_id = ? AND node_id = ? AND released_at IS NULL",
        (json.dumps(list(vm_ids)), project_id, plan_id, node_id),
    )
    return cur.rowcount > 0


def live_allocations(conn, project_id: str) -> list[dict]:
    """Committed-capacity rows still claiming an instance (`released_at` NULL).

    `vm_ids` is the parsed instance-name list recorded at apply time (empty when
    the plan committed but was never materialised).
    """
    rows = conn.execute(
        "SELECT * FROM vm_allocations WHERE project_id = ? AND released_at IS NULL"
        " ORDER BY created_at",
        (project_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("vm_ids_json", None)
        d["vm_ids"] = json.loads(raw) if raw else []
        out.append(d)
    return out


def live_allocation_rows(conn, project_id: str, *, plan_id: str | None = None,
                         node_id: str | None = None) -> list[dict]:
    """Committed-capacity rows still claiming an instance, per node/plan.

    Same LIVE filter as `live_allocations`, but keeps the two facts a *teardown*
    needs and `live_allocations` cannot express:

    * `node_id` — the node the instances were materialised on, so the teardown can
      address that node's own LXD endpoint;
    * `vm_ids_recorded` — whether `vm_ids_json` is NOT NULL, i.e. whether the
      apply path ever wrote down what it created.

    The distinction is the difference between "recorded, and there is nothing to
    delete" (`vm_ids` empty, `vm_ids_recorded` true) and "we do not know what this
    plan materialised" (`vm_ids_recorded` false — an apply interrupted before it
    recorded anything). Teardown must report the second case, never guess names
    and never silently ignore it: a leftover container nobody accounted for is
    exactly the leak this helper exists to make visible.
    """
    where = ["project_id = ?"]
    args: list[Any] = [project_id]
    if plan_id is not None:
        where.append("plan_id = ?")
        args.append(plan_id)
    if node_id is not None:
        where.append("node_id = ?")
        args.append(node_id)
    rows = conn.execute(
        "SELECT * FROM vm_allocations WHERE " + " AND ".join(where) +
        " AND released_at IS NULL ORDER BY created_at",
        args,
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("vm_ids_json", None)
        d["vm_ids"] = json.loads(raw) if raw else []
        d["vm_ids_recorded"] = raw is not None
        out.append(d)
    return out


def release_allocations(conn, project_id: str, *, plan_id: str | None = None,
                        node_id: str | None = None, reason: str | None = None,
                        now: str | None = None) -> dict:
    """Release LIVE committed capacity matching `plan_id` and/or `node_id`.

    Soft-releases each matching LIVE row (marks `released_at`/`release_reason`;
    the row and its audit trail survive) and reports what went back to the pool.
    Idempotent: releasing again finds no LIVE row and reports
    `already_released` — never a double decrement and never an error. `had_commit`
    distinguishes "was once committed" (a repeat release) from "never committed"
    (an unknown plan/node), which the HTTP layer turns into a clean 404.
    """
    if plan_id is None and node_id is None:
        raise ValueError("release_allocations requires plan_id and/or node_id")
    where = ["project_id = ?"]
    args: list[Any] = [project_id]
    if plan_id is not None:
        where.append("plan_id = ?")
        args.append(plan_id)
    if node_id is not None:
        where.append("node_id = ?")
        args.append(node_id)
    where_sql = " AND ".join(where)

    live = conn.execute(
        f"SELECT id, plan_id, node_id, cpu_allocated, ram_allocated_gb, disk_allocated_gb"
        f" FROM vm_allocations WHERE {where_sql} AND released_at IS NULL", args
    ).fetchall()
    ever = conn.execute(
        f"SELECT COUNT(*) AS n FROM vm_allocations WHERE {where_sql}", args
    ).fetchone()["n"]

    at = now or now_iso()
    total = {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0}
    plans: list[str] = []
    for row in live:
        total["cpu_allocated"] += int(row["cpu_allocated"])
        total["ram_allocated_gb"] += int(row["ram_allocated_gb"])
        total["disk_allocated_gb"] += int(row["disk_allocated_gb"])
        if row["plan_id"] not in plans:
            plans.append(row["plan_id"])
        conn.execute(
            "UPDATE vm_allocations SET released_at = ?, release_reason = ? WHERE id = ?",
            (at, reason, row["id"]),
        )
    return {
        "released": total,
        "rows_released": len(live),
        "plans": plans,
        "had_commit": ever > 0,
        "already_released": ever > 0 and not live,
        "released_at": at if live else None,
    }


def decommission_node(conn, project_id: str, node_id: str,
                      reason: str | None = None, now: str | None = None) -> dict | None:
    """Retire a node: release its LIVE committed capacity and drop it from the fleet.

    Idempotent — a node already retired is reported `already_decommissioned`
    without touching it again. Returns ``None`` when no such node exists in the
    project (the HTTP layer turns that into a 404). The node row is kept (soft
    retire) so its telemetry and audit history still resolve.
    """
    row = conn.execute(
        "SELECT id, decommissioned_at FROM nodes WHERE id = ? AND project_id = ?",
        (node_id, project_id),
    ).fetchone()
    if row is None:
        return None
    if row["decommissioned_at"] is not None:
        released = release_allocations(conn, project_id, node_id=node_id)
        return {
            "node_id": node_id, "status": "already_decommissioned",
            "decommissioned_at": row["decommissioned_at"],
            "released": released["released"], "rows_released": released["rows_released"],
        }
    at = now or now_iso()
    conn.execute(
        "UPDATE nodes SET decommissioned_at = ?, decommission_reason = ? WHERE id = ?",
        (at, reason, node_id),
    )
    released = release_allocations(
        conn, project_id, node_id=node_id, reason="node_decommissioned", now=at)
    return {
        "node_id": node_id, "status": "decommissioned", "decommissioned_at": at,
        "released": released["released"], "rows_released": released["rows_released"],
    }
