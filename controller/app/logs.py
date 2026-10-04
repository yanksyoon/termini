"""AI-operator log stream — the append side of dashboard.md §2.

One :class:`OperatorLogs` instance is held on the server. The AI-operator loop
(a separate component) and the human-intervention endpoints call :meth:`append`
to record a line; this persists it, trims the ring buffer, and fans it out to
every SSE/WS subscriber within the same request cycle (well under the ~2 s
budget). Replay/backfill reads from the durable table via :meth:`replay`.

Only AI-operator operational messages and human-intervention records are emitted
here; application/customer logs are C2 and never enter this store
(governance.md §3).
"""
from __future__ import annotations

import datetime
from collections import deque
from typing import Any

from . import store, ulid

VALID_SEVERITIES = ("debug", "info", "warn", "error")
VALID_ACTORS = ("ai_operator", "human")
MAX_MESSAGE_CHARS = 1000  # dashboard.md §2.1


def _now_ms_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class OperatorLogs:
    """Durable, ring-bounded operator log with in-memory tail + broker fan-out."""

    def __init__(self, broker, ring_size: int = 10000) -> None:
        self.broker = broker
        self.ring_size = ring_size
        self._buffer: deque[dict] = deque(maxlen=ring_size)

    def append(
        self,
        conn,
        project_id: str,
        *,
        node_id: str | None = None,
        severity: str = "info",
        actor: str = "ai_operator",
        message: str = "",
        remediation: dict | None = None,
        truncated: bool = False,
    ) -> dict:
        """Persist + broadcast one log line. Returns the full envelope."""
        if severity not in VALID_SEVERITIES:
            severity = "info"
        if actor not in VALID_ACTORS:
            actor = "ai_operator"
        message = (message or "")[:MAX_MESSAGE_CHARS]

        log = {
            "log_id": ulid.new_ulid(),
            "ts": _now_ms_iso(),
            "project_id": project_id,
            "node_id": node_id,
            "severity": severity,
            "actor": actor,
            "message": message,
            "remediation": remediation,
        }
        if truncated:
            log["truncated"] = True

        store.insert_log(conn, log)
        store.trim_logs(conn, project_id, self.ring_size)
        self._buffer.append(log)
        self.broker.publish("log.line", log, project_id)
        return log

    def replay(self, conn, project_id: str, since_log_id: str | None, limit: int | None = None) -> list[dict]:
        """Backfill lines with ``log_id > since_log_id`` from the durable tail."""
        return store.logs_since(conn, project_id, since_log_id, limit or self.ring_size)

    def oldest_retained(self, conn, project_id: str) -> str | None:
        row = conn.execute(
            "SELECT MIN(log_id) AS m FROM operator_logs WHERE project_id = ?", (project_id,)
        ).fetchone()
        return row["m"] if row else None

    @property
    def buffer_len(self) -> int:
        return len(self._buffer)
