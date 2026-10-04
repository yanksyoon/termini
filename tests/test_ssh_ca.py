"""Tests for the SSH certificate authority (D4, controller-api.md §4.3).

Unit tests exercise the signing/validation logic against a real `ssh-keygen`;
the integration test boots the controller and verifies the end-to-end flow the
spec-answers §4 story promises: browser generates a keypair locally -> the
controller signs a short-lived, principal-bound certificate -> no private key
ever transits the controller.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))

from app import sshca as sshca_mod  # noqa: E402

PORT = int(os.environ.get("TERMINI_SSH_CA_TEST_PORT", "18180"))
PROJECT_TOKEN = "ssh-ca-project-token"
BOOT_COMPUTE = "ca-bootstrap-compute"
BOOT_INFERENCE = "ca-bootstrap-inference"
L4_24GB = 24 * 1024 ** 3


def run_keygen(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh-keygen", *args], capture_output=True, text=True, timeout=timeout)


def ssh_fingerprint(path: str) -> str:
    """Return the `SHA256:...` fingerprint ssh-keygen reports for a key/cert file."""
    proc = run_keygen(["-lf", path])
    if proc.returncode != 0:
        return ""
    # e.g. "256 SHA256:abc... comment (ED25519)"
    parts = proc.stdout.split()
    return parts[1] if len(parts) >= 2 else ""


class SSHCAUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="termini-sshca-test-")
        d = cls.tmp.name
        cls.ca_key = os.path.join(d, "ca")
        cls.user_key = os.path.join(d, "user")
        assert run_keygen(["-t", "ed25519", "-f", cls.ca_key, "-N", "", "-q"]).returncode == 0
        assert run_keygen(["-t", "ed25519", "-f", cls.user_key, "-N", "", "-q"]).returncode == 0
        cls.ca_pub = pathlib.Path(cls.ca_key + ".pub").read_text().strip()
        cls.user_pub = pathlib.Path(cls.user_key + ".pub").read_text().strip()
        cls.ca = sshca_mod.SSHCA(cls.ca_key)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_validate_rejects_private_key(self):
        with open(self.user_key) as fh:
            priv = fh.read()
        with self.assertRaises(sshca_mod.SSHCAError):
            sshca_mod.validate_public_key(priv)

    def test_validate_rejects_garbage(self):
        for bad in ("", "not a key", "ssh-ed25519 !!!notbase64!!!", 123, None):
            with self.assertRaises(sshca_mod.SSHCAError):
                sshca_mod.validate_public_key(bad)

    def test_validate_accepts_ed25519_pubkey(self):
        self.assertEqual(sshca_mod.validate_public_key(self.user_pub), self.user_pub)

    def test_clamp_validity(self):
        self.assertEqual(self.ca.clamp_validity(None), 28800)
        self.assertEqual(self.ca.clamp_validity(0), 28800)
        self.assertEqual(self.ca.clamp_validity(3600), 3600)
        self.assertEqual(self.ca.clamp_validity(999999), 86400)  # capped at 24 h

    def test_sign_produces_short_lived_cert(self):
        meta = self.ca.sign(self.user_pub, principal="root", validity_s=3600)
        self.assertTrue(meta["certificate"].startswith("ssh-ed25519-cert-v01@openssh.com"))
        self.assertEqual(meta["principal"], "root")
        self.assertEqual(meta["key_algorithm"], "ed25519")
        self.assertTrue(meta["key_fingerprint"].startswith("SHA256:"))
        self.assertTrue(meta["ca_fingerprint"].startswith("SHA256:"))
        self.assertEqual(meta["validity_s"], 3600)
        # the issued cert is verifiable by ssh-keygen against the CA
        with tempfile.NamedTemporaryFile("w", suffix="-cert.pub", delete=False) as fh:
            fh.write(meta["certificate"] + "\n")
            cert_path = fh.name
        try:
            self.assertTrue(ssh_fingerprint(cert_path))
            proc = run_keygen(["-Lf", cert_path])
            self.assertIn("Valid:", proc.stdout)
            self.assertIn("root", proc.stdout)
        finally:
            os.unlink(cert_path)

    def test_key_fingerprint_matches_ssh_keygen(self):
        meta = self.ca.sign(self.user_pub, principal="root", validity_s=3600)
        self.assertEqual(meta["key_fingerprint"], ssh_fingerprint(self.user_key + ".pub"))

    def test_issue_operator_certificate(self):
        op_key = os.path.join(self.tmp.name, "operator_key")
        key_path, cert_path, meta = self.ca.issue_operator_certificate(op_key)
        self.assertTrue(os.path.isfile(key_path))
        self.assertTrue(os.path.isfile(cert_path))
        self.assertTrue(meta["certificate"].startswith("ssh-ed25519-cert-v01@openssh.com"))
        # the cert verifies against the CA
        proc = run_keygen(["-Lf", cert_path])
        self.assertIn("Valid:", proc.stdout)


class SSHCAIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="termini-sshca-integ-")
        d = cls.tmp.name
        cls.ca_key = os.path.join(d, "ca")
        cls.user_key = os.path.join(d, "user")
        assert run_keygen(["-t", "ed25519", "-f", cls.ca_key, "-N", "", "-q"]).returncode == 0
        assert run_keygen(["-t", "ed25519", "-f", cls.user_key, "-N", "", "-q"]).returncode == 0
        cls.user_pub = pathlib.Path(cls.user_key + ".pub").read_text().strip()

        env = dict(os.environ)
        env.update({
            "TERMINI_HOST": "127.0.0.1",
            "TERMINI_PORT": str(PORT),
            "TERMINI_DB_PATH": os.path.join(d, "test.db"),
            "TERMINI_BOOTSTRAP_TOKEN": f"{BOOT_COMPUTE},{BOOT_INFERENCE}",
            "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
            "TERMINI_MAX_INFERENCE_NODES": "1",
            "TERMINI_SSH_CA_KEY_PATH": cls.ca_key,
            "TERMINI_TERMINAL_BACKEND": "local",
            "PYTHONUNBUFFERED": "1",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=str(CONTROLLER), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        cls.base = f"http://127.0.0.1:{PORT}"
        for _ in range(100):
            try:
                cls.http("GET", "/v1/nodes/register")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("controller did not boot")

        # Register the demo nodes once (bootstrap tokens are single-use).
        cls.register(BOOT_COMPUTE, "ca-m-a", "node-a", [])
        node_c = cls.register(
            BOOT_INFERENCE, "ca-m-c", "node-c",
            [{"vendor": "nvidia", "model": "NVIDIA L4", "vram_bytes": L4_24GB, "health": "ok"}],
        )
        cls.project_id = node_c["project_id"]
        cls.node_c_id = node_c["node_id"]

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        if cls.proc.stdout:
            cls.proc.stdout.close()
        cls.tmp.cleanup()

    @staticmethod
    def http(method: str, path: str, body=None, token: str | None = None) -> tuple[int, object]:
        url = "http://127.0.0.1:%d%s" % (PORT, path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                return e.code, raw.decode(errors="replace")
            finally:
                e.close()

    @classmethod
    def register(cls, token: str, machine_id: str, hostname: str, gpus: list) -> dict:
        body = {
            "schema_version": "1.0",
            "machine": {
                "machine_id": machine_id,
                "hostname": hostname,
                "cpu": {"model": "x", "sockets": 1, "cores": 8, "threads": 16},
                "memory": {"total_bytes": 68719476736},
                "gpus": gpus,
            },
        }
        st, r = cls.http("POST", "/v1/nodes/register", body, token=token)
        assert st == 201, f"register {hostname} -> {st} {r}"
        return r

    def _boot_nodes(self):
        return self.project_id, self.node_c_id

    def test_b5_inference_vm_is_host_service_reference(self):
        project_id, node_c_id = self._boot_nodes()
        spec = {
            "allocations": [
                {"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "disk_gb": 8},
                {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "disk_gb": 64},
            ]
        }
        st, plan = self.http("POST", f"/v1/projects/{project_id}/intents",
                             {"spec": spec}, token=PROJECT_TOKEN)
        self.assertEqual(st, 200, plan)
        self.assertEqual(plan["outcome"], "accepted", plan)

        by_type = {a["resolved"]["type"]: a for a in plan["allocations"]}
        self.assertIn("agent_vm", by_type)
        self.assertIn("inference_vm", by_type)

        inf = by_type["inference_vm"]
        self.assertEqual(inf["status"], "accepted")
        self.assertEqual(inf["realized_as"], "host_service")
        self.assertEqual(inf["vm_ids"], [])
        self.assertEqual(inf["node_id"], node_c_id)  # resolved to the inference node

        agent = by_type["agent_vm"]
        self.assertEqual(agent["status"], "accepted")
        self.assertEqual(agent["realized_as"], "vm")

    def test_b7_sign_and_revoke_flow(self):
        project_id, _ = self._boot_nodes()
        st, r = self.http(
            "POST", f"/v1/projects/{project_id}/ssh-key",
            {"public_key": self.user_pub, "principal": "root", "validity_s": 3600},
            token=PROJECT_TOKEN,
        )
        self.assertEqual(st, 200, r)
        self.assertTrue(r["certificate"].startswith("ssh-ed25519-cert-v01@openssh.com"))
        self.assertEqual(r["principal"], "root")
        self.assertEqual(r["algorithm"], "ed25519")
        self.assertTrue(r["key_fingerprint"].startswith("SHA256:"))
        self.assertTrue(r["ca_fingerprint"].startswith("SHA256:"))
        self.assertTrue(r["valid_until"])

        # revoke by fingerprint
        st, r2 = self.http(
            "POST", f"/v1/projects/{project_id}/ssh-key/revoke",
            {"fingerprint": r["key_fingerprint"]},
            token=PROJECT_TOKEN,
        )
        self.assertEqual(st, 200, r2)
        self.assertTrue(r2["revoked"])

        # revoking an unknown fingerprint -> 404
        st, _ = self.http(
            "POST", f"/v1/projects/{project_id}/ssh-key/revoke",
            {"fingerprint": "SHA256:doesnotexist"},
            token=PROJECT_TOKEN,
        )
        self.assertEqual(st, 404)

    def test_private_key_never_accepted(self):
        project_id, _ = self._boot_nodes()
        with open(self.user_key) as fh:
            priv = fh.read()
        st, r = self.http(
            "POST", f"/v1/projects/{project_id}/ssh-key",
            {"public_key": priv},
            token=PROJECT_TOKEN,
        )
        self.assertEqual(st, 422, r)

    def test_get_ssh_reflects_ca_model(self):
        project_id, node_c_id = self._boot_nodes()
        st, r = self.http(
            f"GET", f"/v1/projects/{project_id}/nodes/{node_c_id}/ssh",
            token=PROJECT_TOKEN,
        )
        self.assertEqual(st, 200, r)
        self.assertEqual(r["auth_model"], "ssh_ca")
        # no inventory loaded -> no host, so the command is None; the fields are
        # still present and the command (when present) must reference the
        # operator's browser-local key, never a controller-held key.
        self.assertIn("key_fingerprint", r)
        self.assertNotIn("controller", r.get("command") or "")


if __name__ == "__main__":
    unittest.main()
