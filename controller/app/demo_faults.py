"""Operator-only demo fault injection — the controller half of the fault console
(`docs/contracts/controller-api.md` §4.7).

Why this exists
---------------
Fault injection used to be `console/seed_demo.py`: a CLI that registers two
SYNTHETIC nodes and oscillates their telemetry. There was no way to trigger a
*specific* fault from a UI, no way to target a chosen node, and no way to clear a
running fault — a demo run therefore depended on a background seeder that was
always on, and on luck about which half of the oscillation was on screen.

This module is the operator side of the fix: a controller-owned *fault driver* the
operator console (`console/fault_console/`) starts and clears over
`POST/DELETE /v1/projects/{pid}/demo/faults`, authenticated with the project token
(never a node token, never public — see §4.7's guardrail note).

What an "injection" is (and is not)
-----------------------------------
An injection takes authorship of one node's telemetry series for its TTL: the
driver writes a faulted sample every `cadence_s` into the **same** store path the
node's own telemetry goes through (`store.insert_samples` / `insert_batch`, plus
the broker fan-out), so the charts, the frozen threshold rules and the AI-operator
loop consume it exactly as if the node had reported it. There is no node-side
agent in this build (`docs/contracts/node-bootstrap.md` is specified, not shipped),
so a controller-side driver is the only thing that can put a chosen node into a
chosen fault *on demand* — which is what the demo needs.

Two consequences worth stating plainly:

* While an injection is open for a node, that node's OWN telemetry is superseded
  (`open_fault_for_node` — the ingest path records the batch but stores none of its
  samples). Without that, a real node's healthy samples would interleave with the
  injected ones and the fault would flap instead of holding the rule across its
  debounce count. This is a demo control surface on a demo project, and it is
  fenced: project token only, one open injection per (node, rule).
* The faulted samples ARE the node's series while the injection runs, and they are
  stored in their own provenance channel: every row the driver writes carries
  `telemetry_samples.source = 'demo_fault'` while the node's own rows carry
  `'node'` (`store.DEMO_FAULT_SOURCE` / `store.NODE_SOURCE`). The channel also owns
  its own `seq` namespace, which is what stops a 1 Hz injected series from
  occupying the node's upcoming `seq` values and starving its real stream after a
  clear (t_c37aa0dc — see `store.insert_samples`). Durable provenance is the
  `demo_faults` row plus the operator-log line ("fault injected by operator: …")
  AND the per-row channel, so the stored series says which samples were simulated.

Safety (the card's §3)
----------------------
1. **Refuse to inject while a remediation for the same (node, rule) is in flight**
   — an active injection for that (node, rule), or a remediation the operator is
   still working on (`_remediation_in_flight`). Both are `409 CONFLICT`.
2. **A fault is always clearable** — `clear()` never refuses on state; clearing an
   already-cleared injection is an idempotent `already_cleared`.
3. **A clear restores the series** — the injection keeps writing for
   `clear_samples_for(rule)` ticks, emitting the healthy baseline verbatim, which
   is the number of consecutive healthy samples the rule's hysteresis needs to
   re-arm (`operator.detect.recovered`). The rule provably returns below
   threshold; the driver does not just stop.
4. **No injection outlives its TTL silently** — at `expires_at` the ticker logs the
   expiry, moves the row to `recovering`, and finishes with a clear. A row left
   `active`/`recovering` by a previous controller process is swept to `expired` at
   boot (`expire_stale_on_boot`) and logged.

The env knob `TERMINI_DEMO_FAULT_GUARD_S` (default 60 s) is the settle window
during which the operator's remediation for a (node, rule) still counts as in
flight; see §4.7.
"""
from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import re
import threading
import time

from . import db, store, ulid
from . import errors
from .operator import detect, triggers
from .operator import store as opstore

log = logging.getLogger("termini.demo_faults")

# --------------------------------------------------------------------------- #
# Attribution (kanban t_9c1362f6). Every caller of this surface presents the SAME
# project token, so the token cannot say who acted — on 2026-10-17 a peer worker's
# clear-all was recorded as `cleared_by=human` ("the operator") and silently
# invalidated another worker's live acceptance. The caller must therefore declare
# itself in `X-Termini-Actor`; when it does not, the row says so instead of
# inventing an operator: `unattributed_project_token`.
UNATTRIBUTED_ACTOR = "unattributed_project_token"
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}$")


def actor_label(actor: str) -> str:
    """The actor as it reads in an operator-log sentence."""
    if actor == "human":
        return "operator"
    if actor == UNATTRIBUTED_ACTOR:
        return "an un-attributed project-token caller (no X-Termini-Actor header)"
    return actor

# --------------------------------------------------------------------------- #
# Scenarios — the two demo faults the operator may inject. `disk-degrade` and
# `latency` are deliberately absent: their detector rules (`disk-degrade-01`,
# `latency-01`) are frozen in node-telemetry.md §5 and stay implemented in
# `operator/detect.py` — a real customer node can still produce those signals
# and the operator must still handle them — but the DEMO can no longer inject
# them (owner decision 2026-10-17). Parameter values mirror the harness's
# (`chaos/chaos.json`), so an injected fault is the same signature the chaos
# runs prove out; `rule_id` is the frozen rule the scenario drives.
# --------------------------------------------------------------------------- #
SCENARIOS: dict[str, dict] = {
    "oom": {
        "rule_id": "oom-guard-01",
        "description": "Ramp host RAM utilisation past the OOM threshold and hold it.",
        "params": {"ram_peak_pct": 97.0, "cpu_util_pct": 92.0, "instance_ram_pct": 85.0},
    },
    "disk-fail": {
        "rule_id": "disk-fail-01",
        "description": "Flip a disk mount read-only (EXT4 remount-ro) — the Scenario B signature.",
        "params": {},
    },
}
SCENARIO_NAMES = tuple(SCENARIOS)
RULE_BY_SCENARIO = {name: spec["rule_id"] for name, spec in SCENARIOS.items()}
SCENARIO_BY_RULE = {rule: name for name, rule in RULE_BY_SCENARIO.items()}

# Healthier-than-threshold values the baseline/healthy series is pinned to, so
# `clear` is guaranteed to satisfy `operator.detect.recovered` whatever the node
# happened to be reporting when the operator injected.
HEALTHY_RAM_PCT = 62.0
HEALTHY_DISK_FREE_PCT = 78.0
HEALTHY_DISK_WRITE_LATENCY_MS = 3.0
HEALTHY_NET_LATENCY_MS = 0.8

MIN_DURATION_S = 5.0
MAX_DURATION_S = 600.0
DEFAULT_DURATION_S = 30.0
DEFAULT_CADENCE_S = 1.0
DEFAULT_GUARD_S = 60.0
CLEAR_SAMPLES_DEFAULT = 3
OPEN_STATES = ("active", "recovering")

# `operator.detect.Thresholds` field that carries the re-arm sample count of each
# frozen rule (`recovered()` uses exactly these).
_CLEAR_ATTR = {
    "oom-guard-01": "oom_clear_consecutive",
    "disk-fail-01": "disk_fail_clear_consecutive",
}


def guard_window_s() -> float:
    """Settle window during which a just-run remediation counts as in flight."""
    try:
        return max(0.0, float(os.environ.get("TERMINI_DEMO_FAULT_GUARD_S", DEFAULT_GUARD_S)))
    except ValueError:
        return DEFAULT_GUARD_S


def clear_samples_for(rule_id: str, thresholds: detect.Thresholds) -> int:
    """How many consecutive healthy samples the rule needs to re-arm."""
    attr = _CLEAR_ATTR.get(rule_id)
    n = getattr(thresholds, attr, CLEAR_SAMPLES_DEFAULT) if attr else CLEAR_SAMPLES_DEFAULT
    try:
        return max(1, int(n))
    except (TypeError, ValueError):
        return CLEAR_SAMPLES_DEFAULT


# --------------------------------------------------------------------------- #
# Sample derivation (pure — unit-testable without a DB)
# --------------------------------------------------------------------------- #
def healthy_sample(base: dict, *, seq: int, ts: str) -> dict:
    """The baseline series: every frozen rule's metric in its healthy band.

    The node's own latest sample is normalised through this, so the baseline is
    always clear-able — including when the operator injects onto a node the seeder
    happened to be oscillating at that moment.
    """
    s = copy.deepcopy(base) if isinstance(base, dict) else {}
    s["seq"] = int(seq)
    s["ts"] = ts
    total = s.get("ram_total_bytes") or 0
    if isinstance(total, (int, float)) and total > 0:
        s["ram_used_bytes"] = int(total * HEALTHY_RAM_PCT / 100.0)
    s["cpu_util_pct"] = 31.2
    for d in s.get("disk") or []:
        if not isinstance(d, dict):
            continue
        tot = d.get("total_bytes")
        if isinstance(tot, (int, float)) and tot > 0:
            d["free_bytes"] = int(tot * HEALTHY_DISK_FREE_PCT / 100.0)
        d["write_latency_ms"] = HEALTHY_DISK_WRITE_LATENCY_MS
        d["mount_ro"] = False
    for n in s.get("net") or []:
        if isinstance(n, dict):
            n["latency_ms"] = HEALTHY_NET_LATENCY_MS
    return s


def fault_sample(base: dict, scenario: str, params: dict | None = None, *, seq: int, ts: str) -> dict:
    """One faulted sample for `scenario`, derived from the healthy baseline.

    Deriving from the baseline (rather than from a hand-built sample) is what
    keeps the injected series inside the frozen telemetry schema: every field the
    node reported is carried over, only the faulted metric moves.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    p = dict(SCENARIOS[scenario].get("params") or {})
    p.update(params or {})
    s = copy.deepcopy(base)
    s["seq"] = int(seq)
    s["ts"] = ts

    if scenario == "oom":
        total = s.get("ram_total_bytes") or 0
        peak = float(p.get("ram_peak_pct", 97.0))
        if isinstance(total, (int, float)) and total > 0:
            s["ram_used_bytes"] = int(total * peak / 100.0)
        s["cpu_util_pct"] = float(p.get("cpu_util_pct", 92.0))
        # Keep the nested inventory consistent: the operator's `heaviest` target
        # resolves against `lxd_instances[]` (see console/seed_demo.py).
        for inst in s.get("lxd_instances") or []:
            if not isinstance(inst, dict):
                continue
            lim = inst.get("ram_limit_bytes")
            if isinstance(lim, (int, float)) and lim > 0:
                inst["ram_used_bytes"] = int(lim * float(p.get("instance_ram_pct", 85.0)) / 100.0)
    elif scenario == "disk-fail":
        for d in s.get("disk") or []:
            if isinstance(d, dict):
                d["mount_ro"] = True
    return s


def baseline_from_hardware(node: dict) -> dict:
    """A minimal healthy baseline for a node that has not reported yet.

    Used only when the fleet row has hardware facts but no stored sample (a node
    that registered seconds ago). Everything the frozen rules read is filled in;
    the rest stays optional.
    """
    hw = json.loads(node["hardware_json"]) if node.get("hardware_json") else {}
    if not isinstance(hw, dict):
        hw = {}
    mem = hw.get("memory") if isinstance(hw.get("memory"), dict) else {}
    ram_total = mem.get("total_bytes") or (int(mem.get("ram_gb", 0)) * 1024 ** 3 if mem.get("ram_gb") else 0)
    disks = hw.get("disks") if isinstance(hw.get("disks"), list) else []
    disk_total = 0
    for d in disks:
        if isinstance(d, dict) and isinstance(d.get("size_bytes"), (int, float)):
            disk_total += int(d["size_bytes"])
    s: dict = {
        "cpu_util_pct": 31.2,
        "cpu_core_count": 8,
        "ram_total_bytes": int(ram_total or 0),
        "ram_used_bytes": int((ram_total or 0) * HEALTHY_RAM_PCT / 100.0),
        "uptime_sec": 3600,
        "services": [
            {"name": "termini-bootstrap", "status": "running"},
            {"name": "lxd", "status": "running"},
            {"name": "sshd", "status": "running"},
        ],
        "lxd_instances": [],
    }
    if disk_total:
        s["disk"] = [{
            "path": "/dev/sda", "total_bytes": disk_total,
            "free_bytes": int(disk_total * HEALTHY_DISK_FREE_PCT / 100.0),
            "write_latency_ms": HEALTHY_DISK_WRITE_LATENCY_MS, "mount_ro": False,
        }]
    return s


# --------------------------------------------------------------------------- #
# Store access (one short transaction each — see app/db.py)
# --------------------------------------------------------------------------- #
def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def insert_fault(conn, row: dict) -> None:
    conn.execute(
        "INSERT INTO demo_faults (injection_id, project_id, node_id, scenario, rule_id,"
        " target, state, duration_s, started_at, expires_at, next_seq, samples_written,"
        " recovery_samples, baseline_json, params_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["injection_id"], row["project_id"], row["node_id"], row["scenario"],
            row["rule_id"], row["target"], row["state"], row["duration_s"],
            row["started_at"], row["expires_at"], row["next_seq"], 0, 0,
            json.dumps(row["baseline"]), json.dumps(row.get("params") or {}),
        ),
    )


def get_fault(conn, injection_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM demo_faults WHERE injection_id = ?", (injection_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_faults(conn, project_id: str, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM demo_faults WHERE project_id = ? ORDER BY started_at DESC, injection_id DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_open_faults(conn, project_id: str) -> list[dict]:
    q = ",".join("?" for _ in OPEN_STATES)
    rows = conn.execute(
        f"SELECT * FROM demo_faults WHERE project_id = ? AND state IN ({q})"
        " ORDER BY started_at ASC",
        (project_id, *OPEN_STATES),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def open_fault_for_node(conn, project_id: str, node_id: str) -> dict | None:
    """The open injection taking authorship of `node_id`'s series, if any.

    Called on every telemetry ingest (indexed lookup) to supersede the node's own
    samples while an injection is running.
    """
    q = ",".join("?" for _ in OPEN_STATES)
    row = conn.execute(
        f"SELECT * FROM demo_faults WHERE project_id = ? AND node_id = ? AND state IN ({q})"
        " ORDER BY started_at DESC LIMIT 1",
        (project_id, node_id, *OPEN_STATES),
    ).fetchone()
    return _row_to_dict(row) if row else None


def open_fault_for_rule(conn, project_id: str, node_id: str, rule_id: str) -> dict | None:
    q = ",".join("?" for _ in OPEN_STATES)
    row = conn.execute(
        f"SELECT * FROM demo_faults WHERE project_id = ? AND node_id = ? AND rule_id = ?"
        f" AND state IN ({q}) ORDER BY started_at DESC LIMIT 1",
        (project_id, node_id, rule_id, *OPEN_STATES),
    ).fetchone()
    return _row_to_dict(row) if row else None


def update_fault(conn, injection_id: str, *, where_state: str | None = None,
                 **fields) -> int:
    """Patch one injection row. Returns the number of rows actually changed.

    `where_state` makes the write a COMPARE-AND-SET: it applies only while the row
    is still in the state the caller observed. The driver reads its open rows at the
    START of a tick and writes at the end, so without this guard a clear (or a TTL)
    landing mid-tick was overwritten by the in-flight tick: on main's CI that showed
    up as a cleared injection resurrected to `active` — an EARLIER state than
    `recovering` — and a series the clear had just restored re-breached.

    A lost CAS is not an error: the newer write (the operator's) is the truth.
    """
    if not fields:
        return 0
    cols = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE demo_faults SET {cols} WHERE injection_id = ?"
    params: list = [*fields.values(), injection_id]
    if where_state is not None:
        sql += " AND state = ?"
        params.append(where_state)
    return conn.execute(sql, params).rowcount


def _epoch(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return triggers.parse_epoch_ms(ts) or 0.0
    except Exception:  # noqa: BLE001 — a malformed stamp must not stop the driver
        return 0.0


def _now_iso() -> str:
    return triggers.now_iso_ms()


def _iso_in(seconds: float) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _remediation_in_flight(conn, node_id: str, rule_id: str, within_s: float) -> dict | None:
    """A remediation for (node, rule) the operator is still working on, if any.

    Two shapes count as in flight:
      * a fired health event for this (node, rule) that has NO audit row yet — the
        loop fired it and the decision/execution is still running;
      * a remediation audit executed within `within_s` — the operator just acted on
        this rule and the fleet has not settled yet.

    Bounded by `within_s` on purpose: the guard exists to stop a *second* fault
    landing on a rule mid-remediation, not to make the console unusable.
    """
    if within_s <= 0:
        return None
    cutoff = _iso_in(-within_s)
    unaudited = conn.execute(
        "SELECT h.id, h.fired_at, h.rule_id FROM health_events h"
        " WHERE h.node_id = ? AND h.rule_id = ? AND h.fired_at >= ?"
        "   AND NOT EXISTS (SELECT 1 FROM remediation_audits a WHERE a.event_id = h.id)"
        " ORDER BY h.fired_at DESC LIMIT 1",
        (node_id, rule_id, cutoff),
    ).fetchone()
    if unaudited is not None:
        return {"kind": "pending_remediation", "fired_at": unaudited["fired_at"]}
    audit = opstore.recent_audit(conn, node_id, rule_id, within_s)
    if audit is not None:
        return {"kind": "recent_remediation", "executed_at": audit["executed_at"],
                "action_kind": audit["action_kind"], "result": audit["result"]}
    return None


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
class DemoFaultInjector(threading.Thread):
    """Writes each open injection's sample series, and closes it at its TTL.

    The thread is a *driver*, not a source of truth: every tick it re-reads the
    open rows from the DB, so a handler that starts or clears an injection in the
    request transaction is never racing an in-memory copy.
    """

    def __init__(self, cfg, project_id: str, *, broker=None, operator_logs=None,
                 publish=None, cadence_s: float = DEFAULT_CADENCE_S,
                 thresholds: detect.Thresholds | None = None,
                 guard_s: float | None = None):
        super().__init__(name="demo-faults", daemon=True)
        self.cfg = cfg
        self.project_id = project_id
        self.broker = broker
        self.operator_logs = operator_logs
        # broker fan-out (node.telemetry / node.health_changed) — supplied by the
        # server so an injected sample lights the dashboard's live panels the same
        # way a real one does.
        self.publish = publish
        self.cadence_s = max(0.1, float(cadence_s))
        self.thresholds = thresholds
        self.guard_s = guard_window_s() if guard_s is None else max(0.0, float(guard_s))
        self._stop = threading.Event()
        self._supersede_logged: set[str] = set()

    # ------------------------------------------------------------- lifecycle
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.expire_stale_on_boot()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the driver must never die silently
                log.exception("demo fault injector tick failed")
            self._stop.wait(self.cadence_s)

    def expire_stale_on_boot(self) -> int:
        """Sweep injections left open by a previous controller process.

        An injection is driven by THIS process's thread, so a row still marked
        `active` after a restart can never be advanced again — it must not be
        reported as live to the console (rule 4: never outlive its TTL silently).
        Called INLINE from `app/main` before the server serves, so the first request
        after a restart already sees the truth; `run()` calls it again for a driver
        started without that (a no-op — nothing is open by then).

        The sweep names WHO restarted the controller (from the deploy tool's
        `DEPLOY_ACTOR`, written next to `DEPLOYED_COMMIT`), because a peer's redeploy
        killing your injection must not read as an operator action — kanban
        t_9c1362f6. No file → `unattributed`, never "human".
        """
        swept = 0
        actor, commit, deployed_at = self._deploy_actor()
        restarted_by = f"controller restarted by {actor_label(actor)}"
        if commit:
            restarted_by += f" (deploy {str(commit)[:12]}"
            restarted_by += f" at {deployed_at})" if deployed_at else ")"
        try:
            with db.session(self.cfg.db_path) as conn:
                rows = list_open_faults(conn, self.project_id)
                for row in rows:
                    update_fault(
                        conn, row["injection_id"], state="expired", cleared_at=_now_iso(),
                        cleared_by="controller_restart",
                        clear_reason=f"{restarted_by} — the driver that owned this "
                                     f"injection is gone",
                    )
                    self._emit_log(
                        conn, row["node_id"],
                        f"fault injection {row['injection_id']} expired: {restarted_by} "
                        f"while {row['scenario']} was {row['state']} — no samples are "
                        f"being written for it",
                        severity="warn",
                        actor=actor,
                    )
                    swept += 1
                if rows:
                    log.warning("swept %d stale demo fault injection(s) at boot (%s)",
                                len(rows), restarted_by)
        except Exception:  # noqa: BLE001 — boot must not die because of the driver
            log.exception("could not sweep stale demo fault injections")
        return swept

    def _deploy_actor(self) -> tuple[str, str | None, str | None]:
        """(actor, commit, deployed_at) from the deploy tool's `DEPLOY_ACTOR` file.

        Deliberately tolerant: a missing, unreadable or malformed file is reported as
        `unattributed` rather than guessed at — the audit line must never claim a
        person did something a worker did.
        """
        path = getattr(self.cfg, "deploy_actor_path", "") or ""
        if not path:
            return UNATTRIBUTED_ACTOR, None, None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            return UNATTRIBUTED_ACTOR, None, None
        if not isinstance(info, dict):
            return UNATTRIBUTED_ACTOR, None, None
        actor = str(info.get("actor") or "").strip()
        if not ACTOR_RE.match(actor):
            actor = UNATTRIBUTED_ACTOR
        commit = info.get("commit") if isinstance(info.get("commit"), str) else None
        deployed_at = (info.get("deployed_at")
                       if isinstance(info.get("deployed_at"), str) else None)
        return actor, commit, deployed_at

    # ------------------------------------------------------------------ tick
    def tick(self, *, now_epoch: float | None = None) -> list[dict]:
        """One driver pass: advance every open injection. Returns their views."""
        now_epoch = time.time() if now_epoch is None else now_epoch
        out: list[dict] = []
        with db.session(self.cfg.db_path) as conn:
            rows = list_open_faults(conn, self.project_id)
            for row in rows:
                try:
                    self._advance(conn, row, now_epoch)
                    fresh = get_fault(conn, row["injection_id"])
                    if fresh:
                        out.append(fresh)
                except Exception:  # noqa: BLE001 — one bad injection must not stop the others
                    log.exception("demo fault %s failed to advance", row["injection_id"])
        return out

    def _advance(self, conn, row: dict, now_epoch: float) -> None:
        injection_id = row["injection_id"]
        node_id = row["node_id"]
        rule_id = row["rule_id"]
        node = store.get_node(conn, node_id)
        if node is None:
            update_fault(conn, injection_id, state="expired", cleared_at=_now_iso(),
                         cleared_by="node_gone", clear_reason="node row no longer exists")
            self._emit_log(conn, node_id,
                           f"fault injection {injection_id} ended: node {node_id} is gone",
                           severity="warn")
            return

        thresholds = self.resolve_thresholds(conn)
        clear_n = clear_samples_for(rule_id, thresholds)
        baseline = json.loads(row["baseline_json"])
        ts = _now_iso()

        if row["state"] == "active":
            elapsed = now_epoch - _epoch(row["started_at"])
            if elapsed >= float(row["duration_s"] or DEFAULT_DURATION_S):
                # TTL: end the fault loudly, then run the recovery burst so the rule
                # re-arms (never stop mid-series and leave the rule breached).
                if not update_fault(conn, injection_id, state="recovering", cleared_at=ts,
                                    cleared_by="ttl", clear_reason="ttl_expired",
                                    where_state="active"):
                    # The operator cleared it while this tick was in flight: their
                    # clear (and its attribution) is the newer truth — do not
                    # overwrite it, and do not log a TTL that did not end anything.
                    return
                self._emit_log(
                    conn, node_id,
                    f"fault TTL expired after {float(row['duration_s']):.0f}s — auto-clearing: "
                    f"node {node.get('registration_id') or node_id} scenario={row['scenario']} "
                    f"(injection {injection_id})",
                )
                return
            params = json.loads(row["params_json"] or "{}")
            sample = fault_sample(baseline, row["scenario"], params, seq=0, ts=ts)
            self._write_sample(conn, node, row, sample, state_after="active")
            return

        if row["state"] == "recovering":
            n = int(row["recovery_samples"] or 0) + 1
            seq = self._next_seq(conn, row, node_id)
            sample = healthy_sample(baseline, seq=seq, ts=ts)
            call_id = self._insert_sample(conn, node, sample)
            if call_id is None:
                return
            done = n >= clear_n
            applied = update_fault(
                conn, injection_id, next_seq=seq + 1,
                samples_written=int(row["samples_written"] or 0) + 1,
                recovery_samples=n,
                state="cleared" if done else "recovering",
                cleared_at=ts if done else row["cleared_at"],
                where_state="recovering",
            )
            if done and applied:
                self._supersede_logged.discard(injection_id)
                # Who ended this injection, for the driver's own log lines (kanban
                # t_9c1362f6). `cleared_by` carries the caller's X-Termini-Actor; the
                # synthetic enders ('ttl', 'controller_restart', 'node_gone') are the
                # controller acting alone, so they fall back to the actor that injected
                # it and only then to 'controller' — a worker's fault must never produce
                # a line that reads as "the operator" (the default used to be 'human').
                self._emit_log(
                    conn, node_id,
                    f"fault cleared: node {node.get('registration_id') or node_id} "
                    f"rule {rule_id} back below threshold after {n} healthy sample(s) "
                    f"(injection {injection_id})",
                    actor=self._ended_by(row),
                )
            return

    def resolve_thresholds(self, conn) -> detect.Thresholds:
        """The thresholds the injector must agree with the operator loop on.

        Used by the driver (which decides when a rule has had enough recovery
        samples) AND by `view()` (which reports `clear_samples_needed`): they must be
        the same numbers, or the console would promise a re-arm count the driver
        does not honour. Durable per-rule config wins; the loop's env defaults are
        the fallback, and `detect.Thresholds()` when nothing is configured at all.
        """
        if self.thresholds is not None:
            return self.thresholds
        parts = opstore.load_trigger_configs(conn)
        return triggers.merge_config(parts) if parts else detect.Thresholds()

    def _next_seq(self, conn, row: dict, node_id: str) -> int:
        """Next sequence number in the INJECTION's own channel.

        The store drops any sample whose `seq` is not greater than the highest it
        already holds *for that sample source*, and the injector writes to
        `store.DEMO_FAULT_SOURCE` — never the node's own channel. That separation
        is the fix for t_c37aa0dc: a 1 Hz injected series used to share the node's
        `seq` head, so after a 60 s injection the node's own slower counter was
        tens of seqs behind and every real sample it posted was dropped (measured
        2 m 19 s of missing node telemetry after a clear).
        """
        last = store.max_seq(conn, node_id, store.DEMO_FAULT_SOURCE)
        return max(int(row["next_seq"] or 0), last + 1, 1)

    def _insert_sample(self, conn, node: dict, sample: dict) -> str | None:
        call_id = ulid.new_ulid()
        inserted, dropped = store.insert_samples(
            conn, node["id"], call_id, [sample], source=store.DEMO_FAULT_SOURCE
        )
        batch = {
            "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
            "call_id": call_id,
            "node_id": node["id"],
            "schema_version": 1,
            "governance_chain_tip": ulid.new_ulid(),
            "samples": [sample],
        }
        now = store.now_iso()
        store.insert_batch(conn, batch, node["id"], inserted, dropped, now)
        conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node["id"]))
        if inserted <= 0:
            return None
        if self.publish is not None:
            try:
                self.publish(conn, node, sample)
            except Exception:  # noqa: BLE001 — observability must not stop the driver
                log.exception("failed to publish an injected sample")
        return call_id

    def _write_sample(self, conn, node: dict, row: dict, sample: dict, *, state_after: str) -> None:
        # `row` was read at the START of this tick; a clear (or a TTL) can have landed
        # since. Re-read before doing anything, so a superseded tick does not write a
        # stale breaching sample, and keep the write a compare-and-set on the state
        # this tick observed: an unconditional write RESURRECTED a cleared injection
        # (`active` over `recovering`) and re-breached the series the clear had just
        # restored. Losing the race is not an error — the operator's clear is newer.
        fresh = get_fault(conn, row["injection_id"])
        if fresh is None or fresh["state"] != row["state"]:
            return
        seq = self._next_seq(conn, row, node["id"])
        sample["seq"] = seq
        if self._insert_sample(conn, node, sample) is None:
            # A dropped sample means the sequence lost a race with the node's own
            # reporting; leave the row alone and let the next tick retry above it.
            return
        update_fault(
            conn, row["injection_id"], next_seq=seq + 1,
            samples_written=int(row["samples_written"] or 0) + 1, state=state_after,
            where_state=row["state"],
        )

    # ------------------------------------------------------------ console API
    def start_injection(self, conn, project_id: str, body: dict, *,
                        actor: str = UNATTRIBUTED_ACTOR) -> dict:
        """Start an injection. Raises `errors.ApiError` on any refusal."""
        if project_id != self.project_id:
            raise errors.permission_denied("project_id does not match token scope")
        if not isinstance(body, dict):
            raise errors.invalid_argument("body must be a JSON object")

        scenario = body.get("scenario")
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise errors.invalid_argument(
                "scenario must be one of " + ", ".join(SCENARIO_NAMES),
                [{"field": "scenario", "reason": f"got {scenario!r}"}],
            )
        rule_id = RULE_BY_SCENARIO[scenario]

        node_id = body.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise errors.invalid_argument("node_id is required")
        node = store.get_node(conn, node_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {node_id} not found")

        target = body.get("target")
        if target not in ("real", "synthetic"):
            raise errors.invalid_argument(
                "target must be 'real' or 'synthetic' (the node's provenance)",
                [{"field": "target", "reason": f"got {target!r}"}],
            )
        provenance = "synthetic" if node.get("synthetic") else "real"
        if target != provenance:
            raise errors.invalid_argument(
                f"node {node_id} is a {provenance} node; target={target} does not match it",
                [{"field": "target", "reason": f"node provenance is {provenance}"}],
            )

        duration = body.get("duration_s", body.get("duration", DEFAULT_DURATION_S))
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise errors.invalid_argument("duration_s must be a number of seconds")
        duration = float(duration)
        if duration < MIN_DURATION_S or duration > MAX_DURATION_S:
            raise errors.invalid_argument(
                f"duration_s must be between {MIN_DURATION_S:.0f} and {MAX_DURATION_S:.0f} seconds",
                [{"field": "duration_s", "reason": f"got {duration}"}],
            )

        # Guard 1a: one open injection per (node, rule).
        existing = open_fault_for_rule(conn, project_id, node_id, rule_id)
        if existing is not None:
            raise errors.err(
                409, "CONFLICT",
                f"fault already active on node {node_id}: injection "
                f"{existing['injection_id']} ({existing['scenario']}, {existing['state']})",
                [{"field": "node_id", "reason": "fault_already_active"},
                 {"field": "injection_id", "reason": existing["injection_id"]}],
            )
        # Guard 1b: refuse while a remediation for the same (node, rule) is in flight.
        inflight = _remediation_in_flight(conn, node_id, rule_id, self.guard_s)
        if inflight is not None:
            raise errors.err(
                409, "CONFLICT",
                f"refusing to inject {scenario} on node {node_id}: a remediation for "
                f"{rule_id} is in flight ({inflight['kind']})",
                [{"field": "node_id", "reason": "remediation_in_flight"},
                 {"field": "rule_id", "reason": inflight["kind"]}],
            )

        # Baseline: the node's own latest sample, normalised healthy (so a clear
        # always returns the rule below its clear threshold); hardware facts when
        # the node has not reported yet.
        latest = store.latest_sample(conn, node_id)
        if latest is None:
            hw_base = baseline_from_hardware(node)
            if not hw_base.get("ram_total_bytes"):
                raise errors.err(
                    409, "CONFLICT",
                    f"node {node_id} has reported no telemetry and no hardware facts to "
                    "build a baseline from",
                    [{"field": "node_id", "reason": "no_telemetry_baseline"}],
                )
            latest = hw_base
        baseline = healthy_sample(latest, seq=0, ts=_now_iso())
        seq = self._next_seq(conn, {"next_seq": 0}, node_id)

        injection_id = ulid.new_ulid()
        started = _now_iso()
        row = {
            "injection_id": injection_id,
            "project_id": project_id,
            "node_id": node_id,
            "scenario": scenario,
            "rule_id": rule_id,
            "target": target,
            "state": "active",
            "duration_s": duration,
            "started_at": started,
            "expires_at": _iso_in(duration),
            "next_seq": seq,
            "baseline": baseline,
            # The caller's X-Termini-Actor travels with the row so the driver's
            # later lines (supersede, recovery-complete) can name the worker that
            # started the fault instead of defaulting to "the operator".
            "params": {**dict(SCENARIOS[scenario]["params"] or {}), "actor": actor},
        }
        insert_fault(conn, row)
        self._supersede_logged.discard(injection_id)
        # The client dashboard's live AI-operator panel is where the judge looks;
        # the line says who did it and that node telemetry is superseded.
        self._emit_log(
            conn, node_id,
            f"fault injected by {actor_label(actor)}: node "
            f"{node.get('registration_id') or node_id} "
            f"scenario={scenario} rule={rule_id} target={target} ttl={duration:.0f}s "
            f"(injection {injection_id}; node telemetry superseded while active)",
            actor=actor,
        )
        fresh = get_fault(conn, injection_id)
        return self.view(fresh, node, thresholds=self.resolve_thresholds(conn))

    def clear(self, conn, project_id: str, injection_id: str, *,
              reason: str = "cleared_by_operator", actor: str = UNATTRIBUTED_ACTOR) -> dict:
        """Clear one injection. Idempotent; never refuses on state (rule 2)."""
        if project_id != self.project_id:
            raise errors.permission_denied("project_id does not match token scope")
        row = get_fault(conn, injection_id)
        if row is None or row["project_id"] != project_id:
            raise errors.not_found(f"injection {injection_id} not found")
        node = store.get_node(conn, row["node_id"])
        thresholds = self.resolve_thresholds(conn)
        if row["state"] in ("cleared", "expired"):
            view = self.view(row, node, thresholds=thresholds)
            view["status"] = "already_cleared" if row["state"] == "cleared" else "already_expired"
            return view
        self._close(conn, row, node, reason=reason, actor=actor)
        return self.view(get_fault(conn, injection_id), node, thresholds=thresholds)

    def clear_all(self, conn, project_id: str, *, node_id: str | None = None,
                  reason: str = "cleared_by_operator",
                  actor: str = UNATTRIBUTED_ACTOR) -> dict:
        if project_id != self.project_id:
            raise errors.permission_denied("project_id does not match token scope")
        cleared = []
        thresholds = self.resolve_thresholds(conn)
        for row in list_open_faults(conn, project_id):
            if node_id and row["node_id"] != node_id:
                continue
            node = store.get_node(conn, row["node_id"])
            self._close(conn, row, node, reason=reason, actor=actor)
            cleared.append(self.view(get_fault(conn, row["injection_id"]), node,
                                     thresholds=thresholds))
        return {"cleared": cleared, "count": len(cleared)}

    def _close(self, conn, row: dict, node: dict | None, *, reason: str, actor: str) -> None:
        ts = _now_iso()
        update_fault(conn, row["injection_id"], state="recovering", cleared_at=ts,
                     cleared_by=actor, clear_reason=reason)
        thresholds = self.resolve_thresholds(conn)
        n = clear_samples_for(row["rule_id"], thresholds)
        name = (node or {}).get("registration_id") or row["node_id"]
        self._emit_log(
            conn, row["node_id"],
            f"fault cleared by {actor_label(actor)}: node {name} "
            f"scenario={row['scenario']} "
            f"rule={row['rule_id']} — restoring the healthy series, {n} sample(s) "
            f"to re-arm (injection {row['injection_id']})",
            actor=actor,
        )

    # -------------------------------------------------------------- rendering
    def view(self, row: dict | None, node: dict | None = None, *,
             thresholds: detect.Thresholds | None = None) -> dict:
        """The console/contract view of one injection (no secrets, no raw logs).

        `thresholds` should be `resolve_thresholds(conn)` when a connection is at
        hand, so `clear_samples_needed` is the count the driver will actually emit.
        """
        if not row:
            return {}
        effective = thresholds or self.thresholds or detect.Thresholds()
        return {
            "schema_version": "1.0",
            "injection_id": row["injection_id"],
            "project_id": row["project_id"],
            "node_id": row["node_id"],
            "node_registration_id": (node or {}).get("registration_id"),
            "node_role": (node or {}).get("role"),
            "scenario": row["scenario"],
            "rule_id": row["rule_id"],
            "target": row["target"],
            "state": row["state"],
            "duration_s": row["duration_s"],
            "started_at": row["started_at"],
            "expires_at": row["expires_at"],
            "cleared_at": row["cleared_at"],
            "cleared_by": row["cleared_by"],
            "clear_reason": row["clear_reason"],
            "samples_written": row["samples_written"],
            "recovery_samples": row["recovery_samples"],
            "clear_samples_needed": clear_samples_for(row["rule_id"], effective),
            "open": row["state"] in OPEN_STATES,
        }

    # -------------------------------------------------------------- internals
    def _fault_actor(self, row: dict | None) -> str:
        """The actor a driver-emitted line about `row` belongs to.

        The row's `params_json` carries the caller's X-Termini-Actor at injection
        time; `cleared_by` carries it again when somebody cleared the injection.
        Neither exists for an injection started before this field was recorded, in
        which case the honest answer is `controller` — never `human`, which is what
        made a worker's fault read as an operator action (kanban t_9c1362f6).
        """
        try:
            params = json.loads((row or {}).get("params_json") or "{}")
        except (TypeError, ValueError):
            params = {}
        actor = str(params.get("actor") or "").strip()
        return actor if ACTOR_RE.match(actor) else "controller"

    def _ended_by(self, row: dict) -> str:
        """Who ended the injection: the clearer, else the injector, else 'controller'."""
        by = str(row.get("cleared_by") or "").strip()
        if by and by not in ("ttl", "node_gone", "controller_restart"):
            return by
        return self._fault_actor(row)

    def _emit_log(self, conn, node_id: str, message: str, *, severity: str = "info",
                  actor: str = "controller") -> None:
        if self.operator_logs is None:
            return
        try:
            self.operator_logs.append(
                conn, self.project_id, node_id=node_id, severity=severity,
                actor=actor, message=message, remediation=None,
            )
        except Exception:  # noqa: BLE001 — the log stream must never stop the driver
            log.exception("failed to emit a demo-fault operator log line")

    def supersede_log(self, conn, node: dict, fault: dict) -> None:
        """Say once per injection that a node's own samples are being superseded."""
        if fault["injection_id"] in self._supersede_logged:
            return
        self._supersede_logged.add(fault["injection_id"])
        self._emit_log(
            conn, node["id"],
            f"node telemetry superseded by active fault injection "
            f"{fault['injection_id']} ({fault['scenario']}); the injected series is "
            f"authoritative until it is cleared",
            severity="warn",
            actor=self._fault_actor(fault),
        )
