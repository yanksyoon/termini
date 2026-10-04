"""Configuration loading + governance-safe validation (agent-runtime.md §4.4/§5.5)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The only on-prem inference base the agent may ever talk to (agent-runtime.md §19.1).
# D5 freezes the router at 10.10.0.13:8080; we also accept the node-C underlay CIDR so
# a re-IPA'd demo host does not silently fall back to a public endpoint.
ALLOWED_INFERENCE_NETWORKS = ("10.10.0.0/24",)
ALLOWED_INFERENCE_PORTS = (8080,)

DEFAULT_CONFIG_PATH = "/etc/termini/agent.yaml"
DEFAULT_INFERENCE_TOKEN_FILE = "/etc/termini/inference.token"


class ConfigError(ValueError):
    """Raised when the on-disk config is missing or violates the frozen contract."""


@dataclass(frozen=True)
class InferenceConfig:
    base_url: str
    default_model: str
    allowed_models: tuple[str, ...]
    token_file: str = DEFAULT_INFERENCE_TOKEN_FILE
    stream: bool = True
    connect_timeout_s: float = 2.0
    request_timeout_s: float = 120.0


@dataclass(frozen=True)
class BrokerConfig:
    endpoint: str
    pull_interval_s: float = 1.0
    max_concurrent_tasks: int = 2


@dataclass(frozen=True)
class LoggingConfig:
    sink: str = "/var/log/termini/agent.jsonl"
    local_only: bool = True


@dataclass(frozen=True)
class AgentConfig:
    project_id: str
    vm_id: str
    node_id: str
    agent_image_version: str
    inference: InferenceConfig
    broker: BrokerConfig
    logging: LoggingConfig
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------------
    def inference_token(self) -> str | None:
        try:
            return Path(self.inference.token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def as_public_dict(self) -> dict[str, Any]:
        """A redacted view (no token material) suitable for the status surface."""
        return {
            "project_id": self.project_id,
            "vm_id": self.vm_id,
            "node_id": self.node_id,
            "agent_image_version": self.agent_image_version,
            "inference": {
                "base_url": self.inference.base_url,
                "default_model": self.inference.default_model,
                "allowed_models": list(self.inference.allowed_models),
                "stream": self.inference.stream,
            },
            "broker": {"endpoint": self.broker.endpoint,
                       "max_concurrent_tasks": self.broker.max_concurrent_tasks},
            "logging": {"sink": self.logging.sink, "local_only": self.logging.local_only},
        }


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing required key {where}.{key}")
    return mapping[key]


def validate_inference_url(base_url: str, *, dev_override: bool = False) -> None:
    """Refuse any inference base URL outside the on-prem allow-list (§5.5)."""
    if dev_override:
        return
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme != "http":
        raise ConfigError(f"inference.base_url must be http, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigError(f"inference.base_url has no host: {base_url!r}")
    addr = ipaddress.ip_address(parsed.hostname)
    if not any(addr in ipaddress.ip_network(net) for net in ALLOWED_INFERENCE_NETWORKS):
        raise ConfigError(
            f"inference.base_url {base_url!r} is outside the on-prem allow-list "
            f"{ALLOWED_INFERENCE_NETWORKS}; the agent VM has no route to anything else"
        )
    if parsed.port not in ALLOWED_INFERENCE_PORTS:
        raise ConfigError(f"inference.base_url port {parsed.port} not in {ALLOWED_INFERENCE_PORTS}")


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH, *,
                dev_override: bool | None = None) -> AgentConfig:
    """Load /etc/termini/agent.yaml and validate it against the frozen contract."""
    if dev_override is None:
        dev_override = os.environ.get("TERMINI_AGENT_DEV") == "1"

    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config {p} must be a YAML mapping")

    inj = data.get("inference") or {}
    brk = data.get("broker") or {}
    log = data.get("logging") or {}

    base_url = str(_require(inj, "base_url", "inference"))
    validate_inference_url(base_url, dev_override=dev_override)

    allowed = tuple(inj.get("allowed_models") or ["deepseek-flash"])
    default_model = str(inj.get("default_model", allowed[0]))
    if default_model not in allowed:
        raise ConfigError(f"inference.default_model {default_model!r} not in allowed_models {allowed}")

    cfg = AgentConfig(
        project_id=str(_require(data, "project_id", "root")),
        vm_id=str(_require(data, "vm_id", "root")),
        node_id=str(_require(data, "node_id", "root")),
        agent_image_version=str(data.get("agent_image_version", "unknown")),
        inference=InferenceConfig(
            base_url=base_url,
            default_model=default_model,
            allowed_models=allowed,
            token_file=str(inj.get("token_file", DEFAULT_INFERENCE_TOKEN_FILE)),
            stream=bool(inj.get("stream", True)),
            connect_timeout_s=float(inj.get("connect_timeout_s", 2.0)),
            request_timeout_s=float(inj.get("request_timeout_s", 120.0)),
        ),
        broker=BrokerConfig(
            endpoint=str(_require(brk, "endpoint", "broker")),
            pull_interval_s=float(brk.get("pull_interval_s", 1.0)),
            max_concurrent_tasks=int(brk.get("max_concurrent_tasks", 2)),
        ),
        logging=LoggingConfig(
            sink=str(log.get("sink", "/var/log/termini/agent.jsonl")),
            local_only=bool(log.get("local_only", True)),
        ),
        raw=data,
    )
    return cfg
