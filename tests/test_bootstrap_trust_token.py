"""Node-side LXD trust-token minting (`termini-bootstrap mint-trust-token`).

Unit-level only: `lxc config trust add` needs a live, initialised LXD daemon, so
`subprocess.run` is replaced with a fake. What is verified here is the parsing of
the printed CertificateAddToken, the success/failure contract, and that the token
is written 0600 to the well-known path the controller reads.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import tempfile
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DAEMON = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"

# A plausible base64 CertificateAddToken (base64 of a small JSON blob).
TOKEN = ("eyJjbGllbnRfbmFtZSI6InRlcm1pbmktY29udHJvbGxlciIsImZpbmdlcnByaW50Ijoi"
         "YWJjZGVmIiwiYWRkcmVzc2VzIjpbIjEwLjEwLjAuMTE6ODQ0MyJdLCJzZWNyZXQiOiJz"
         "ZWNyZXQifQ==")


def load_daemon(env: dict[str, str]):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("termini_bootstrap_trust_test", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _CompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSubprocess:
    """Stand-in for the daemon module's ``subprocess`` reference (never the real one)."""

    SubprocessError = subprocess.SubprocessError

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple] = []

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.tok_path = str(self.root / "lxd-trust-token")
        self.mod = load_daemon({"TERMINI_LXD_TRUST_TOKEN_PATH": self.tok_path})


class TestExtract(_Base):
    def test_picks_longest_base64_run_and_ignores_noise(self):
        out = f"Please note this token.\n{TOKEN}\n"
        self.assertEqual(self.mod.extract_trust_token(out), TOKEN)

    def test_none_when_no_token(self):
        self.assertIsNone(self.mod.extract_trust_token(""))
        self.assertIsNone(self.mod.extract_trust_token("short"))


class TestMint(_Base):
    def test_success_returns_token(self):
        self.mod.subprocess = _FakeSubprocess(_CompletedProcess(0, stdout=TOKEN + "\n"))
        self.assertEqual(self.mod.mint_lxd_trust_token("termini-controller"), TOKEN)
        argv = self.mod.subprocess.calls[0][0][0]
        self.assertEqual(argv[:4], ["lxc", "config", "trust", "add"])
        self.assertIn("--name", argv)

    def test_nonzero_exit_returns_none(self):
        self.mod.subprocess = _FakeSubprocess(
            _CompletedProcess(1, stderr="Error: LXD is not initialised"))
        self.assertIsNone(self.mod.mint_lxd_trust_token())

    def test_missing_binary_returns_none(self):
        self.mod.subprocess = _FakeSubprocess(FileNotFoundError("lxc"))
        self.assertIsNone(self.mod.mint_lxd_trust_token())

    def test_cmd_writes_token_0600_and_prints(self):
        self.mod.subprocess = _FakeSubprocess(_CompletedProcess(0, stdout=TOKEN + "\n"))
        rc = self.mod.cmd_mint_trust_token(["termini-bootstrap", "mint-trust-token",
                                            "--out", self.tok_path])
        self.assertEqual(rc, 0)
        p = pathlib.Path(self.tok_path)
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_text().strip(), TOKEN)
        self.assertEqual(0o600, p.stat().st_mode & 0o777)

    def test_cmd_failure_is_nonzero_and_writes_nothing(self):
        self.mod.subprocess = _FakeSubprocess(_CompletedProcess(1, stderr="boom"))
        rc = self.mod.cmd_mint_trust_token(["termini-bootstrap", "mint-trust-token",
                                            "--out", self.tok_path])
        self.assertEqual(rc, 1)
        self.assertFalse(pathlib.Path(self.tok_path).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
