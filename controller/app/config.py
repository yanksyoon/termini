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


def _decision_mode() -> str:
    """Operator decision mode (operator-decision.md §12/§13): deterministic | model."""
    raw = os.environ.get("TERMINI_OPERATOR_DECISION_MODE", "deterministic").strip()
    mode = raw or "deterministic"
    if mode not in ("deterministic", "model"):
        raise RuntimeError(
            f"TERMINI_OPERATOR_DECISION_MODE must be 'deterministic' or 'model', "
            f"got {raw!r}"
        )
    return mode


def _lxd_node_scheme() -> str:
    """Scheme assumed for a node LXD endpoint reported as a bare `host:port`.

    Real fleet nodes serve LXD over HTTPS with mTLS (infra-decisions.md D2/D6); the
    stub demo never reports an endpoint, so it keeps using the global
    TERMINI_LXD_BASE_URL (an http:// URL) untouched.
    """
    raw = os.environ.get("TERMINI_LXD_NODE_SCHEME", "").strip().lower() or "https"
    if raw not in ("http", "https"):
        raise RuntimeError(
            f"TERMINI_LXD_NODE_SCHEME must be 'http' or 'https', got {raw!r}"
        )
    return raw


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
    ssh_ca_key_path: str | None     # controller-held SSH CA private key (D4)
    ssh_ca_public_key: str | None   # CA public half (fingerprint/comment), else derived
    ssh_operator_key_path: str      # controller-local operator identity key (terminal bridge)
    ssh_key_comment: str
    ssh_client_key_path: str        # operator's browser-local key path shown in the SSH string
    ssh_cert_principal: str         # principal bound into issued certs (demo: root)
    ssh_cert_default_validity_s: int  # default cert TTL (8 h)
    ssh_cert_max_validity_s: int      # hard cert TTL ceiling (24 h)
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
    operator_lease_ttl_s: float      # single-owner lease TTL (migration 0007)
    playbooks_dir: str
    # Model-driven decision step (operator-decision.md §13).
    operator_decision_mode: str      # deterministic (default) | model
    operator_decision_timeout_s: float
    operator_decision_min_confidence: float
    operator_decision_max_fallbacks: int
    operator_decision_fallback_cooldown_s: float
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
    # LXD mTLS client-trust wiring (app/lxdtrust.py).
    lxd_client_dir: str            # where the controller mints/reuses its LXD client cert
    lxd_server_ca: str | None      # pin the node's server cert (else verify is skipped, demo parity)
    lxd_trust_token: str | None    # LXD certificate-add trust token (one-time)
    lxd_trust_password: str | None # node core.trust_password (legacy alternative)
    lxd_trust_name: str            # LXD trust-store entry name for the controller cert
    lxd_trust_nodes: tuple[str, ...]  # nodes to auto-trust at startup (base URLs)
    lxd_auto_provision: bool       # mint the client cert at startup when absent
    lxd_request_timeout_s: float
    lxd_retry_max: int
    lxd_retry_backoff_s: float
    # Apply-plan materialisation (per-node LXD targeting).
    lxd_workload_image: str       # preloaded LXD image alias (D3: no registry at boot)
    lxd_node_scheme: str          # scheme assumed for a node endpoint given as host:port
    lxd_create_timeout_s: float   # ceiling on waiting for the create operation
    lxd_start_timeout_s: float    # ceiling on waiting for the start operation

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

        db_path = os.environ.get("TERMINI_DB_PATH", "").strip() or "data/controller.db"
        # The controller's LXD client identity lives next to the SQLite store by
        # default, so it is provisioned once and survives restarts (app/lxdtrust.py).
        lxd_client_dir = os.environ.get("TERMINI_LXD_CLIENT_DIR", "").strip() \
            or str(Path(db_path).parent / "lxd-client")

        return cls(
            host=os.environ.get("TERMINI_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_int_env("TERMINI_PORT", 8080),
            db_path=db_path,
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
            ssh_ca_key_path=_opt_str("TERMINI_SSH_CA_KEY_PATH"),
            ssh_ca_public_key=_opt_str("TERMINI_SSH_CA_PUBLIC_KEY"),
            ssh_operator_key_path=os.environ.get(
                "TERMINI_SSH_OPERATOR_KEY_PATH", "~/.termini/operator_key"
            ).strip() or "~/.termini/operator_key",
            ssh_key_comment=os.environ.get("TERMINI_SSH_KEY_COMMENT", "termini-operator-cert").strip()
            or "termini-operator-cert",
            ssh_client_key_path=os.environ.get("TERMINI_SSH_CLIENT_KEY_PATH", "~/.ssh/termini_operator").strip()
            or "~/.ssh/termini_operator",
            ssh_cert_principal=os.environ.get("TERMINI_SSH_CERT_PRINCIPAL", "root").strip()
            or "root",
            ssh_cert_default_validity_s=_int_env("TERMINI_SSH_CERT_DEFAULT_VALIDITY_S", 28800),
            ssh_cert_max_validity_s=_int_env("TERMINI_SSH_CERT_MAX_VALIDITY_S", 86400),
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
            operator_lease_ttl_s=_float_env("TERMINI_OPERATOR_LEASE_TTL_S", 10.0),
            playbooks_dir=playbooks_dir,
            operator_decision_mode=_decision_mode(),
            operator_decision_timeout_s=_float_env("TERMINI_OPERATOR_DECISION_TIMEOUT_S", 30.0),
            operator_decision_min_confidence=_float_env("TERMINI_OPERATOR_DECISION_MIN_CONFIDENCE", 0.5),
            operator_decision_max_fallbacks=_int_env("TERMINI_OPERATOR_DECISION_MAX_FALLBACKS", 3),
            operator_decision_fallback_cooldown_s=_float_env("TERMINI_OPERATOR_DECISION_FALLBACK_COOLDOWN_S", 300.0),
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
            lxd_client_dir=lxd_client_dir,
            lxd_server_ca=_opt_str("TERMINI_LXD_SERVER_CA"),
            lxd_trust_token=_opt_str("TERMINI_LXD_TRUST_TOKEN"),
            lxd_trust_password=_opt_str("TERMINI_LXD_TRUST_PASSWORD"),
            lxd_trust_name=os.environ.get(
                "TERMINI_LXD_TRUST_NAME", "termini-controller"
            ).strip() or "termini-controller",
            lxd_trust_nodes=_opt_list("TERMINI_LXD_TRUST_NODES"),
            lxd_auto_provision=_bool_env("TERMINI_LXD_AUTO_PROVISION", True),
            lxd_request_timeout_s=_float_env("TERMINI_LXD_TIMEOUT_S", 10.0),
            lxd_retry_max=_int_env("TERMINI_LXD_RETRY_MAX", 3),
            lxd_retry_backoff_s=_float_env("TERMINI_LXD_RETRY_BACKOFF_S", 0.5),
            lxd_workload_image=os.environ.get("TERMINI_LXD_WORKLOAD_IMAGE", "").strip()
            or "agent-vm",
            lxd_node_scheme=_lxd_node_scheme(),
            lxd_create_timeout_s=_float_env("TERMINI_LXD_CREATE_TIMEOUT_S", 120.0),
            lxd_start_timeout_s=_float_env("TERMINI_LXD_START_TIMEOUT_S", 120.0),
        )

    @staticmethod
    def new_secret(nbytes: int = 32) -> str:
        """256-bit base64url secret, matching the contract's opaque token class."""
        return secrets.token_urlsafe(nbytes)
