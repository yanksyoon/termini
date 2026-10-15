"""Entrypoint — `python3 -m app.main` (from the controller/ directory).

Bootstraps config, applies migrations, seeds single-use bootstrap tokens,
resolves the demo project (generating a project token once if none is
configured), loads the infra inventory, wires the event broker + operator log
store + terminal manager, and serves.
"""
from __future__ import annotations

import dataclasses
import logging
import sys
import threading
import time
from typing import Any, Dict

from . import config, db, events, inventory, logs, lxdtrust, server, sshca, store, terminal
from .operator import Thresholds, OperatorLoop
from .operator import decision as op_decision
from .operator import egress as op_egress
from .operator import executor as op_executor
from .operator import playbook as op_playbook

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


def _reconcile_allocations_once(srv: server.ControllerServer) -> None:
    """One startup reconciliation pass (self-heal) — never blocks or kills boot.

    A restarted demo must not come up already saturated by commits whose VMs are
    gone (the defect: stale allocations filled a node and permanently 422'd the
    headline intent). Runs in a daemon thread so a slow/unreachable node cannot
    delay serving; an unreachable node is skipped, never released.
    """
    try:
        with db.session(srv.cfg.db_path) as conn:
            result = server.reconcile_allocations(conn, srv.cfg, srv.project["id"])
        if result["released_count"]:
            log.info("startup reconcile released %d stale allocation(s): %s",
                     result["released_count"],
                     ", ".join(f"{r['plan_id']}({r['reason']})" for r in result["released"]))
    except Exception:  # noqa: BLE001 — self-heal must never take the controller down
        log.exception("startup allocation reconcile failed")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    cfg = config.Config.load()

    applied = db.migrate(cfg.db_path)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))

    # --- LXD mTLS client identity + trust (node-bootstrap.md §5/§6.3) ----------
    # The operator's LXD REST calls are authenticated by a TLS client certificate.
    # Mint it once (idempotent: reused across restarts) and hand the paths to the
    # executor, so a real node's LXD answers 200 instead of 403. See app/lxdtrust.py.
    lxd_identity = lxdtrust.resolve_identity(
        explicit_cert=cfg.lxd_client_cert,
        explicit_key=cfg.lxd_client_key,
        client_dir=cfg.lxd_client_dir,
        common_name=cfg.lxd_trust_name,
        provision=cfg.lxd_auto_provision,
    )
    if lxd_identity is not None:
        log.info("LXD client identity ready: %s (fingerprint=%s)",
                 lxd_identity.cert_path, lxd_identity.fingerprint)
    else:
        log.warning("no LXD client identity — operator LXD calls will be unauthenticated")

    # Plumb the minted/reused identity into the (frozen) config so the apply-plan
    # materialiser also authenticates with it. `op_target.executor_for_node`
    # builds its LxdExecutor from `cfg.lxd_client_cert`/`_key` only, so without
    # this the apply path presents NO client certificate: against a real node's
    # self-signed LXD the request fails TLS verification
    # (CERTIFICATE_VERIFY_FAILED) before trust is even consulted. The explicit
    # TERMINI_LXD_CLIENT_CERT/_KEY override is respected (this is a no-op when set).
    if lxd_identity is not None and not cfg.lxd_client_cert:
        cfg = dataclasses.replace(
            cfg,
            lxd_client_cert=lxd_identity.cert_path,
            lxd_client_key=lxd_identity.key_path,
        )

    # Optionally install that certificate into configured nodes' trust stores on
    # startup (idempotent; skips nodes that already trust us). Opt-in via
    # TERMINI_LXD_TRUST_NODES + TERMINI_LXD_TRUST_TOKEN/PASSWORD.
    _trust_secret = cfg.lxd_trust_token or cfg.lxd_trust_password
    if lxd_identity is not None and cfg.lxd_trust_nodes and _trust_secret:
        for _node, _res in lxdtrust.ensure_trusted_all(
            cfg.lxd_trust_nodes, lxd_identity, _trust_secret,
            name=cfg.lxd_trust_name, server_ca=cfg.lxd_server_ca,
            timeout_s=cfg.lxd_request_timeout_s,
        ):
            (log.info if _res.ok else log.warning)(
                "LXD trust %s: %s — %s", _node, _res.status, _res.detail)

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
    ca = sshca.SSHCA(
        cfg.ssh_ca_key_path,
        cfg.ssh_ca_public_key,
        default_validity_s=cfg.ssh_cert_default_validity_s,
        max_validity_s=cfg.ssh_cert_max_validity_s,
        principal=cfg.ssh_cert_principal,
    )

    srv = server.ControllerServer(
        (cfg.host, cfg.port), cfg, project, broker, operator_logs, inv,
        terminal_manager, ca,
    )

    if inv.loaded:
        log.info("inventory loaded: %d node(s), ssh=%s@:%s",
                 len(inv._nodes), inv.ssh_user, inv.ssh_port)
    else:
        log.info("no inventory loaded (SSH string/terminal bridge will have no public_ip)")
    if ca.available:
        log.info("SSH CA ready (fingerprint=%s)", ca.fingerprint or "unknown")
    else:
        log.info("SSH CA not configured (POST /ssh-key and the SSH terminal backend disabled)")

    threading.Thread(target=_approval_sweeper, args=(srv,), daemon=True).start()
    threading.Thread(target=_reconcile_allocations_once, args=(srv,), daemon=True).start()

    # Heal never-applied, restart-orphaned capacity reservations BEFORE serving
    # (server.release_orphaned_reservations). `_reconcile_allocations_once` cannot
    # touch these — it reconciles against the node, and a commit with no recorded
    # instances has no absence to prove — so without this a reservation from a
    # previous process survives forever and permanently shrinks a node's free
    # capacity. This is pure database work (no node calls), so it runs inline:
    # the first request after a restart must already see the healed capacity.
    try:
        with db.session(cfg.db_path) as conn:
            healed = server.release_orphaned_reservations(
                conn, project["id"], live_plan_ids=srv.plans.keys())
        if healed["released_count"]:
            log.info(
                "startup released %d orphaned capacity reservation(s) "
                "(never applied; their plan died with a previous process): %s",
                healed["released_count"],
                ", ".join(
                    f"{r['plan_id']}({r['released']['cpu_allocated']} vCPU/"
                    f"{r['released']['ram_allocated_gb']} GiB)" for r in healed["released"]),
            )
    except Exception:  # noqa: BLE001 — self-heal must never take the controller down
        log.exception("startup orphaned-reservation release failed")

    log.info("controller listening on %s:%s (db=%s)", cfg.host, cfg.port, cfg.db_path)

    loop = None
    if cfg.operator_enabled:
        thresholds = Thresholds(
            oom_ram_pct=cfg.oom_ram_pct,
            oom_clear_pct=cfg.oom_clear_pct,
            oom_consecutive=cfg.oom_consecutive,
            oom_clear_consecutive=cfg.oom_clear_consecutive,
            disk_fail_consecutive=cfg.disk_fail_consecutive,
            disk_fail_clear_consecutive=cfg.disk_fail_clear_consecutive,
            disk_free_pct=cfg.disk_free_pct,
            disk_free_clear_pct=cfg.disk_free_clear_pct,
            disk_free_consecutive=cfg.disk_free_consecutive,
            disk_write_latency_ms=cfg.disk_write_latency_ms,
            disk_write_latency_clear_ms=cfg.disk_write_latency_clear_ms,
            disk_latency_consecutive=cfg.disk_latency_consecutive,
            disk_free_slope_bps=cfg.disk_free_slope_bps,
            disk_clear_consecutive=cfg.disk_clear_consecutive,
            latency_ms=cfg.latency_ms,
            latency_clear_ms=cfg.latency_clear_ms,
            latency_consecutive=cfg.latency_consecutive,
            latency_clear_consecutive=cfg.latency_clear_consecutive,
        )
        try:
            playbooks = op_playbook.load_playbooks(cfg.playbooks_dir)
        except Exception:
            log.exception("failed to load playbooks from %s; operator loop disabled",
                          cfg.playbooks_dir)
            playbooks = {}
        if playbooks:
            provider = op_decision.build_decision_provider(
                cfg, playbooks=playbooks,
                instance_names=cfg.lxd_instance_names,
                default_instance_name=cfg.lxd_default_instance_name,
            )
            # Where did the answer come from? The decision endpoint may serve from
            # local weights or forward to a managed model, and the audit must say
            # which (external_call.py records it; egress.py observes it). The probe
            # is cached and bounded so it can never dominate a decision's latency.
            if isinstance(provider, op_decision.ModelDecisionProvider):
                provider.egress_probe = op_egress.CachedEgressProbe(cfg.inference_base_url)
                provider.egress_endpoint = cfg.inference_base_url
                project_id = project["id"]

                def _egress_audit(record: Dict[str, Any], _pid: str = project_id) -> None:
                    """Land the external-model audit row in the operator's log stream.

                    The dashboard's live AI log is where a judge reads what the
                    operator did; the destination of a model call belongs there, not
                    only in the journal.
                    """
                    try:
                        with db.session(cfg.db_path) as conn:
                            operator_logs.append(
                                conn, _pid, severity="info", actor="ai_operator",
                                message=op_egress.audit_message(record),
                            )
                    except Exception:  # noqa: BLE001 - observability must not break the loop
                        log.exception("failed to record the external-model audit row")

                provider.audit_sink = _egress_audit
            # Hand the loop an executor that presents the controller's LXD client
            # certificate (resolved above), so its REST calls are authenticated.
            lxd_exec = op_executor.LxdExecutor(
                cfg.lxd_base_url,
                dry_run=cfg.operator_dry_run,
                timeout_s=cfg.lxd_request_timeout_s,
                retry_max=cfg.lxd_retry_max,
                backoff_s=cfg.lxd_retry_backoff_s,
                client_cert=lxd_identity.cert_path if lxd_identity else cfg.lxd_client_cert,
                client_key=lxd_identity.key_path if lxd_identity else cfg.lxd_client_key,
                server_ca=cfg.lxd_server_ca,
            )
            loop = OperatorLoop(
                cfg, project, thresholds, playbooks,
                instance_names=cfg.lxd_instance_names,
                default_instance_name=cfg.lxd_default_instance_name,
                logs=operator_logs,
                provider=provider,
                lxd=lxd_exec,
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
