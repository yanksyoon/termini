"""Tests for the LXD REST executor against a stub LXD server."""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app.operator.executor import LxdExecutor  # noqa: E402
from app.operator.models import RemediationAction  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        # Every state change is now gated on LXD's own existence probe
        # (`GET /1.0/instances/<name>` -> 404 refuses the action as
        # `target_missing`, executor.py), so a test that expects a remediation to
        # LAND must declare the instance it acts on. The stub answers 404 for an
        # instance it does not know, exactly as real LXD does — which is the
        # failure this guard exists to prevent (model decides -> HTTP 404).
        self.lxd.set_state("agent-c1", "Running")

    def test_restart_issues_real_http_call(self):
        ex = LxdExecutor(self.lxd.base_url)
        r = ex.restart_instance("agent-c1")
        self.assertEqual(r.result, "ok")
        self.assertEqual(r.method, "PUT")
        self.assertEqual(r.path, "/1.0/instances/agent-c1/state")
        restarts = self.lxd.restart_requests()
        self.assertEqual(len(restarts), 1)
        _, _, body = restarts[0]
        self.assertEqual(body["action"], "restart")

    def test_dry_run_sends_nothing(self):
        ex = LxdExecutor(self.lxd.base_url, dry_run=True)
        r = ex.restart_instance("agent-c1")
        self.assertEqual(r.result, "dry_run")
        self.assertEqual(r.path, "/1.0/instances/agent-c1/state")
        self.assertEqual(self.lxd.requests, [])

    def test_prune_images_deletes_cached(self):
        self.lxd.images = [
            {"fingerprint": "fp-a", "cached": True, "auto_update": False},
            {"fingerprint": "fp-b", "cached": False, "auto_update": True},
            {"fingerprint": "fp-c", "cached": False, "auto_update": False},
        ]
        ex = LxdExecutor(self.lxd.base_url)
        r = ex.prune_images()
        self.assertEqual(r.result, "ok")
        deletes = [p for (m, p, _) in self.lxd.requests if m == "DELETE"]
        self.assertEqual(sorted(deletes), ["/1.0/images/fp-a", "/1.0/images/fp-b"])

    def test_prune_dry_run_sends_nothing(self):
        self.lxd.images = [{"fingerprint": "fp-a", "cached": True, "auto_update": False}]
        ex = LxdExecutor(self.lxd.base_url, dry_run=True)
        r = ex.prune_images()
        self.assertEqual(r.result, "dry_run")
        self.assertEqual(self.lxd.requests, [])

    def test_execute_dispatches_on_kind(self):
        ex = LxdExecutor(self.lxd.base_url)
        action = RemediationAction(kind="instance.restart",
                                   params={"instance_name": "agent-c1", "force": True})
        r = ex.execute(action)
        self.assertEqual(r.result, "ok")
        self.assertEqual(len(self.lxd.restart_requests()), 1)

    def test_retry_on_transient_error(self):
        # Point the executor at a closed port: connection refused -> retry -> error.
        ex = LxdExecutor("http://127.0.0.1:1", retry_max=2, backoff_s=0.01)
        r = ex.restart_instance("agent-c1")
        self.assertEqual(r.result, "error")

    def test_delete_through_stub_is_ok(self):
        ex = LxdExecutor(self.lxd.base_url)
        # Stub always returns 200; here we assert the delete path is exercised.
        # The 404 fold (an instance that is NOT there -> `already_applied`, never
        # `ok`) is covered by tests/test_operator_target_missing.py.
        r = ex.delete_instance("agent-c1")
        self.assertEqual(r.result, "ok")
        self.assertEqual(r.path, "/1.0/instances/agent-c1")

    # ------------------------------------------------ idempotent stop (DEFECT 4)
    def test_stop_of_a_stopped_instance_is_an_idempotent_no_op(self):
        """LXD's "already stopped" answer folds to `already_applied`.

        MEASURED DEFECT (live): the first real remediation attempt stopped an
        already-STOPPED instance, LXD answered
        `operation Failure: The instance is already stopped`, and the audit
        recorded `result=error` for a remediation that was in fact a correct
        no-op — mirroring the already-running fold (#133).
        """
        self.lxd.strict_stop = True
        self.lxd.set_state("agent-c1", "Stopped")

        r = LxdExecutor(self.lxd.base_url).stop_instance("agent-c1")

        self.assertEqual(r.result, "already_applied")
        self.assertEqual(r.method, "PUT")
        self.assertEqual(r.path, "/1.0/instances/agent-c1/state")
        self.assertIn("already stopped", r.detail)

    def test_a_failed_operation_carrying_the_LIVE_message_is_also_folded(self):
        """The exact LIVE transport: the stop operation itself FAILS.

        The sibling test above uses LXD's synchronous 400. Real node-a answered with
        a *failed operation* — the audit row recorded `result=error`, detail
        `operation Failure: The instance is already stopped` (remediation_audits,
        2026-10-13T12:24:37Z) — so the fold must cover the operation path too, not
        just the synchronous one.
        """
        self.lxd.operation_status = "Failure"
        self.lxd.operation_error = "The instance is already stopped"
        self.lxd.set_state("agent-c1", "Running")

        r = LxdExecutor(self.lxd.base_url).stop_instance("agent-c1")

        self.assertEqual(r.result, "already_applied")
        self.assertIn("already stopped", r.detail)

    def test_stop_of_a_running_instance_still_really_stops_it(self):
        """Control: the fold must not swallow a genuine stop."""
        self.lxd.strict_stop = True
        self.lxd.set_state("agent-c1", "Running")

        r = LxdExecutor(self.lxd.base_url).stop_instance("agent-c1")

        self.assertEqual(r.result, "ok")
        self.assertEqual(self.lxd.state_of("agent-c1"), "Stopped")

    def test_a_genuine_stop_failure_is_not_folded(self):
        """A real (non-"already stopped") stop failure stays an `error`."""
        self.lxd.operation_status = "Failure"
        self.lxd.set_state("agent-c1", "Running")

        r = LxdExecutor(self.lxd.base_url).stop_instance("agent-c1")

        self.assertEqual(r.result, "error")

    def test_stop_of_repeated_remediation_is_a_no_op_on_the_second_run(self):
        """Stop twice against real LXD semantics: no error on the re-run."""
        self.lxd.strict_stop = True
        self.lxd.set_state("agent-c1", "Running")
        ex = LxdExecutor(self.lxd.base_url)

        first = ex.stop_instance("agent-c1")
        second = ex.stop_instance("agent-c1")

        self.assertEqual(first.result, "ok")
        self.assertEqual(second.result, "already_applied")
        self.assertEqual(self.lxd.state_of("agent-c1"), "Stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
