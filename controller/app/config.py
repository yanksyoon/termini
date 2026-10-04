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
        )

    @staticmethod
    def new_secret(nbytes: int = 32) -> str:
        """256-bit base64url secret, matching the contract's opaque token class."""
        return secrets.token_urlsafe(nbytes)
