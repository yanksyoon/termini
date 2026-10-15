#!/usr/bin/env python3
"""The client-facing install flow: a customer bootstraps a node with their OWN credential.

Task `t_70bd0f2b`. The install path used to be entirely provider-driven: the
enrollment credential could only be minted by someone with a shell on the
controller (`POST /v1/bootstrap-tokens` is project-token AND loopback only), and
there was no client-facing install surface, no documented customer one-liner and
no "your node is registered" confirmation anywhere a client could see.

What this file pins, end to end and on a real controller process:

  1. the controller serves the client installer, the client status helper and the
     node runtime over a PUBLIC, unauthenticated, secret-free `/install/` surface
     — and serves nothing else (`infra/bootstrap/client/termini-node-install.sh`,
     `controller/app/server.py::_serve_install`);
  2. a node really registers through the customer's own single-use credential, and
     the client then reads that node's health back with the node's OWN token
     (the project token is never involved — the guardrail of the card);
  3. re-running the installer on an already-registered node reuses its persisted
     identity and does NOT burn a second credential (acceptance (b) of the card);
  4. the three failure modes a customer can hit — rejected credential, already-used
     credential, unreachable controller — produce DISTINCT, actionable messages
     (acceptance (c)), because the classification lives in one place
     (`termini-node-status.py::classify`).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"
INSTALLER = REPO / "infra" / "bootstrap" / "client" / "termini-node-install.sh"
STATUS_HELPER = REPO / "infra" / "bootstrap" / "client" / "termini-node-status.py"
DAEMON = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"
ISSUE_ENROLLMENT = REPO / "infra" / "bootstrap" / "client" / "issue-enrollment.py"


def _load_cfg(db_path: str):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a,boot-c",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_LXD_BASE_URL": "http://127.0.0.1:1",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
        "TERMINI_OPERATOR_ENABLED": "false",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


class _ControllerCase(unittest.TestCase):
    """Boots the real ControllerServer on loopback and drives it over HTTP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-client-install-")
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

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _raw_get(self, path: str) -> tuple[int, bytes, str]:
        req = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read(), e.headers.get("Content-Type", "")
            finally:
                e.close()

    def _get_json(self, path: str, token: str | None = PROJECT_TOKEN):
        req = urllib.request.Request(self.base + path, method="GET")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def _mint(self, count: int = 1, token: str | None = PROJECT_TOKEN) -> list[str]:
        data = json.dumps({"count": count}).encode()
        req = urllib.request.Request(
            self.base + "/v1/bootstrap-tokens", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["tokens"]

    def _token_state(self, token: str) -> str | None:
        with db.session(self.db_path) as conn:
            row = conn.execute("SELECT used_at FROM bootstrap_tokens WHERE token_hash = ?",
                               (store.sha256(token),)).fetchone()
        return None if row is None else row["used_at"]


# --------------------------------------------------------------------------- 1. the /install surface

class InstallSurfaceTest(_ControllerCase):
    def test_serves_the_client_installer_without_any_token(self):
        status, body, ctype = self._raw_get("/install/termini-node.sh")
        self.assertEqual(status, 200)
        self.assertIn("shellscript", ctype)
        text = body.decode()
        # It is the real artifact, byte-for-byte, and it is what the docs hand out.
        self.assertEqual(body, INSTALLER.read_bytes())
        self.assertIn("termini-node-install", text)
        self.assertIn("--token", text)

    def test_serves_the_status_helper_and_the_node_runtime(self):
        for name, path in (("termini-node-status.py", STATUS_HELPER),
                           ("termini-bootstrap.py", DAEMON),
                           ("termini-bootstrap.service",
                            REPO / "infra/bootstrap/node/termini-bootstrap.service")):
            status, body, _ = self._raw_get(f"/install/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(body, path.read_bytes(), name)

    def test_only_named_artifacts_are_served(self):
        # No request path component is ever used to build a filesystem path.
        for path in ("/install/", "/install", "/install/../../etc/passwd",
                     "/install/%2e%2e%2fetc%2fpasswd", "/install/other.sh",
                     "/install/termini-node.sh/extra"):
            status, body, _ = self._raw_get(path)
            self.assertNotEqual(status, 200, f"{path} must not be served")
            self.assertNotEqual(body, INSTALLER.read_bytes())

    def test_no_install_artifact_carries_the_project_token(self):
        """The card's guardrail: the project token must not reach a client artifact."""
        import re
        token_re = re.compile(rb"[A-Za-z0-9_\-]{16,}")
        for name in ("termini-node.sh", "termini-node-status.py",
                     "termini-bootstrap.py", "termini-bootstrap.service"):
            status, body, _ = self._raw_get(f"/install/{name}")
            self.assertEqual(status, 200)
            self.assertNotIn(PROJECT_TOKEN.encode(), body, name)
            self.assertNotIn(b"demo-project-token", body, name)
            # And the artifact must not NAME a project/scoped token either.
            for m in token_re.findall(body):
                self.assertNotEqual(m, PROJECT_TOKEN.encode(), name)
        # The status helper's own credential story: node token only.
        self.assertIn("node_token", STATUS_HELPER.read_text())


# --------------------------------------------------------------------------- 2. the installer + helper scripts

class InstallerCliTest(unittest.TestCase):
    """Argument handling that does not need a controller at all."""

    def test_it_is_posix_sh_it_can_be_piped_into(self):
        """The documented one-liner pipes it into `sh` (dash on Ubuntu)."""
        head = INSTALLER.read_text().splitlines()[0]
        self.assertEqual(head, "#!/bin/sh")

    def test_help_documents_the_credential_and_never_a_project_token(self):
        proc = subprocess.run(["sh", str(INSTALLER), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--token", proc.stdout)
        self.assertIn("single-use enrollment credential", proc.stdout)
        self.assertNotIn(PROJECT_TOKEN, proc.stdout)

    def test_requires_a_controller_url(self):
        proc = subprocess.run(["sh", str(INSTALLER), "--token", "x"],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("controller URL", proc.stderr)

    def test_unknown_flag_is_refused(self):
        proc = subprocess.run(["sh", str(INSTALLER), "--nope"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown argument", proc.stderr)


class InstallerScriptTest(_ControllerCase):
    """The installer's file/credential handling, driven against a live controller.

    Using the real controller (rather than a stub) is deliberate: it is also what
    proves the installer CONSUMES the public /install/ surface — the node runtime
    and the status helper both come from there.
    """

    def _run(self, root: str, *args, expect_ok=True):
        proc = subprocess.run(
            ["sh", str(INSTALLER), "--root", root, "--controller", self.base, *args],
            capture_output=True, text=True, timeout=180)
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def _root(self) -> str:
        tmp = tempfile.mkdtemp(prefix="termini-install-root-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_no_start_writes_config_credential_runtime_and_helper(self):
        root = self._root()
        proc = self._run(root, "--token", "one-shot", "--name", "node-x",
                         "--role", "compute")
        cfg = pathlib.Path(root) / "etc/termini/config.yaml"
        token = pathlib.Path(root) / "var/lib/termini/enrollment.token"
        helper = pathlib.Path(root) / "usr/local/bin/termini-node-status"
        daemon = pathlib.Path(root) / "usr/local/bin/termini-bootstrap"
        unit = pathlib.Path(root) / "etc/systemd/system/termini-bootstrap.service"
        self.assertIn(f"controller_url: {self.base}", cfg.read_text())
        self.assertIn("node_name: node-x", cfg.read_text())
        self.assertIn("role_hint: compute", cfg.read_text())
        self.assertEqual(token.read_text().strip(), "one-shot")
        for p in (cfg, token):
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600", p)
        # The public /install/ surface is what supplies the runtime and the helper.
        self.assertEqual(daemon.read_bytes(), DAEMON.read_bytes())
        self.assertEqual(unit.read_bytes(),
                         (REPO / "infra/bootstrap/node/termini-bootstrap.service").read_bytes())
        self.assertEqual(helper.read_bytes(), STATUS_HELPER.read_bytes())
        self.assertTrue(proc.stdout.strip())
        self.assertNotIn("one-shot", proc.stdout)  # never echo the credential

    def test_requires_a_credential_on_a_machine_that_is_not_registered(self):
        root = self._root()
        proc = self._run(root, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no enrollment credential", proc.stderr)

    def test_existing_identity_needs_no_credential_and_burns_none(self):
        """Acceptance (b) at the script level: a registered node never re-enrolls."""
        root = self._root()
        ident = pathlib.Path(root) / "var/lib/termini/identity/node.json"
        ident.parent.mkdir(parents=True, exist_ok=True)
        ident.write_text(json.dumps({"node_id": "01M4TGD31WXH97JAA5S6A4R10D",
                                     "node_token": "node-token-value",
                                     "role": "compute"}))
        token_path = pathlib.Path(root) / "var/lib/termini/enrollment.token"
        token_path.write_text("previously-used-credential\n")
        proc = self._run(root, "--token", "a-fresh-credential-that-must-survive")
        self.assertIn("already registered", proc.stdout)
        self.assertIn("NOT used and is NOT consumed", proc.stdout)
        # The fresh credential was neither written nor spent, and the node keeps
        # the identity it registered with.
        self.assertEqual(token_path.read_text().strip(), "previously-used-credential")
        self.assertEqual(json.loads(ident.read_text())["node_id"],
                         "01M4TGD31WXH97JAA5S6A4R10D")

    def test_a_credential_file_is_read_instead_of_the_command_line(self):
        root = self._root()
        cred_file = pathlib.Path(root) / "credential.txt"
        cred_file.write_text("credential-from-a-file\n")
        self._run(root, "--token-file", str(cred_file), "--name", "node-y")
        written = (pathlib.Path(root) / "var/lib/termini/enrollment.token").read_text()
        self.assertEqual(written.strip(), "credential-from-a-file")


class FailureMessageTest(unittest.TestCase):
    """Acceptance (c): the three failure modes are distinct and actionable."""

    def _explain(self, state: dict) -> subprocess.CompletedProcess:
        tmp = tempfile.mkdtemp(prefix="termini-state-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return subprocess.run([sys.executable, str(STATUS_HELPER), "--explain-state", path],
                              capture_output=True, text=True, timeout=60)

    def test_explain_state_distinguishes_the_three_failure_modes(self):
        cases = {
            "credential_rejected": (
                {"state": "REGISTRATION_FAILED", "failure_kind": "auth",
                 "last_error": "register failed: HTTP 401 UNAUTHENTICATED: unknown bootstrap token",
                 "registration_attempts": 4},
                3, "enrollment credential was rejected"),
            "credential_already_used": (
                {"state": "REGISTRATION_FAILED", "failure_kind": "auth",
                 "last_error": "register failed: HTTP 401 UNAUTHENTICATED: bootstrap token already used",
                 "registration_attempts": 4},
                4, "already been used"),
            "controller_unreachable": (
                {"state": "REGISTRATION_FAILED", "failure_kind": "unreachable_dns",
                 "last_error": "register unreachable (DNS name could not be resolved)",
                 "registration_attempts": 4},
                5, "could not be reached"),
        }
        messages = {}
        for name, (state, code, needle) in cases.items():
            proc = self._explain(state)
            self.assertEqual(proc.returncode, code, f"{name}: {proc.stdout}{proc.stderr}")
            self.assertIn(needle, proc.stderr + proc.stdout, name)
            messages[name] = (proc.stdout + proc.stderr).strip()
        self.assertEqual(len(set(messages.values())), 3, messages)

    def test_explain_state_reports_a_healthy_node(self):
        proc = self._explain({"state": "ONLINE"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- 3. real registration

class ClientEnrollmentEndToEndTest(_ControllerCase):
    """A node registers through the customer's credential, then reads itself back."""

    def _installer(self, root: str, token: str):
        return subprocess.run(
            ["sh", str(INSTALLER), "--root", root, "--no-start",
             "--controller", self.base, "--token", token, "--name", "client-node-1"],
            capture_output=True, text=True, timeout=180)

    def _daemon_env(self, root: str) -> dict:
        env = dict(os.environ)
        env.update({
            "TERMINI_CONFIG": os.path.join(root, "etc/termini/config.yaml"),
            "TERMINI_TOKEN_FILE": os.path.join(root, "var/lib/termini/enrollment.token"),
            "TERMINI_IDENTITY_DIR": os.path.join(root, "var/lib/termini/identity"),
            "TERMINI_STATE_PATH": os.path.join(root, "var/lib/termini/state.json"),
            "TERMINI_SPOOL_DIR": os.path.join(root, "var/lib/termini/telemetry"),
        })
        return env

    def _start_daemon(self, root: str):
        env = self._daemon_env(root)
        proc = subprocess.Popen([sys.executable, str(DAEMON)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self._kill, proc)
        state_path = env["TERMINI_STATE_PATH"]
        deadline = time.time() + 45
        state = {}
        while time.time() < deadline:
            try:
                with open(state_path, encoding="utf-8") as fh:
                    state = json.load(fh)
            except (OSError, json.JSONDecodeError):
                state = {}
            if state.get("state") in ("ONLINE", "REGISTRATION_FAILED"):
                break
            time.sleep(0.25)
        return proc, state, env

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_credential_registers_the_node_and_the_client_reads_it_back(self):
        root = tempfile.mkdtemp(prefix="termini-client-node-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        token_1 = self._mint(2)[0]

        proc = self._installer(root, token_1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        daemon, state, env = self._start_daemon(root)
        self.assertEqual(state.get("state"), "ONLINE", state)

        # The credential is spent exactly once, by this registration.
        self.assertIsNotNone(self._token_state(token_1))

        # ... and the client's own view works with the NODE's credential only.
        show = subprocess.run([sys.executable, str(STATUS_HELPER), "--json"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(show.returncode, 0, show.stdout + show.stderr)
        view = json.loads(show.stdout)
        node_id = view["identity"]["node_id"]
        self.assertEqual(view["identity"]["project_id"], PROJECT_ID)
        self.assertTrue(view["identity"]["node_token"])
        self.assertTrue(view["samples"], "the node must have streamed a first sample")
        # The client's view is scoped to its own node: the same call for another
        # node is refused, and the project token was never needed to make either.
        status, _ = self._get_json(f"/v1/nodes/{node_id}/telemetry/recent",
                                   token=view["identity"]["node_token"])
        self.assertEqual(status, 200)

        with db.session(self.db_path) as conn:
            nodes = store.list_nodes(conn, PROJECT_ID)
        self.assertEqual([n["id"] for n in nodes], [node_id])

    def test_rerun_reuses_the_identity_and_burns_no_second_credential(self):
        """Acceptance (b): a re-run must not consume a second enrollment credential."""
        root = tempfile.mkdtemp(prefix="termini-client-node-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        token_1, token_2 = self._mint(2)

        first = self._installer(root, token_1)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        daemon, state, env = self._start_daemon(root)
        self.assertEqual(state.get("state"), "ONLINE", state)
        self._kill(daemon)
        node_id = json.loads(pathlib.Path(env["TERMINI_IDENTITY_DIR"], "node.json").read_text())["node_id"]

        # Re-run WITH a fresh credential: the node is already registered, so the
        # installer must reuse the identity and leave the credential untouched.
        second = self._installer(root, token_2)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already registered", second.stdout)
        self.assertIn("NOT used and is NOT consumed", second.stdout)
        self.assertIsNone(self._token_state(token_2),
                          "the re-run consumed a second enrollment credential")

        # The credential that was NOT consumed is still good for a DIFFERENT
        # machine, which is the end-to-end proof that nothing burned it.
        third = subprocess.run(
            ["sh", str(INSTALLER), "--root", root + "-second", "--no-start",
             "--controller", self.base, "--token", token_2, "--name", "client-node-2"],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        second_root = root + "-second"
        self.addCleanup(shutil.rmtree, second_root, ignore_errors=True)
        daemon2, state2, env2 = self._start_daemon(second_root)
        self.assertEqual(state2.get("state"), "ONLINE", state2)
        node_id_2 = json.loads(
            pathlib.Path(env2["TERMINI_IDENTITY_DIR"], "node.json").read_text())["node_id"]
        self.assertNotEqual(node_id, node_id_2)
        self.assertIsNotNone(self._token_state(token_2))


# --------------------------------------------------------------------------- 4. provider-side handover

class IssueEnrollmentTest(_ControllerCase):
    """The provider-side handover: mint, print the customer command, leak nothing."""

    def _pt_file(self) -> str:
        path = os.path.join(self.tmp, "project-token.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PROJECT_TOKEN + "\n")
        os.chmod(path, 0o600)
        return path

    def _run(self, *args, env_extra=None, expect_ok=True):
        env = dict(os.environ)
        env.update(env_extra or {})
        proc = subprocess.run([sys.executable, str(ISSUE_ENROLLMENT), *args],
                              capture_output=True, text=True, timeout=120, env=env)
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_handover_names_a_real_artifact_and_never_the_project_token(self):
        out = os.path.join(self.tmp, "handover.txt")
        proc = self._run("--public-url", self.base, "--controller", self.base,
                         "--project-token-file", self._pt_file(),
                         "--name", "node-d", "--count", "2", "--out", out)
        self.assertIn("install surface OK", proc.stdout)
        self.assertIn("/install/termini-node.sh", proc.stdout)
        self.assertEqual(proc.stdout.count("--- Termini node enrollment"), 2)
        self.assertNotIn(PROJECT_TOKEN, proc.stdout)
        self.assertNotIn("demo-project-token", proc.stdout)
        handover = pathlib.Path(out).read_text()
        self.assertNotIn(PROJECT_TOKEN, handover)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o600")
        # The command must name the PUBLIC url the customer can reach, and pass
        # the credential and the controller explicitly.
        self.assertIn(f"curl -fsSL {self.base}/install/termini-node.sh | sudo sh -s -- \\", handover)
        self.assertIn(f"--controller {self.base} --token ", handover)
        # Distinct, single-use credentials per node.
        creds = extract_credentials(handover)
        self.assertEqual(len(creds), 2)
        self.assertEqual(len(set(creds)), 2)

    def test_the_handed_out_credential_really_registers_a_node(self):
        out = os.path.join(self.tmp, "handover.txt")
        self._run("--public-url", self.base, "--controller", self.base,
                  "--project-token-file", self._pt_file(), "--name", "node-e",
                  "--out", out)
        credential = extract_credentials(pathlib.Path(out).read_text())[0]
        # An unused, unknown credential authenticates the recording path only when
        # it registers; the minted row must be real and unconsumed.
        self.assertIsNone(self._token_state(credential))
        body = {
            "schema_version": "1.0",
            "machine": {"machine_id": "client-handover-node", "hostname": "node-e",
                        "cpu": {"model": "x", "cores": 2, "threads": 2},
                        "memory": {"total_bytes": 2 * 1024 ** 3},
                        "disks": [], "gpus": []},
        }
        req = urllib.request.Request(
            self.base + "/v1/nodes/register", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + credential},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            self.assertEqual(r.status, 201)
        self.assertIsNotNone(self._token_state(credential))

    def test_refuses_to_hand_over_without_the_project_token(self):
        proc = self._run("--public-url", self.base, "--controller", self.base,
                         "--project-token-file", os.path.join(self.tmp, "nope"),
                         expect_ok=False)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("project token", proc.stderr.lower())

    def test_refuses_a_handover_when_the_public_url_has_no_install_surface(self):
        proc = self._run("--public-url", "http://127.0.0.1:1",
                         "--controller", self.base,
                         "--project-token-file", self._pt_file(),
                         expect_ok=False)
        self.assertEqual(proc.returncode, 5)
        self.assertIn("install surface", proc.stderr)


def extract_credentials(handover_text: str) -> list[str]:
    """Pull the bare credential lines out of a handover block."""
    import re
    return re.findall(r"^  ([A-Za-z0-9_\-]{16,})$", handover_text, re.M)


if __name__ == "__main__":
    unittest.main()
