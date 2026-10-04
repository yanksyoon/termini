"""Static, versioned extraction prompt for natural-language resource intents.

The template is frozen (``intent-extract v1``); its only dynamic placeholder is
the customer prompt, which is classified C2 and processed on-prem only
(governance.md §7.3, agent-runtime.md §14.1). The model is asked to emit JSON
only, matching the ``allocation_spec`` shape of
``docs/contracts/vm-allocation.schema.json``. Few-shot examples cover the
canonical "3 agents + 1 fast LLM" case.
"""

from __future__ import annotations

from typing import Dict, List

TEMPLATE_VERSION = "intent-extract v1"

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
      "passthrough_gpu": <bool, true only for inference_vm>,
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
- "LLM", "model", "inference", "GPU server", "fast LLM" -> type "inference_vm".
- passthrough_gpu must be true for inference_vm and false for agent_vm.
- Always emit integer values; never emit floats, strings, or nulls for count/vcpus/ram_gb/disk_gb.
- target_node is always "auto" (the controller places VMs by capacity).
- If a request is ambiguous or asks for an unknown resource, output
  {{"allocations": []}} rather than guessing.

You may recommend generous sizes — a downstream deterministic validator clamps
every number to the host's real capacity, so your job is faithful extraction,
not safety. Extract the user's intent literally (e.g. a user asking for 1000
vCPUs gets vcpus: 1000; the validator will clamp it)."""

# Few-shot examples. The first covers the canonical demo prompt.
FEW_SHOT: List[Dict[str, str]] = [
    {
        "user": "deploy 3 agents + 1 fast LLM",
        "assistant": (
            '{"allocations": ['
            '{"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, '
            '"disk_gb": 8, "passthrough_gpu": false, "target_node": "auto"}, '
            '{"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, '
            '"disk_gb": 64, "passthrough_gpu": true, "target_node": "auto"}'
            "]}"
        ),
    },
    {
        "user": "spin up 5 worker agents for web scraping",
        "assistant": (
            '{"allocations": ['
            '{"type": "agent_vm", "count": 5, "vcpus": 2, "ram_gb": 4, '
            '"disk_gb": 16, "passthrough_gpu": false, "target_node": "auto"}'
            "]}"
        ),
    },
    {
        "user": "one GPU box with 32 vcpus and 64GB RAM to serve the big model",
        "assistant": (
            '{"allocations": ['
            '{"type": "inference_vm", "count": 1, "vcpus": 32, "ram_gb": 64, '
            '"disk_gb": 128, "passthrough_gpu": true, "target_node": "auto"}'
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
