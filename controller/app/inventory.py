"""Infra inventory loading + SSH access-string construction (dashboard.md §3.2).

The controller is seeded with the infra-emitted ``inventory.json`` (infra/gcp
writes it; see inventory.schema.json). ``public_ip`` is NOT present in node
telemetry (``ip_public`` is denied by governance.md §4.2), so this inventory is
the only source of the node's routable address for the SSH string and terminal
bridge.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

_SAFE_HOST = re.compile(r"^[A-Za-z0-9._-]+$")

DEFAULT_INVENTORY_PATHS = (
    "/home/ubuntu/.termini/inventory.json",
    "~/.termini/inventory.json",
)


class Inventory:
    """Loaded inventory with per-node SSH resolution."""

    def __init__(self, data: dict | None, ssh_user: str = "ubuntu", ssh_port: int = 22) -> None:
        self.data = data or {}
        self.ssh_user = (self.data.get("ssh") or {}).get("user") or ssh_user
        self.ssh_port = (self.data.get("ssh") or {}).get("port") or ssh_port
        self._nodes: list[dict] = self.data.get("nodes") or []

    @classmethod
    def load(cls, path: str | None, ssh_user: str, ssh_port: int) -> "Inventory":
        """Load inventory from `path`, or the default locations, or empty."""
        candidates: list[str] = []
        if path:
            candidates.append(path)
        candidates.extend(DEFAULT_INVENTORY_PATHS)
        for c in candidates:
            p = Path(os.path.expanduser(c))
            if p.is_file():
                try:
                    return cls(json.loads(p.read_text()), ssh_user, ssh_port)
                except (json.JSONDecodeError, OSError):
                    continue
        return cls(None, ssh_user, ssh_port)

    @property
    def loaded(self) -> bool:
        return bool(self.data)

    def find(self, node: dict) -> dict | None:
        """Map a registered node row to its inventory entry.

        Prefer an exact ``registration_id`` match against inventory ``name`` or
        ``hostname``; fall back to role-based assignment in registration order
        (compute -> node-a/node-b, inference -> node-c) since the bootstrap
        daemon's ``registration_id`` is a machine id that does not match the
        inventory labels.
        """
        reg_id = (node.get("registration_id") or "").lower()
        role = node.get("role") or "compute"
        for n in self._nodes:
            if reg_id and reg_id in ((n.get("name") or "").lower(), (n.get("hostname") or "").lower()):
                return n
        # role-based fallback (deterministic given the fixed 3-node demo)
        if role == "inference":
            for n in self._nodes:
                if n.get("role") == "inference":
                    return n
            return None
        compute = [n for n in self._nodes if n.get("role") == "compute"]
        return compute[0] if compute else None

    def public_ip_for(self, node: dict) -> str | None:
        entry = self.find(node)
        if not entry:
            return None
        ip = entry.get("public_ip")
        return ip if isinstance(ip, str) and ip else None


def fingerprint_from_public_key(pubkey_line: str) -> tuple[str, str]:
    """Return (algorithm, SHA256 fingerprint) for an OpenSSH public key line."""
    parts = pubkey_line.strip().split()
    algo = parts[0] if parts else "unknown"
    b64 = parts[1] if len(parts) >= 2 else (parts[0] if parts else "")
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        return algo, ""
    digest = base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode()
    return algo, f"SHA256:{digest}"


def public_key_from_private(key_path: str) -> str | None:
    """Derive the public key from a private key via ``ssh-keygen -y``."""
    if not key_path or not Path(key_path).is_file():
        return None
    try:
        out = subprocess.run(
            ["ssh-keygen", "-y", "-f", key_path],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        return out.stdout.strip()
    return None


def ssh_command_for(
    node: dict,
    inventory: Inventory,
    *,
    ca_public_key_line: str | None,
    key_comment: str,
    client_key_path: str,
) -> dict:
    """Build the GET /nodes/{id}/ssh payload (dashboard.md §3.2, D4 CA model).

    The copy-paste command references the *operator's* browser-local private key
    (``client_key_path``); the controller holds only the SSH CA, whose public
    half is surfaced as ``key_algorithm``/``key_fingerprint`` (the trust anchor).
    The signed short-lived certificate is installed alongside the browser-local
    key, so no long-lived controller key ever appears in the string.
    """
    host = node.get("public_ip") or inventory.public_ip_for(node)
    user = inventory.ssh_user
    port = inventory.ssh_port

    algo = "unknown"
    fp = ""
    comment = key_comment
    if ca_public_key_line:
        parts = ca_public_key_line.strip().split()
        algo, fp = fingerprint_from_public_key(ca_public_key_line)
        if len(parts) >= 3:
            comment = parts[2]

    if host and not _SAFE_HOST.match(host):
        host = None  # never render shell metacharacters
    if not _SAFE_HOST.match(user):
        user = "ubuntu"

    command = None
    if host:
        port_flag = f"-p {port} " if port and port != 22 else ""
        command = f"ssh -i {client_key_path} {port_flag}{user}@{host}".strip()

    return {
        "node_id": node["id"],
        "user": user,
        "host": host,
        "port": port,
        "key_algorithm": algo,
        "key_fingerprint": fp,
        "key_comment": comment,
        "auth_model": "ssh_ca",
        "command": command,
    }
