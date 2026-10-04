"""SSH certificate authority (infra-decisions.md D4, controller-api.md §4.3).

The controller holds an Ed25519 SSH CA. A user uploads a *public* key; the
controller signs it into a short-lived, principal-bound OpenSSH certificate via
``ssh-keygen -s``. The CA private key never leaves the controller and the user's
private key never transits it (only the public key is uploaded). Certificates
default to 8 h and are never issued for more than 24 h.
"""
from __future__ import annotations

import base64
import datetime
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import inventory, ulid

CERT_MAX_VALIDITY_S = 86400      # never more than 24 h (controller-api.md §4.3)
CERT_DEFAULT_VALIDITY_S = 28800  # 8 h
CERT_DEFAULT_PRINCIPAL = "root"

# OpenSSH public-key type prefix -> short name returned to the client.
_KEY_ALGOS = {
    "ssh-ed25519": "ed25519",
    "ssh-rsa": "rsa",
    "ecdsa-sha2-nistp256": "ecdsa",
    "ecdsa-sha2-nistp384": "ecdsa",
    "ecdsa-sha2-nistp521": "ecdsa",
}

_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# ssh-keygen -I identity: a bounded, shell-safe string.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")


class SSHCAError(Exception):
    """A public key could not be validated or signed."""


def validate_public_key(line: object) -> str:
    """Validate a user-supplied OpenSSH public key line; return it normalized.

    Raises :class:`SSHCAError` for anything that is not a single public key
    line. Private-key material is explicitly rejected so a private key can never
    (even accidentally) transit the controller.
    """
    if not isinstance(line, str):
        raise SSHCAError("public_key must be a string")
    text = line.strip()
    if not text:
        raise SSHCAError("public_key is empty")
    if _PRIVATE_KEY_MARKER.search(text):
        raise SSHCAError("public_key looks like a private key — upload the public key only")
    parts = text.split()
    if len(parts) < 2:
        raise SSHCAError("public_key is not a valid OpenSSH public key line")
    algo = parts[0]
    if algo not in _KEY_ALGOS:
        raise SSHCAError(f"unsupported public key type {algo!r}")
    try:
        base64.b64decode(parts[1], validate=True)
    except Exception:
        raise SSHCAError("public_key has invalid base64 encoding")
    return text


class SSHCA:
    """Controller-held SSH certificate authority (D4)."""

    def __init__(
        self,
        ca_key_path: str | None,
        ca_public_key_line: str | None = None,
        *,
        default_validity_s: int = CERT_DEFAULT_VALIDITY_S,
        max_validity_s: int = CERT_MAX_VALIDITY_S,
        principal: str = CERT_DEFAULT_PRINCIPAL,
    ) -> None:
        self.ca_key_path = ca_key_path or ""
        self._ca_public_key_line = ca_public_key_line
        self.default_validity_s = default_validity_s
        self.max_validity_s = max_validity_s
        self.principal = principal

    @property
    def available(self) -> bool:
        return bool(self.ca_key_path) and Path(self.ca_key_path).is_file()

    @property
    def public_key_line(self) -> str | None:
        if self._ca_public_key_line:
            return self._ca_public_key_line
        if self.available:
            self._ca_public_key_line = inventory.public_key_from_private(self.ca_key_path)
            return self._ca_public_key_line
        return None

    @property
    def fingerprint(self) -> str:
        line = self.public_key_line
        if not line:
            return ""
        _, fp = inventory.fingerprint_from_public_key(line)
        return fp

    def clamp_validity(self, validity_s: object) -> int:
        """Clamp a requested TTL to (default, max]: non-positive -> default."""
        if not isinstance(validity_s, int) or isinstance(validity_s, bool) or validity_s <= 0:
            return self.default_validity_s
        return min(validity_s, self.max_validity_s)

    def sign(
        self,
        public_key_line: object,
        *,
        principal: str | None = None,
        validity_s: object = None,
        identity: str = "operator",
    ) -> dict:
        """Sign a public key into a short-lived certificate.

        Returns the issuance record (``certificate``, ``ca_fingerprint``,
        ``principal``, ``valid_from``/``valid_until``, ``key_fingerprint``, …).
        Raises :class:`SSHCAError` on invalid input or a failed signing.
        """
        if not self.available:
            raise SSHCAError("SSH CA is not configured (no CA key)")
        public_key_line = validate_public_key(public_key_line)
        principal = (principal or self.principal).strip()
        if not _SAFE_ID.match(principal):
            raise SSHCAError("principal contains invalid characters")
        validity = self.clamp_validity(validity_s)
        identity = (identity or "operator").strip()
        if not _SAFE_ID.match(identity):
            raise SSHCAError("identity contains invalid characters")

        with tempfile.TemporaryDirectory(prefix="termini-sshca-") as td:
            pub_path = os.path.join(td, "user.pub")
            with open(pub_path, "w", encoding="utf-8") as fh:
                fh.write(public_key_line + "\n")
            cmd = [
                "ssh-keygen", "-s", self.ca_key_path,
                "-I", identity, "-n", principal,
                "-V", f"+{validity}s", pub_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                raise SSHCAError(f"ssh-keygen signing failed: {proc.stderr.strip()}")
            cert_path = (pub_path[:-4] + "-cert.pub") if pub_path.endswith(".pub") else (pub_path + "-cert.pub")
            try:
                with open(cert_path, encoding="utf-8") as fh:
                    certificate = fh.read().strip()
            except OSError as exc:
                raise SSHCAError(f"certificate not produced: {exc}") from exc

        algo, key_fp = inventory.fingerprint_from_public_key(public_key_line)
        now = int(time.time())
        return {
            "cert_id": ulid.new_ulid(),
            "certificate": certificate,
            "ca_fingerprint": self.fingerprint,
            "principal": principal,
            "key_id": identity,
            "key_algorithm": _KEY_ALGOS.get(algo, algo),
            "key_fingerprint": key_fp,
            "valid_from": _iso(now),
            "valid_until": _iso(now + validity),
            "validity_s": validity,
        }

    def issue_operator_certificate(
        self,
        operator_key_path: str,
        *,
        principal: str | None = None,
        validity_s: object = None,
        identity: str = "termini-controller",
    ) -> tuple[str, str, dict]:
        """Issue a short-lived cert for the controller's own operator key.

        Used by the demo terminal bridge as its client identity instead of a
        long-lived controller-held key. Ensures the operator keypair exists
        (generates an Ed25519 key once), signs it, and writes the certificate
        alongside it. Returns ``(key_path, cert_path, meta)``.
        """
        if not self.available:
            raise SSHCAError("SSH CA is not configured")
        key_path = Path(os.path.expanduser(operator_key_path))
        if not key_path.is_file():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise SSHCAError(f"failed to generate operator key: {proc.stderr.strip()}")
        pub_line = inventory.public_key_from_private(str(key_path))
        if not pub_line:
            raise SSHCAError("could not derive operator public key")
        meta = self.sign(pub_line, principal=principal, validity_s=validity_s, identity=identity)
        cert_path = key_path.with_name(key_path.name + "-cert.pub")
        cert_path.write_text(meta["certificate"] + "\n", encoding="utf-8")
        return str(key_path), str(cert_path), meta


def _iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
