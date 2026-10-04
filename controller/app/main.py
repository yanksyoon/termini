"""Entrypoint — `python3 -m app.main` (from the controller/ directory).

Bootstraps config, applies migrations, seeds single-use bootstrap tokens,
resolves the demo project (generating a project token once if none is
configured), and serves.
"""
from __future__ import annotations

import logging
import sys

from . import config, db, server, store

log = logging.getLogger("termini.controller")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    cfg = config.Config.load()

    applied = db.migrate(cfg.db_path)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))

    with db.session(cfg.db_path) as conn:
        now = store.now_iso()
        store.seed_bootstrap_tokens(conn, list(cfg.bootstrap_tokens), now)
        project, generated_token = store.ensure_project(conn, cfg, now)

    if generated_token:
        print(
            "[termini] no TERMINI_PROJECT_TOKEN set — generated one (save it, it is "
            "persisted in the DB):\n"
            f"[termini] project_token = {generated_token}",
            file=sys.stderr,
        )

    srv = server.ControllerServer((cfg.host, cfg.port), cfg, project)
    log.info("controller listening on %s:%s (db=%s)", cfg.host, cfg.port, cfg.db_path)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
