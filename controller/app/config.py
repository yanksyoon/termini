"""Termini Controller — runtime configuration.

Every tunable is externalized via environment variables (no hardcoded
credentials, ports, or store URLs). Values are read once at startup and held in
a small immutable namespace.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    ssh_key_path: str | None        # controller-held key for the terminal bridge proxy
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

    # --- AI-operator remediation loop ---
    operator_enabled: bool
    operator_poll_interval_s: float
    operator_window: int
    operator_dry_run: bool
    operator_cooldown_s: float
    cadence_report_s: float
    playbooks_dir: str
    # Detection thresholds (config, not code — task requirement).
    oom_ram_pct: float
    oom_clear_pct: float
    oom_consecutive: int
    oom_clear_consecutive: int
    disk_fail_consecutive: int
    disk_fail_clear_consecutive: int
    disk_free_pct: float
    disk_free_clear_pct: float
    disk_free_consecutive: int
    disk_write_latency_ms: float
    disk_write_latency_clear_ms: float
    disk_latency_consecutive: int
    disk_free_slope_bps: float
    disk_clear_consecutive: int
    latency_ms: float
    latency_clear_ms: float
    latency_consecutive: int
    latency_clear_consecutive: int
    # LXD REST target + instance-name mapping.
    lxd_base_url: str
    lxd_instance_names: dict[str, str]
    lxd_default_instance_name: str
    lxd_client_cert: str | None
    lxd_client_key: str | None
    lxd_request_timeout_s: float
    lxd_retry_max: int
    lxd_retry_backoff_s: float

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

        playbooks_dir = os.environ.get("TERMINI_PLAYBOOKS_DIR", "").strip()
        if not playbooks_dir:
            playbooks_dir = str(Path(__file__).resolve().parent.parent / "playbooks")

        instance_names_raw = os.environ.get("TERMINI_LXD_INSTANCE_NAMES", "").strip()
        instance_names: dict[str, str] = {}
        if instance_names_raw:
            try:
                parsed = json.loads(instance_names_raw)
                if not isinstance(parsed, dict):
                    raise ValueError("must be a JSON object")
                instance_names = {str(k): str(v) for k, v in parsed.items()}
            except ValueError as exc:
                raise RuntimeError(
                    f"TERMINI_LXD_INSTANCE_NAMES must be a JSON object: {exc}"
                ) from exc

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
            ssh_key_comment=os.environ.get("TERMINI_SSH_KEY_COMMENT", "termini-operator-cert").strip()
            or "termini-operator-cert",
            ssh_client_key_path=os.environ.get("TERMINI_SSH_CLIENT_KEY_PATH", "~/.ssh/termini_operator").strip()
            or "~/.ssh/termini_operator",
            ssh_user=os.environ.get("TERMINI_SSH_USER", "ubuntu").strip() or "ubuntu",
            ssh_port=_int_env("TERMINI_SSH_PORT", 22),
            log_ring_size=_int_env("TERMINI_LOG_RING_SIZE", 10000),
            approval_timeout_s=_int_env("TERMINI_APPROVAL_TIMEOUT_S", 300),
            terminal_idle_timeout_s=_int_env("TERMINI_TERMINAL_IDLE_TIMEOUT_S", 900),
            terminal_backend=os.environ.get("TERMINI_TERMINAL_BACKEND", "ssh").strip()
            or "ssh",
            dashboard_origins=_opt_list("TERMINI_DASHBOARD_ORIGINS"),
            operator_enabled=_bool_env("TERMINI_OPERATOR_ENABLED", True),
            operator_poll_interval_s=_float_env("TERMINI_OPERATOR_POLL_INTERVAL_S", 1.0),
            operator_window=_int_env("TERMINI_OPERATOR_WINDOW", 60),
            operator_dry_run=_bool_env("TERMINI_OPERATOR_DRY_RUN", False),
            operator_cooldown_s=_float_env("TERMINI_OPERATOR_COOLDOWN_S", 60.0),
            cadence_report_s=_float_env("TERMINI_OPERATOR_CADENCE_REPORT_S", 30.0),
            playbooks_dir=playbooks_dir,
            oom_ram_pct=_float_env("TERMINI_OOM_RAM_PCT", 95.0),
            oom_clear_pct=_float_env("TERMINI_OOM_CLEAR_PCT", 90.0),
            oom_consecutive=_int_env("TERMINI_OOM_CONSECUTIVE", 2),
            oom_clear_consecutive=_int_env("TERMINI_OOM_CLEAR_CONSECUTIVE", 3),
            disk_fail_consecutive=_int_env("TERMINI_DISK_FAIL_CONSECUTIVE", 1),
            disk_fail_clear_consecutive=_int_env("TERMINI_DISK_FAIL_CLEAR_CONSECUTIVE", 3),
            disk_free_pct=_float_env("TERMINI_DISK_FREE_PCT", 5.0),
            disk_free_clear_pct=_float_env("TERMINI_DISK_FREE_CLEAR_PCT", 15.0),
            disk_free_consecutive=_int_env("TERMINI_DISK_FREE_CONSECUTIVE", 3),
            disk_write_latency_ms=_float_env("TERMINI_DISK_WRITE_LATENCY_MS", 50.0),
            disk_write_latency_clear_ms=_float_env("TERMINI_DISK_WRITE_LATENCY_CLEAR_MS", 30.0),
            disk_latency_consecutive=_int_env("TERMINI_DISK_LATENCY_CONSECUTIVE", 5),
            disk_free_slope_bps=_float_env("TERMINI_DISK_FREE_SLOPE_BPS", -5_000_000.0),
            disk_clear_consecutive=_int_env("TERMINI_DISK_CLEAR_CONSECUTIVE", 3),
            latency_ms=_float_env("TERMINI_LATENCY_MS", 250.0),
            latency_clear_ms=_float_env("TERMINI_LATENCY_CLEAR_MS", 150.0),
            latency_consecutive=_int_env("TERMINI_LATENCY_CONSECUTIVE", 3),
            latency_clear_consecutive=_int_env("TERMINI_LATENCY_CLEAR_CONSECUTIVE", 3),
            lxd_base_url=os.environ.get("TERMINI_LXD_BASE_URL", "").strip()
            or "http://127.0.0.1:8443",
            lxd_instance_names=instance_names,
            lxd_default_instance_name=os.environ.get(
                "TERMINI_LXD_DEFAULT_INSTANCE_NAME", ""
            ).strip() or "agent",
            lxd_client_cert=os.environ.get("TERMINI_LXD_CLIENT_CERT", "").strip() or None,
            lxd_client_key=os.environ.get("TERMINI_LXD_CLIENT_KEY", "").strip() or None,
            lxd_request_timeout_s=_float_env("TERMINI_LXD_TIMEOUT_S", 10.0),
            lxd_retry_max=_int_env("TERMINI_LXD_RETRY_MAX", 3),
            lxd_retry_backoff_s=_float_env("TERMINI_LXD_RETRY_BACKOFF_S", 0.5),
        )

    @staticmethod
    def new_secret(nbytes: int = 32) -> str:
        """256-bit base64url secret, matching the contract's opaque token class."""
        return secrets.token_urlsafe(nbytes)
