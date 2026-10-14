"""Static, versioned extraction prompt for natural-language resource intents.

The template is frozen (``intent-extract v1``); its only dynamic placeholder is
the customer prompt, which is classified C2 and processed on-prem only
(governance.md §7.3, agent-runtime.md §14.1). The model is asked to emit JSON
only, matching the ``allocation_spec`` shape of
``docs/contracts/vm-allocation.schema.json``. Few-shot examples cover the
canonical "3 agents + 1 fast LLM" case.
"""

from __future__ import annotations

import json
from typing import Dict, List

from .types import DEFAULT_AGENT_VM

TEMPLATE_VERSION = "intent-extract v2"

# The agent_vm size shown to the model when the user names no size. Sourced from
# `types.DEFAULT_AGENT_VM` so the prompt and the validator defaults cannot drift
# apart — a model that copies the example and a model that omits the field must
# both land on a size the demo's smallest node can actually host.
_AGENT_DEFAULT = DEFAULT_AGENT_VM

# The subset of the allocation_spec schema the extractor must emit. Kept tight
# so output parses directly into the validator's input.
ALLOCATION_JSON_SCHEMA = """
{
  "allocations": [
    {
      "type": "agent_vm" | "inference_vm",
      "count": <int, 1..64>,
      "vcpus": <int, 1..128>,
      "ram_gb": <int, 1..512>,
      "disk_gb": <int, 4..4096>,
      "passthrough_gpu": <bool, always false — GPU passthrough is day-2 only>,
      "target_node": "auto"
    }
  ]
}
""".strip()

SYSTEM_PROMPT = f"""You are the Termini resource-intent extractor (template {TEMPLATE_VERSION}).

Turn a free-text resource request into an LXD allocation spec. Output ONLY a
single JSON object with this exact shape and no prose, no markdown fences, no
trailing text:

{ALLOCATION_JSON_SCHEMA}

Rules:
- "agents", "workers", "agent hosts" -> type "agent_vm".
- "LLM", "model", "inference", "GPU server", "fast LLM" -> type "inference_vm" (a
  reference to the always-on host inference service, not a VM to launch).
- passthrough_gpu must always be false (GPU passthrough is day-2 only).
- Always emit integer values; never emit floats, strings, or nulls for count/vcpus/ram_gb/disk_gb.
- target_node is always "auto" (the controller places VMs by capacity).
- If a request is ambiguous or asks for an unknown resource, output
  {{"allocations": []}} rather than guessing.

Sizing:
- If the user NAMES a size ("4 vcpu", "32 GB"), extract it literally — a
  downstream deterministic validator clamps every number to the host's real
  capacity, so your job there is faithful extraction, not safety. A user asking
  for 1000 vCPUs gets vcpus: 1000; the validator clamps it.
- If the user does NOT name a size, do NOT invent a generous one. Emit the
  documented demo default for that class: agent_vm -> vcpus
  {_AGENT_DEFAULT["vcpus"]}, ram_gb {_AGENT_DEFAULT["ram_gb"]}, disk_gb
  {_AGENT_DEFAULT["disk_gb"]}; inference_vm -> vcpus 4, ram_gb 8, disk_gb 32.
  The demo's compute node is small (2 vCPU / 8 GiB), so an unspecified agent
  size larger than the default is rejected by the guardrail instead of
  deploying anything."""

# Few-shot examples. The first covers the canonical demo prompt. The `agent_vm`
# sizes are the documented default (`_AGENT_DEFAULT`) — a judge typing "deploy 2
# worker agents" with no sizes must get an example of exactly what to emit, or
# the model invents a size the compute node cannot host (Defect B).
def _agent_vm_example(count: int) -> dict:
    return {
        "type": "agent_vm",
        "count": count,
        "vcpus": _AGENT_DEFAULT["vcpus"],
        "ram_gb": _AGENT_DEFAULT["ram_gb"],
        "disk_gb": _AGENT_DEFAULT["disk_gb"],
        "passthrough_gpu": False,
        "target_node": "auto",
    }


def _inference_vm_reference() -> dict:
    return {
        "type": "inference_vm",
        "count": 1,
        "vcpus": 8,
        "ram_gb": 16,
        "disk_gb": 64,
        "passthrough_gpu": False,
        "target_node": "auto",
    }


FEW_SHOT: List[Dict[str, str]] = [
    {
        "user": "deploy 3 agents + 1 fast LLM",
        # Built with json.dumps rather than an interpolated literal so the
        # example is guaranteed to parse and cannot drift from `_AGENT_DEFAULT`.
        "assistant": json.dumps(
            {"allocations": [_agent_vm_example(3), _inference_vm_reference()]}
        ),
    },
    {
        "user": "spin up 5 worker agents for web scraping",
        "assistant": json.dumps({"allocations": [_agent_vm_example(5)]}),
    },
    {
        "user": "one GPU box with 32 vcpus and 64GB RAM to serve the big model",
        "assistant": (
            '{"allocations": ['
            '{"type": "inference_vm", "count": 1, "vcpus": 32, "ram_gb": 64, '
            '"disk_gb": 128, "passthrough_gpu": false, "target_node": "auto"}'
            "]}"
        ),
    },
]


def build_messages(prompt: str) -> List[Dict[str, str]]:
    """Build the OpenAI-style message list for a single extraction call."""
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example in FEW_SHOT:
        messages.append({"role": "user", "content": example["user"]})
        messages.append({"role": "assistant", "content": example["assistant"]})
    messages.append({"role": "user", "content": prompt})
    return messages
