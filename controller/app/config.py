"""Termini Controller — runtime configuration.

Every tunable is externalized via environment variables (no hardcoded
credentials, ports, or store URLs). Values are read once at startup and held in
a small immutable namespace.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


def _opt_str(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def _opt_list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def _require_nonempty(name: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        raise RuntimeError(
            f"{name} is required and must be set in the environment. "
            f"No credentials are hardcoded."
        )
    return raw.strip()


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    # Auth — bootstrap tokens (single-use) and project tokens (frontend/dashboard).
    bootstrap_tokens: tuple[str, ...]
    project_tokens: tuple[str, ...]
    project_id: str | None
    # Deterministic role allocation knobs.
    max_inference_nodes: int
    gpu_vram_min_bytes: int
    # Governance.
    min_redactor_version: int
    # Cadence defaults returned to nodes on registration.
    heartbeat_interval_s: int
    telemetry_interval_ms: int
    telemetry_batch_max: int
    # W5 dashboard surfaces (t_3468e5f0).
    inventory_path: str | None      # infra inventory.json (public_ip/ssh user+port)
    ssh_key_path: str | None        # break-glass private key for the terminal bridge
    ssh_public_key: str | None      # public half (fingerprint/comment), else derived
    ssh_key_comment: str
    ssh_client_key_path: str        # operator's local key path shown in the SSH string
    ssh_user: str                   # fallback when no inventory is loaded
    ssh_port: int
    log_ring_size: int
    approval_timeout_s: int         # pending_approval auto-deny after this many seconds
    terminal_idle_timeout_s: int    # idle terminal session close (15 min default)
    terminal_backend: str           # "ssh" (default) | "local" (test shell)
    dashboard_origins: tuple[str, ...]  # Origin allowlist for terminal/WS (empty = any)

    @classmethod
    def load(cls) -> "Config":
        # Bootstrap tokens: required (comma-separated, single-use each).
        bt_raw = _require_nonempty("TERMINI_BOOTSTRAP_TOKEN")
        bootstrap = tuple(t.strip() for t in bt_raw.split(",") if t.strip())
        if not bootstrap:
            raise RuntimeError("TERMINI_BOOTSTRAP_TOKEN must contain at least one token")

        pt_raw = os.environ.get("TERMINI_PROJECT_TOKEN", "")
        project = tuple(t.strip() for t in pt_raw.split(",") if t.strip())

        project_id = os.environ.get("TERMINI_PROJECT_ID", "").strip() or None

        return cls(
            host=os.environ.get("TERMINI_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_int_env("TERMINI_PORT", 8080),
            db_path=os.environ.get("TERMINI_DB_PATH", "").strip()
            or "data/controller.db",
            bootstrap_tokens=bootstrap,
            project_tokens=project,
            project_id=project_id,
            max_inference_nodes=_int_env("TERMINI_MAX_INFERENCE_NODES", 1),
            gpu_vram_min_bytes=_int_env("TERMINI_GPU_VRAM_MIN_BYTES", 12 * 1024 ** 3),
            min_redactor_version=_int_env("TERMINI_MIN_REDACTOR_VERSION", 3),
            heartbeat_interval_s=_int_env("TERMINI_HEARTBEAT_INTERVAL_S", 5),
            telemetry_interval_ms=_int_env("TERMINI_TELEMETRY_INTERVAL_MS", 1000),
            telemetry_batch_max=_int_env("TERMINI_TELEMETRY_BATCH_MAX", 200),
            inventory_path=_opt_str("TERMINI_INVENTORY_PATH"),
            ssh_key_path=_opt_str("TERMINI_SSH_KEY_PATH"),
            ssh_public_key=_opt_str("TERMINI_SSH_PUBLIC_KEY"),
            ssh_key_comment=os.environ.get("TERMINI_SSH_KEY_COMMENT", "termini-breakglass").strip()
            or "termini-breakglass",
            ssh_client_key_path=os.environ.get("TERMINI_SSH_CLIENT_KEY_PATH", "~/.ssh/termini_breakglass").strip()
            or "~/.ssh/termini_breakglass",
            ssh_user=os.environ.get("TERMINI_SSH_USER", "ubuntu").strip() or "ubuntu",
            ssh_port=_int_env("TERMINI_SSH_PORT", 22),
            log_ring_size=_int_env("TERMINI_LOG_RING_SIZE", 10000),
            approval_timeout_s=_int_env("TERMINI_APPROVAL_TIMEOUT_S", 300),
            terminal_idle_timeout_s=_int_env("TERMINI_TERMINAL_IDLE_TIMEOUT_S", 900),
            terminal_backend=os.environ.get("TERMINI_TERMINAL_BACKEND", "ssh").strip()
            or "ssh",
            dashboard_origins=_opt_list("TERMINI_DASHBOARD_ORIGINS"),
        )

    @staticmethod
    def new_secret(nbytes: int = 32) -> str:
        """256-bit base64url secret, matching the contract's opaque token class."""
        return secrets.token_urlsafe(nbytes)
