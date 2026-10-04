"""Integration tests for the operator loop: a real (stub) LXD REST call is issued
from a real SQLite-backed decision/audit trail, with idempotency and dry-run.

Runs the loop's `run_once()` in-process against a temp DB and a stub LXD server.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app import db, store, ulid  # noqa: E402
from app.operator import Thresholds, OperatorLoop  # noqa: E402
from app.operator import detect, playbook, triggers  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
from app.operator.models import (  # noqa: E402
    ExecResult,
    HealthEvent,
    RemediationAction,
)
from stub_lxd import StubLxd  # noqa: E402

RAM_TOTAL = 34_359_738_368
RAM_HIGH = 34_000_000_000


def oom_sample(seq: int, ts: str, vm_id: str) -> dict:
    return {
        "seq": seq, "ts": ts,
        "ram_total_bytes": RAM_TOTAL,
        "ram_used_bytes": RAM_HIGH,
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                  "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
        "lxd_instances": [
            {"vm_id": vm_id, "state": "running", "ram_used_bytes": 10_000_000_000,
             "ram_limit_bytes": 16_000_000_000, "disk_used_bytes": 1_000_000_000},
        ],
    }


def disk_sample(seq: int, ts: str, free: int, vm_id: str | None = None) -> dict:
    s = {
        "seq": seq, "ts": ts,
        "ram_total_bytes": RAM_TOTAL, "ram_used_bytes": RAM_TOTAL // 2,
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": free, "write_latency_ms": 3.0, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
    }
    if vm_id:
        s["lxd_instances"] = [
            {"vm_id": vm_id, "state": "running", "ram_used_bytes": 5_000_000_000,
             "ram_limit_bytes": 16_000_000_000, "disk_used_bytes": 4_000_000_000},
        ]
    return s


class LoopIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-op-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)

        self.cfg = SimpleNamespace(
            db_path=self.db_path,
            operator_poll_interval_s=1.0,
            operator_cooldown_s=0.0,       # disabled so idempotency is exercised
            operator_window=60,
            lxd_base_url=self.lxd.base_url,
            operator_dry_run=False,
            lxd_request_timeout_s=5.0,
            lxd_retry_max=2,
            lxd_retry_backoff_s=0.01,
            lxd_client_cert=None,
            lxd_client_key=None,
        )

        with db.session(self.db_path) as conn:
            self.project_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.project_id, "test", "hash", 1, store.now_iso()),
            )
            self.project = {"id": self.project_id, "project_token_hash": "hash"}
            self.node_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
                " hostname_hash, role, node_token_hash, hardware_json, os_json,"
                " lxd_json, role_hint, installer_version, status, last_seen_at,"
                " registered_at, source_ip)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.node_id, self.project_id, "reg-1", None,
                 "5f2a9c1e7b4d4a1f", "compute", "tokhash", "{}", None, None,
                 "auto", "0.1.0", "online", store.now_iso(), store.now_iso(), None),
            )

        self.vm_id = ulid.new_ulid()
        self.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))

    def _make_loop(self, *, dry_run=False, cooldown=0.0):
        cfg = SimpleNamespace(**{**self.cfg.__dict__,
                                 "operator_dry_run": dry_run,
                                 "operator_cooldown_s": cooldown})
        lxd = LxdExecutor(self.lxd.base_url, dry_run=dry_run, retry_max=2, backoff_s=0.01)
        loop = OperatorLoop(cfg, self.project, Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd)
        return loop

    def _insert(self, samples):
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), samples)

    def _audits(self):
        with db.session(self.db_path) as conn:
            return opstore.list_audits(conn, self.node_id, 20)

    def test_oom_issues_real_lxd_restart_and_audits(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop()
        produced = loop.run_once()

        self.assertEqual(len(produced), 1)
        restarts = self.lxd.restart_requests()
        self.assertEqual(len(restarts), 1)
        _, path, body = restarts[0]
        self.assertEqual(path, "/1.0/instances/agent-c1/state")
        self.assertEqual(body["action"], "restart")

        audits = self._audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["result"], "ok")
        self.assertEqual(audits[0]["action_kind"], "instance.restart")
        self.assertEqual(audits[0]["node_id"], self.node_id)
        self.assertEqual(audits[0]["lxd_path"], "/1.0/instances/agent-c1/state")

        with db.session(self.db_path) as conn:
            events = opstore.list_health_events(conn, self.node_id, 20)
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["rule_id"], "oom-guard-01")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action_kind"], "instance.restart")

    def test_sustained_breach_fires_once_latched(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop()
        loop.run_once()
        produced = loop.run_once()  # same sustained condition on the next poll

        # Hysteresis latch: the trigger stays "fired" until the metric recovers,
        # so the sustained breach produces exactly one command and one audit.
        self.assertEqual(len(produced), 0)
        self.assertEqual(len(self.lxd.restart_requests()), 1)  # still exactly one
        self.assertEqual(len(self._audits()), 1)

    def test_refire_after_recovery_is_a_new_episode(self):
        # Episode 1: sustained OOM fires and is remediated.
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop()
        loop.run_once()
        self.assertEqual(len(self.lxd.restart_requests()), 1)

        # Recovery: memory returns to a healthy band long enough to re-arm the latch.
        self._recover_ram(loop)
        self.assertEqual(len(self.lxd.restart_requests()), 1)  # no re-fire while recovering

        # Episode 2: the fault re-occurs and must fire again (a new episode).
        self._insert([oom_sample(600 + i, f"2026-10-04T08:10:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        produced = loop.run_once()
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.lxd.restart_requests()), 2)  # a genuine new command

    def _recover_ram(self, loop):
        # To re-arm, the last `oom_clear_consecutive` samples must be healthy.
        # Insert healthy samples and poll so the latch clears.
        for i in range(6):
            self._insert([{
                "seq": 500 + i,
                "ts": f"2026-10-04T08:06:{i:02d}Z",
                "ram_total_bytes": RAM_TOTAL,
                "ram_used_bytes": RAM_TOTAL // 4,  # ~25%, healthy
                "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                          "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                          "mount_ro": False}],
                "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
            }])
            loop.run_once()

    def test_disk_fail_mount_ro_fires(self):
        # Scenario B signature: EXT4 remount read-only -> disk-fail-01.
        for i in range(1):
            self._insert([disk_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z",
                                      200_000_000_000, self.vm_id)
                          | {"disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                                       "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                                       "mount_ro": True}]}])
        loop = self._make_loop()
        produced = loop.run_once()
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["rule_id"], "disk-fail-01")
        stops = [(m, p, b) for (m, p, b) in self.lxd.requests
                 if m == "PUT" and p.endswith("/state") and isinstance(b, dict)
                 and b.get("action") == "stop"]
        self.assertEqual(len(stops), 1)

    def test_persisted_latch_survives_restart(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop1 = self._make_loop()
        loop1.run_once()
        self.assertEqual(len(self.lxd.restart_requests()), 1)

        # Simulate a controller restart: a brand-new loop over the same DB. The
        # latch must reload from storage and NOT re-fire the already-remediated fault.
        loop2 = self._make_loop()
        produced = loop2.run_once()
        self.assertEqual(len(produced), 0)
        self.assertEqual(len(self.lxd.restart_requests()), 1)

    def test_thresholds_are_seeded_and_persisted(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop()
        loop.run_once()

        with db.session(self.db_path) as conn:
            configs = opstore.load_trigger_configs(conn)
        # Every frozen rule has a persisted config subset after the first poll.
        self.assertEqual(set(configs), {"oom-guard-01", "disk-fail-01",
                                        "disk-degrade-01", "latency-01"})
        # And a fresh loop over the same DB reconstructs equivalent thresholds.
        merged = triggers.merge_config(configs)
        self.assertEqual(merged.oom_ram_pct, detect.Thresholds().oom_ram_pct)
        self.assertEqual(merged.disk_fail_consecutive, 1)

    def test_dry_run_logs_without_executing(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop(dry_run=True)
        produced = loop.run_once()

        self.assertEqual(len(produced), 1)
        self.assertEqual(self.lxd.requests, [])  # nothing actually sent
        audits = self._audits()
        self.assertEqual(audits[0]["result"], "dry_run")
        self.assertEqual(audits[0]["dry_run"], 1)
        self.assertEqual(audits[0]["lxd_path"], "/1.0/instances/agent-c1/state")

    def test_disk_degrade_drains_workload(self):
        self._insert([disk_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z",
                                  5_000_000_000, self.vm_id)
                      for i in range(6)])
        loop = self._make_loop()
        produced = loop.run_once()

        self.assertEqual(len(produced), 1)
        stops = [(m, p, b) for (m, p, b) in self.lxd.requests
                 if m == "PUT" and p.endswith("/state") and isinstance(b, dict)
                 and b.get("action") == "stop"]
        self.assertEqual(len(stops), 1)
        _, path, body = stops[0]
        self.assertEqual(path, "/1.0/instances/agent-c1/state")
        self.assertEqual(body["action"], "stop")
        audits = self._audits()
        self.assertEqual(audits[0]["action_kind"], "instance.stop")
        self.assertEqual(audits[0]["result"], "ok")

    def test_cooldown_suppresses_refire(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop(cooldown=3600.0)
        loop.run_once()
        produced = loop.run_once()

        self.assertEqual(len(produced), 0)  # second poll suppressed by cooldown
        self.assertEqual(len(self.lxd.restart_requests()), 1)

    # ------------------------------------------------------------------ D2
    def _insert_audit_with_key(self, key, result_str="ok", executed_at=None):
        """Persist a decision + audit with a fixed idempotency key, matching the
        production loop's write path (ms-precision `now_iso`)."""
        now = triggers.now_iso_ms()
        with db.session(self.db_path) as conn:
            event = HealthEvent(
                event_id=ulid.new_ulid(), node_id=self.node_id,
                rule_id="oom-guard-01", type="oom", metric="ram_used_bytes",
                value=96.0, threshold=">= 95% of ram_total_bytes", fired_at=now,
            )
            opstore.insert_health_event(conn, event)
            decision_id = ulid.new_ulid()
            action = RemediationAction(
                kind="instance.restart", params={"instance_name": "agent-c1"},
                reason="oom", playbook="oom-guard-01", rule_id="oom-guard-01",
            )
            opstore.insert_decision(conn, decision_id, event, action, now)
            audit_id = ulid.new_ulid()
            result = ExecResult(result=result_str, method="PUT",
                                path="/1.0/instances/agent-c1/state",
                                body='{"action":"restart"}', status=200)
            opstore.insert_audit(conn, audit_id, decision_id, event, action,
                                 result, key, now, dry_run=False)
            if executed_at is not None:
                conn.execute("UPDATE remediation_audits SET executed_at = ?",
                             (executed_at,))

    def test_idempotency_key_time_bounded(self):
        key = f"{self.node_id}:oom-guard-01:instance.restart:agent-c1:1"
        self._insert_audit_with_key(key, "ok")

        with db.session(self.db_path) as conn:
            # Within the window the prior ok audit is a dedup source.
            self.assertIsNotNone(
                opstore.audit_by_idempotency_key(conn, key, within_s=3600))
            # Age it past the window.
            conn.execute("UPDATE remediation_audits SET executed_at = ?",
                         ("2000-01-01T00:00:00.000Z",))

        with db.session(self.db_path) as conn:
            # Expired -> no dedup source (must re-fire).
            self.assertIsNone(
                opstore.audit_by_idempotency_key(conn, key, within_s=3600))
            # Explicitly unbounded -> still returned regardless of age.
            self.assertIsNotNone(
                opstore.audit_by_idempotency_key(conn, key, within_s=None))

    def test_expired_idempotency_audit_refires(self):
        # Episode 1: sustained OOM fires and is remediated (one real command).
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop()
        loop.run_once()
        self.assertEqual(len(self.lxd.restart_requests()), 1)

        # Age the audit past the 24h idempotency window and drop the persisted
        # trigger latch so a later fault produces the SAME episode key (a
        # lost/reset-latch path). An unbounded lookup would treat the stale "ok"
        # audit as already_applied and silently suppress this re-remediation.
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE remediation_audits SET executed_at = ?",
                         ("2000-01-01T00:00:00.000Z",))
            conn.execute("DELETE FROM trigger_state")

        # A new OOM episode (same key after the latch reset) must issue a real
        # LXD command.
        self._insert([oom_sample(600 + i, f"2026-10-04T08:10:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        produced = loop.run_once()
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.lxd.restart_requests()), 2)  # a real new command


if __name__ == "__main__":
    unittest.main(verbosity=2)
