"""Persistence for the operator loop: health events, remediation decisions
(decision log), and remediation audits. All functions take an open connection
and run inside the caller's transaction, matching `app/store.py` conventions.

Idempotency is enforced here at the schema level:
  * `remediation_decisions.event_id` is UNIQUE — one health event can produce at
    most one decision, so a double-fired event can never issue two commands.
  * `remediation_audits.idempotency_key` is intentionally NOT UNIQUE — a failed
    remediation (result=error) can be retried and re-audited. A prior
    ok/dry_run audit for the same key deduplicates a repeat remediation ONLY
    within the bounded idempotency window (controller-api.md §1.5, 24h); an
    expired entry no longer suppresses a genuine later episode.
"""
from __future__ import annotations

import json
import time

from .. import ulid

EVIDENCE_SAMPLE_CAP = 20  # bound the evidence window we persist

# controller-api.md §1.5: two state-changing requests with the same idempotency
# key within 24h are the same resource/plan. An audit older than this window
# must not suppress a genuine re-remediation.
IDEMPOTENCY_WINDOW_S = 24 * 60 * 60


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


def insert_decision(conn, decision_id: str, event, action, now_iso: str,
                    *, source: str = "deterministic", confidence: float | None = None,
                    fallback_reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO remediation_decisions (id, event_id, node_id, rule_id, playbook,"
        " action_kind, action_params, reason, decision_source, confidence,"
        " fallback_reason, decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id, event.event_id, event.node_id, event.rule_id,
            action.playbook, action.kind, json.dumps(action.params), action.reason,
            source, confidence, fallback_reason, now_iso,
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


def audit_by_idempotency_key(conn, key: str,
                             within_s: float | None = IDEMPOTENCY_WINDOW_S) -> dict | None:
    """Most recent audit for `key`, restricted to the bounded idempotency window.

    A prior ok/dry_run audit with the same key only counts as "already applied"
    if it was executed within `within_s` seconds ago (default: the §1.5 24h
    window). An expired entry is ignored, so a genuine later episode re-fires
    instead of being silently suppressed forever. `within_s=None` (or <= 0)
    disables the time bound and returns the most recent entry regardless of age.
    """
    if within_s is None or within_s <= 0:
        row = conn.execute(
            "SELECT * FROM remediation_audits WHERE idempotency_key = ?"
            " ORDER BY executed_at DESC LIMIT 1", (key,)
        ).fetchone()
    else:
        cutoff = time.time() - within_s
        row = conn.execute(
            "SELECT * FROM remediation_audits WHERE idempotency_key = ?"
            " AND executed_at >= ? ORDER BY executed_at DESC LIMIT 1",
            (key, _iso_from_epoch(cutoff)),
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


# ------------------------------------------------------------------ lease
# Single-owner operator lease (migration 0007). The in-process loop and the
# standalone CLI both acquire this before issuing any remediation; a non-owner
# is a standby that issues nothing. `acquire_lease` is a single atomic SQL
# statement so two concurrent callers (SQLite serializes writers) can never both
# conclude they own the lease.

def acquire_lease(conn, project_id: str, owner_id: str, now_iso: str, ttl_s: float) -> bool:
    """Atomically acquire (or renew) the per-project operator lease.

    Returns True if `owner_id` now holds the lease. A non-expired lease held by a
    different owner is left untouched (that owner keeps acting; this caller is a
    standby). The caller's own unexpired lease is renewed (expiry extended). A
    lease whose `expires_at` has passed is taken over regardless of prior owner.

    `now_iso` and the stored timestamps must share the same RFC3339-with-millis
    format (see `triggers.now_iso_ms` / `_iso_from_epoch`), so lexicographic
    comparison is chronological. `expires_at` is derived from `now_iso` (not the
    wall clock) so the lease is deterministic in the caller's clock and unit
    testable with a fixed `now_iso`.
    """
    from . import triggers as _triggers
    epoch = _triggers.parse_epoch_ms(now_iso)
    if epoch is None:
        epoch = time.time()
    expires_at = _iso_from_epoch(epoch + ttl_s)
    conn.execute(
        """
        INSERT INTO operator_lease (project_id, owner_id, acquired_at, expires_at)
        VALUES (:pid, :owner, :now, :expiry)
        ON CONFLICT(project_id) DO UPDATE SET
            owner_id = CASE
                WHEN operator_lease.expires_at <= :now OR operator_lease.owner_id = :owner
                    THEN :owner ELSE operator_lease.owner_id END,
            acquired_at = CASE
                WHEN operator_lease.expires_at <= :now THEN :now ELSE operator_lease.acquired_at END,
            expires_at = CASE
                WHEN operator_lease.expires_at <= :now OR operator_lease.owner_id = :owner
                    THEN :expiry ELSE operator_lease.expires_at END
        """,
        {"pid": project_id, "owner": owner_id, "now": now_iso, "expiry": expires_at},
    )
    row = conn.execute(
        "SELECT owner_id FROM operator_lease WHERE project_id = ?", (project_id,)
    ).fetchone()
    return row is not None and row["owner_id"] == owner_id


def get_lease(conn, project_id: str) -> dict | None:
    """Return the current lease row for a project (or None if never acquired)."""
    row = conn.execute(
        "SELECT project_id, owner_id, acquired_at, expires_at"
        " FROM operator_lease WHERE project_id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ fallback
# Per-(node, rule) model-decision fallback state (migration 0009). The loop
# tracks a fallback streak and a forced-deterministic cooldown window so a
# stuck model fault can never starve the deterministic fast-path
# (operator-decision.md §10.2).

def load_decision_fallback_state(conn, node_id: str, rule_id: str) -> dict | None:
    row = conn.execute(
        "SELECT node_id, rule_id, fallback_streak, forced_until, updated_at"
        " FROM decision_fallback_state WHERE node_id = ? AND rule_id = ?",
        (node_id, rule_id),
    ).fetchone()
    return dict(row) if row else None


def save_decision_fallback_state(conn, node_id: str, rule_id: str,
                                 fallback_streak: int, forced_until: str | None,
                                 now: str) -> None:
    conn.execute(
        "INSERT INTO decision_fallback_state (node_id, rule_id, fallback_streak,"
        " forced_until, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(node_id, rule_id) DO UPDATE SET"
        " fallback_streak=excluded.fallback_streak,"
        " forced_until=excluded.forced_until, updated_at=excluded.updated_at",
        (node_id, rule_id, int(fallback_streak), forced_until, now),
    )
