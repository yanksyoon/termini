"""Entrypoint — `python3 -m app.main` (from the controller/ directory).

Bootstraps config, applies migrations, seeds single-use bootstrap tokens,
resolves the demo project (generating a project token once if none is
configured), and serves.
"""
from __future__ import annotations

import logging
import sys

from . import config, db, server, store
from .operator import Thresholds, OperatorLoop
from .operator import playbook as op_playbook

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

    loop = None
    if cfg.operator_enabled:
        thresholds = Thresholds(
            oom_ram_pct=cfg.oom_ram_pct,
            oom_consecutive=cfg.oom_consecutive,
            disk_free_pct=cfg.disk_free_pct,
            disk_free_consecutive=cfg.disk_free_consecutive,
            disk_write_latency_ms=cfg.disk_write_latency_ms,
            disk_latency_consecutive=cfg.disk_latency_consecutive,
            disk_free_slope_bps=cfg.disk_free_slope_bps,
            latency_ms=cfg.latency_ms,
            latency_consecutive=cfg.latency_consecutive,
        )
        try:
            playbooks = op_playbook.load_playbooks(cfg.playbooks_dir)
        except Exception:
            log.exception("failed to load playbooks from %s; operator loop disabled",
                          cfg.playbooks_dir)
            playbooks = {}
        if playbooks:
            loop = OperatorLoop(
                cfg, project, thresholds, playbooks,
                instance_names=cfg.lxd_instance_names,
                default_instance_name=cfg.lxd_default_instance_name,
            )
            loop.start()
        else:
            log.warning("no playbooks loaded; operator loop disabled")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if loop is not None:
            loop.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
