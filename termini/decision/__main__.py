"""Thin CLI for local runs of the decision adapter.

Mirrors ``termini.intent.__main__``. All modes print JSON and exit non-zero on a
:class:`~termini.decision.types.DecisionError`, so it composes in scripts.

Offline (no model call) — parse the contract's §4 example response, or any
canned completion, through the same parse+repair path the live path uses:

    python -m termini.decision --example-response
    python -m termini.decision --example --raw '{"action_id": "nope"}'

Offline — show exactly what would be sent to the model for the §3 example input:

    python -m termini.decision --example --prompt

Live — render the §3 example input and call the on-prem endpoint (needs the
``TERMINI_INFERENCE_*`` env vars and a reachable router):

    python -m termini.decision --example

A request payload can be supplied instead of the built-in example:

    python -m termini.decision --input path/to/decision_input.json --prompt
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from .adapter import decide, parse, render
from .types import DecisionError, DecisionResponse

# The contract's §3 example DecisionInput, verbatim (operator-decision.md).
EXAMPLE_DECISION_INPUT: Dict[str, Any] = {
    "schema_version": 1,
    "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
    "project_id": "01M4349RPSZFTB60AVJEG0ZZY9",
    "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
    "rule_id": "oom-guard-01",
    "type": "oom",
    "trigger": {
        "metric": "ram_used_bytes",
        "value": 94.7,
        "threshold": ">= 95% of ram_total_bytes for 2 consecutive samples",
        "fired_at": "2026-10-04T07:49:05.000Z",
        "breach_started_at": "2026-10-04T07:49:03.000Z",
        "episode": 3,
    },
    "state": {
        "seq": 1050,
        "ts": "2026-10-04T07:49:05.000Z",
        "cpu_util_pct": 88.0,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 32641751449,
        "disk": [
            {
                "path": "/dev/sda",
                "total_bytes": 274877906944,
                "free_bytes": 214748364800,
                "write_latency_ms": 3.0,
                "mount_ro": False,
            }
        ],
        "net": [
            {"iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8}
        ],
        "gpu": [
            {
                "model": "NVIDIA L4",
                "util_pct": 88.0,
                "vram_total_bytes": 25769803776,
                "vram_used_bytes": 12884901888,
                "temp_celsius": 62.0,
            }
        ],
    },
    "evidence": [
        {
            "seq": 1049,
            "ts": "2026-10-04T07:49:04.000Z",
            "ram_used_bytes": 32300000000,
            "ram_total_bytes": 34359738368,
        }
    ],
    "target_resource": {
        "node_id": "01M4349RPTT29RNTC6VBCXQ0RC",
        "instances": [
            {
                "vm_id": "01M4349RPXWDC52ZDAVMPCMJSH",
                "instance_name": "agent-01H4KZ9Q",
                "state": "running",
                "cpu_util_pct": 12.0,
                "ram_used_bytes": 10000000000,
                "ram_limit_bytes": 16000000000,
                "disk_used_bytes": 1000000000,
            }
        ],
    },
    "allowed_action_space": [
        {
            "action_id": "instance.restart",
            "params_schema": {
                "instance_name": "string",
                "force": "bool",
                "timeout_s": "int 1..120",
            },
        },
        {
            "action_id": "instance.stop",
            "params_schema": {"instance_name": "string", "force": "bool"},
        },
        {"action_id": "instance.delete", "params_schema": {"instance_name": "string"}},
        {"action_id": "image.prune", "params_schema": {}},
    ],
}

# The contract's §4 example DecisionResponse, as the model would emit it.
EXAMPLE_COMPLETION = json.dumps(
    {
        "schema_version": 1,
        "request_id": "01M4349RQ09HMAX6RC9YPYQH4W",
        "action_id": "instance.restart",
        "params": {"instance_name": "agent-01H4KZ9Q", "force": True, "timeout_s": 30},
        "confidence": 0.87,
        "rationale": (
            "host RAM at 95% with one instance holding 29% of RAM; "
            "restart it to reclaim."
        ),
    }
)


def _load_input(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return EXAMPLE_DECISION_INPUT
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _emit(value: "DecisionResponse | DecisionError") -> int:
    if isinstance(value, DecisionError):
        print(json.dumps({"outcome": "error", **value.to_dict()}, indent=2))
        return 1
    print(json.dumps({"outcome": "response", **value.to_dict()}, indent=2))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m termini.decision",
        description="Run the operator decision adapter locally (contract example payloads).",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="path to a DecisionInput JSON payload (default: the built-in §3 example)",
    )
    parser.add_argument(
        "--example",
        action="store_true",
        help="use the built-in §3 example DecisionInput (the default)",
    )
    parser.add_argument(
        "--example-response",
        action="store_true",
        help="parse the built-in §4 example model completion (offline, no model call)",
    )
    parser.add_argument(
        "--raw",
        default=None,
        help="parse this string as the model completion instead of calling the model",
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="print the rendered model messages and exit (offline, no model call)",
    )
    args = parser.parse_args(argv)

    try:
        decision_input = _load_input(args.input)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot load input: {exc}", file=sys.stderr)
        return 2

    if args.prompt:
        print(json.dumps(render(decision_input), indent=2))
        return 0

    if args.example_response:
        return _emit(parse(EXAMPLE_COMPLETION, decision_input))

    if args.raw is not None:
        return _emit(parse(args.raw, decision_input))

    # Live path: render + call the on-prem endpoint + parse.
    return _emit(decide(decision_input))


if __name__ == "__main__":
    raise SystemExit(main())
