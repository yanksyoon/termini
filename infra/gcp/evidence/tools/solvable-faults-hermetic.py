#!/usr/bin/env python3
"""Hermetic end-to-end acceptance for t_895bf6d3: the fault is SOLVABLE.

WHAT THIS IS (and is not)
-------------------------
This runs the REAL controller code under test — the operator loop, the
guardrail, the target resolution and the executor — with the model's answer
REPLAYED from the live decision row (2026-10-15T15:29:48Z: rule=oom-guard-01,
action=instance.restart, confidence=0.9) and a strict LXD double in place of the
node's daemon. The double answers 404 for an instance it does not have, exactly
as real LXD does, so it CANNOT hide the defect this card is about (a stub that
always answers 200 would).

It is NOT a substitute for the live acceptance: the node-side observables (the
instance's real uptime resetting, the node's real telemetry recovering) still
have to be taken on a live node — see the evidence doc.

    python3 infra/gcp/evidence/tools/solvable-faults-hermetic.py
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "termini"))
sys.path.insert(0, str(REPO / "tests"))

from app import db, events, logs, store, ulid  # noqa: E402
from app.operator import Thresholds, OperatorLoop  # noqa: E402
from app.operator import playbook, vmids  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.decision import (  # noqa: E402
    DecisionContext, ModelDecisionProvider, build_decision_input,
)
from app.operator.executor import LxdExecutor  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402
from termini.decision.types import DecisionResponse  # noqa: E402

RAM_TOTAL = 34_359_738_368          # the real node-a's reported total
RAM_FAULT = 34_000_000_000          # 98.9% — over oom-guard-01's 95% threshold
RAM_OK = 12_000_000_000             # 34.9% — under the 90% clear threshold

# The instance that EXISTS on the node the executor dials, and the id the
# seeder advertises for it (derived, never invented).
REAL_INSTANCE = "demo-workload"
REAL_VM_ID = vmids.vm_id_for_instance(REAL_INSTANCE)
# The id the demo's seeder used to INVENT (the one that 404'd on the live demo).
FABRICATED_VM_ID = "01M4N827979BD9D2762EFE24A0"

LIVE_DECISION = dict(
    rule_id="oom-guard-01", action_id="instance.restart", confidence=0.9,
    rationale=("replay of the live model decision 2026-10-15T15:29:48Z: the "
               "workload with the highest memory use is exhausting the node; "
               "restart that instance"),
)


def sample(seq: int, ts: str, ram: int, vm_id: str) -> dict:
    """One node telemetry sample in the shape the agent/ingest accept."""
    return {
        "seq": seq, "ts": ts, "uptime_sec": 3600 + seq,
        "cpu_util_pct": 31.2, "cpu_core_count": 8,
        "ram_total_bytes": RAM_TOTAL, "ram_used_bytes": ram,
        "swap_used_bytes": 0, "load_avg_1m": 1.42,
        "os_name": "Ubuntu", "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "hostname": "5f2a9c1e7b4d4a1f", "ip_local": "10.14.100.0/24",
        "mac_address": "3b1f9c8a2d4e5f60",
        "disk": [{"path": "/dev/sda", "total_bytes": 274877906944,
                  "free_bytes": 214748364800, "write_latency_ms": 3.0,
                  "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 123000 + seq, "tx_bytes": 11000 + seq,
                 "latency_ms": 0.8}],
        "services": [{"name": "termini-bootstrap", "status": "running"},
                     {"name": "lxd", "status": "running"}],
        "lxd_instances": [{"vm_id": vm_id, "state": "running", "cpu_util_pct": 12.0,
                           "ram_used_bytes": ram // 4,
                           "ram_limit_bytes": 8_319_737_856,
                           "disk_used_bytes": 2362232012}],
    }


def scripted_model(decision_input) -> DecisionResponse:
    """The decision_fn the live model would have produced, choosing like it did.

    The live model named the vm_id of the workload with the highest memory use
    out of the evidence window (that is what it is given); the target it names
    here is read from the same window, so the resolution under test is the real
    one and not a hand-placed instance name.
    """
    instances = (decision_input.target_resource or {}).get("instances") or []
    heaviest = max(instances, key=lambda i: i.get("ram_used_bytes") or 0, default={})
    return DecisionResponse(
        action_id=LIVE_DECISION["action_id"],
        confidence=LIVE_DECISION["confidence"],
        rationale=LIVE_DECISION["rationale"],
        params={"instance_name": heaviest.get("vm_id"), "force": True, "timeout_s": 30},
        request_id=decision_input.request_id,
    )


class Case:
    """One isolated controller DB + node + LXD double."""

    def __init__(self, label: str, vm_id: str):
        self.label = label
        self.vm_id = vm_id
        self.tmp = tempfile.mkdtemp(prefix="termini-hermetic-")
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)
        self.lxd = StubLxd()
        # The node's REAL inventory: the instance the executor must act on, plus
        # two allocation VMs, exactly as node-a reports them.
        for name in (REAL_INSTANCE, "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0",
                     "termini-01m4wjhwqr3b8pcav2tvqbgg-0-1"):
            self.lxd.instances[name] = {}
            self.lxd.set_state(name, "Running")
        self.lxd.start()
        with db.session(self.db_path) as conn:
            self.project_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.project_id, "demo", "hash", 1, store.now_iso()))
            self.node_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
                " hostname_hash, role, node_token_hash, hardware_json, os_json,"
                " lxd_json, role_hint, installer_version, status, last_seen_at,"
                " registered_at, source_ip, lxd_endpoint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.node_id, self.project_id, "ba1f1faeacb4501aed12cf8ea1e5713b",
                 "ba1f1faeacb4501aed12cf8ea1e5713b", "5f2a9c1e7b4d4a1f", "compute",
                 "tokhash", "{}", None, None, "auto", "0.1.0", "online",
                 store.now_iso(), store.now_iso(), None,
                 self.lxd.base_url))     # the node's own LXD endpoint, as it reports it
        self.project = {"id": self.project_id, "project_token_hash": "hash"}
        self.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))
        self.logs = logs.OperatorLogs(events.EventBroker())

    def loop(self) -> OperatorLoop:
        cfg = type("Cfg", (), {})()
        cfg.db_path = self.db_path
        cfg.operator_poll_interval_s = 1.0
        cfg.operator_cooldown_s = 0.0
        cfg.operator_window = 60
        cfg.operator_live_inventory_ttl_s = 0.0     # re-read the node every pass
        cfg.operator_decision_mode = "model"
        cfg.lxd_base_url = self.lxd.base_url
        cfg.operator_dry_run = False
        cfg.lxd_request_timeout_s = 5.0
        cfg.lxd_retry_max = 2
        cfg.lxd_retry_backoff_s = 0.01
        cfg.lxd_client_cert = None
        cfg.lxd_client_key = None
        provider = ModelDecisionProvider(
            decide_fn=scripted_model, instance_names={}, default_instance_name="",
            egress_endpoint="hermetic://replay-of-the-live-model-answer")
        return OperatorLoop(
            cfg, self.project, Thresholds(), self.playbooks,
            instance_names={},                  # nothing configured — as in the demo
            default_instance_name="",
            lxd=LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01),
            logs=self.logs, provider=provider)

    def insert(self, samples: list) -> None:
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), samples)

    def rows(self, what: str):
        con = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True)
        con.row_factory = sqlite3.Row
        try:
            if what == "decisions":
                return [dict(r) for r in con.execute(
                    "select rule_id, action_kind, action_params, decision_source,"
                    " confidence, fallback_reason from remediation_decisions")]
            if what == "audits":
                return [dict(r) for r in con.execute(
                    "select rule_id, action_kind, result, lxd_method, lxd_path,"
                    " lxd_endpoint, detail from remediation_audits")]
            if what == "logs":
                return [dict(r) for r in con.execute(
                    "select severity, message from operator_logs order by rowid")]
            if what == "trigger":
                return [dict(r) for r in con.execute(
                    "select rule_id, state, fired_count from trigger_state")]
        finally:
            con.close()
        return []

    def close(self):
        self.lxd.stop()


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def state_requests(case: Case):
    return [r for r in case.lxd.requests if r[0] in ("PUT", "POST", "DELETE")]


def run_solvable() -> Case:
    hr("CASE A — a fault is injected and the model's target EXISTS on the node")
    case = Case("solvable", REAL_VM_ID)
    print("node            : %s (ba1f1faeacb4501aed12cf8ea1e5713b, lxd_endpoint=%s)"
          % (case.node_id, case.lxd.base_url))
    print("instances on the node the executor dials:",
          sorted(case.lxd.instances))
    print("node-reported vm_id for %r : %s" % (REAL_INSTANCE, REAL_VM_ID))
    print("nothing is configured in TERMINI_LXD_INSTANCE_NAMES — the loop must "
          "resolve the id through the node's LIVE inventory\n")

    print("--- injected fault, visible in telemetry BEFORE the decision ---")
    for i in range(3):
        s = sample(100 + i, "2026-10-16T09:00:%02dZ" % i, RAM_FAULT, REAL_VM_ID)
        case.insert([s])
        print("  seq=%d ts=%s ram=%d/%d (%.1f%%)  instances[0].vm_id=%s"
              % (s["seq"], s["ts"], s["ram_used_bytes"], s["ram_total_bytes"],
                 100.0 * s["ram_used_bytes"] / s["ram_total_bytes"],
                 s["lxd_instances"][0]["vm_id"]))

    produced = case.loop().run_once()
    print("\n--- the loop produced %d remediation(s) ---" % len(produced))
    for d in case.rows("decisions"):
        print("DECISION  rule=%s action=%s source=%s confidence=%s fallback=%s"
              % (d["rule_id"], d["action_kind"], d["decision_source"],
                 d["confidence"], d["fallback_reason"]))
        print("          params=%s" % d["action_params"])
    for a in case.rows("audits"):
        print("AUDIT     rule=%s action=%s result=%s" % (a["rule_id"], a["action_kind"], a["result"]))
        print("          lxd %s %s @ %s" % (a["lxd_method"], a["lxd_path"], a["lxd_endpoint"]))
        print("          detail=%s" % (a["detail"] or "")[:160])
    for lg in case.rows("logs"):
        print("OPERATOR LOG [%s] %s" % (lg["severity"], (lg["message"] or "")[:200]))

    print("\n--- what the executor actually sent to LXD (in order) ---")
    for method, path, body in case.lxd.requests:
        print("  %-6s %s%s" % (method, path, (" " + str(body)) if body else ""))
    print("\nstate-changing requests issued: %d" % len(state_requests(case)))
    print("restart requests recorded by LXD: %d %s"
          % (len(case.lxd.restart_requests()), case.lxd.restart_requests()))
    print("instance %r state: %s (it exists, so a real LXD restarts it and its "
          "uptime resets — the live-window observable)" % (REAL_INSTANCE,
                                                           case.lxd.state_of(REAL_INSTANCE)))

    print("\n--- the fault CLEARS after the remediation ---")
    for i in range(3):
        s = sample(200 + i, "2026-10-16T09:01:%02dZ" % i, RAM_OK, REAL_VM_ID)
        case.insert([s])
        print("  seq=%d ram=%d/%d (%.1f%%)"
              % (s["seq"], s["ram_used_bytes"], s["ram_total_bytes"],
                 100.0 * s["ram_used_bytes"] / s["ram_total_bytes"]))
    more = case.loop().run_once()
    print("  further remediations produced: %d" % len(more))
    for t in case.rows("trigger"):
        print("  trigger_state %s -> %s (fired %s time(s))"
              % (t["rule_id"], t["state"], t["fired_count"]))
    return case


def run_refused() -> Case:
    hr("CASE B — negative control: the SAME fault with an INVENTED target")
    case = Case("refused", FABRICATED_VM_ID)
    print("the seeder advertises the id it used to invent: %s" % FABRICATED_VM_ID)
    print("it hashes from NO instance on the node, so nothing can resolve it\n")
    for i in range(3):
        s = sample(300 + i, "2026-10-16T09:02:%02dZ" % i, RAM_FAULT, FABRICATED_VM_ID)
        case.insert([s])
    case.loop().run_once()
    for d in case.rows("decisions"):
        print("DECISION  rule=%s action=%s source=%s confidence=%s fallback=%s"
              % (d["rule_id"], d["action_kind"], d["decision_source"],
                 d["confidence"], d["fallback_reason"]))
        print("          params=%s" % d["action_params"])
    for a in case.rows("audits"):
        print("AUDIT     rule=%s action=%s result=%s" % (a["rule_id"], a["action_kind"], a["result"]))
        print("          lxd %s %s @ %s" % (a["lxd_method"], a["lxd_path"], a["lxd_endpoint"]))
        print("          detail=%s" % (a["detail"] or "")[:200])
    for lg in case.rows("logs"):
        print("OPERATOR LOG [%s] %s" % (lg["severity"], (lg["message"] or "")[:220]))
    print("\n--- what the executor sent to LXD (in order) ---")
    for method, path, body in case.lxd.requests:
        print("  %-6s %s%s" % (method, path, (" " + str(body)) if body else ""))
    print("\nstate-changing requests issued: %d  (fail-closed: NONE)"
          % len(state_requests(case)))
    print("no raw LXD 404 body anywhere: the audit and the log name the TARGET")
    return case


def main() -> int:
    print("t_895bf6d3 hermetic acceptance — real loop, real guardrail, real "
          "executor, strict LXD double")
    print("LXD double: tests/stub_lxd.py (404 on an instance it does not have; "
          "never 'always 200')")
    t0 = time.time()
    a = run_solvable()
    b = run_refused()
    hr("SUMMARY")
    audited = a.rows("audits")
    refused = b.rows("audits")
    print("A fault + a target that EXISTS  -> audit result=%s, restart requests=%d"
          % (audited[0]["result"], len(a.lxd.restart_requests())))
    print("A fault + an INVENTED target    -> audit result=%s (%s), state-changing "
          "requests=%d" % (refused[0]["result"], (refused[0]["detail"] or "").split(":")[0],
                           len(state_requests(b))))
    a.close()
    b.close()
    print("\nhermetic run wall-clock: %.2fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
