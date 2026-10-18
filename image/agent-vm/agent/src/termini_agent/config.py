"""Configuration loading + governance-safe validation (agent-runtime.md §4.4/§5.5)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

# The only on-prem inference base the agent may ever talk to (agent-runtime.md §19.1).
# D5 freezes the router at 10.10.0.13:8080; we also accept the node-C underlay CIDR so
# a re-IPA'd demo host does not silently fall back to a public endpoint.
ALLOWED_INFERENCE_NETWORKS = ("10.10.0.0/24",)
ALLOWED_INFERENCE_PORTS = (8080,)
# Extra on-prem networks a deployment may whitelist (e.g. the nested-LXD lab, where
# "Node C" is `demo-node-c` on the 10.14.100.0/24 host bridge). They must still be
# private and specific: a public range or 0.0.0.0/0 is always refused.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

DEFAULT_CONFIG_PATH = "/etc/termini/agent.yaml"
DEFAULT_INFERENCE_TOKEN_FILE = "/etc/termini/inference.token"

# The endpoint's OpenAI-compatible surface lives under /v1 (agent-runtime.md §8.2:
# `/v1/models`, `/v1/chat/completions`), and the `openai` SDK appends
# `/chat/completions` to whatever base URL it is handed. This is the single place the
# configured base URL is turned into that form.
OPENAI_API_SUFFIX = "/v1"


def normalize_inference_base_url(base_url: str) -> str:
    """Return the OpenAI-compatible base URL — `<scheme>://<host>:<port>/v1`.

    Both spellings a deployment may write in `agent.yaml` must produce the identical
    request path `/v1/chat/completions`:

        http://10.10.0.13:8080      -> http://10.10.0.13:8080/v1
        http://10.10.0.13:8080/     -> http://10.10.0.13:8080/v1
        http://10.10.0.13:8080/v1   -> http://10.10.0.13:8080/v1
        http://10.10.0.13:8080/v1/  -> http://10.10.0.13:8080/v1

    The front door serves only `/health`, `/v1/models` and `/v1/chat/completions`
    (`infra/inference/proxy.py`); anything else is a 404, so a base URL without
    `/v1` makes every agent LLM call fail at the first token.
    """
    base = str(base_url).strip().rstrip("/")
    if not base.endswith(OPENAI_API_SUFFIX):
        base += OPENAI_API_SUFFIX
    return base


def inference_chat_completions_url(base_url: str) -> str:
    """The exact POST target the runtime uses: normalised base + `/chat/completions`."""
    return f"{normalize_inference_base_url(base_url)}/chat/completions"


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
    # On-prem CIDRs an operator explicitly whitelisted (default: the frozen D5 range).
    allowed_networks: tuple[str, ...] = ALLOWED_INFERENCE_NETWORKS

    @property
    def api_base_url(self) -> str:
        """The base URL the OpenAI SDK must be given: always ends in `/v1`.

        `base_url` is kept exactly as configured (that is what the status surface
        reports and what an operator typed); every wire request goes through this
        normalised form so `http://h:8080` and `http://h:8080/v1` behave identically.
        """
        return normalize_inference_base_url(self.base_url)

    def chat_completions_url(self) -> str:
        """Absolute POST target for `/v1/chat/completions`."""
        return inference_chat_completions_url(self.base_url)


@dataclass(frozen=True)
class BrokerConfig:
    endpoint: str
    pull_interval_s: float = 1.0
    max_concurrent_tasks: int = 2


@dataclass(frozen=True)
class TasksConfig:
    """The in-VM task-submit surface's bounds (agent-runtime.md §5.6).

    `max_concurrent` is the per-INSTANCE bound: the number of tasks this instance
    will run at once for its owner. It is what stops a client's task load from
    saturating the node, so it defaults to the broker's worker count (one worker
    per concurrent task) rather than to something open-ended.
    """

    max_concurrent: int = 2
    result_sink: str = "/var/lib/termini/agent/results"


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
    tasks: TasksConfig = field(default_factory=TasksConfig)

    # -- derived ---------------------------------------------------------------
    def inference_token(self) -> str | None:
        try:
            return Path(self.inference.token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @property
    def telemetry_class(self) -> str:
        """The governing telemetry class (`governance.telemetry_class`, §4.4).

        Defaults to C0 when the key is absent: the runtime only ever emits C0/C1
        metadata, and an older config predating the key must not silently lose the
        startup diagnostics C0 allows.
        """
        gov = self.raw.get("governance") if isinstance(self.raw, dict) else None
        return str((gov or {}).get("telemetry_class", "C0")).strip().upper() or "C0"

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
                "allowed_networks": list(self.inference.allowed_networks),
                "stream": self.inference.stream,
            },
            "broker": {"endpoint": self.broker.endpoint,
                       "max_concurrent_tasks": self.broker.max_concurrent_tasks},
            "tasks": {"max_concurrent": self.tasks.max_concurrent,
                      "result_sink": self.tasks.result_sink},
            "logging": {"sink": self.logging.sink, "local_only": self.logging.local_only},
        }


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing required key {where}.{key}")
    return mapping[key]


def _validate_networks(allowed_networks: Sequence[str]) -> tuple[str, ...]:
    """Refuse a whitelist that is empty, malformed, or not strictly private."""
    if not allowed_networks:
        raise ConfigError("inference.allowed_networks may not be empty")
    validated: list[str] = []
    for net in allowed_networks:
        try:
            parsed = ipaddress.ip_network(str(net), strict=True)
        except ValueError as exc:
            raise ConfigError(f"inference.allowed_networks entry {net!r} is not a CIDR: {exc}") from exc
        if parsed.version != 4:
            raise ConfigError(f"inference.allowed_networks entry {net!r} must be IPv4")
        if not any(parsed.subnet_of(private) for private in _PRIVATE_NETWORKS):
            raise ConfigError(
                f"inference.allowed_networks entry {net!r} is not a private (RFC1918) range"
            )
        validated.append(str(parsed))
    return tuple(validated)


def validate_inference_url(
    base_url: str,
    *,
    dev_override: bool = False,
    allowed_networks: Sequence[str] | None = None,
) -> None:
    """Refuse any inference base URL outside the on-prem allow-list (§5.5)."""
    if dev_override:
        return
    from urllib.parse import urlparse

    networks = tuple(allowed_networks) if allowed_networks is not None else ALLOWED_INFERENCE_NETWORKS
    parsed = urlparse(base_url)
    if parsed.scheme != "http":
        raise ConfigError(f"inference.base_url must be http, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigError(f"inference.base_url has no host: {base_url!r}")
    addr = ipaddress.ip_address(parsed.hostname)
    if not any(addr in ipaddress.ip_network(net) for net in networks):
        raise ConfigError(
            f"inference.base_url {base_url!r} is outside the on-prem allow-list "
            f"{networks}; the agent VM has no route to anything else"
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
    tsk = data.get("tasks") or {}

    allowed_networks = _validate_networks(
        inj.get("allowed_networks") or list(ALLOWED_INFERENCE_NETWORKS)
    )
    base_url = str(_require(inj, "base_url", "inference"))
    validate_inference_url(base_url, dev_override=dev_override,
                           allowed_networks=allowed_networks)

    allowed = tuple(inj.get("allowed_models") or ["gemma"])
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
            allowed_networks=allowed_networks,
        ),
        broker=BrokerConfig(
            endpoint=str(_require(brk, "endpoint", "broker")),
            pull_interval_s=float(brk.get("pull_interval_s", 1.0)),
            max_concurrent_tasks=int(brk.get("max_concurrent_tasks", 2)),
        ),
        tasks=TasksConfig(
            # The §5.6 per-instance bound. Defaults to the broker's worker count so
            # an image predating this key still gets a real bound, never "unlimited".
            max_concurrent=int(tsk.get("max_concurrent", brk.get("max_concurrent_tasks", 2))),
            result_sink=str(tsk.get("result_sink", "/var/lib/termini/agent/results")),
        ),
        logging=LoggingConfig(
            sink=str(log.get("sink", "/var/log/termini/agent.jsonl")),
            local_only=bool(log.get("local_only", True)),
        ),
        raw=data,
    )
    return cfg
