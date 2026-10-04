"""CLI migration runner — `python3 -m app.migrate`.

Reads only the DB path (no auth config required) so it can run standalone;
the server applies the same migrations automatically at startup.
"""
from __future__ import annotations

import os

from . import db


def main() -> int:
    db_path = os.environ.get("TERMINI_DB_PATH", "").strip() or "data/controller.db"
    applied = db.migrate(db_path)
    print("applied:", ", ".join(applied) if applied else "(none — already up to date)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
