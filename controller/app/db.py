"""SQLite persistence layer (stdlib `sqlite3`, zero third-party deps).

Design notes:
- WAL journal mode + foreign keys on.
- One short-lived connection per operation (thread-safe by construction under
  ThreadingHTTPServer; SQLite WAL supports concurrent readers and serialized
  writers with a busy timeout).
- Schema is versioned via a `schema_migrations` table; migration SQL lives in
  `app/migrations/` and is applied idempotently at startup (and via `make migrate`).
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: str) -> sqlite3.Connection:
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextlib.contextmanager
def session(db_path: str):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _pending_migrations(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL"
        ")"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    out: list[tuple[int, str, str]] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.stem.split("_", 1)[0])
        name = path.stem
        if version not in applied:
            out.append((version, name, path.read_text()))
    return sorted(out, key=lambda t: t[0])


def migrate(db_path: str) -> list[str]:
    """Apply all pending migrations; returns the names applied (empty if none)."""
    applied: list[str] = []
    with session(db_path) as conn:
        for version, name, sql in _pending_migrations(conn):
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _now_iso()),
            )
            applied.append(name)
    return applied


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
