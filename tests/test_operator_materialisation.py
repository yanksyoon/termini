#!/usr/bin/env python3
"""Materialisation primitives on LxdExecutor: create_instance + wait_operation.

These are the calls that turn an accepted plan's allocations into real instances on
a node. The properties that matter for a live demo, and that this file pins down:

  * a create issues POST /1.0/instances with the right source/config body;
  * it WAITS on the operation - LXD answers 202 and "the POST returned" does not
    mean "the instance exists", so reporting success early would commit capacity
    for an instance that is still being created;
  * a duplicate name is `already_applied`, not an error, so a replayed
    materialisation is a no-op (same rule delete_instance applies to a 404);
  * a failed or timed-out operation is an error, never a silent pass;
  * dry-run sends nothing.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "controller"))

from app.operator.executor import LxdExecutor, _operation_url  # noqa: E402
from app.operator.models import RemediationAction                # noqa: E402


class _FakeTransport(LxdExecutor):
    """LxdExecutor with the HTTP layer replaced by a scripted responder."""

    def __init__(self, script=None, **kw):
        super().__init__("https://10.10.0.11:8443", **kw)
        self.calls: list[tuple[str, str, dict | None]] = []
        self._script = script or {}

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        from app.operator.models import ExecResult
        status, payload = self._script.get((method, path), (200, "{}"))
        return ExecResult(
            result="ok" if status < 400 else "error",
            method=method, path=path, body=json.dumps(body or {}),
            detail=payload, status=status,
        )

    def _read_json(self, path):
        from app.operator.models import ExecResult
        self.calls.append(("GET", path, None))   # record the poll, same as _request
        status, payload = self._script.get(("GET", path), (200, json.dumps(
            {"metadata": {"status": "Success"}})))
        r = ExecResult(result="ok" if status < 400 else "error", method="GET",
                       path=path, status=status, detail=payload)
        try:
            return r, json.loads(payload)
        except json.JSONDecodeError:
            return r, None


class OperationUrlTest(unittest.TestCase):
    def test_extracts_async_operation(self):
        raw = json.dumps({"type": "async", "operation": "/1.0/operations/abc123"})
        self.assertEqual(_operation_url(raw), "/1.0/operations/abc123")

    def test_returns_empty_for_non_async(self):
        self.assertEqual(_operation_url(json.dumps({"type": "sync"})), "")
        self.assertEqual(_operation_url("not json"), "")
        self.assertEqual(_operation_url("{}"), "")


class CreateInstanceTest(unittest.TestCase):
    OP = "/1.0/operations/op-1"

    def _ok_script(self):
        return {
            ("POST", "/1.0/instances"): (202, json.dumps(
                {"type": "async", "operation": self.OP})),
            ("GET", self.OP): (200, json.dumps({"metadata": {"status": "Success"}})),
        }

    def test_create_posts_body_and_waits_for_the_operation(self):
        ex = _FakeTransport(self._ok_script())
        r = ex.create_instance("agent-1", image_alias="agent-vm", cpu=2, memory="4GiB")

        self.assertEqual(r.result, "ok")
        self.assertEqual(r.operation, self.OP)          # waited, not fire-and-forget
        self.assertEqual([c[0] for c in ex.calls], ["POST", "GET"])
        self.assertEqual(ex.calls[0][1], "/1.0/instances")
        body = ex.calls[0][2]
        self.assertEqual(body["name"], "agent-1")
        self.assertEqual(body["source"], {"type": "image", "alias": "agent-vm"})
        self.assertEqual(body["config"], {"limits.cpu": "2", "limits.memory": "4GiB"})
        self.assertEqual(body["profiles"], ["default"])

    def test_fingerprint_is_used_when_given_instead_of_alias(self):
        ex = _FakeTransport(self._ok_script())
        ex.create_instance("agent-1", image_fingerprint="fp-deadbeef")
        self.assertEqual(ex.calls[0][2]["source"],
                         {"type": "image", "fingerprint": "fp-deadbeef"})

    def test_duplicate_name_is_already_applied_not_an_error(self):
        # A replayed materialisation must not fail the run.
        ex = _FakeTransport({("POST", "/1.0/instances"):
                             (500, json.dumps({"error": "Instance already exists"}))})
        r = ex.create_instance("agent-1", image_alias="agent-vm")
        self.assertEqual(r.result, "already_applied")
        self.assertEqual(len(ex.calls), 1)              # no wait attempted

    def test_failed_operation_is_an_error(self):
        script = {
            ("POST", "/1.0/instances"): (202, json.dumps({"operation": self.OP})),
            ("GET", self.OP): (200, json.dumps({"metadata": {"status": "Failure",
                                                            "err": "no space"}})),
        }
        ex = _FakeTransport(script)
        r = ex.create_instance("agent-1", image_alias="agent-vm")
        self.assertEqual(r.result, "error")
        self.assertIn("Failure", r.detail)

    def test_operation_timeout_is_an_error_not_an_early_pass(self):
        script = {
            ("POST", "/1.0/instances"): (202, json.dumps({"operation": self.OP})),
            ("GET", self.OP): (200, json.dumps({"metadata": {"status": "Running"}})),
        }
        ex = _FakeTransport(script)
        r = ex.wait_operation(self.OP, timeout_s=0.01, poll_s=0.001)
        self.assertEqual(r.result, "error")
        self.assertIn("still Running", r.detail)

    def test_missing_image_raises(self):
        ex = _FakeTransport(self._ok_script())
        with self.assertRaises(ValueError):
            ex.create_instance("agent-1")

    def test_dry_run_sends_nothing(self):
        # Use the real _request (not the fake) so the dry-run branch is the one
        # under test; it returns before any socket is opened.
        ex = LxdExecutor("https://10.10.0.11:8443", dry_run=True)
        r = ex.create_instance("agent-1", image_alias="agent-vm")
        self.assertEqual(r.result, "dry_run")
        self.assertEqual(r.method, "POST")
        self.assertEqual(r.path, "/1.0/instances")
        self.assertIn("agent-1", r.body)

    def test_dispatch_routes_instance_create(self):
        ex = _FakeTransport(self._ok_script())
        action = RemediationAction(kind="instance.create", params={
            "instance_name": "agent-9", "image_alias": "agent-vm",
            "vcpus": 4, "memory": "8GiB"})
        r = ex.execute(action)
        self.assertEqual(r.result, "ok")
        body = ex.calls[0][2]
        self.assertEqual(body["name"], "agent-9")
        self.assertEqual(body["config"]["limits.cpu"], "4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
