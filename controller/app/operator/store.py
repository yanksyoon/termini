"""Persistence for the operator loop: health events, remediation decisions
(decision log), and remediation audits. All functions take an open connection
and run inside the caller's transaction, matching `app/store.py` conventions.

Idempotency is enforced here at the schema level:
  * `remediation_decisions.event_id` is UNIQUE — one health event can produce at
    most one decision, so a double-fired event can never issue two commands.
  * `remediation_audits.idempotency_key` is UNIQUE — a repeated (node, rule,
    target) remediation within the cooldown window is deduplicated before any
    LXD call is made.
"""
from __future__ import annotations

import json
import time

from .. import ulid

EVIDENCE_SAMPLE_CAP = 20  # bound the evidence window we persist


def insert_health_event(conn, event) -> None:
    conn.execute(
        "INSERT INTO health_events (id, node_id, rule_id, type, metric, value,"
        " threshold, evidence_seq_start, evidence_seq_end, evidence_json, fired_at,"
        " breach_started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id, event.node_id, event.rule_id, event.type, event.metric,
            event.value, event.threshold,
            _seq_of(event.evidence, first=True), _seq_of(event.evidence, first=False),
            _evidence_json(event.evidence), event.fired_at,
            getattr(event, "breach_started_at", None),
        ),
    )


def decision_exists(conn, event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM remediation_decisions WHERE event_id = ?", (event_id,)
    ).fetchone()
    return row is not None


def insert_decision(conn, decision_id: str, event, action, now_iso: str) -> None:
    conn.execute(
        "INSERT INTO remediation_decisions (id, event_id, node_id, rule_id, playbook,"
        " action_kind, action_params, reason, decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id, event.event_id, event.node_id, event.rule_id,
            action.playbook, action.kind, json.dumps(action.params), action.reason,
            now_iso,
        ),
    )


def insert_audit(conn, audit_id: str, decision_id: str, event, action,
                 result, idempotency_key: str, now_iso: str, dry_run: bool) -> None:
    # `result` is an operator.executor.ExecResult; duck-typed here to avoid a
    # store->executor import cycle.
    conn.execute(
        "INSERT INTO remediation_audits (id, decision_id, event_id, node_id,"
        " rule_id, action_kind, result, detail, lxd_method, lxd_path, lxd_body,"
        " idempotency_key, dry_run, executed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit_id, decision_id, event.event_id, event.node_id, event.rule_id,
            action.kind, result.result, (result.detail or "")[:1000], result.method,
            result.path, (result.body or "")[:1000], idempotency_key,
            1 if dry_run else 0, now_iso,
        ),
    )


def recent_audit(conn, node_id: str, rule_id: str, within_s: float) -> dict | None:
    """Most recent audit for (node, rule) after `within_s` seconds ago, if any."""
    cutoff = time.time() - within_s
    row = conn.execute(
        "SELECT * FROM remediation_audits WHERE node_id = ? AND rule_id = ?"
        " AND executed_at >= ? ORDER BY executed_at DESC LIMIT 1",
        (node_id, rule_id, _iso_from_epoch(cutoff)),
    ).fetchone()
    return dict(row) if row else None


def idempotency_key_for(node_id: str, rule_id: str, action_kind: str, target: str,
                        episode: int = 0) -> str:
    # Episode-scoped: a genuine re-occurrence (after clear + re-breach) gets a new
    # episode number and thus a new key, so it is NOT wrongly deduplicated against
    # the prior episode's command. Within one episode the latch prevents re-fire,
    # so this key is a belt-and-suspenders guard only.
    return f"{node_id}:{rule_id}:{action_kind}:{target}:{episode}"


def audit_by_idempotency_key(conn, key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM remediation_audits WHERE idempotency_key = ?"
        " ORDER BY executed_at DESC LIMIT 1", (key,)
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ queries
def list_health_events(conn, node_id: str | None, limit: int) -> list[dict]:
    if node_id:
        rows = conn.execute(
            "SELECT * FROM health_events WHERE node_id = ? ORDER BY fired_at DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM health_events ORDER BY fired_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_event_view(r) for r in rows]


def list_decisions(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM remediation_decisions ORDER BY decided_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["action_params"] = json.loads(d["action_params"])
        except (json.JSONDecodeError, TypeError):
            pass
        out.append(d)
    return out


def list_audits(conn, node_id: str | None, limit: int) -> list[dict]:
    if node_id:
        rows = conn.execute(
            "SELECT * FROM remediation_audits WHERE node_id = ? ORDER BY executed_at DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM remediation_audits ORDER BY executed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ helpers
def _seq_of(evidence: list, first: bool) -> int | None:
    if not evidence:
        return None
    sample = evidence[0] if first else evidence[-1]
    seq = sample.get("seq") if isinstance(sample, dict) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def _evidence_json(evidence: list) -> str:
    return json.dumps(evidence[-EVIDENCE_SAMPLE_CAP:])


def _iso_from_epoch(epoch: float) -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _event_view(row) -> dict:
    d = dict(row)
    try:
        d["evidence"] = json.loads(d["evidence_json"])
    except (json.JSONDecodeError, TypeError):
        pass
    d.pop("evidence_json", None)
    return d


# ------------------------------------------------------------------ triggers
# Durable threshold config + per-(node, rule) trigger latch state (migration
# 0003). The loop reads config on every poll and state per node, and writes
# both back in the same transaction as the health-event/decision/audit writes.

def load_trigger_configs(conn) -> dict:
    """Return {rule_id: config_subset_dict} from durable storage."""
    out: dict = {}
    for r in conn.execute("SELECT rule_id, config_json FROM trigger_config").fetchall():
        try:
            out[r["rule_id"]] = json.loads(r["config_json"])
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def save_trigger_config(conn, rule_id: str, config_dict: dict, source: str, now: str) -> None:
    conn.execute(
        "INSERT INTO trigger_config (rule_id, config_json, source, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(rule_id) DO UPDATE SET config_json=excluded.config_json,"
        " source=excluded.source, updated_at=excluded.updated_at",
        (rule_id, json.dumps(config_dict), source, now),
    )


def load_trigger_state(conn, node_id: str) -> dict:
    """Return {rule_id: {state, last_fired_at, last_cleared_at, fired_count}}."""
    out: dict = {}
    rows = conn.execute(
        "SELECT rule_id, state, last_fired_at, last_cleared_at, fired_count"
        " FROM trigger_state WHERE node_id = ?",
        (node_id,),
    ).fetchall()
    for r in rows:
        out[r["rule_id"]] = {
            "state": r["state"],
            "last_fired_at": r["last_fired_at"],
            "last_cleared_at": r["last_cleared_at"],
            "fired_count": r["fired_count"],
        }
    return out


def save_trigger_state(conn, node_id: str, rule_id: str, state: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO trigger_state (node_id, rule_id, state, last_fired_at,"
        " last_cleared_at, fired_count, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(node_id, rule_id) DO UPDATE SET state=excluded.state,"
        " last_fired_at=excluded.last_fired_at,"
        " last_cleared_at=excluded.last_cleared_at,"
        " fired_count=excluded.fired_count, updated_at=excluded.updated_at",
        (
            node_id, rule_id, state.get("state", "armed"),
            state.get("last_fired_at"), state.get("last_cleared_at"),
            int(state.get("fired_count", 0)), now,
        ),
    )
