"""The operator loop — closes the control loop:

    poll telemetry store -> detect threshold breach -> choose playbook action
        -> execute against the node's LXD REST API -> audit (event/action/result)

Runs as a daemon thread inside the controller process (started by `app/main`),
polling every `operator_poll_interval_s` (tuned to 1-2 s so a fault is detected
before the OOM-killer or log noise ends the run). Each poll is a single
short-lived SQLite transaction; trigger state, event, decision, and audit writes
happen in the same transaction, so the latch and the decision log are always
consistent with what was actually issued.

Each fired remediation is also published to the controller's operator log stream
(`OperatorLogs`, docs/contracts/dashboard.md §2) when a sink is injected by
`app/main`, so the dashboard's live AI-operator panel reflects the loop. The sink
is optional — unit tests and the standalone CLI construct the loop without one.

Detection is **stateful and persisted** (see `operator/triggers.py` and migration
0003): thresholds live in `trigger_config` (durable, not in-memory) and the
per-(node, rule) hysteresis latch lives in `trigger_state`, so

  * a single noisy sample never fires (debounce = N consecutive breaching samples),
  * a recovering metric never re-fires (latch until it returns to the clear band),
  * a controller restart never re-fires an already-remediated fault (latch persists).

Detection latency is measured and logged on every fire:
`breach_started_at -> fired_at` (detection) and `fired_at -> executed_at`
(remediation), both ms-precision.
"""
from __future__ import annotations

import logging
import threading
import time

from .. import db, store, ulid
from . import detect, executor, playbook, triggers
from . import store as opstore
from .models import ExecResult, RemediationAction

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
        logs=None,  # OperatorLogs sink: the dashboard's live AI-operator stream (see below)
    ):
        super().__init__(name="operator-loop", daemon=True)
        self.cfg = cfg
        self.project = project
        self.thresholds = thresholds  # env-seeded defaults (used to seed trigger_config)
        self.playbooks = playbooks
        # OperatorLogs sink for the dashboard's live AI-operator stream
        # (docs/contracts/dashboard.md §2). Optional: None keeps the loop
        # self-contained for unit tests and the standalone CLI.
        self.logs = logs
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
        self.cadence_report_s = getattr(cfg, "cadence_report_s", 30.0)
        self._stop = threading.Event()
        self._polls = 0
        self._max_lag_s = 0.0

    # -------------------------------------------------------------- lifecycle
    def run(self) -> None:
        log.info("operator loop started (poll=%ss, dry_run=%s)",
                 self.poll_interval_s, self.lxd.dry_run)
        report_at = time.monotonic() + self.cadence_report_s
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.run_once()
            except Exception:
                log.exception("operator loop iteration failed")
            dur = time.monotonic() - t0
            self._polls += 1
            lag = max(0.0, dur - self.poll_interval_s)
            self._max_lag_s = max(self._max_lag_s, lag)
            if lag > 0.0:
                log.warning(
                    "operator poll falling behind: took %.2fs > interval %.2fs "
                    "(nodes=%d)", dur, self.poll_interval_s,
                    self._node_count(),
                )
            if time.monotonic() >= report_at:
                log.info(
                    "operator cadence: %d polls, max lag %.2fs, interval %.2fs "
                    "(sustained %s)", self._polls, self._max_lag_s,
                    self.poll_interval_s, "OK" if self._max_lag_s < self.poll_interval_s else "BEHIND",
                )
                self._polls = 0
                self._max_lag_s = 0.0
                report_at = time.monotonic() + self.cadence_report_s
            # Target the configured interval regardless of work duration, so the
            # poll cadence is genuinely poll_interval_s (not interval + work).
            self._stop.wait(max(0.0, self.poll_interval_s - dur))

    def stop(self) -> None:
        self._stop.set()

    def _node_count(self) -> int:
        try:
            with db.session(self.cfg.db_path) as conn:
                return len(store.list_nodes(conn, self.project["id"]))
        except Exception:
            return -1

    # ------------------------------------------------------------------- core
    def run_once(self) -> list[dict]:
        """One full poll->detect->choose->execute->audit pass. Returns the audit
        records produced this pass (empty if nothing fired)."""
        produced: list[dict] = []
        with db.session(self.cfg.db_path) as conn:
            th = self._load_thresholds(conn)
            engine = triggers.TriggerEngine(th)
            for node in store.list_nodes(conn, self.project["id"]):
                produced.extend(self._process_node(conn, node, engine))
        return produced

    def _load_thresholds(self, conn) -> detect.Thresholds:
        """Read durable threshold config; seed it from env defaults on first run."""
        parts = opstore.load_trigger_configs(conn)
        if not parts:
            now = triggers.now_iso_ms()
            for rule_id, subset in triggers.split_config(self.thresholds).items():
                opstore.save_trigger_config(conn, rule_id, subset, "env", now)
            return self.thresholds
        return triggers.merge_config(parts)

    def _process_node(self, conn, node: dict, engine: triggers.TriggerEngine) -> list[dict]:
        node_id = node["id"]
        samples_desc = store.recent_samples(conn, node_id, self.window)
        samples_asc = list(reversed(samples_desc))
        now = triggers.now_iso_ms()

        state = opstore.load_trigger_state(conn, node_id)
        events, new_state = engine.evaluate(node_id, samples_asc, state, now)
        for rule_id, st in new_state.items():
            opstore.save_trigger_state(conn, node_id, rule_id, st, now)

        produced: list[dict] = []
        for event in events:
            audit = self._handle_event(conn, node_id, event, now)
            if audit is not None:
                produced.append(audit)
        return produced

    def _handle_event(self, conn, node_id: str, event, now: str) -> dict | None:
        # Detection latency: fault-onset (first breaching sample) -> trigger fire.
        detect_lat = triggers.detection_latency_s(event)
        log.info("node %s rule %s FIRED: detection_latency=%ss (breach=%s -> fired=%s)",
                 node_id, event.rule_id, detect_lat, event.breach_started_at, event.fired_at)

        # Cooldown: a time-based supplement to the hysteresis latch.
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
            self._emit_log(conn, node_id, f"{event.rule_id}: no playbook action; recorded only")
            return None

        decision_id = ulid.new_ulid()
        opstore.insert_decision(conn, decision_id, event, action, now)

        target = action.params.get("instance_name", "")
        idem_key = opstore.idempotency_key_for(
            node_id, event.rule_id, action.kind, target, episode=event.episode
        )

        # Idempotency: same episode already applied -> no-op (safety net only;
        # the latch is the primary dedup).
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
        # Remediation latency: trigger fire -> remediation issued.
        remed_lat = self._latency_s(event.fired_at, now)
        log.info("node %s rule %s -> %s %s: %s (remediation_latency=%ss)",
                 node_id, event.rule_id, action.kind, target, result.result, remed_lat)
        message = f"{event.rule_id}: {action.kind} {target} -> {result.result}".strip()
        threshold = getattr(event, "threshold", None)
        if threshold:
            message += f" (threshold: {threshold})"
        self._emit_log(conn, node_id, message)
        return {
            "audit_id": audit_id, "decision_id": decision_id, "event_id": event.event_id,
            "node_id": node_id, "rule_id": event.rule_id, "action_kind": action.kind,
            "result": result.result,
            "detection_latency_s": detect_lat, "remediation_latency_s": remed_lat,
        }

    def _emit_log(self, conn, node_id: str, message: str) -> None:
        """Publish one AI-operator line to the dashboard stream (dashboard.md §2).

        No-op when the loop was constructed without a sink (unit tests / CLI).
        Failures are logged and swallowed: the control loop must never die
        because the observability stream hiccuped.
        """
        if self.logs is None:
            return
        try:
            self.logs.append(
                conn, self.project["id"],
                node_id=node_id, severity="info", actor="ai_operator",
                message=message, remediation=None,
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to emit operator log line")

    @staticmethod
    def _latency_s(start: str | None, end: str | None) -> float | None:
        s = triggers.parse_epoch_ms(start)
        e = triggers.parse_epoch_ms(end)
        if s is None or e is None:
            return None
        return round(e - s, 3)
