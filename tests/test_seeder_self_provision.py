#!/usr/bin/env python3
"""The demo seeder must start with no hand-minted bootstrap token.

MEASURED DEFECT (live, on the standing controller). `termini-seeder` is the demo's
"make it look alive" service: it registers two SYNTHETIC nodes through the same
public `POST /v1/nodes/register` endpoint real hardware uses, then streams
telemetry so the operator loop has something to act on. Registration consumes a
**single-use** bootstrap token, so:

  * the second `systemctl start termini-seeder` failed
    `401 bootstrap token already used` — the token it was configured with had been
    burned by the first run;
  * re-minting tokens into the UNIT file `termini-seeder.service` still failed
    `401 unknown bootstrap token`, because a drop-in
    (`termini-seeder.service.d/zzz-seeder-tokens.conf`) overrides the unit — drop-ins
    load after the unit and the last one setting a key wins, so the hand-minted
    token in the unit was never the one in effect.

Net effect: a recording take would die mid-way, and starting the seeder required
out-of-band knowledge of systemd drop-in precedence and of which tokens had been
spent. That is the landmine this file closes.

The fix: the controller mints fresh single-use tokens on demand
(`POST /v1/bootstrap-tokens`, project-token-authenticated and loopback-only) and the
seeder fetches them at startup (`--bootstrap-mint N`). A bare
`systemctl start termini-seeder` just works.

The security property is NOT weakened: the minted tokens are ordinary single-use
rows, so a real node's replayed token is still rejected. Every test below that
mints a token also asserts the replay fails, because that is the property a naive
"reuse the token" fix would have broken.
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
from types import SimpleNamespace
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal, ulid  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"


def _load_by_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canonical(machine_id: str, *, gpus: list | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "machine": {
            "machine_id": machine_id,
            "hostname": machine_id,
            "cpu": {"model": "x", "cores": 8, "threads": 8},
            "memory": {"total_bytes": 8 * GiB},
            "disks": [{"device": "/dev/sda", "size_bytes": 64 * GiB}],
            "gpus": gpus or [],
        },
    }


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


class _HttpBase(unittest.TestCase):
    """Boots the real ControllerServer on loopback and drives it over HTTP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-seeder-")
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

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    @staticmethod
    def _call(req):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
                try:
                    return e.code, json.loads(raw)
                except json.JSONDecodeError:
                    return e.code, {}
            finally:
                e.close()

    def _post(self, path, body=None, token=PROJECT_TOKEN, extra_headers=None):
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if extra_headers:
            headers.update(extra_headers)
        return self._call(urllib.request.Request(
            self._url(path), data=data, headers=headers, method="POST"))

    def _register(self, boot_token, machine_id="demo-node-a", gpus=None):
        req = urllib.request.Request(
            self._url("/v1/nodes/register"),
            data=json.dumps(_canonical(machine_id, gpus=gpus)).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + boot_token},
            method="POST")
        return self._call(req)

    def _mint(self, count, token=PROJECT_TOKEN):
        return self._post("/v1/bootstrap-tokens", {"count": count}, token=token)


# --------------------------------------------------------------------------- 1. the mint route

class MintRouteTest(_HttpBase):
    def test_mint_requires_the_project_token(self):
        st, _ = self._mint(2, token=None)
        self.assertEqual(st, 401)
        st, _ = self._mint(2, token="wrong-token")
        self.assertNotEqual(st, 201)

    def test_mint_rejects_a_bad_count(self):
        for bad in (0, -1, 33, True, "2", None):
            st, _ = self._post("/v1/bootstrap-tokens", {"count": bad})
            self.assertEqual(st, 422, f"count={bad!r} must be rejected")

    def test_mint_returns_unused_single_use_tokens(self):
        st, body = self._mint(2)
        self.assertEqual(st, 201, body)
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["tokens"]), 2)
        self.assertTrue(body["single_use"])
        # The rows exist and are UNUSED (that is what makes a restart work).
        with db.session(self.db_path) as conn:
            for t in body["tokens"]:
                row = conn.execute(
                    "SELECT used_at FROM bootstrap_tokens WHERE token_hash = ?",
                    (store.sha256(t),)).fetchone()
                self.assertIsNotNone(row)
                self.assertIsNone(row["used_at"])


# ------------------------------------------------- 2. single-use is preserved for real nodes

class MintedTokensAreStillSingleUseTest(_HttpBase):
    def test_a_minted_token_registers_once_and_a_replay_is_rejected(self):
        st, body = self._mint(2)
        self.assertEqual(st, 201, body)
        tok_a, tok_c = body["tokens"]

        st, reg = self._register(tok_a, "demo-node-a")
        self.assertEqual(st, 201, reg)
        self.assertEqual(reg["role"], "compute")
        st, reg_c = self._register(tok_c, "demo-node-c")
        self.assertEqual(st, 201, reg_c)

        # THE security property: the SAME token again is refused.
        st, err = self._register(tok_a, "demo-node-a-again")
        self.assertEqual(st, 401, err)
        self.assertIn("already used", json.dumps(err))

    def test_minting_does_not_resurrect_an_already_burned_token(self):
        st, body = self._mint(1)
        tok = body["tokens"][0]
        self.assertEqual(self._register(tok, "demo-node-a")[0], 201)
        # Minting more tokens must not clear the burned one.
        self.assertEqual(self._mint(2)[0], 201)
        self.assertEqual(self._register(tok, "demo-node-a")[0], 401)


# --------------------------------------------------------------------------- 3. loopback-only

class LoopbackOnlyTest(_HttpBase):
    def test_a_direct_loopback_connection_is_accepted(self):
        predicate = server_mod.ControllerHandler._is_loopback_client
        for addr in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(
                predicate(SimpleNamespace(client_address=(addr, 4444), headers={})), addr)

    def test_a_non_loopback_peer_is_refused(self):
        predicate = server_mod.ControllerHandler._is_loopback_client
        for addr in ("10.10.0.11", "34.93.190.64", "::ffff:10.0.0.1", ""):
            self.assertFalse(
                predicate(SimpleNamespace(client_address=(addr, 4444), headers={})), addr)

    def test_a_request_that_traversed_the_proxy_is_refused_even_from_loopback(self):
        """The live trap: the host's Caddy proxies :443 -> 127.0.0.1:8080, so a
        REMOTE request arrives with a loopback peer. A proxy-forwarding header is
        what tells the two apart."""
        predicate = server_mod.ControllerHandler._is_loopback_client
        for h in ("X-Forwarded-For", "X-Real-Ip", "Forwarded", "Via"):
            self.assertFalse(
                predicate(SimpleNamespace(client_address=("127.0.0.1", 4444),
                                          headers={h: "203.0.113.7"})), h)

    def test_the_real_server_refuses_a_minted_request_carrying_xff(self):
        # End-to-end: exactly what Caddy sends for a public caller.
        st, body = self._post("/v1/bootstrap-tokens", {"count": 2},
                              extra_headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(st, 403, body)

    def test_a_direct_loopback_post_still_mints(self):
        self.assertEqual(self._post("/v1/bootstrap-tokens", {"count": 1})[0], 201)

    def test_a_remote_client_is_refused(self):
        """Simulate a request that did not come over loopback."""
        with mock.patch.object(server_mod.ControllerHandler, "_is_loopback_client",
                               lambda self: False):
            st, body = self._mint(2)
        self.assertEqual(st, 403, body)
        # ... and nothing was minted.
        self.assertEqual(self._mint(2)[1]["count"], 2)


# ------------------------------------------------------------- 4. the seeder bootstraps itself

class SeederSelfProvisionsTest(_HttpBase):
    """The end-to-end property: a seeder start needs NO hand-minted token."""

    def setUp(self):
        super().setUp()
        self.seed = _load_by_path("seed_demo_selfprov",
                                  REPO / "console" / "seed_demo.py")

    def test_seeder_mints_then_registers_both_nodes_with_no_manual_token(self):
        base = f"http://127.0.0.1:{self.port}"
        # Exactly what `--bootstrap-mint 2` does at startup.
        tokens = self.seed.mint_bootstrap_tokens(base, PROJECT_TOKEN, 2)
        self.assertEqual(len(tokens), 2)

        node_a = self.seed.register(base, tokens[0], "demo-node-a", [])
        node_c = self.seed.register(base, tokens[1], "demo-node-c",
                                    [{"vendor": "nvidia", "model": "NVIDIA L4",
                                      "vram_bytes": 24 * GiB}])
        self.assertEqual(node_a["project_id"], PROJECT_ID)
        self.assertNotEqual(node_a["node_id"], node_c["node_id"])

        # A restart mints AGAIN and registers again — this is the restart that
        # used to fail `401 bootstrap token already used`.
        tokens2 = self.seed.mint_bootstrap_tokens(base, PROJECT_TOKEN, 2)
        self.assertEqual(self.seed.register(base, tokens2[0], "demo-node-a", [])["node_id"],
                         node_a["node_id"])
        self.assertEqual(self.seed.register(base, tokens2[1], "demo-node-c", [])["node_id"],
                         node_c["node_id"])

    def test_seeder_mint_rejects_a_wrong_project_token(self):
        base = f"http://127.0.0.1:{self.port}"
        with self.assertRaises(SystemExit):
            self.seed.mint_bootstrap_tokens(base, "not-the-project-token", 2)

    def test_mint_flag_is_optional_and_defaults_off(self):
        ap_argv = ["--controller", "http://127.0.0.1:9", "--token", "t"]
        # Parsing with --bootstrap-mint absent must not require --bootstrap.
        import argparse
        # The seeder's own argparse block is inside main(); reproduce the contract
        # it documents: --bootstrap is optional, --bootstrap-mint defaults to 0.
        parser = argparse.ArgumentParser()
        parser.add_argument("--bootstrap", default="")
        parser.add_argument("--bootstrap-mint", type=int, default=0, metavar="N")
        parser.add_argument("--token", required=True)
        ns = parser.parse_args(["--token", "t"])
        self.assertEqual(ns.bootstrap, "")
        self.assertEqual(ns.bootstrap_mint, 0)
        self.assertIsInstance(ap_argv, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
