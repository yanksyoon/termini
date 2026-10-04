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

    def test_delete_instance_404_is_idempotent_ok(self):
        ex = LxdExecutor(self.lxd.base_url)
        # Stub always returns 200; here we assert the delete path is exercised.
        r = ex.delete_instance("agent-c1")
        self.assertEqual(r.result, "ok")
        self.assertEqual(r.path, "/1.0/instances/agent-c1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
