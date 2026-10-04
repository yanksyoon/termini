"""Thin CLI for manual demoing.

Full pipeline (LLM extraction + clamp validation):
    python -m termini.intent "deploy 3 agents + 1 fast LLM"

Constrained host (clamps every field to the supplied maxima):
    python -m termini.intent "deploy 3 agents + 1 fast LLM" \
        --host-caps '{"agent_vm": {"vcpus": 2, "ram_gb": 4, "disk_gb": 16},
                      "inference_vm": {"vcpus": 4, "ram_gb": 8, "disk_gb": 32}}'

Validator-only (no model call — feed raw component JSON straight to the clamp):
    python -m termini.intent --raw '[{"type":"agent_vm","count":3,"vcpus":99,
        "ram_gb":512,"disk_gb":8}]' --host-caps '{"vcpus":8,"ram_gb":16,"disk_gb":64}'

Prints the ValidationResult as JSON and exits non-zero when the outcome is
rejected (so it composes in scripts).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .extractor import extract_and_validate
from .types import DEFAULT_HOST_CAPS, HostCaps, ValidationResult
from .validator import clamp_validate


def _parse_host_caps(text: Optional[str]) -> Any:
    if not text:
        return DEFAULT_HOST_CAPS
    return json.loads(text)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m termini.intent",
        description="Extract and clamp a natural-language resource intent.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="free-text resource request (omit when using --raw)",
    )
    parser.add_argument(
        "--host-caps",
        default=None,
        help="JSON host-capacity table, e.g. "
        '\'{"agent_vm":{"vcpus":8,"ram_gb":16,"disk_gb":64},'
        '"inference_vm":{"vcpus":32,"ram_gb":64,"disk_gb":512}}\'',
    )
    parser.add_argument(
        "--raw",
        default=None,
        help="skip the model and feed this raw component JSON straight to the validator",
    )
    args = parser.parse_args(argv)

    try:
        host_caps = _parse_host_caps(args.host_caps)
    except json.JSONDecodeError as exc:
        print(f"error: invalid --host-caps JSON: {exc}", file=sys.stderr)
        return 2

    try:
        if args.raw is not None:
            result: ValidationResult = clamp_validate(json.loads(args.raw), host_caps)
        else:
            if not args.prompt:
                print("error: provide a prompt or --raw", file=sys.stderr)
                return 2
            result = extract_and_validate(args.prompt, host_caps)
    except json.JSONDecodeError as exc:
        print(f"error: invalid --raw JSON: {exc}", file=sys.stderr)
        return 2
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
