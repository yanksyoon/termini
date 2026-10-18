"""Launch-time agent configuration (agent-runtime.md §4.4).

WHY THIS MODULE EXISTS
----------------------
The baked image ships `/etc/termini/agent.yaml` as a PLACEHOLDER — the bake
cannot know which project, node or instance the image will be launched as (its
`project_id`/`vm_id`/`node_id` are literal `01J0000…` dummies) and it does not
enable the service. §4.4 therefore makes the LAUNCH responsible for two things:

  1. writing the instance's own `/etc/termini/agent.yaml` (0644: the agent
     user reads it; the secret is the separate 0600 inference token) with the ids
     that instance actually has, and
  2. `systemctl enable --now termini-agent.service` (the bake leaves it
     disabled, so a bare `lxc launch` boots an instance whose agent never runs).

`_apply_plan` used to call `create_instance()` with no `cloud-init.user-data` at
all, so instances created by `Scale intent → Apply` booted with the placeholder
config and a dead service. That is not a cosmetic gap: the task surface is on
`127.0.0.1:9101` inside the instance, so a dead agent is a "Give task" that can
never succeed (`AGENT_UNREACHABLE`), and a placeholder `vm_id` is one the
controller can never address.

Rendered shape (a cloud-config document, YAML):

    #cloud-config
    write_files:
      - path: /etc/termini/agent.yaml
        owner: root:root
        permissions: '0644'
        content: |
          project_id: 01J…
          vm_id: NNB9287…
          ...
    runcmd:
      - [ systemctl, enable, --now, termini-agent.service ]

Every substituted value is validated as a single-line scalar first: these come
from config and registration rows, and a newline in one would let a value break
out of the YAML block it is written into.
"""
from __future__ import annotations

import os
from typing import Iterable

DEFAULT_AGENT_MODEL = "gemma"
DEFAULT_BROKER_ENDPOINT = "http://10.20.1.1:7071"
DEFAULT_RESULT_SINK = "/var/lib/termini/agent/results"
DEFAULT_LOG_SINK = "/var/log/termini/agent.jsonl"
DEFAULT_MAX_CONCURRENT = 2


class AgentConfigError(ValueError):
    """A value that cannot be written into the launch document safely."""


def _scalar(value: object, field: str) -> str:
    """Validate one substituted value and return it as a single-line scalar."""
    text = "" if value is None else str(value)
    if not text:
        raise AgentConfigError(f"{field} is empty")
    if any(ch in text for ch in "\r\n\t"):
        raise AgentConfigError(f"{field} must be a single line (got {text!r})")
    if text != text.strip():
        raise AgentConfigError(f"{field} has leading/trailing whitespace ({text!r})")
    if any(ch in text for ch in "\"'#{}[]&*!|>%@`"):
        # YAML-significant characters would need quoting; refusing keeps the
        # rendered document predictable instead of relying on quoting rules.
        raise AgentConfigError(f"{field} contains a YAML-significant character ({text!r})")
    if ": " in text or text.startswith(("-", "?", ":")):
        # `a: b` opens a mapping, `- x` a sequence: a value that starts or
        # contains these would change the document's structure.
        raise AgentConfigError(f"{field} would change the YAML structure ({text!r})")
    return text


def _list_scalar(values: Iterable[str], field: str) -> str:
    items = [_scalar(v, field) for v in values]
    if not items:
        raise AgentConfigError(f"{field} is empty")
    return "[" + ", ".join(items) + "]"


def agent_inference_base_url(cfg) -> str:
    """The base URL the IN-GUEST agent dials (always ends in `/v1`).

    `cfg.inference_base_url` is the OPERATOR's diagnosis address, which since
    the D9 split is a loopback provider listener the guest cannot reach. The
    guest-facing address is a separate knob; it defaults to the operator address
    with `/v1` appended, which is what the baked template has always carried.
    """
    raw = (os.environ.get("TERMINI_AGENT_INFERENCE_BASE_URL") or "").strip()
    if not raw:
        raw = (getattr(cfg, "inference_base_url", "") or "").strip()
    if not raw:
        raise AgentConfigError(
            "no guest-facing inference base URL (set TERMINI_AGENT_INFERENCE_BASE_URL)"
        )
    raw = raw.rstrip("/")
    return raw if raw.endswith("/v1") else raw + "/v1"


def render_agent_config(*, project_id: str, vm_id: str, node_id: str,
                        inference_base_url: str,
                        allowed_models: Iterable[str] = (DEFAULT_AGENT_MODEL,),
                        default_model: str | None = None,
                        broker_endpoint: str = DEFAULT_BROKER_ENDPOINT,
                        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                        result_sink: str = DEFAULT_RESULT_SINK,
                        log_sink: str = DEFAULT_LOG_SINK,
                        image_version: str = "") -> str:
    """The instance's own /etc/termini/agent.yaml body (agent-runtime.md §4.4)."""
    models = list(allowed_models)
    model = _scalar(default_model or (models[0] if models else DEFAULT_AGENT_MODEL),
                    "inference.default_model")
    lines = [
        "# Termini agent runtime config — rendered at launch (agent-runtime.md §4.4).",
        f"project_id: {_scalar(project_id, 'project_id')}",
        f"vm_id: {_scalar(vm_id, 'vm_id')}",
        f"node_id: {_scalar(node_id, 'node_id')}",
    ]
    if image_version:
        lines.append(f"agent_image_version: {_scalar(image_version, 'agent_image_version')}")
    lines += [
        "inference:",
        f"  base_url: {_scalar(inference_base_url, 'inference.base_url')}",
        f"  default_model: {model}",
        f"  allowed_models: {_list_scalar(models, 'inference.allowed_models')}",
        "  token_file: /etc/termini/inference.token",
        "  stream: true",
        "  connect_timeout_s: 2",
        "  request_timeout_s: 120",
        "broker:",
        f"  endpoint: {_scalar(broker_endpoint, 'broker.endpoint')}",
        "  pull_interval_s: 1",
        f"  max_concurrent_tasks: {int(max_concurrent)}",
        "tasks:",
        f"  max_concurrent: {int(max_concurrent)}",
        f"  result_sink: {_scalar(result_sink, 'tasks.result_sink')}",
        "logging:",
        f"  sink: {_scalar(log_sink, 'logging.sink')}",
        "  local_only: true",
        "governance:",
        "  telemetry_class: C0",
        "  egress: deny",
        "",
    ]
    return "\n".join(lines)


def render_user_data(**kwargs) -> str:
    """The cloud-init `user-data` that writes the config and starts the agent.

    `write_files` + `runcmd` only: no packages, no network. The bake already put
    the runtime and the image's own dependencies in place (AR-1: no installs at
    launch), so this is a two-second first boot, not a provisioning run.
    """
    body = render_agent_config(**kwargs)
    indented = "\n".join(("          " + line) if line else line
                         for line in body.splitlines())
    return (
        "#cloud-config\n"
        "write_files:\n"
        "  - path: /etc/termini/agent.yaml\n"
        "    owner: root:root\n"
        # 0644, like the bake's copy: the runtime runs as `agent` and must be
        # able to read it, and it holds no secret (the inference token is the
        # secret file, 0600 agent:agent, written by the bake).
        "    permissions: '0644'\n"
        "    content: |\n"
        f"{indented}\n"
        "runcmd:\n"
        "  - [ systemctl, enable, --now, termini-agent.service ]\n"
    )
