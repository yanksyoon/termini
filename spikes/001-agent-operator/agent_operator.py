#!/usr/bin/env python3
"""Run the REAL production operator CLI with the agent brain swapped in.

Same arguments as `python3 -m app.operator.cli`, so the chaos harness can drive
it through TERMINI_CHAOS_OPERATOR_CMD with the usual placeholders:

    TERMINI_CHAOS_OPERATOR_CMD="python3 <this> --controller-url {controller_url} \
        --lxd-url {lxd_url} --node {node_id} --project-token {project_token} \
        --thresholds '{thresholds_json}' --audit-out /tmp/agent-audit.jsonl"

Nothing tracked is modified: `app.operator.playbook.choose` is replaced at
import time, before the CLI binds it.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_controller() -> str:
    override = os.environ.get("TERMINI_CONTROLLER_DIR")
    if override:
        return override
    # <repo>/spikes/001-agent-operator/ -> <repo>/controller
    guess = os.path.abspath(os.path.join(HERE, "..", "..", "controller"))
    if os.path.isdir(os.path.join(guess, "app")):
        return guess
    raise SystemExit(
        "cannot locate the controller package; set TERMINI_CONTROLLER_DIR"
    )


CONTROLLER = _find_controller()
for p in (CONTROLLER, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.operator import playbook  # noqa: E402
import agent  # noqa: E402

# THE SWAP. One line, no tracked file edited.
playbook.choose = agent.choose

if __name__ == "__main__":
    print(f"[agent-operator] brain = {agent.LLM_URL} model={agent.LLM_MODEL} "
          f"(playbook.choose patched)", flush=True)
    from app.operator.cli import main  # noqa: E402

    raise SystemExit(main())
