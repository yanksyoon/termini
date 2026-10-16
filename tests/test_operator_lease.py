"""Tests for the single-owner operator lease (migration 0007).

The lease is what makes remediation idempotent across operator instances: the
in-process `OperatorLoop` and the standalone CLI both acquire it before acting,
so only one operator may remediate per project at a time.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app import db, store, ulid  # noqa: E402
from app.operator import Thresholds, OperatorLoop  # noqa: E402
from app.operator import playbook, triggers  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402


def iso(sec: int) -> str:
    return f"2026-10-04T08:00:{sec:02d}.000Z"


class LeaseStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-lease-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)
        self.pid = ulid.new_ulid()

    def _acquire(self, owner, ttl=10.0, now_sec=0):
        with db.session(self.db_path) as conn:
            return opstore.acquire_lease(conn, self.pid, owner, iso(now_sec), ttl)

    def _lease(self):
        with db.session(self.db_path) as conn:
            return opstore.get_lease(conn, self.pid)

    def test_fresh_acquire_owns(self):
        self.assertTrue(self._acquire("loop:p1", now_sec=0))
        self.assertEqual(self._lease()["owner_id"], "loop:p1")

    def test_renew_extends_expiry_without_reacquire(self):
        self._acquire("loop:p1", ttl=10.0, now_sec=0)
        first = self._lease()
        self.assertTrue(self._acquire("loop:p1", ttl=10.0, now_sec=5))
        second = self._lease()
        self.assertEqual(second["owner_id"], "loop:p1")
        # renewing keeps the original acquisition time but extends expiry
        self.assertEqual(second["acquired_at"], first["acquired_at"])
        self.assertGreater(second["expires_at"], first["expires_at"])

    def test_standby_does_not_take_over(self):
        self._acquire("loop:p1", ttl=10.0, now_sec=0)
        self.assertFalse(self._acquire("cli:X", now_sec=5))
        self.assertEqual(self._lease()["owner_id"], "loop:p1")
        self.assertEqual(self._lease()["expires_at"], iso(10))

    def test_takeover_after_expiry(self):
        self._acquire("loop:p1", ttl=10.0, now_sec=0)
        self.assertTrue(self._acquire("cli:X", now_sec=11))
        lease = self._lease()
        self.assertEqual(lease["owner_id"], "cli:X")
        self.assertEqual(lease["acquired_at"], iso(11))

    def test_same_owner_reacquires_after_expiry(self):
        self._acquire("loop:p1", ttl=10.0, now_sec=0)
        self.assertTrue(self._acquire("loop:p1", now_sec=11))
        lease = self._lease()
        self.assertEqual(lease["owner_id"], "loop:p1")
        self.assertEqual(lease["acquired_at"], iso(11))


class LeaseLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-lease-loop-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        # The remediation target must exist on the node (see test_operator_loop).
        self.lxd.set_state("agent-c1", "Running")

        self.cfg = SimpleNamespace(
            db_path=self.db_path,
            operator_poll_interval_s=1.0,
            operator_cooldown_s=0.0,
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
                (self.project_id, "t", "h", 1, store.now_iso()),
            )
            self.node_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
                " hostname_hash, role, node_token_hash, hardware_json, os_json,"
                " lxd_json, role_hint, installer_version, status, last_seen_at,"
                " registered_at, source_ip)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.node_id, self.project_id, "reg-1", None, "hh", "compute",
                 "th", "{}", None, None, "auto", "0.1.0", "online",
                 store.now_iso(), store.now_iso(), None),
            )
        self.project = {"id": self.project_id, "project_token_hash": "h"}
        self.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))
        self.vm_id = "oom-race-vm"

    def _loop(self):
        return OperatorLoop(
            self.cfg, self.project, Thresholds(), self.playbooks,
            instance_names={self.vm_id: "agent-c1"},
            default_instance_name="agent",
            lxd=LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01),
        )

    def _insert_oom(self, base: int):
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), [
                {
                    "seq": base + i, "ts": f"2026-10-04T08:00:{i:02d}.000Z",
                    "ram_total_bytes": 34_359_738_368, "ram_used_bytes": 34_000_000_000,
                    "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                              "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                              "mount_ro": False}],
                    "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
                    "lxd_instances": [{"vm_id": self.vm_id, "state": "running",
                                       "ram_used_bytes": 10_000_000_000,
                                       "ram_limit_bytes": 16_000_000_000,
                                       "disk_used_bytes": 1_000_000_000}],
                }
                for i in range(3)
            ])

    def test_loop_owns_lease_and_second_operator_stands_by(self):
        self._insert_oom(100)
        loop = self._loop()
        produced = loop.run_once()
        self.assertEqual(len(produced), 1)  # the loop is owner and fires
        self.assertEqual(len(self.lxd.restart_requests()), 1)

        with db.session(self.db_path) as conn:
            # A second operator (e.g. the standalone CLI) is a standby while the
            # loop holds an unexpired lease.
            self.assertFalse(opstore.acquire_lease(
                conn, self.project_id, "cli:X", triggers.now_iso_ms(), 10.0))
            self.assertEqual(
                opstore.get_lease(conn, self.project_id)["owner_id"],
                f"loop:{self.project_id}",
            )

    def test_loop_identity_stable_across_restart(self):
        # A "restart" is a fresh loop over the same DB; its stable owner_id means
        # it re-acquires its own lease instead of waiting out the old TTL.
        loop1 = self._loop()
        loop2 = self._loop()
        self.assertEqual(loop1.owner_id, loop2.owner_id)
        self.assertEqual(loop2.owner_id, f"loop:{self.project_id}")

    def test_loop_standby_log_names_actual_owner(self):
        # A different operator (the CLI) holds an unexpired lease, so the loop
        # is a standby. Its standby log line must name that holder — not itself.
        with db.session(self.db_path) as conn:
            self.assertTrue(opstore.acquire_lease(
                conn, self.project_id, "cli:X", triggers.now_iso_ms(), 10.0))
        loop = self._loop()
        with self.assertLogs("termini.operator.loop", level="INFO") as cm:
            produced = loop.run_once()
        self.assertEqual(len(produced), 0)  # standby issues nothing
        joined = "\n".join(cm.output)
        self.assertIn("lease held by cli:X", joined)
        self.assertIn(f"self_id=loop:{self.project_id}", joined)


class CliStandbyLogTest(unittest.TestCase):
    """The standalone CLI's standby line must name the actual lease holder.

    Before the fix it interpolated the CLI's own `owner_id` into the line while
    claiming "another operator owns the lease" — attributing the standby to the
    wrong identity. The lease response's `owner_id` is the current holder.
    """

    def test_standby_log_names_actual_owner(self):
        from app.operator import cli as cli_mod

        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(s):
            clock["t"] += s

        def fake_http(method, url, body=None, token=None, timeout=10.0):
            # Another operator holds the lease; this CLI is a standby.
            return 200, {"owner": False, "owner_id": "loop:p1"}

        captured = []
        with mock.patch.object(cli_mod.time, "monotonic", fake_monotonic), \
             mock.patch.object(cli_mod.time, "sleep", fake_sleep), \
             mock.patch.object(cli_mod, "_http_json", fake_http):
            cli_mod.run_operator(
                controller_url="http://ctl", project_token="t", node_id="n1",
                lxd_url="http://lxd", thresholds=Thresholds(), playbooks={},
                instance_names={}, default_instance_name="agent",
                poll_interval_s=1.0, duration_s=2.0, dry_run=True,
                audit_out=None, limit=10, log=captured.append,
            )
        standby_lines = [m for m in captured if "standby" in m]
        self.assertEqual(len(standby_lines), 1)
        self.assertIn("lease held by loop:p1", standby_lines[0])
        self.assertIn("self_id=cli:", standby_lines[0])
        self.assertNotIn("owner_id=", standby_lines[0])

    def test_lease_error_fails_closed_not_owned(self):
        # A lease request that errors (non-200 / non-dict body) must NOT be
        # treated as ownership: the CLI stands by and issues no remediation.
        from app.operator import cli as cli_mod

        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(s):
            clock["t"] += s

        def fake_http(method, url, body=None, token=None, timeout=10.0):
            return 0, "URLError: connection refused"

        captured = []
        with mock.patch.object(cli_mod.time, "monotonic", fake_monotonic), \
             mock.patch.object(cli_mod.time, "sleep", fake_sleep), \
             mock.patch.object(cli_mod, "_http_json", fake_http):
            result = cli_mod.run_operator(
                controller_url="http://ctl", project_token="t", node_id="n1",
                lxd_url="http://lxd", thresholds=Thresholds(), playbooks={},
                instance_names={}, default_instance_name="agent",
                poll_interval_s=1.0, duration_s=2.0, dry_run=True,
                audit_out=None, limit=10, log=captured.append,
            )
        self.assertEqual(result["audits"], [])   # nothing remediated
        self.assertEqual(result["fired"], [])    # no trigger ever evaluated
        standby_lines = [m for m in captured if "standby" in m]
        self.assertEqual(len(standby_lines), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
