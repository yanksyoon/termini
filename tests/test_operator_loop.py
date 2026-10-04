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
from app.operator import playbook, store as opstore  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
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

    def test_idempotent_repeat_does_not_duplicate_command(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                      for i in range(3)])
        loop = self._make_loop()
        loop.run_once()
        loop.run_once()  # same sustained condition re-detected on the next poll

        self.assertEqual(len(self.lxd.restart_requests()), 1)  # still exactly one
        audits = self._audits()
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[1]["result"], "already_applied")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
