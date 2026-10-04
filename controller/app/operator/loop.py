"""The operator loop — closes the control loop:

    poll telemetry store -> detect threshold breach -> choose playbook action
        -> execute against the node's LXD REST API -> audit (event/action/result)

Runs as a daemon thread inside the controller process (started by `app/main`),
polling every `operator_poll_interval_s`. Each poll is a single short-lived SQLite
transaction; execution and audit write happen in the same transaction, so the
decision log is always consistent with what was actually issued.

Idempotency / anti-flap (see operator/store.py schema notes):
  * one health event -> at most one decision (UNIQUE event_id),
  * repeated remediation of the same (node, rule, action, target) is deduplicated
    by idempotency_key before any LXD call,
  * a cooldown window suppresses re-firing the same rule on the same node.
"""
from __future__ import annotations

import logging
import threading

from .. import db, store, ulid
from . import detect, executor, playbook
from . import store as opstore
from .types import ExecResult, RemediationAction

log = logging.getLogger("termini.operator.loop")


class OperatorLoop(threading.Thread):
    def __init__(
        self,
        cfg,
        project: dict,
        thresholds: detect.Thresholds,
        playbooks: dict[str, dict],
        *,
        instance_names: dict[str, str] | None = None,
        default_instance_name: str = "agent",
        lxd: executor.LxdExecutor | None = None,
    ):
        super().__init__(name="operator-loop", daemon=True)
        self.cfg = cfg
        self.project = project
        self.thresholds = thresholds
        self.playbooks = playbooks
        self.instance_names = instance_names or {}
        self.default_instance_name = default_instance_name
        self.lxd = lxd or executor.LxdExecutor(
            cfg.lxd_base_url,
            dry_run=cfg.operator_dry_run,
            timeout_s=cfg.lxd_request_timeout_s,
            retry_max=cfg.lxd_retry_max,
            backoff_s=cfg.lxd_retry_backoff_s,
            client_cert=cfg.lxd_client_cert,
            client_key=cfg.lxd_client_key,
        )
        self.poll_interval_s = max(0.1, cfg.operator_poll_interval_s)
        self.cooldown_s = cfg.operator_cooldown_s
        self.window = cfg.operator_window
        self._stop = threading.Event()

    # -------------------------------------------------------------- lifecycle
    def run(self) -> None:
        log.info("operator loop started (poll=%ss, dry_run=%s)",
                 self.poll_interval_s, self.lxd.dry_run)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("operator loop iteration failed")
            self._stop.wait(self.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- core
    def run_once(self) -> list[dict]:
        """One full poll->detect->choose->execute->audit pass. Returns the audit
        records produced this pass (empty if nothing fired)."""
        produced: list[dict] = []
        with db.session(self.cfg.db_path) as conn:
            for node in store.list_nodes(conn, self.project["id"]):
                produced.extend(self._process_node(conn, node))
        return produced

    def _process_node(self, conn, node: dict) -> list[dict]:
        node_id = node["id"]
        samples_desc = store.recent_samples(conn, node_id, self.window)
        samples_asc = list(reversed(samples_desc))
        now = store.now_iso()

        events = detect.evaluate(node_id, samples_asc, self.thresholds, now)
        produced: list[dict] = []
        for event in events:
            audit = self._handle_event(conn, node_id, event, now)
            if audit is not None:
                produced.append(audit)
        return produced

    def _handle_event(self, conn, node_id: str, event, now: str) -> dict | None:
        # Cooldown: don't flap on a sustained breach.
        if self.cooldown_s > 0 and opstore.recent_audit(
            conn, node_id, event.rule_id, self.cooldown_s
        ):
            return None

        opstore.insert_health_event(conn, event)

        action = playbook.choose(
            event, self.playbooks, self.instance_names, self.default_instance_name
        )
        if action is None:
            log.info("event %s (%s) has no playbook action; recorded only",
                     event.rule_id, node_id)
            return None

        decision_id = ulid.new_ulid()
        opstore.insert_decision(conn, decision_id, event, action, now)

        target = action.params.get("instance_name", "")
        idem_key = opstore.idempotency_key_for(
            node_id, event.rule_id, action.kind, target
        )

        # Idempotency: same node+rule+action+target already applied -> no-op.
        prior = opstore.audit_by_idempotency_key(conn, idem_key)
        if prior is not None and prior["result"] in ("ok", "dry_run"):
            result = ExecResult(
                result="already_applied",
                detail=f"idempotency key {idem_key} already applied "
                       f"({prior['executed_at']})",
            )
            dry_run = False
        else:
            result = self.lxd.execute(action)
            dry_run = bool(self.lxd.dry_run)

        audit_id = ulid.new_ulid()
        opstore.insert_audit(
            conn, audit_id, decision_id, event, action, result, idem_key, now, dry_run
        )
        log.info("node %s rule %s -> %s %s: %s", node_id, event.rule_id,
                 action.kind, target, result.result)
        return {
            "audit_id": audit_id, "decision_id": decision_id, "event_id": event.event_id,
            "node_id": node_id, "rule_id": event.rule_id, "action_kind": action.kind,
            "result": result.result,
        }
