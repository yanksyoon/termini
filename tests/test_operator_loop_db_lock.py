"""DEFECT 1 regression — the operator loop must NOT hold a SQLite write
transaction across the model call.

MEASURED DEFECT (live standing fleet, 2026-10-13). The loop held ONE transaction
for a whole poll, including the decision step, which is a MODEL call and is
minutes long on CPU (the contract's 20-sample evidence window measured ~414 s).
SQLite has a single writer, so for the entire duration of that model call every
OTHER write failed ``sqlite3.OperationalError: database is locked`` — telemetry
ingest returned HTTP 500, ``POST /intents`` returned HTTP 500, and a healthy fleet
rendered as all-offline. A ``busy_timeout`` cannot cover a 400 s hold; the
transaction must simply not span the call.

These tests are the honest proof: a concurrent write is attempted *while a model
call is provably in flight*. On the old shape it blocks/fails; on the split shape
(the detection transaction is committed before remediation, and the model call
runs with no transaction open) it succeeds promptly.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app import db, store, ulid  # noqa: E402
from app.operator import Thresholds, OperatorLoop  # noqa: E402
from app.operator import decision, playbook  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

RAM_TOTAL = 34_359_738_368
RAM_HIGH = 34_000_000_000  # >= 95% of total -> oom-guard-01 fires


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


class BlockingModelProvider:
    """A DecisionProvider whose ``decide`` blocks until the test releases it.

    Stands in for the real model call (llama.cpp on node-c): it makes the loop
    provably sit inside a decision for as long as the test needs, which is exactly
    the window in which a transaction spanning the call would lock the database.
    """

    def __init__(self, inner: "decision.DecisionProvider"):
        self.inner = inner
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def decide(self, event, context: "decision.DecisionContext") -> "decision.DecisionResult":
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=30):
            raise RuntimeError("test never released the model call")
        result = self.inner.decide(event, context)
        result.source = "model"  # take the model path (exercises fallback-state writes)
        return result


class OperatorLoopNoLongTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-op-lock-")
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
            operator_decision_mode="model",
            operator_decision_timeout_s=5.0,
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

        self.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))
        self.vm_id = ulid.new_ulid()
        self.inner = decision.DeterministicDecisionProvider(
            playbooks=self.playbooks, instance_names={self.vm_id: "agent-c1"})
        self.provider = BlockingModelProvider(self.inner)
        self.loop = OperatorLoop(
            self.cfg, self.project, Thresholds(), self.playbooks,
            instance_names={self.vm_id: "agent-c1"},
            default_instance_name="agent",
            lxd=LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01),
            provider=self.provider,
        )

    def _insert_oom(self):
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), [
                oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", self.vm_id)
                for i in range(3)
            ])

    def _concurrent_write(self):
        """A telemetry-ingest-shaped INSERT on its OWN connection.

        A short busy timeout (2 s) is deliberate: if the loop is holding the write
        lock this raises ``OperationalError: database is locked`` quickly instead
        of hanging for the global 15 s default. This is the same statement class
        the ingest path and ``POST /intents`` issue.
        """
        conn = sqlite3.connect(self.db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute(
                "INSERT INTO telemetry_batches (call_id, node_id, ts_received,"
                " schema_version, accepted_count, rejected_count, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ulid.new_ulid(), self.node_id, store.now_iso(), 1, 1, 0, "{}"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_concurrent_write_succeeds_while_a_model_call_is_in_flight(self):
        self._insert_oom()

        failures: list = []

        def run():
            try:
                self.loop.run_once()
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(self.provider.started.wait(10.0),
                        "the model call never started — test harness is wrong")
        self.assertEqual(failures, [])

        # We are provably INSIDE the model call now. A concurrent writer must get
        # through: nothing in the loop may hold the SQLite write lock.
        t0 = time.monotonic()
        lock_error = None
        try:
            self._concurrent_write()
        except sqlite3.OperationalError as exc:
            lock_error = exc
        elapsed = time.monotonic() - t0

        self.provider.release.set()
        worker.join(20.0)
        self.assertFalse(worker.is_alive(), "the model call never returned")
        self.assertEqual(failures, [])

        self.assertIsNone(
            lock_error,
            f"concurrent write failed while the model call was in flight: {lock_error!r}")
        self.assertLess(
            elapsed, 1.5,
            f"concurrent write took {elapsed:.2f}s during the model call — it was "
            f"serialized behind the loop's transaction")

        # The loop still produced its decision and audit once the call returned.
        with db.session(self.db_path) as conn:
            audits = opstore.list_audits(conn, self.node_id, 20)
            decisions = opstore.list_decisions(conn, 20)
        self.assertEqual(len(audits), 1)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(audits[0]["result"], "ok")

    def test_many_concurrent_writes_during_a_model_call(self):
        """Not just one: repeated writes for a sustained in-flight decision."""
        self._insert_oom()
        failures: list = []

        def run():
            try:
                self.loop.run_once()
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(self.provider.started.wait(10.0))

        errors: list = []
        deadline = time.monotonic() + 2.0
        n = 0
        while time.monotonic() < deadline:
            try:
                self._concurrent_write()
                n += 1
            except sqlite3.OperationalError as exc:
                errors.append(exc)
                break

        self.provider.release.set()
        worker.join(20.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(errors, [], f"concurrent write locked out: {errors!r}")
        self.assertGreater(n, 5, f"only {n} concurrent writes got through")


if __name__ == "__main__":
    unittest.main(verbosity=2)
