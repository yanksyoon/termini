"""Tests for the controller's LXD mTLS client-trust wiring (`app/lxdtrust.py`).

The defect being fixed is a *transport* one — an untrusted client gets
`403 Forbidden` from `GET /1.0/instances` — so the load-bearing test here is a
real **mutual-TLS** stub LXD server: it requests the client certificate, keeps a
trust set of SHA-256 fingerprints, answers 403 for an unknown client and 200 once
the client has consumed a trust token. That exercises the actual handshake +
`POST /1.0/certificates` path, not a mock over plain HTTP.

What still needs a live node: `lxc config trust add` token minting on the node
side (tested for parsing only — see `test_bootstrap_trust_token.py`) and the real
`lxd init`-initialised daemon. See the PR body for the split.

Requires the `openssl` binary (used to generate the client identity + the stub
server cert); the classes that need it skip cleanly when it is absent.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app import lxdtrust  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402

HAS_OPENSSL = shutil.which("openssl") is not None
requires_openssl = unittest.skipUnless(HAS_OPENSSL, "openssl binary required")


# --------------------------------------------------------------------------- stub LXD
class _StubLxdServer(ThreadingHTTPServer):
    """A mutual-TLS LXD stub: 403 until the client cert is in the trust set."""

    daemon_threads = True

    def __init__(self, handler_cls, ssl_ctx, expected_secret, *, conflict_on_post=False):
        self.ssl_ctx = ssl_ctx
        self.trusted: set[str] = set()
        self.expected_secret = expected_secret
        self.conflict_on_post = conflict_on_post
        self.requests: list[tuple[str, str, dict | None]] = []
        self.cert_posts = 0
        super().__init__(("127.0.0.1", 0), handler_cls)

    def get_request(self):  # wrap each accepted connection in TLS
        sock, addr = super().get_request()
        return self.ssl_ctx.wrap_socket(sock, server_side=True), addr

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _StubLxdServer  # type: ignore[assignment]

    def log_message(self, *args):  # silence
        return

    def _peer_fp(self) -> str | None:
        cert = self.connection.getpeercert(binary_form=True)
        return hashlib.sha256(cert).hexdigest() if cert else None

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n)) if n > 0 else None

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.server.requests.append(("GET", self.path, None))
        if self.path == "/1.0/instances":
            fp = self._peer_fp()
            if fp and fp in self.server.trusted:
                self._send(200, {"type": "sync", "metadata": []})
            else:
                self._send(403, {"error": "Forbidden", "error_code": 403})
        elif self.path == "/1.0":
            self._send(200, {"type": "sync", "metadata": {"api": "1.0"}})
        else:
            self._send(404, {"error": "not found", "error_code": 404})

    def do_POST(self):  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, body))
        if self.path != "/1.0/certificates":
            self._send(404, {"error": "not found", "error_code": 404})
            return
        self.server.cert_posts += 1
        if self.server.conflict_on_post:
            self._send(409, {"error": "Certificate already in the trust store",
                             "error_code": 409})
            return
        secret = (body or {}).get("password")
        if secret and secret == self.server.expected_secret:
            fp = self._peer_fp()
            if fp:
                self.server.trusted.add(fp)
            self._send(200, {"type": "sync", "metadata": {}})
        else:
            self._send(403, {"error": "Forbidden", "error_code": 403})


def _openssl_server_cert(cert: str, key: str) -> None:
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec",
         "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=stub-lxd",
         "-addext", "extendedKeyUsage=serverAuth"],
        check=True, capture_output=True,
    )


class _StubHarness(unittest.TestCase):
    """Base: temp dir, a client identity, and a running TLS stub LXD."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def _identity(self, name: str = "test-ctrl") -> lxdtrust.LxdIdentity:
        return lxdtrust.ensure_identity(
            str(self.dir / f"{name}.crt"), str(self.dir / f"{name}.key"),
            common_name=name,
        )

    def _stub(self, client_ca: str, expected_secret: str = "tok-1", *,
              conflict_on_post: bool = False):
        """Start a TLS stub LXD that requests the client certificate.

        Real LXD asks for (but does not chain-verify) the client cert and
        validates it by SHA-256 fingerprint against its trust store. Python's
        `ssl` has no "request but don't verify" mode, so the stub loads the
        client's self-signed cert as a trust anchor with
        `VERIFY_X509_PARTIAL_CHAIN` — a stub artifact; the app-level 200/403
        logic (fingerprint vs trust set) is what is under test.
        """
        if not HAS_OPENSSL:
            self.skipTest("openssl required")
        scert, skey = str(self.dir / "srv.crt"), str(self.dir / "srv.key")
        _openssl_server_cert(scert, skey)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=scert, keyfile=skey)
        ctx.load_verify_locations(cafile=client_ca)
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.verify_mode = ssl.CERT_OPTIONAL
        srv = _StubLxdServer(_Handler, ctx, expected_secret,
                             conflict_on_post=conflict_on_post)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv


# --------------------------------------------------------------------------- identity
@requires_openssl
class IdentityTest(_StubHarness):
    def test_generates_then_reuses_without_rotation(self):
        cert = str(self.dir / "c.crt")
        key = str(self.dir / "c.key")
        first = lxdtrust.ensure_identity(cert, key)
        self.assertTrue(first.available)
        self.assertEqual(len(first.fingerprint), 64)
        self.assertEqual(first.fingerprint, first.fingerprint.lower())
        self.assertTrue(set(first.fingerprint) <= set("0123456789abcdef"))
        # the private key is locked down
        self.assertEqual(0o600, (self.dir / "c.key").stat().st_mode & 0o777)

        before = pathlib.Path(cert).read_bytes()
        again = lxdtrust.ensure_identity(cert, key)
        # A restart reuses the SAME identity (no silent rotation).
        self.assertEqual(first.fingerprint, again.fingerprint)
        self.assertEqual(before, pathlib.Path(cert).read_bytes())

    def test_identity_is_usable_as_tls_client_cert(self):
        ident = self._identity()
        ctx = LxdExecutor._make_ssl_ctx(ident.cert_path, ident.key_path)
        self.assertIsInstance(ctx, ssl.SSLContext)
        # no server pin supplied -> demo-parity skip-verify
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_server_ca_pins_the_server(self):
        ident = self._identity()
        ctx = LxdExecutor._make_ssl_ctx(ident.cert_path, ident.key_path, server_ca=ident.cert_path)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_no_cert_no_ca_is_none(self):
        self.assertIsNone(LxdExecutor._make_ssl_ctx(None, None))
        self.assertIsNone(LxdExecutor._make_ssl_ctx(None, None, server_ca=None))

    def test_fingerprint_matches_openssl_sha256_of_der(self):
        ident = self._identity()
        self.assertEqual(
            hashlib.sha256(
                ssl.PEM_cert_to_DER_cert(pathlib.Path(ident.cert_path).read_text())
            ).hexdigest(),
            ident.fingerprint,
        )

    def test_incomplete_identity_is_regenerated(self):
        cert = self.dir / "half.crt"
        key = self.dir / "half.key"
        ident = lxdtrust.ensure_identity(str(cert), str(key))
        key.unlink()  # key lost, cert stranded -> mint a fresh pair
        fresh = lxdtrust.ensure_identity(str(cert), str(key))
        self.assertTrue(fresh.available)
        self.assertNotEqual(ident.fingerprint, fresh.fingerprint)

    def test_resolve_identity_honours_explicit_then_provision_flag(self):
        self.assertIsNone(lxdtrust.resolve_identity(
            explicit_cert=None, explicit_key=None,
            client_dir=str(self.dir / "d"), provision=False,
        ))
        ident = lxdtrust.resolve_identity(
            explicit_cert=None, explicit_key=None, client_dir=str(self.dir / "d"),
        )
        self.assertTrue((self.dir / "d" / lxdtrust.CLIENT_CERT_FILE).is_file())
        self.assertTrue(ident.available)

    def test_identity_dir_default_is_beside_the_db(self):
        self.assertEqual("data/lxd-client",
                         lxdtrust.identity_dir_from_db("data/controller.db"))


# --------------------------------------------------------------------------- trust
@requires_openssl
class TrustTest(_StubHarness):
    def test_probe_reports_403_before_and_200_after_trust(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1")

        before = lxdtrust.probe_trusted(srv.base_url, ident)
        self.assertFalse(before.trusted)
        self.assertEqual(before.status, 403)

        res = lxdtrust.ensure_trusted(srv.base_url, ident, "tok-1")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(res.status, "trusted")

        after = lxdtrust.probe_trusted(srv.base_url, ident)
        self.assertTrue(after.trusted)
        self.assertEqual(after.status, 200)

    def test_ensure_trusted_is_idempotent_no_second_post(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1")
        first = lxdtrust.ensure_trusted(srv.base_url, ident, "tok-1")
        self.assertTrue(first.ok, first.detail)
        self.assertEqual(srv.cert_posts, 1)

        second = lxdtrust.ensure_trusted(srv.base_url, ident, "tok-1")
        self.assertTrue(second.ok)
        self.assertEqual(second.status, "already_trusted")
        self.assertEqual(srv.cert_posts, 1)  # no duplicate POST

    def test_wrong_secret_does_not_trust(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "right")
        res = lxdtrust.ensure_trusted(srv.base_url, ident, "wrong")
        self.assertFalse(res.ok)
        self.assertEqual(srv.cert_posts, 1)
        self.assertFalse(lxdtrust.probe_trusted(srv.base_url, ident).trusted)

    def test_409_already_present_is_folded_to_ok(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1", conflict_on_post=True)
        res = lxdtrust.ensure_trusted(srv.base_url, ident, "tok-1", probe_before=False)
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "already_trusted")

    def test_missing_secret_is_error_without_network(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1")
        res = lxdtrust.ensure_trusted(srv.base_url, ident, None)
        self.assertFalse(res.ok)
        self.assertEqual(srv.cert_posts, 0)

    def test_unreachable_node_is_error_not_a_blind_post(self):
        ident = self._identity()
        res = lxdtrust.ensure_trusted("https://127.0.0.1:1", ident, "tok-1", timeout_s=1.0)
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "error")

    def test_trust_client_and_list_instances_primitives(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1")
        ex = LxdExecutor(srv.base_url, client_cert=ident.cert_path,
                         client_key=ident.key_path, retry_max=1)
        self.assertEqual(ex.list_instances().status, 403)
        r = ex.trust_client("tok-1")
        self.assertEqual(r.result, "ok")
        self.assertEqual(ex.list_instances().status, 200)


# --------------------------------------------------------------------------- CLI
@requires_openssl
class CliTest(_StubHarness):
    def test_verify_exit_codes(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1")
        args = ["--key", ident.key_path, "--cert", ident.cert_path]
        rc = lxdtrust.main(["verify", *args, "--node", srv.base_url])
        self.assertEqual(rc, 3)  # untrusted
        lxdtrust.main(["trust", *args, "--node", srv.base_url, "--token", "tok-1"])
        rc = lxdtrust.main(["verify", *args, "--node", srv.base_url])
        self.assertEqual(rc, 0)  # trusted

    def test_cert_command_is_idempotent(self):
        d = str(self.dir / "cli")
        rc1 = lxdtrust.main(["cert", "--dir", d])
        rc2 = lxdtrust.main(["cert", "--dir", d])
        self.assertEqual((rc1, rc2), (0, 0))

    def test_trust_requires_a_secret(self):
        ident = self._identity()
        srv = self._stub(ident.cert_path, "tok-1")
        rc = lxdtrust.main(["trust", "--cert", ident.cert_path, "--key", ident.key_path,
                            "--node", srv.base_url])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
