#!/usr/bin/env python3
"""The remediation target must EXIST on the node — fail-closed, never a 404.

Measured defect this file pins down (live demo, 2026-10-15T15:29:48Z):

    decisions row: rule=oom-guard-01 action=instance.restart decision_source=model
                   confidence=0.9 fallback_reason=None
    audit row:     result=error detail="HTTP 404: Failed to fetch instance
                   \\"01M4N827979BD9D2762EFE24A0\\" ... Instance not found"

The model diagnosed the fault correctly, the guardrail resolved its target to an
id the demo's seeder had invented, and the executor PUT that id at a real node
where no such instance exists. On camera that reads as the AI failing.

The rule this file enforces: a state change is issued ONLY when LXD answers its
existence probe for the target (`GET /1.0/instances/<name>`); otherwise the
executor REFUSES (`ExecResult.reason == "target_missing"`), issues no write, and
both the audit row and the operator log line carry the reason — never a bare
`HTTP 404`, and never `ok` for a target that is not there.

Two sides are covered:

  * executor level — the refusal (and the cases that must NOT be refused: a probe
    that could not answer, and dry-run);
  * loop level — the operator log line and audit row a client sees, plus the LIVE
    inventory resolution that makes a model-chosen vm_id land on the real
    instance it was derived from.
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

from app import db, events, logs, store, ulid  # noqa: E402
from app.operator import Thresholds, OperatorLoop  # noqa: E402
from app.operator import playbook, vmids  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.executor import TARGET_MISSING, LxdExecutor  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

RAM_TOTAL = 34_359_738_368
RAM_HIGH = 34_000_000_000
# The id the live seeder invented for its synthetic node (the one that 404'd).
FABRICATED_VM_ID = "01M4N827979BD9D2762EFE24A0"
# A real instance name on the node the demo's executor dials.
REAL_INSTANCE = "termini-demo-workload-0-0"


class _ScriptedTransport(LxdExecutor):
    """LxdExecutor with its HTTP layer replaced by a scripted responder."""

    def __init__(self, script=None, **kw):
        super().__init__("https://10.10.0.11:8443", **kw)
        self.calls: list[tuple[str, str, dict | None]] = []
        self._script = script or {}

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        from app.operator.models import ExecResult
        status, payload = self._script.get((method, path), (200, "{}"))
        return ExecResult(result="ok" if status < 400 else "error", method=method,
                          path=path, body="", detail=payload, status=status)

    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [c for c in self.calls if c[0] in ("PUT", "POST", "PATCH", "DELETE")]


class RefusalTest(unittest.TestCase):
    """`instance_exists` == False -> refuse, before any state change."""

    def setUp(self):
        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.ex = LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01)

    def test_restart_of_a_missing_target_is_refused(self):
        r = self.ex.restart_instance(FABRICATED_VM_ID)

        self.assertEqual(r.result, "error")
        self.assertEqual(r.reason, TARGET_MISSING)
        self.assertEqual(self.lxd.state_requests(), [])      # NOTHING was issued
        # The audit/detail speaks about the TARGET, not about HTTP: no raw LXD
        # 404 body, which is what made the live audit read as an AI failure.
        self.assertIn(TARGET_MISSING, r.detail)
        self.assertIn(FABRICATED_VM_ID, r.detail)
        self.assertNotIn("HTTP 404", r.detail)
        self.assertNotIn("Instance not found", r.detail)

    def test_stop_of_a_missing_target_is_refused(self):
        r = self.ex.stop_instance(FABRICATED_VM_ID)
        self.assertEqual((r.result, r.reason), ("error", TARGET_MISSING))
        self.assertEqual(self.lxd.state_requests(), [])

    def test_start_of_a_missing_target_is_refused(self):
        r = self.ex.start_instance(FABRICATED_VM_ID)
        self.assertEqual((r.result, r.reason), ("error", TARGET_MISSING))
        self.assertEqual(self.lxd.state_requests(), [])

    def test_restart_of_an_existing_target_still_lands(self):
        # The guard must not cost the working path anything.
        self.lxd.set_state("agent-c1", "Running")
        r = self.ex.restart_instance("agent-c1")
        self.assertEqual(r.result, "ok")
        self.assertEqual(r.reason, "")
        self.assertEqual(len(self.lxd.restart_requests()), 1)

    def test_delete_of_a_missing_target_is_never_ok(self):
        # `delete` keeps its idempotency rule (there is nothing to delete), but it
        # must not report a SUCCESSFUL remediation for a target that is not there.
        ex = _ScriptedTransport({("DELETE", "/1.0/instances/ghost"):
                                 (404, '{"error":"Instance \\"ghost\\" not found"}')})
        r = ex.delete_instance("ghost")
        self.assertEqual(r.result, "already_applied")
        self.assertEqual(r.reason, TARGET_MISSING)
        self.assertIn(TARGET_MISSING, r.detail)


class ProbeNotRefusedTest(unittest.TestCase):
    """A probe that CANNOT answer must not become a refusal.

    "Could not ask" (unreachable node, 5xx, dry-run) is not "the instance is not
    there": refusing on it would turn a transient blip into a failed remediation,
    which is a new failure mode, not a fix.
    """

    def test_5xx_probe_lets_the_write_through(self):
        ex = _ScriptedTransport({
            ("GET", "/1.0/instances/agent-1"): (500, '{"error":"boom"}'),
            ("GET", "/1.0/instances/agent-1/state"): (200, '{"metadata":{"status":"Running"}}'),
            ("PUT", "/1.0/instances/agent-1/state"): (202, '{"operation":"/1.0/operations/op1"}'),
            ("GET", "/1.0/operations/op1"): (200, '{"metadata":{"status":"Success"}}'),
        })
        r = ex.restart_instance("agent-1")
        self.assertEqual(r.result, "ok")
        self.assertEqual(r.reason, "")
        self.assertIn(("PUT", "/1.0/instances/agent-1/state", {"action": "restart",
                                                              "force": True,
                                                              "timeout": 30}), ex.calls)

    def test_dry_run_probes_nothing_and_still_reports_dry_run(self):
        ex = LxdExecutor("https://10.10.0.11:8443", dry_run=True)
        r = ex.restart_instance("agent-1")
        self.assertEqual(r.result, "dry_run")
        self.assertEqual(r.reason, "")
        self.assertEqual(r.method, "PUT")


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


class LoopRefusalTest(unittest.TestCase):
    """The loop's audit row and operator log line for a non-existent target."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-target-")
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
        # The sink the dashboard's AI-operator panel reads (app/main wires the same
        # one in production); without it there is no log line to assert on.
        self.logs = logs.OperatorLogs(events.EventBroker())

    def _make_loop(self, **cfg_overrides):
        cfg = SimpleNamespace(**{**self.cfg.__dict__, **cfg_overrides})
        return OperatorLoop(
            cfg, self.project, Thresholds(), self.playbooks,
            instance_names={},                 # nothing configured: as in the demo
            default_instance_name="demo-workload",
            lxd=LxdExecutor(self.lxd.base_url, retry_max=2, backoff_s=0.01),
            logs=self.logs,
        )

    def _insert(self, samples):
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), samples)

    def _audits(self):
        with db.session(self.db_path) as conn:
            return opstore.list_audits(conn, self.node_id, 20)

    def _logs(self):
        with db.session(self.db_path) as conn:
            return list(conn.execute(
                "SELECT severity, message FROM operator_logs ORDER BY ts DESC LIMIT 10"))

    def test_fabricated_target_is_refused_with_a_reason_in_the_log(self):
        self._insert([oom_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z", FABRICATED_VM_ID)
                      for i in range(3)])
        loop = self._make_loop()
        produced = loop.run_once()

        self.assertEqual(len(produced), 1)
        # No LXD write was issued at all — that is the point of fail-closed.
        self.assertEqual(self.lxd.state_requests(), [])

        audits = self._audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["result"], "error")
        self.assertTrue(audits[0]["detail"].startswith(TARGET_MISSING),
                        audits[0]["detail"])
        self.assertNotIn("HTTP 404", audits[0]["detail"])

        # The CLIENT-UI line (operator_logs.message is what the dashboard renders
        # in the AI-operator panel) names the reason instead of a bare "-> error".
        refusal = [row for row in self._logs() if TARGET_MISSING in (row[1] or "")]
        self.assertEqual(len(refusal), 1, self._logs())
        severity, message = refusal[0]
        self.assertEqual(severity, "warn")
        self.assertIn(FABRICATED_VM_ID, message)
        self.assertNotIn("HTTP 404", message)

    def test_live_inventory_resolves_the_vm_id_to_the_real_instance(self):
        """A vm_id the NODE reported resolves to the instance it came from.

        This is the other half of the fix: with the node's real inventory in the
        decision context, the model's `vm_id` maps to a real LXD name and the
        remediation LANDS (audit `ok`) instead of being refused.
        """
        real_vm_id = vmids.vm_id_for_instance(REAL_INSTANCE)
        # The node has this instance (its inventory probe must list it) …
        self.lxd.instances[REAL_INSTANCE] = {}
        self.lxd.set_state(REAL_INSTANCE, "Running")
        # … and its telemetry reports the derived vm_id, with NO config mapping.
        self._insert([oom_sample(200 + i, f"2026-10-04T09:00:{i:02d}Z", real_vm_id)
                      for i in range(3)])

        produced = self._make_loop().run_once()

        self.assertEqual(len(produced), 1)
        restarts = self.lxd.restart_requests()
        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0][1], f"/1.0/instances/{REAL_INSTANCE}/state")
        self.assertEqual(self._audits()[0]["result"], "ok")
