#!/usr/bin/env python3
"""The demo runner must not hand-mint the seeder's bootstrap pair.

MEASURED DEFECT (local, persistent ``TERMINI_DEMO_STATE``). ``console/run-demo.sh``
hands the seeder a FIXED pair of single-use bootstrap tokens
(``--bootstrap "$BOOTSTRAP"``). The first run against a fresh state dir burns both
tokens registering ``demo-node-a`` and ``demo-node-c``; a SECOND run against the
same state dir reuses the same pair and dies
``401 bootstrap token already used`` — exactly the deployed-seeder landmine this
repo already fixed with ``--bootstrap-mint 2`` (see test_seeder_self_provision.py),
but which the local dev runner never picked up.

The fix: the runner calls the seeder with ``--bootstrap-mint 2``, so the seeder
asks the controller for two FRESH single-use tokens on every start. Nothing else
about the runner changes — ``TERMINI_BOOTSTRAP_TOKEN`` must stay non-empty for the
controller's own config, so the fixed pair is still seeded for the controller but
is no longer what the seeder authenticates with.

Two guards below:

  1. ``RunnerSeederInvocationTest`` — parses ``console/run-demo.sh`` and asserts the
     ``seed_demo.py`` invocation carries ``--bootstrap-mint 2`` and does NOT pass
     ``--bootstrap "$BOOTSTRAP"``. This fails if the hand-minted pair is restored.
  2. ``RunnerSeederReplayTest`` — boots the real controller and runs the seeder's
     ``main()`` with the runner's flags twice against ONE database, asserting both
     runs register both nodes. This is the mint -> register -> replay property in
     the runner's own shape; it fails on the old invocation (fixed single-use pair
     cannot survive a replay).
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import config, db, events, inventory, logs, server as server_mod, store, terminal, sshca  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"


def _load_by_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_cfg(db_path: str):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a,boot-c",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_MAX_INFERENCE_NODES": "1",
        "TERMINI_LXD_BASE_URL": "http://127.0.0.1:1",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


class RunnerSeederInvocationTest(unittest.TestCase):
    """The runner's seed_demo.py line must self-provision, not hand-mint."""

    def _runner_lines(self) -> list[str]:
        return (REPO / "console" / "run-demo.sh").read_text().splitlines()

    def test_seeder_invocation_uses_bootstrap_mint_not_fixed_pair(self):
        lines = self._runner_lines()
        # The seed_demo.py invocation block (the `python3 "$HERE/seed_demo.py"` line
        # plus the continuation lines that carry --token / --bootstrap*).
        idx = next(i for i, ln in enumerate(lines) if "seed_demo.py" in ln)
        invocation = "\n".join(lines[idx:idx + 4])
        self.assertIn("--bootstrap-mint 2", invocation)
        # The fixed hand-minted pair must NOT be what the seeder authenticates with.
        self.assertNotIn("--bootstrap \"$BOOTSTRAP\"", invocation)
        self.assertNotIn('--bootstrap "$BOOTSTRAP"', invocation)

    def test_controller_still_gets_a_nonempty_bootstrap_token(self):
        # config._require_nonempty forces TERMINI_BOOTSTRAP_TOKEN to be non-empty, so
        # the runner must keep seeding the controller with *some* token even though
        # the seeder no longer uses it.
        body = "\n".join(self._runner_lines())
        self.assertIn('TERMINI_BOOTSTRAP_TOKEN="$BOOTSTRAP"', body)
        self.assertIn('BOOTSTRAP="${BOOTSTRAP:-demo-boot-a,demo-boot-c}"', body)


class _HttpBase(unittest.TestCase):
    """Boots the real ControllerServer on loopback and drives it over HTTP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-run-demo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)
        self.cfg = _load_cfg(self.db_path)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=100)
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


class RunnerSeederReplayTest(_HttpBase):
    """The runner's seeder command survives a restart against one database."""

    def setUp(self):
        super().setUp()
        self.seed = _load_by_path("seed_demo_run_demo", REPO / "console" / "seed_demo.py")

    def _run_seeder_once(self) -> int:
        # Exactly the flags run-demo.sh passes to seed_demo.py now.
        return self.seed.main([
            "--controller", f"http://127.0.0.1:{self.port}",
            "--token", PROJECT_TOKEN,
            "--bootstrap-mint", "2",
            "--duration", "0.4",
        ])

    def test_seeder_runs_twice_against_one_db_and_registers_both_nodes_both_times(self):
        first = self._run_seeder_once()
        second = self._run_seeder_once()
        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        with db.session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT machine_id FROM nodes WHERE synthetic = 1 ORDER BY machine_id"
            ).fetchall()
        names = sorted(r["machine_id"] for r in rows)
        # Both runs register the same two synthetic nodes; no duplicate rows.
        self.assertEqual(names, ["demo-node-a", "demo-node-c"])

    def test_hand_minted_fixed_pair_still_fails_on_replay(self):
        # Negative control proving the runner's OLD shape was the defect: registering
        # the same fixed single-use token twice dies on the second attempt.
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, ["fixed-boot"], store.now_iso())
        fixed = self.seed.register(f"http://127.0.0.1:{self.port}", "fixed-boot",
                                   "demo-node-x", [])
        self.assertIn("node_id", fixed)
        with self.assertRaises(SystemExit):
            self.seed.register(f"http://127.0.0.1:{self.port}", "fixed-boot",
                               "demo-node-y", [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
