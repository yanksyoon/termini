"""Entrypoint — `python3 -m app.main` (from the controller/ directory).

Bootstraps config, applies migrations, seeds single-use bootstrap tokens,
resolves the demo project (generating a project token once if none is
configured), loads the infra inventory, wires the event broker + operator log
store + terminal manager, and serves.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

from . import config, db, events, inventory, logs, server, store, terminal

log = logging.getLogger("termini.controller")


def _approval_sweeper(srv: server.ControllerServer, interval_s: int = 15) -> None:
    """Auto-deny pending_approval commands past their timeout (dashboard.md §4.4)."""
    project_id = srv.project["id"]
    timeout = srv.cfg.approval_timeout_s
    while True:
        time.sleep(interval_s)
        try:
            with db.session(srv.cfg.db_path) as conn:
                from . import commands
                n = commands.expire_pending(conn, srv.operator_logs, project_id, timeout)
                if n:
                    log.info("auto-denied %d expired remediation command(s)", n)
        except Exception:  # noqa: BLE001 — sweeper must never die
            log.exception("approval sweeper error")


def _resolve_public_key(cfg: config.Config) -> str | None:
    if cfg.ssh_public_key:
        return cfg.ssh_public_key
    if cfg.ssh_key_path:
        return inventory.public_key_from_private(cfg.ssh_key_path)
    return None


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

    inv = inventory.Inventory.load(cfg.inventory_path, cfg.ssh_user, cfg.ssh_port)
    broker = events.EventBroker()
    operator_logs = logs.OperatorLogs(broker, ring_size=cfg.log_ring_size)
    terminal_manager = terminal.TerminalManager()
    public_key_line = _resolve_public_key(cfg)

    srv = server.ControllerServer(
        (cfg.host, cfg.port), cfg, project, broker, operator_logs, inv,
        terminal_manager, public_key_line,
    )

    if inv.loaded:
        log.info("inventory loaded: %d node(s), ssh=%s@:%s",
                 len(inv._nodes), inv.ssh_user, inv.ssh_port)
    else:
        log.info("no inventory loaded (SSH string/terminal bridge will have no public_ip)")

    threading.Thread(target=_approval_sweeper, args=(srv,), daemon=True).start()

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
