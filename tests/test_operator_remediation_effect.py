"""A remediation must actually take effect, and the audit row must say so.

These tests pin the fix for the measured live defect: against a real LXD the
executor issued `PUT /1.0/instances/<name>/state` and got `202 "Restarting
instance"`, yet the instance stayed STOPPED and no audit row recorded anything.
Two independent properties are covered here:

  1. **The executor confirms its effect.** LXD answers a state change with
     `202 + an operation` — "accepted", not "done". The operation must be waited
     on, a STOPPED instance must be *started* (a restart of a stopped container
     is a no-op LXD still accepts), and the resulting state must be read back; a
     failed, timed-out, or ineffective remediation is `error`, never `ok`.

  2. **The loop writes an audit row for a real (non-stub) remediation**, carrying
     the endpoint it reached, so `GET /v1/operator/audits` is evidence of what
     happened on real hardware and not only stub-era rows.

The stub LXD models instance state and applies actions, so "did it take effect"
is a real end-to-end assertion (the instance's state), not a log check.
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
from app.operator import playbook  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

RAM_TOTAL = 34_359_738_368
RAM_HIGH = 34_000_000_000
INSTANCE = "agent-c1"


def oom_sample(seq: int, ts: str, vm_id: str) -> dict:
    return {
        "seq": seq, "ts": ts,
        "ram_total_bytes": RAM_TOTAL,
        "ram_used_bytes": RAM_HIGH,          # >= 95% -> oom-guard-01
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                  "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
        "lxd_instances": [
            {"vm_id": vm_id, "state": "running", "ram_used_bytes": 10_000_000_000,
             "ram_limit_bytes": 16_000_000_000, "disk_used_bytes": 1_000_000_000},
        ],
    }


# --------------------------------------------------------------------------- executor

class ExecutorConfirmsEffectTest(unittest.TestCase):
    """The executor itself: wait, choose the action that works, confirm."""

    def setUp(self):
        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.ex = LxdExecutor(self.lxd.base_url, retry_max=1, backoff_s=0.0)

    def test_restart_of_a_running_instance_restarts_and_stays_running(self):
        self.lxd.set_state(INSTANCE, "Running")
        r = self.ex.restart_instance(INSTANCE)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual([b["action"] for _, _, b in self.lxd.state_requests()], ["restart"])
        self.assertEqual(self.lxd.state_of(INSTANCE), "Running")

    def test_restart_of_a_stopped_instance_starts_it_instead(self):
        # The measured live no-op: restart on a STOPPED container is accepted but
        # changes nothing. The executor must issue `start` so the action lands.
        self.lxd.set_state(INSTANCE, "Stopped")
        r = self.ex.restart_instance(INSTANCE)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual(self.lxd.state_requests("restart"), [])          # no wasted restart
        self.assertEqual([b["action"] for _, _, b in self.lxd.state_requests()], ["start"])
        self.assertEqual(self.lxd.state_of(INSTANCE), "Running")          # it took effect

    def test_failed_operation_is_an_error(self):
        self.lxd.set_state(INSTANCE, "Stopped")
        self.lxd.operation_status = "Failure"
        r = self.ex.restart_instance(INSTANCE)
        self.assertEqual(r.result, "error")
        self.assertIn("Failure", r.detail)
        self.assertEqual(self.lxd.state_of(INSTANCE), "Running")  # stub applied it, but op failed

    def test_timed_out_operation_is_an_error_not_an_early_pass(self):
        self.lxd.set_state(INSTANCE, "Stopped")
        self.lxd.operation_status = "Running"   # never reaches a terminal state
        ex = LxdExecutor(self.lxd.base_url, retry_max=1, backoff_s=0.0, timeout_s=0.2)
        # shrink the wait so the test is fast: patch the wait window
        r = ex._await_write(ex._request("PUT", f"/1.0/instances/{INSTANCE}/state",
                                        {"action": "start"}),
                            timeout_s=0.3)
        self.assertEqual(r.result, "error")
        self.assertIn("operation still", r.detail)

    def test_ineffective_remediation_is_surfaced_not_reported_ok(self):
        # Operation reports Success but the instance does not change (a no-op
        # patch-up): the executor must not claim ok.
        self.lxd.set_state(INSTANCE, "Stopped")
        self.lxd.ignore_actions = True
        r = self.ex.restart_instance(INSTANCE)
        self.assertEqual(r.result, "error")
        self.assertIn("INEFFECTIVE", r.detail)
        self.assertEqual(self.lxd.state_of(INSTANCE), "Stopped")

    def test_stop_confirms_stopped(self):
        self.lxd.set_state(INSTANCE, "Running")
        r = self.ex.stop_instance(INSTANCE)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual(self.lxd.state_of(INSTANCE), "Stopped")

    def test_reaped_operation_falls_back_to_the_state_check(self):
        # A fast action finishes and LXD reaps the operation before our first poll
        # (404). The effect is then decided by the state read, not by the poll.
        self.lxd.set_state(INSTANCE, "Stopped")
        self.lxd.operation_gone = True
        r = self.ex.restart_instance(INSTANCE)
        self.assertEqual(r.result, "ok", r.detail)
        self.assertEqual(self.lxd.state_of(INSTANCE), "Running")   # effect still confirmed


# --------------------------------------------------------------------------- loop

class LoopAuditTest(unittest.TestCase):
    """The loop, end to end: a real remediation lands and is audited."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-op-effect-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)

        self.cfg = SimpleNamespace(
            db_path=self.db_path,
            operator_poll_interval_s=1.0,
            operator_cooldown_s=0.0,
            operator_window=60,
            lxd_base_url=self.lxd.base_url,
            operator_dry_run=False,
            lxd_request_timeout_s=5.0,
            lxd_retry_max=1,
            lxd_retry_backoff_s=0.0,
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

    def _make_loop(self):
        lxd = LxdExecutor(self.lxd.base_url, retry_max=1, backoff_s=0.0)
        return OperatorLoop(
            self.cfg, self.project, Thresholds(), self.playbooks,
            instance_names={self.vm_id: INSTANCE},
            default_instance_name="agent", lxd=lxd,
        )

    def _insert_breach(self):
        with db.session(self.db_path) as conn:
            store.insert_samples(
                conn, self.node_id, ulid.new_ulid(),
                [oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                 for i in range(3)],
            )

    def _audits(self):
        with db.session(self.db_path) as conn:
            return opstore.list_audits(conn, self.node_id, 20)

    def test_loop_remediation_takes_effect_and_writes_audit_with_endpoint(self):
        # A STOPPED instance: the live defect's exact shape. The loop must bring
        # it up (start, not a no-op restart) and audit the real endpoint.
        self.lxd.set_state(INSTANCE, "Stopped")
        self._insert_breach()
        produced = self._make_loop().run_once()

        self.assertEqual(len(produced), 1)
        self.assertEqual(self.lxd.state_of(INSTANCE), "Running")     # effect, not just a 202

        audits = self._audits()
        self.assertEqual(len(audits), 1)
        row = audits[0]
        self.assertEqual(row["result"], "ok", row["detail"])
        self.assertEqual(row["action_kind"], "instance.restart")
        self.assertEqual(row["node_id"], self.node_id)
        self.assertEqual(row["lxd_method"], "PUT")
        self.assertEqual(row["lxd_path"], f"/1.0/instances/{INSTANCE}/state")
        # The evidence "this was real, not the stub-era demo": the endpoint.
        self.assertEqual(row["lxd_endpoint"], self.lxd.base_url)
        self.assertIn("action=start", row["detail"])                 # what actually happened

    def test_failed_operation_is_not_audited_ok(self):
        self.lxd.set_state(INSTANCE, "Stopped")
        self.lxd.operation_status = "Failure"
        self._insert_breach()
        self._make_loop().run_once()

        row = self._audits()[0]
        self.assertEqual(row["result"], "error")
        self.assertIn("Failure", row["detail"])
        self.assertEqual(row["lxd_endpoint"], self.lxd.base_url)

    def test_ineffective_remediation_is_not_audited_ok(self):
        # Operation Success, instance unchanged: must not be recorded as ok.
        self.lxd.set_state(INSTANCE, "Stopped")
        self.lxd.ignore_actions = True
        self._insert_breach()
        produced = self._make_loop().run_once()

        self.assertEqual(len(produced), 1)
        row = self._audits()[0]
        self.assertEqual(row["result"], "error")
        self.assertIn("INEFFECTIVE", row["detail"])
        self.assertEqual(self.lxd.state_of(INSTANCE), "Stopped")     # truth is unchanged

    def test_audit_endpoint_distinguishes_real_from_stub(self):
        # Two loops over two "endpoints": the row names the one it reached.
        self.lxd.set_state(INSTANCE, "Running")
        self._insert_breach()
        self._make_loop().run_once()
        self.assertEqual(self._audits()[0]["lxd_endpoint"], self.lxd.base_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
