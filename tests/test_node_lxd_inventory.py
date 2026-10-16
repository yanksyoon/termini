#!/usr/bin/env python3
"""The node's `lxd_instances[]` must actually be READ on a deployed node.

Measured on termini-node-a (2026-10-15): the agent's inventory probe ran
`lxc list --format=json`, the unit's own `NoNewPrivileges=true` blocked the snap
wrapper (`cannot change profile for the next exec call: Operation not permitted`),
and the node therefore reported NO `lxd_instances[]` at all — silently. With the
array missing the operator's guardrail has nothing to resolve a model-chosen
target against, which is the other half of "model decides -> 404": it is
invisible in the dashboard, it logs one line per SAMPLE, and the demo's claimed
path can never run.

The fix is to read LXD over its own unix socket (no privilege escalation needed),
with the CLI kept as a fallback. These tests pin both transports, the C0 id the
entries carry, §4.6's "running instances only" rule (the answer below deliberately
carries a stopped instance too), and the "omit the field rather than claim an
empty node" rule.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app.operator import vmids  # noqa: E402
from app import fields as F      # noqa: E402


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


AGENT = _load(REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py",
              "termini_bootstrap_inventory_under_test")

LXD_ENVELOPE = {
    "type": "sync",
    "status": "Success",
    "status_code": 200,
    "operation": "",
    "error_code": 0,
    "error": "",
    "metadata": [
        {
            "name": "demo-workload",
            "status": "Running",
            "state": {
                "cpu": {"usage": 123456789},
                "memory": {"usage": 536870912, "total": 7516192768},
                "disk": {"root": {"usage": 2362232012}},
            },
        },
        {
            "name": "termini-01m4wjhwqr3b8pcav2tvqbgg-0-1",
            "status": "Stopped",
            "state": {},
        },
    ],
}


class _FakeLxd(unittest.TestCase):
    """A real unix-socket HTTP server answering LXD's instance list."""

    def setUp(self):
        self.requests: list[str] = []
        self.dir = tempfile.mkdtemp(prefix="termini-lxd-sock-")
        self.sock_path = os.path.join(self.dir, "unix.socket")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        try:
            self.srv.close()
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                first = conn.recv(65536).split(b"\r\n")[0].decode(errors="replace")
                self.requests.append(first)
                body = json.dumps(LXD_ENVELOPE).encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body) + body
                )
            except OSError:
                pass
            finally:
                conn.close()


class SocketTransportTest(_FakeLxd):
    def setUp(self):
        super().setUp()
        self._orig = AGENT.LXD_UNIX_SOCKETS
        AGENT.LXD_UNIX_SOCKETS = (self.sock_path,)
        self.addCleanup(self._restore)

    def _restore(self):
        AGENT.LXD_UNIX_SOCKETS = self._orig

    def test_the_inventory_is_read_over_the_socket_and_carries_derived_vm_ids(self):
        inv = AGENT.lxd_instance_inventory({})

        self.assertTrue(self.requests, "no request reached the socket")
        self.assertIn("/1.0/instances?recursion=2", self.requests[0])
        # §4.6: one entry per RUNNING instance. The daemon's answer above carries
        # a stopped instance as well, and a stopped instance is not a remediation
        # target — it must not be advertised to the guardrail.
        self.assertEqual(len(inv), 1)
        by_id = {e["vm_id"]: e for e in inv}
        self.assertIn(vmids.vm_id_for_instance("demo-workload"), by_id,
                      "the controller could not resolve this entry back to its instance")
        self.assertNotIn(vmids.vm_id_for_instance("termini-01m4wjhwqr3b8pcav2tvqbgg-0-1"),
                         by_id, "a stopped instance must not be reported (§4.6)")
        self.assertRegex(vmids.vm_id_for_instance("demo-workload"), F.ULID_PATTERN)
        running = by_id[vmids.vm_id_for_instance("demo-workload")]
        self.assertEqual(running["state"], "running")           # LXD "Running" -> contract
        self.assertEqual(running["ram_used_bytes"], 536870912)
        self.assertEqual(running["ram_limit_bytes"], 7516192768)
        self.assertEqual(running["disk_used_bytes"], 2362232012)
        # The instance NAME is never on the wire: the entry is C0 either way.
        self.assertNotIn("demo-workload", json.dumps(inv))
        self.assertNotIn("name", running)


class UnavailableTest(unittest.TestCase):
    """No socket AND no CLI must yield None (field omitted), never an empty list."""

    def setUp(self):
        self._orig_sockets = AGENT.LXD_UNIX_SOCKETS
        self._orig_cli = AGENT._lxd_instances_over_cli
        self._orig_dead = AGENT._INVENTORY_CLI_DEAD_UNTIL
        AGENT.LXD_UNIX_SOCKETS = (os.path.join(tempfile.mkdtemp(), "absent.socket"),)
        AGENT._INVENTORY_CLI_DEAD_UNTIL = 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        AGENT.LXD_UNIX_SOCKETS = self._orig_sockets
        AGENT._lxd_instances_over_cli = self._orig_cli
        AGENT._INVENTORY_CLI_DEAD_UNTIL = self._orig_dead

    def test_both_transports_down_reports_no_inventory(self):
        AGENT._lxd_instances_over_cli = lambda: (None, "lxc: blocked")
        self.assertIsNone(AGENT.lxd_instance_inventory({}))

    def test_cli_fallback_is_used_when_the_socket_is_gone(self):
        AGENT._lxd_instances_over_cli = lambda: ([{"name": "demo-workload",
                                                   "status": "Running", "state": {}}], "")
        inv = AGENT.lxd_instance_inventory({})
        self.assertEqual([e["vm_id"] for e in inv],
                         [vmids.vm_id_for_instance("demo-workload")])

    def test_lxd_inventory_zero_disables_the_probe_entirely(self):
        called = []
        orig = AGENT._lxd_instances_over_socket
        AGENT._lxd_instances_over_socket = lambda: (called.append(1), (None, ""))[1]
        try:
            self.assertIsNone(AGENT.lxd_instance_inventory({"lxd_inventory": "0"}))
            self.assertEqual(called, [])
        finally:
            AGENT._lxd_instances_over_socket = orig


if __name__ == "__main__":
    unittest.main()
