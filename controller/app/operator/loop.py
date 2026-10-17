"""The operator loop — closes the control loop:

    poll telemetry store -> detect threshold breach -> choose playbook action
        -> execute against the node's LXD REST API -> audit (event/action/result)

Runs as a daemon thread inside the controller process (started by `app/main`),
polling every `operator_poll_interval_s` (tuned to 1-2 s so a fault is detected
before the OOM-killer or log noise ends the run).

Transaction shape (this is a correctness rule, not an optimisation): a poll is
**two phases**, and NO SQLite transaction is ever open across the slow steps
(the model call, and the LXD REST call).

  * **detect** — one short write transaction: renew the single-owner lease, load
    the durable trigger config/state, evaluate every node's triggers, persist the
    latch state and the fired health events. It is committed *before* any
    remediation starts.
  * **remediate** — per fired event, with no transaction open: make the decision
    (a MODEL call, minutes long on CPU), then write the decision, the idempotency
    lookup, the LXD call, and the audit each in their own short transaction.

Before this split, the whole pass — model call included — was one transaction, so
SQLite's single-writer lock was held for the entire decision. On the contract's
20-sample evidence window that is ~400 s on CPU, during which EVERY other writer
(telemetry ingest, `POST /intents`) failed `sqlite3.OperationalError: database is
locked` (HTTP 500) and a healthy fleet rendered as all-offline. A `busy_timeout`
cannot fix a 400 s hold; the transaction must not span the call.

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
`breach_started_at -> fired_at` (detection) and chooser-start -> remediation
completed (remediation, chooser/selection time included), both ms-precision.
"""
from __future__ import annotations

import logging
import threading
import time

from .. import db, store, ulid
from . import decision, detect, executor, target, triggers, vmids
from . import store as opstore
from .models import ExecResult, RemediationAction

log = logging.getLogger("termini.operator.loop")


def _refusal_summary(result) -> str:
    """One-line reason an action was refused, for the operator log / client UI.

    Returns "" for an action that actually ran. `ExecResult.reason` is the
    machine-readable code (`executor.TARGET_MISSING`); the first clause of the
    detail carries the human-readable explanation. Both are printed so the log
    line says *why* nothing was issued — the previous line said only
    `-> error`, with the reason buried in the audit detail, which on the demo
    screen is indistinguishable from the AI failing.
    """
    reason = str(getattr(result, "reason", "") or "")
    if not reason:
        return ""
    detail = str(getattr(result, "detail", "") or "").strip()
    body = detail[len(reason):].lstrip(": ").strip() if detail.startswith(reason) else detail
    head = body.split(". ", 1)[0].strip()
    return f"{reason}: {head}" if head else reason


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
        provider: decision.DecisionProvider | None = None,  # decision seam (§2)
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
        # `self.lxd` is the FALLBACK executor. The loop resolves an executor PER
        # NODE (see `_executor_for`) so a remediation reaches the node that actually
        # holds the instance (target.py); the executor passed in here — production
        # hands in a credentialed one bound to the global `TERMINI_LXD_BASE_URL` —
        # is used only for a node that reported no endpoint. Before this, one global
        # executor bound to `TERMINI_LXD_BASE_URL` (node-a) was used for EVERY event,
        # so a fault on node-c was remediated against node-a.
        self._executors: dict[str, executor.LxdExecutor] = {}
        self.lxd = lxd or executor.LxdExecutor(
            cfg.lxd_base_url,
            dry_run=cfg.operator_dry_run,
            timeout_s=cfg.lxd_request_timeout_s,
            retry_max=cfg.lxd_retry_max,
            backoff_s=cfg.lxd_retry_backoff_s,
            client_cert=cfg.lxd_client_cert,
            client_key=cfg.lxd_client_key,
            server_ca=getattr(cfg, "lxd_server_ca", None),
        )
        self.poll_interval_s = max(0.1, cfg.operator_poll_interval_s)
        self.cooldown_s = cfg.operator_cooldown_s
        self.window = cfg.operator_window
        self.cadence_report_s = getattr(cfg, "cadence_report_s", 30.0)
        # Single-owner lease (migration 0007): only one operator may act per
        # project. The in-process loop's identity is stable across a controller
        # restart ("loop:<project_id>") so a restart re-acquires its own lease
        # instead of standing by for the previous incarnation's TTL to lapse.
        self.owner_id = f"loop:{project['id']}"
        self.lease_ttl_s = max(1.0, float(getattr(cfg, "operator_lease_ttl_s", 10.0)))
        # A pass may spend its whole decision timeout inside ONE model call, and
        # the loop deliberately holds no transaction (hence no SQLite lock) across
        # it. So a pass renews the lease for at least the longest step it can take
        # — otherwise a concurrent operator could take the lease over while this
        # one is legitimately busy inside a model call, and the single-owner
        # invariant would be lost. This is a row update, not a held lock.
        self.decision_timeout_s = max(
            0.0, float(getattr(cfg, "operator_decision_timeout_s", 30.0) or 30.0))
        self.pass_lease_ttl_s = max(self.lease_ttl_s, self.decision_timeout_s + 30.0)
        self._standby_logged = False

        # Decision step (operator-decision.md §2/§8/§10/§12). The loop no longer
        # calls playbook.choose directly; it calls provider.decide(event, ctx).
        self._deterministic = decision.DeterministicDecisionProvider(
            playbooks=playbooks, instance_names=self.instance_names,
            default_instance_name=self.default_instance_name,
        )
        self.decision_mode = (getattr(cfg, "operator_decision_mode", "deterministic")
                              or "deterministic")
        self.decision_max_fallbacks = int(getattr(
            cfg, "operator_decision_max_fallbacks", 3))
        self.decision_fallback_cooldown_s = float(getattr(
            cfg, "operator_decision_fallback_cooldown_s", 300.0))
        self.allowed_actions = list(getattr(cfg, "operator_decision_allowed_actions",
                                            None) or list(decision.ACTION_IDS))
        self.provider = provider or self._deterministic
        # Inject the deterministic fallback into a model provider so a fallback
        # always produces the exact playbook action (never an empty playbook).
        if isinstance(self.provider, decision.ModelDecisionProvider) \
                and self.provider.deterministic is None:
            self.provider.deterministic = self._deterministic

        self._stop = threading.Event()
        self._polls = 0
        self._max_lag_s = 0.0
        # {node_id: (monotonic_at, {vm_id -> LXD instance name})} — the LIVE
        # instance inventory of a node, refreshed when a remediation is about to
        # run (see `_live_instance_names`). It is what makes a model-chosen
        # `vm_id` resolvable to a target that exists, instead of a config table
        # that has to be kept in step with the fleet by hand.
        self._live_names: dict[str, tuple[float, dict[str, str]]] = {}
        self.live_inventory_ttl_s = max(
            0.0, float(getattr(cfg, "operator_live_inventory_ttl_s", 30.0) or 0.0))

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

    def _session(self):
        """A fresh short-lived connection/transaction (see `app/db.session`)."""
        return db.session(self.cfg.db_path)

    def _executor_for(self, node_id: str) -> executor.LxdExecutor:
        """The LXD executor for `node_id`'s OWN endpoint.

        Materialisation already resolves a per-node executor
        (`app/operator/target.executor_for_node`); remediation must do the same or
        the two halves of the loop touch different machines. The executor the loop
        was constructed with is the FALLBACK for a node that reported no endpoint —
        NOT an override. This matters in production: `app/main` hands the loop a
        credentialed executor bound to the global `TERMINI_LXD_BASE_URL`, so
        treating that executor as "the" executor for every node silently re-created
        the defect (a fault on node-c was remediated against node-a).
        """
        if node_id in self._executors:
            return self._executors[node_id]
        node = None
        try:
            with self._session() as conn:
                node = store.get_node(conn, node_id)
        except Exception:  # noqa: BLE001 — never let a lookup failure stop a remediation
            log.exception("could not load node %s for executor resolution", node_id)
            node = None
        endpoint = (node or {}).get("lxd_endpoint")
        if endpoint:
            # Re-target a copy of the loop's executor (same client identity, same
            # timeouts/TLS policy) at this node's endpoint.
            base = target.endpoint_base_url(
                endpoint, fallback=self.lxd.base_url,
                default_scheme=getattr(self.cfg, "lxd_node_scheme", "https"))
            ex = self.lxd.with_base_url(base)
        else:
            ex = self.lxd
        self._executors[node_id] = ex
        return ex

    def _node_count(self) -> int:
        try:
            with db.session(self.cfg.db_path) as conn:
                return len(store.list_nodes(conn, self.project["id"]))
        except Exception:
            return -1

    def _live_instance_names(self, node_id: str) -> dict[str, str]:
        """`{vm_id -> LXD instance name}` for the instances present on `node_id`.

        The guardrail resolves a model-chosen target through
        `DecisionContext.instance_names`; production wires that to a static
        `TERMINI_LXD_INSTANCE_NAMES` map, which cannot know what is actually
        running. On the live demo nothing filled the map, so a model target was
        passed through verbatim and the executor PUT an instance name that did not
        exist on the node -> the audit read `HTTP 404 ... Instance not found`.

        This reads the node's OWN inventory instead (`GET /1.0/instances`, the
        same authenticated probe reconciliation already uses) and derives each
        `vm_id` with `vmids.vm_id_for_instance`, so:

          * a vm_id the node reported in telemetry resolves to the instance it
            was derived from — and the remediation lands on that instance;
          * a vm_id that does NOT hash from any instance on this node resolves to
            nothing, the guardrail keeps the value as given, and the executor
            refuses it fail-closed (`executor.TARGET_MISSING`) instead of 404ing.

        Never called with a transaction open (it is an HTTP call); a node that
        cannot be read contributes no mapping and is never treated as empty.
        """
        cached = self._live_names.get(node_id)
        now = time.monotonic()
        if cached and (now - cached[0]) < self.live_inventory_ttl_s:
            return cached[1]
        ex = self._executor_for(node_id)
        try:
            reachable, names = ex.list_instance_names()
        except Exception:  # noqa: BLE001 — a lookup failure must not stop a remediation
            log.exception("could not read the live instance inventory of node %s", node_id)
            return cached[1] if cached else {}
        if not reachable:
            log.info("node %s instance inventory unreadable (%s); keeping configured names",
                     node_id, ex.base_url)
            return cached[1] if cached else {}
        mapping = vmids.instance_names_for_vm_ids(names)
        self._live_names[node_id] = (now, mapping)
        return mapping

    # ------------------------------------------------------------------- core
    def run_once(self) -> list[dict]:
        """One full poll->detect->choose->execute->audit pass. Returns the audit
        records produced this pass (empty if nothing fired).

        Two phases, and NO transaction spans the slow steps:

        1. **detect** (one short write transaction, committed before anything
           slow): lease, thresholds, per-node trigger evaluation + latch/event
           writes.
        2. **remediate** (no transaction open across the model/LXD call): decide,
           write, execute, audit — each in its own short transaction.

        Holding any transaction across phase 2 is the live defect this shape
        removes: on CPU a decision is minutes long, and while a transaction was
        held every other writer was locked out (`database is locked`).
        """
        pending: list[tuple[str, object, decision.DecisionContext]] = []
        with self._session() as conn:
            # Single-owner lease: if another operator (e.g. the standalone CLI
            # driven by OPERATOR_CMD) holds the lease, this loop is a standby and
            # issues nothing this pass. Renews its own lease when it is the owner.
            if not opstore.acquire_lease(
                conn, self.project["id"], self.owner_id,
                triggers.now_iso_ms(), self.pass_lease_ttl_s,
            ):
                if not self._standby_logged:
                    lease = opstore.get_lease(conn, self.project["id"])
                    lease_owner = lease["owner_id"] if lease else None
                    log.info(
                        "operator loop is standby (lease held by %s) self_id=%s",
                        lease_owner, self.owner_id,
                    )
                    self._standby_logged = True
                return []
            self._standby_logged = False
            engine = triggers.TriggerEngine(self._load_thresholds(conn))
            for node in store.list_nodes(conn, self.project["id"]):
                pending.extend(self._detect_node(conn, node, engine))
        # Detection is committed and the write lock released HERE. Nothing below
        # may run with a transaction open across the model/LXD call.
        produced: list[dict] = []
        for node_id, event, ctx in pending:
            audit = self._remediate(node_id, event, ctx)
            if audit is not None:
                produced.append(audit)
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

    def _detect_node(self, conn, node: dict,
                     engine: triggers.TriggerEngine) -> list[tuple[str, object, decision.DecisionContext]]:
        """Evaluate one node's triggers inside the detection transaction.

        Persists the (possibly new) hysteresis latch state and, for each event
        that is past the cooldown, the fired health event; returns the events to
        remediate together with their C0 decision context. The decision/model
        call is deliberately NOT made here — nothing that can take minutes may
        run while this write transaction is open.
        """
        node_id = node["id"]
        samples_desc = store.recent_samples(conn, node_id, self.window)
        samples_asc = list(reversed(samples_desc))
        now = triggers.now_iso_ms()

        state = opstore.load_trigger_state(conn, node_id)
        events, new_state = engine.evaluate(node_id, samples_asc, state, now)
        for rule_id, st in new_state.items():
            opstore.save_trigger_state(conn, node_id, rule_id, st, now)

        pending: list[tuple[str, object, decision.DecisionContext]] = []
        for event in events:
            # Cooldown: a time-based supplement to the hysteresis latch. Checked
            # here (read-only) rather than in the remediation transaction, so the
            # model call never runs under a held transaction.
            if self.cooldown_s > 0 and opstore.recent_audit(
                conn, node_id, event.rule_id, self.cooldown_s
            ):
                continue
            opstore.insert_health_event(conn, event)
            pending.append((node_id, event, self._build_context(event, samples_asc)))
        return pending

    def _remediate(self, node_id: str, event, ctx: decision.DecisionContext) -> dict | None:
        """Decide -> write decision -> execute -> audit ONE event.

        Holds NO transaction across the decision (a model call in model mode) or
        the LXD call; every DB write around them is its own short transaction.
        This is DEFECT 1's fix: a concurrent writer (telemetry ingest, a new
        `POST /intents`) is never blocked behind this loop's slow steps.
        """
        # Detection latency: fault-onset (first breaching sample) -> trigger fire.
        detect_lat = triggers.detection_latency_s(event)
        log.info("node %s rule %s FIRED: detection_latency=%ss (breach=%s -> fired=%s)",
                 node_id, event.rule_id, detect_lat, event.breach_started_at, event.fired_at)

        # Renew the single-owner lease for the duration of this (possibly long)
        # pass, in its own short transaction. If another operator took the lease
        # over while we were between passes, we are a standby and issue nothing.
        with self._session() as conn:
            if not opstore.acquire_lease(
                conn, self.project["id"], self.owner_id,
                triggers.now_iso_ms(), self.pass_lease_ttl_s,
            ):
                log.info("operator loop lost the lease; skipping %s/%s",
                         node_id, event.rule_id)
                return None

        now = triggers.now_iso_ms()
        # Resolve the node's LIVE instance inventory into the decision context
        # BEFORE the decision, with no transaction open (it is an HTTP call to the
        # node's LXD). Without it a model-chosen `vm_id` cannot resolve to the
        # instance it came from and the write 404s (see `_live_instance_names`).
        live_names = self._live_instance_names(node_id)
        if live_names:
            merged = dict(ctx.instance_names or {})
            merged.update(live_names)
            ctx.instance_names = merged
        # Start the remediation clock BEFORE the chooser so chooser/selection time
        # is included in remediation_latency_s (t_2c00b7fe). A model-driven
        # chooser can dominate the wall-clock cost.
        remed_start = time.monotonic()
        result = self._decide(node_id, event, ctx, now)
        action = result.action
        trace = result.trace
        if action is None:
            log.info("event %s (%s) has no playbook action; recorded only",
                     event.rule_id, node_id)
            self._emit_log_line(node_id, f"{event.rule_id}: no playbook action; recorded only")
            return None

        decision_id = ulid.new_ulid()
        with self._session() as conn:
            opstore.insert_decision(
                conn, decision_id, event, action, now,
                source=result.source, confidence=result.confidence,
                fallback_reason=result.fallback_reason,
                incident=result.incident,
                trace=trace,
            )

        target = action.params.get("instance_name", "")
        idem_key = opstore.idempotency_key_for(
            node_id, event.rule_id, action.kind, target, episode=event.episode
        )

        # Resolve the executor for THIS event's node before the idempotency
        # short-circuit, so the audit records the node actually dialled (or would
        # be dialled) rather than a global default that may be a different machine.
        ex = self._executor_for(node_id)

        # Idempotency: same episode already applied -> no-op (safety net only;
        # the latch is the primary dedup). The lookup is bounded by the §1.5 24h
        # idempotency window, so an expired prior audit never suppresses a
        # genuine later episode; failed entries are already excluded below.
        with self._session() as conn:
            prior = opstore.audit_by_idempotency_key(conn, idem_key)
        if prior is not None and prior["result"] in ("ok", "dry_run"):
            result = ExecResult(
                result="already_applied",
                detail=f"idempotency key {idem_key} already applied "
                       f"({prior['executed_at']})",
            )
            dry_run = False
        else:
            # F05 restart-refusal guard: while the node's latest telemetry still
            # shows a read-only disk mount, refuse to (re)start the workload. The
            # refusal is guardrail-level — applied to the C0 telemetry state the
            # decision already carries, whatever the model/playbook decided.
            refusal = decision.fenced_action_refusal(ctx.state, action.kind)
            if refusal:
                result = ExecResult(
                    result="error", reason=refusal,
                    detail=decision.fenced_refusal_detail(ctx.state, action.kind, node_id),
                )
                dry_run = bool(ex.dry_run)
            else:
                # No transaction is open across the LXD REST call either.
                result = ex.execute(action)
                dry_run = bool(ex.dry_run)

        audit_id = ulid.new_ulid()
        # Remediation latency: chooser start -> remediation completed (includes
        # chooser/selection time, which the previous `fired_at -> now` calc omitted).
        remed_lat = round(time.monotonic() - remed_start, 3)
        # A refused action says WHY, in the operator log the client UI renders.
        # Without this the live demo showed `-> error (threshold: ...)` with the
        # reason (`HTTP 404 ... Instance not found`) buried in the audit detail,
        # which reads as the AI failing rather than as the guard refusing a target
        # that is not on the node.
        refusal = _refusal_summary(result)
        # §4.3 trace: the `executed` and `audit_recorded` steps exist only now,
        # after the executor has produced its result — appended in the audit's
        # transaction so a trace never asserts an outcome before it is real.
        if trace is not None:
            exec_state = (decision.TRACE_STATE_REJECTED if result.result == "error"
                          else decision.TRACE_STATE_OK)
            exec_detail = f"result={result.result} latency_ms={decision.safe_int_display(remed_lat * 1000)}"
            if refusal:
                exec_detail += f" reason={refusal}"
            trace.add("executed", f"action executed: {result.result}", exec_detail,
                      state=exec_state)
            trace.add("audit_recorded", "audit row written",
                      f"audit_id={audit_id} decision_id={decision_id}",
                      state=decision.TRACE_STATE_OK)
        message = f"{event.rule_id}: {action.kind} {target} -> {result.result}".strip()
        if refusal:
            message += f" ({refusal})"
        threshold = getattr(event, "threshold", None)
        if threshold:
            message += f" (threshold: {threshold})"
        with self._session() as conn:
            opstore.insert_audit(
                conn, audit_id, decision_id, event, action, result, idem_key, now, dry_run,
                lxd_endpoint=getattr(ex, "base_url", ""),
            )
            if trace is not None:
                opstore.append_decision_trace(conn, decision_id, trace)
            self._emit_log(conn, node_id, message,
                           severity="warn" if refusal else "info")
        log.info("node %s rule %s -> %s %s: %s (remediation_latency=%ss)",
                 node_id, event.rule_id, action.kind, target, result.result, remed_lat)
        return {
            "audit_id": audit_id, "decision_id": decision_id, "event_id": event.event_id,
            "node_id": node_id, "rule_id": event.rule_id, "action_kind": action.kind,
            "result": result.result,
            "detection_latency_s": detect_lat, "remediation_latency_s": remed_lat,
        }

    # ---------------------------------------------------------------- decision
    def _build_context(self, event, samples_asc: list[dict]) -> decision.DecisionContext:
        """Assemble the C0 decision context from the event + latest telemetry."""
        evidence = samples_asc[-opstore.EVIDENCE_SAMPLE_CAP:]
        latest = samples_asc[-1] if samples_asc else {}
        instances = latest.get("lxd_instances", []) if isinstance(latest, dict) else []
        return decision.DecisionContext(
            event=event,
            state=dict(latest) if isinstance(latest, dict) else {},
            evidence=list(evidence),
            instances=list(instances) if isinstance(instances, list) else [],
            instance_names=self.instance_names,
            default_instance_name=self.default_instance_name,
            allowed_actions=self.allowed_actions,
            project_id=self.project["id"],
        )

    def _decide(self, node_id: str, event, ctx: decision.DecisionContext,
                now: str) -> decision.DecisionResult:
        """Run the decision step, applying the fallback-streak cap (§10.2).

        The provider call (a MODEL call in model mode) runs with NO transaction
        open: the fallback state is read in one short transaction before it and
        written in another after it, so a minutes-long model call cannot lock the
        database (DEFECT 1).
        """
        # Deterministic mode: never construct a model request (§12).
        if self.decision_mode != "model":
            return self._deterministic.decide(event, ctx)

        with self._session() as conn:
            state = opstore.load_decision_fallback_state(conn, node_id, event.rule_id)
        # Forced-deterministic cooldown: skip the model path entirely for this
        # (node, rule), emit one warn line at entry, and use deterministic.
        if decision.in_forced_cooldown(state, now):
            log.warning("node %s rule %s: forced deterministic (fallback cooldown)",
                        node_id, event.rule_id)
            self._emit_log_line(
                node_id, f"{event.rule_id}: forced deterministic (fallback cooldown)",
                severity="warn")
            return self._deterministic.decide(event, ctx)

        # NO transaction is open across the model call.
        result = self.provider.decide(event, ctx)

        if result.source == "model":
            new_state, _ = decision.transition_fallback_state(
                state, "success", now, self.decision_max_fallbacks,
                self.decision_fallback_cooldown_s,
            )
            with self._session() as conn:
                opstore.save_decision_fallback_state(
                    conn, node_id, event.rule_id, new_state["fallback_streak"],
                    new_state["forced_until"], now,
                )
        elif result.source == "fallback":
            new_state, forced = decision.transition_fallback_state(
                state, "fallback", now, self.decision_max_fallbacks,
                self.decision_fallback_cooldown_s,
            )
            log.warning("node %s rule %s: model decision fell back (reason=%s)",
                        node_id, event.rule_id, result.fallback_reason)
            with self._session() as conn:
                opstore.save_decision_fallback_state(
                    conn, node_id, event.rule_id, new_state["fallback_streak"],
                    new_state["forced_until"], now,
                )
                self._emit_log(
                    conn, node_id,
                    f"{event.rule_id}: fallback ({result.fallback_reason}) -> deterministic",
                    severity="warn")
                if forced:
                    self._emit_log(
                        conn, node_id,
                        f"{event.rule_id}: fallback cap reached; forcing deterministic for "
                        f"{self.decision_fallback_cooldown_s:.0f}s",
                        severity="warn",
                    )
        return result

    # ------------------------------------------------------------------ logging
    def _emit_log_line(self, node_id: str, message: str, severity: str = "info") -> None:
        """Publish one operator line in its own short transaction.

        The standalone form of `_emit_log` for call sites that are not already
        inside a session (so a log line never forces a transaction to be held).
        """
        if self.logs is None:
            return
        try:
            with self._session() as conn:
                self._emit_log(conn, node_id, message, severity=severity)
        except Exception:  # noqa: BLE001
            log.exception("failed to emit operator log line")

    def _emit_log(self, conn, node_id: str, message: str, severity: str = "info") -> None:
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
                node_id=node_id, severity=severity, actor="ai_operator",
                message=message, remediation=None,
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to emit operator log line")
