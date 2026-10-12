"""Controller -> node LXD mutual-TLS trust wiring.

LXD authenticates every REST client by the TLS client certificate it presents
against the node's *trust store* (`lxc config trust list`). The controller's
`LxdExecutor` has always been *able* to present a client certificate
(`_make_ssl_ctx` -> `load_cert_chain`), but nothing in the controller ever
**minted** one or got it **into a node's trust store**. Measured on the deployed
stack, an untrusted client gets `GET /1.0` -> 200 but `GET /1.0/instances` ->
`403 {"error":"Forbidden"}` — that 403 is the defect this module fixes.

Two halves, matching `docs/contracts/node-bootstrap.md` §5/§6.3:

  1. **Identity** (`ensure_identity` / `resolve_identity`): make sure the
     controller has a client cert + key on disk, reusing whatever is already
     there so a restart never rotates the controller's LXD identity. The path
     defaults to `<db_dir>/lxd-client/` — the same data directory the SQLite
     store lives in — so it is provisioned once and persists.
  2. **Trust** (`ensure_trusted` / `probe_trusted`): add that certificate to a
     node's LXD trust store over the network, idempotently.

Trust mechanism (chosen — one mechanism, not a menu)
----------------------------------------------------
The node's LXD REST API exposes an *unauthenticated* `POST /1.0/certificates`
for exactly this purpose: a client presents its own certificate in the TLS
handshake and a **secret** in the request body, and LXD adds the presented
certificate to the trust store. The body field is `password`; LXD accepts in it
**either**:

  * a *certificate-add trust token* (the base64 `CertificateAddToken` printed by
    `lxc config trust add --name …` on the node — the recommended, one-time,
    self-expiring flow), **or**
  * the node's `core.trust_password` (the legacy shared-secret flow).

Both are consumed identically, so the controller has ONE code path
(`LxdExecutor.trust_client` -> `POST /1.0/certificates {"type":"client","password":…}`)
and the node side decides which secret it hands out.

`password` (not the newer `trust_token` field) is used deliberately: the node
image pins LXD `5.21/stable`, where a token is consumed through `password`; the
field is still accepted (deprecated) on 6.x. See
`docs/contracts/node-bootstrap.md` §5 and the LXD rest-api reference for
`POST /1.0/certificates`.

Idempotency: `ensure_trusted` probes first (`GET /1.0/instances`, 200=trusted,
403/401=untrusted) and is a **no-op when already trusted** — the same
"re-running the same step is safe" rule the executor applies to a 404 on delete.
A 409/"already" reply from LXD is also folded to success.

Certificate generation — honest scope
-------------------------------------
Python's stdlib cannot generate or sign an X.509 certificate (no key generation,
no signing primitives), and the controller is deliberately a zero-third-party
dependency service (see `controller/app/db.py`). This module therefore shells
out to the `openssl` binary via `subprocess`, exactly as the rest of the
controller already does for crypto-adjacent work (`app/sshca.py` shells to
`ssh-keygen`, `app/inventory.py`/`app/terminal.py` shell out too). `openssl` is
present on the controller VM (Ubuntu). If it is missing, identity provisioning
is skipped with a clear log rather than silently producing an unusable identity.

The *fingerprint* (LXD's identity for a certificate: lowercase hex SHA-256 of
the DER) is computed in pure Python, so it needs no external tool.

Use from the shell::

    python3 -m app.lxdtrust cert
    python3 -m app.lxdtrust verify --node https://10.10.0.11:8443
    python3 -m app.lxdtrust trust  --node https://10.10.0.11:8443 --token <trust-token>
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .operator.executor import LxdExecutor

log = logging.getLogger("termini.ldxtrust")

# Default LXD trust-store entry name for the controller's client certificate.
CERT_DEFAULT_NAME = "termini-controller"
# File names inside the identity directory.
CLIENT_CERT_FILE = "controller-lxd-client.crt"
CLIENT_KEY_FILE = "controller-lxd-client.key"
# Subdirectory of the controller data dir (next to controller.db) by default.
DEFAULT_IDENTITY_SUBDIR = "lxd-client"

_PEM_CERT_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----",
    re.DOTALL,
)


# --------------------------------------------------------------------------- identity
@dataclass(frozen=True)
class LxdIdentity:
    """The controller's LXD client certificate identity on disk."""

    cert_path: str
    key_path: str
    fingerprint: str

    @property
    def available(self) -> bool:
        return bool(self.cert_path) and bool(self.key_path) \
            and Path(self.cert_path).is_file() and Path(self.key_path).is_file()


def fingerprint_of_pem_text(pem_text: str | bytes) -> str:
    """LXD's certificate identity: lowercase hex SHA-256 of the DER bytes.

    Reads the first PEM certificate block and hashes its DER form — no external
    tool, and exactly the value LXD shows in `lxc config trust list`.
    """
    raw = pem_text.encode() if isinstance(pem_text, str) else pem_text
    m = _PEM_CERT_RE.search(raw)
    if not m:
        raise ValueError("no PEM certificate block found")
    try:
        der = base64.b64decode(re.sub(rb"\s+", b"", m.group(1)), validate=True)
    except (binascii.Error, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid PEM/base64 in certificate: {exc}") from exc
    return hashlib.sha256(der).hexdigest()


def fingerprint_of_cert_file(cert_path: str) -> str:
    with open(cert_path, "rb") as fh:
        return fingerprint_of_pem_text(fh.read())


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def _openssl(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["openssl", *args], capture_output=True, text=True, timeout=timeout, check=False,
    )


def generate_self_signed_client_identity(
    cert_path: str,
    key_path: str,
    *,
    common_name: str = CERT_DEFAULT_NAME,
    days: int = 3650,
) -> None:
    """Write a fresh self-signed EC P-256 client certificate + key.

    The certificate is scoped to TLS client authentication (`extendedKeyUsage =
    clientAuth`), which is what LXD requires of a trust-store client cert. The
    key is written 0600 and the cert 0644.
    """
    if not openssl_available():
        raise RuntimeError(
            "openssl binary not found on PATH — cannot mint an LXD client "
            "certificate (Python stdlib cannot generate X.509). Install openssl "
            "or provision TERMINI_LXD_CLIENT_CERT/TERMINI_LXD_CLIENT_KEY."
        )
    for p in (cert_path, key_path):
        parent = Path(p).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
    proc = _openssl(
        "req", "-x509", "-newkey", "ec",
        "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-keyout", key_path, "-out", cert_path,
        "-days", str(int(days)),
        "-subj", f"/CN={common_name}",
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
        "-addext", "extendedKeyUsage=clientAuth",
    )
    if proc.returncode != 0:
        # Never leave a half-written key behind.
        for p in (cert_path, key_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise RuntimeError(f"openssl failed to generate client identity: {proc.stderr.strip()}")
    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)


def load_identity(cert_path: str, key_path: str) -> LxdIdentity | None:
    """Load an existing on-disk identity, or None if either half is missing."""
    if not cert_path or not key_path:
        return None
    if not (Path(cert_path).is_file() and Path(key_path).is_file()):
        return None
    try:
        fp = fingerprint_of_cert_file(cert_path)
    except (OSError, ValueError) as exc:
        log.warning("could not read LXD client certificate %s: %s", cert_path, exc)
        return None
    return LxdIdentity(cert_path=cert_path, key_path=key_path, fingerprint=fp)


def ensure_identity(
    cert_path: str,
    key_path: str,
    *,
    common_name: str = CERT_DEFAULT_NAME,
) -> LxdIdentity:
    """Reuse the identity at `cert_path`/`key_path`, generating it only if absent.

    Restarts therefore never rotate the controller's LXD identity. Raises
    RuntimeError if a fresh certificate must be minted and cannot be.
    """
    existing = load_identity(cert_path, key_path)
    if existing is not None:
        return existing
    # If only one half survived, mint a fresh pair (a cert without its key is
    # unusable, and a key without its cert is untrusted).
    if Path(cert_path).exists() or Path(key_path).exists():
        log.warning(
            "LXD client identity incomplete (cert=%s key=%s) — regenerating BOTH",
            Path(cert_path).is_file(), Path(key_path).is_file(),
        )
    generate_self_signed_client_identity(cert_path, key_path, common_name=common_name)
    ident = load_identity(cert_path, key_path)
    if ident is None:  # pragma: no cover - generate() validated the output
        raise RuntimeError("generated identity is unreadable")
    return ident


def identity_dir_from_db(db_path: str) -> str:
    """Default identity directory: `<db_dir>/lxd-client/` beside the SQLite file."""
    return str(Path(db_path).parent / DEFAULT_IDENTITY_SUBDIR)


def resolve_identity(
    *,
    explicit_cert: str | None,
    explicit_key: str | None,
    client_dir: str,
    common_name: str = CERT_DEFAULT_NAME,
    provision: bool = True,
) -> LxdIdentity | None:
    """Resolve the controller's LXD client identity, minting one if needed.

    An explicit `explicit_cert`/`explicit_key` (i.e. `TERMINI_LXD_CLIENT_CERT`)
    always wins and is never generated. Otherwise the identity lives under
    `client_dir`, generated once when absent. Returns None (rather than raising)
    when provisioning is disabled or impossible, so controller startup is never
    blocked by a missing openssl — the operator sees a warning.
    """
    if explicit_cert:
        ident = load_identity(explicit_cert, explicit_key or "")
        if ident is None:
            log.warning(
                "TERMINI_LXD_CLIENT_CERT=%s set but cert/key not both readable "
                "(key=%s) — LXD calls will be unauthenticated",
                explicit_cert, explicit_key,
            )
        return ident
    if not provision:
        return None
    cert_path = str(Path(client_dir) / CLIENT_CERT_FILE)
    key_path = str(Path(client_dir) / CLIENT_KEY_FILE)
    try:
        return ensure_identity(cert_path, key_path, common_name=common_name)
    except (RuntimeError, OSError) as exc:
        log.warning("could not provision an LXD client identity: %s", exc)
        return None


# --------------------------------------------------------------------------- trust
@dataclass(frozen=True)
class TrustProbe:
    """Observed trust state of the controller on one node.

    `trusted` is True (200 from an authenticated endpoint), False (401/403 — the
    defect signature), or None when the node could not be reached / answered
    unexpectedly.
    """

    trusted: bool | None
    status: int | None
    detail: str


@dataclass(frozen=True)
class TrustResult:
    ok: bool
    status: str      # trusted | already_trusted | untrusted | error
    detail: str
    probe_status: int | None = None


def _executor(base_url: str, identity: LxdIdentity, server_ca: str | None,
              timeout_s: float, retry_max: int) -> LxdExecutor:
    return LxdExecutor(
        base_url,
        timeout_s=timeout_s,
        retry_max=retry_max,
        backoff_s=0.2,
        client_cert=identity.cert_path,
        client_key=identity.key_path,
        server_ca=server_ca,
    )


def probe_trusted(
    base_url: str,
    identity: LxdIdentity,
    *,
    server_ca: str | None = None,
    timeout_s: float = 10.0,
) -> TrustProbe:
    """Observable trust check: an authenticated `GET /1.0/instances`.

    200 => the controller's cert is trusted by this node (the fix works).
    401/403 => not trusted (the defect this module repairs).
    Anything else / unreachable => trusted=None with the reason.
    """
    ex = _executor(base_url, identity, server_ca, timeout_s, retry_max=1)
    r = ex.list_instances()
    if r.status == 200:
        return TrustProbe(True, 200, "authenticated GET /1.0/instances -> 200")
    if r.status in (401, 403):
        return TrustProbe(
            False, r.status,
            f"GET /1.0/instances -> HTTP {r.status} (controller cert not trusted)",
        )
    return TrustProbe(
        None, r.status,
        f"could not determine trust: {r.result} / {str(r.detail)[:200]}",
    )


def ensure_trusted(
    base_url: str,
    identity: LxdIdentity,
    secret: str | None,
    *,
    name: str = CERT_DEFAULT_NAME,
    server_ca: str | None = None,
    timeout_s: float = 10.0,
    probe_before: bool = True,
) -> TrustResult:
    """Make the controller's client certificate trusted by the node at `base_url`.

    `secret` is either a certificate-add trust token or the node's
    `core.trust_password`; both are posted as the `password` field LXD expects.
    Idempotent: if the probe already reports 200 this is a no-op.
    """
    if not identity.available:
        return TrustResult(False, "error", "controller LXD identity is not available on disk")
    if not secret:
        return TrustResult(
            False, "error",
            "no trust secret supplied (need an LXD trust token or core.trust_password)",
        )

    if probe_before:
        before = probe_trusted(base_url, identity, server_ca=server_ca, timeout_s=timeout_s)
        if before.trusted is True:
            return TrustResult(True, "already_trusted", before.detail, before.status)
        if before.trusted is None:
            # Do not blindly POST a secret to a node we could not even reach.
            return TrustResult(False, "error",
                               f"cannot reach {base_url}: {before.detail}", before.status)

    ex = _executor(base_url, identity, server_ca, timeout_s, retry_max=2)
    r = ex.trust_client(secret, name=name)
    if r.result == "already_applied":
        return TrustResult(True, "already_trusted", str(r.detail), r.status)
    if r.result != "ok":
        return TrustResult(
            False, "error",
            f"trust install failed: HTTP {r.status}: {str(r.detail)[:300]}", r.status,
        )

    after = probe_trusted(base_url, identity, server_ca=server_ca, timeout_s=timeout_s)
    if after.trusted is True:
        return TrustResult(True, "trusted",
                           f"certificate added to trust store; {after.detail}", after.status)
    return TrustResult(
        False, "error",
        f"POST /1.0/certificates accepted but the probe is still {after.detail}",
        after.status,
    )


def ensure_trusted_all(
    base_urls,
    identity: LxdIdentity,
    secret: str | None,
    *,
    name: str = CERT_DEFAULT_NAME,
    server_ca: str | None = None,
    timeout_s: float = 10.0,
) -> list[tuple[str, TrustResult]]:
    out: list[tuple[str, TrustResult]] = []
    for base_url in base_urls:
        out.append((base_url, ensure_trusted(
            base_url, identity, secret, name=name, server_ca=server_ca, timeout_s=timeout_s,
        )))
    return out


# --------------------------------------------------------------------------- CLI
def _cli_identity(args) -> LxdIdentity:
    """Resolve identity for the CLI: explicit flags or the default data dir."""
    client_dir = args.dir or identity_dir_from_db(
        os.environ.get("TERMINI_DB_PATH", "").strip() or "data/controller.db"
    )
    ident = resolve_identity(
        explicit_cert=args.cert or (os.environ.get("TERMINI_LXD_CLIENT_CERT") or None),
        explicit_key=args.key or (os.environ.get("TERMINI_LXD_CLIENT_KEY") or None),
        client_dir=client_dir,
        common_name=args.name,
    )
    if ident is None:
        raise SystemExit(
            "no LXD client identity available (set --cert/--key or install openssl)"
        )
    return ident


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m app.lxdtrust",
        description="Controller LXD mTLS identity + node trust tooling.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _identity_args(sp):
        sp.add_argument("--cert", help="client certificate path (default: data dir)")
        sp.add_argument("--key", help="client key path (default: data dir)")
        sp.add_argument("--dir", help="identity directory (default: <db_dir>/lxd-client)")
        sp.add_argument("--name", default=CERT_DEFAULT_NAME,
                        help="certificate CN / LXD trust entry name")

    c = sub.add_parser("cert", help="ensure the controller client cert exists; print it")
    _identity_args(c)

    for verb in ("trust", "verify"):
        sp = sub.add_parser(verb, help=f"{verb} the controller cert against a node's LXD")
        _identity_args(sp)
        sp.add_argument("--node", action="append", required=True, metavar="URL",
                        help="LXD base URL, e.g. https://10.10.0.11:8443 (repeatable)")
        sp.add_argument("--ca", help="pin the node's server cert (PEM) instead of -k-style skip")
        sp.add_argument("--timeout", type=float, default=10.0, help="per-request timeout (s)")
        if verb == "trust":
            sp.add_argument("--token", help="LXD certificate-add trust token")
            sp.add_argument("--password", help="node core.trust_password")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)

    if args.cmd == "cert":
        ident = _cli_identity(args)
        print(f"cert={ident.cert_path}")
        print(f"key={ident.key_path}")
        print(f"fingerprint={ident.fingerprint}")
        return 0

    ident = _cli_identity(args)
    if args.cmd == "verify":
        worst = 0
        for node in args.node:
            probe = probe_trusted(node, ident, server_ca=args.ca, timeout_s=args.timeout)
            state = {True: "TRUSTED", False: "NOT TRUSTED", None: "UNKNOWN"}[probe.trusted]
            print(f"{node}: {state} — {probe.detail}")
            if probe.trusted is not True:
                worst = 3 if probe.trusted is False else 2
        return worst

    # trust
    secret = args.token or args.password
    if not secret:
        print("error: --token or --password is required", file=sys.stderr)
        return 2
    kind = "token" if args.token else "password"
    rc = 0
    for node, res in ensure_trusted_all(
        args.node, ident, secret, name=args.name, server_ca=args.ca, timeout_s=args.timeout,
    ):
        print(f"{node} [{kind}]: {res.status} — {res.detail}")
        if not res.ok:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
