"""Per-agent operations (t_44a2b87b) — start / stop / restart, audited like a node action.

The Agents panel needs three things that did not exist on `main`:

  1. an agent identifier it actually holds — the telemetry `vm_id`
     (`node-telemetry.md` §4.6), a one-way hash, NOT the LXD instance name —
     must resolve to the instance on the owning node;
  2. the action must be a real state change that is confirmed, not a 202;
  3. the attempt must be human-audited in the same envelope as node
     Restart / Drain: a `commands` row (type / issued_by / state / result) and a
     content-free operator-log line.

Everything here runs against `tests/stub_lxd.StubLxd` — the same stub the
materialisation tests use, whose state actions mirror LXD's own semantics (404 for
an unknown instance, an async operation for a real change, the resulting state
readable back). No fleet, no network.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from app import agent_actions, agent_tasks, config, db, events, inventory, logs, server as server_mod, sshca, store, terminal  # noqa: E402
from app.operator import vmids  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"
PLAN = "01M4WJHWQR3B8PCAV2TVQBGG00"
VM = "termini-01m4wjhwqr3b8pcav2tvqbgg-0-0"     # the LXD instance name
VM_ID = vmids.vm_id_for_instance(VM)             # what the client sees

COMPUTE_HW = {
    "cpu": {"cores": 8, "threads": 8},
    "memory": {"total_bytes": 32 * 1024 ** 3},
    "gpus": [],
    "disks": [{"size_bytes": 256 * 1024 ** 3}],
}


def _load_cfg(db_path: str, lxd_base: str) -> config.Config:
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_LXD_BASE_URL": lxd_base,
        "TERMINI_LXD_CREATE_TIMEOUT_S": "10",
        "TERMINI_LXD_START_TIMEOUT_S": "10",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
    }
    with unittest.mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


class AgentActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-agentactions-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.lxd.instances[VM] = {"name": VM}
        self.lxd.set_state(VM, "Running")

        self.cfg = _load_cfg(self.db_path, self.lxd.base_url)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())
            reg = {"registration_id": "compute-1", "machine_id": "compute-1",
                   "hostname": "compute-1", "role_hint": None,
                   "installer_version": None, "hardware_json": COMPUTE_HW,
                   "os": None, "lxd": None, "lxd_endpoint": None}
            self.node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, "compute", store.sha256("compute-1"), None,
                store.now_iso())
            store.commit_allocation(
                conn, PROJECT_ID, PLAN, self.node_id,
                cpu_allocated=2, ram_allocated_gb=4, disk_allocated_gb=8,
                now=store.now_iso())
            store.set_allocation_instances(conn, PROJECT_ID, PLAN, self.node_id, [VM])

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=200)
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None),
        )
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _action(self, action, vm=VM_ID):
        url = (f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}"
               f"/agents/{vm}/actions/{action}")
        req = urllib.request.Request(
            url, data=b"{}", method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + PROJECT_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {}

    def _command(self, command_id):
        with db.session(self.db_path) as conn:
            row = conn.execute("SELECT * FROM commands WHERE command_id = ?",
                               (command_id,)).fetchone()
            return dict(row) if row else None

    def _log_lines(self):
        with db.session(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM operator_logs ORDER BY ts, log_id").fetchall()]

    # -- (1) the identifier the client holds resolves to the instance ---------
    def test_telemetry_vm_id_resolves_to_the_instance_name(self):
        with db.session(self.db_path) as conn:
            res = agent_tasks.resolve_agent_target(conn, self.cfg, PROJECT_ID, VM_ID)
        self.assertIsNone(res.reason)
        self.assertEqual(res.node["id"], self.node_id)
        self.assertEqual(res.instance_name, VM)

    def test_the_instance_name_still_resolves(self):
        with db.session(self.db_path) as conn:
            res = agent_tasks.resolve_agent_target(conn, self.cfg, PROJECT_ID, VM)
        self.assertIsNone(res.reason)
        self.assertEqual(res.node["id"], self.node_id)
        self.assertEqual(res.instance_name, VM)

    # -- (2) a real, confirmed state change ------------------------------------
    def test_restart_reaches_lxd_and_is_confirmed(self):
        st, body = self._action("restart")
        self.assertEqual(st, 202, body)
        self.assertEqual(body["state"], "succeeded", body)
        self.assertEqual(body["type"], "vm.restart")
        self.assertEqual(self.lxd.state_of(VM), "Running")
        self.assertEqual(len(self.lxd.restart_requests()), 1)
        self.assertTrue(self.lxd.restart_requests()[0][1].endswith(f"/instances/{VM}/state"))

    def test_stop_and_start_change_the_instance_state(self):
        st, body = self._action("stop")
        self.assertEqual(st, 202, body)
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(self.lxd.state_of(VM), "Stopped")

        st, body = self._action("start")
        self.assertEqual(st, 202, body)
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(self.lxd.state_of(VM), "Running")

    def test_stop_is_idempotent_when_already_stopped(self):
        self.lxd.set_state(VM, "Stopped")
        self.lxd.strict_stop = True
        st, body = self._action("stop")
        self.assertEqual(st, 202, body)
        self.assertEqual(body["result"]["result"], "already_applied", body)

    def test_a_refused_lxd_call_is_a_failed_command_not_a_lie(self):
        self.lxd.fail_start = True
        self.lxd.set_state(VM, "Stopped")
        st, body = self._action("start")
        self.assertEqual(st, 202, body)          # the attempt was issued and audited
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["result"]["result"], "error")
        self.assertNotEqual(self.lxd.state_of(VM), "Running")

    # -- (3) the human-audited record ------------------------------------------
    def test_action_is_a_human_audited_command_with_a_log_line(self):
        st, body = self._action("restart")
        self.assertEqual(st, 202, body)
        cmd = self._command(body["command_id"])
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["type"], "vm.restart")
        self.assertEqual(cmd["issued_by"], "human")
        self.assertEqual(cmd["node_id"], self.node_id)
        self.assertEqual(cmd["state"], "succeeded")
        result = json.loads(cmd["result_json"])
        self.assertEqual(result["result"], "ok")
        self.assertEqual(result["action"], "restart")

        lines = self._log_lines()
        mine = [ln for ln in lines if body["command_id"] in (ln["remediation_json"] or "")]
        self.assertTrue(mine, lines)
        self.assertEqual(mine[0]["actor"], "human")
        self.assertIn("vm.restart", mine[0]["message"])
        # The instance name never crosses into the audit stream.
        self.assertNotIn(VM, mine[0]["message"])
        self.assertNotIn(VM, cmd["payload_json"])

    # -- (4) explicit failure states, never a blank/500 ------------------------
    def test_unknown_agent_is_404_with_the_known_ids(self):
        unknown = vmids.vm_id_for_instance("termini-nope-0-0")
        st, body = self._action("restart", vm=unknown)
        self.assertEqual(st, 404, body)
        self.assertEqual(body["error"]["code"], "AGENT_NOT_FOUND")
        details = body["error"]["details"][0]
        self.assertEqual(details["reason"], "unknown")
        self.assertIn(VM_ID, details["known_agent_ids"])

    def test_unknown_action_is_404(self):
        st, body = self._action("frobnicate")
        self.assertEqual(st, 404, body)

    def test_offline_node_is_refused_before_lxd(self):
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE nodes SET status = 'offline', last_seen_at = ?",
                         ("2020-01-01T00:00:00Z",))
            conn.commit()
        st, body = self._action("restart")
        self.assertEqual(st, 503, body)
        self.assertEqual(body["error"]["code"], "NODE_OFFLINE")
        self.assertEqual(self.lxd.restart_requests(), [])

    def test_requires_the_project_token(self):
        url = (f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}"
               f"/agents/{VM_ID}/actions/restart")
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertIn(ctx.exception.code, (401, 403))


# `unittest.mock` is imported lazily above through unittest
import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
