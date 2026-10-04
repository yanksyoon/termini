"""W7 acceptance checks — one independent observation per W1–W6 criterion.

Each check returns a result row:
    {
      "id": "W2.1",
      "workstream": "W2 — Control plane: ...",
      "criterion": "<verbatim acceptance criterion>",
      "test_method": "<independent observation made>",
      "status": "pass" | "fail" | "skipped",
      "detail": "<findings>",
      "artifact": "<relative evidence path or None>",
      "reason": "<required when status != pass>",
    }
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable, Dict, List, Optional


class CheckContext:
    """Everything a check needs to observe the live system."""

    def __init__(
        self,
        *,
        controller_url: str,
        project_token: Optional[str],
        project_id: Optional[str],
        bootstrap_token: Optional[str],
        repo_root: str,
        evidence,  # EvidenceRecorder
        env_hash: dict,
    ):
        self.controller_url = controller_url
        self.project_token = project_token
        self.project_id = project_id
        self.bootstrap_token = bootstrap_token
        self.repo_root = repo_root
        self.ev = evidence
        self.env = env_hash
        self.state: Dict[str, Any] = {}


def run(cmd: str, timeout: int = 30) -> str:
    """Run a shell command and return combined stdout+stderr (never raises)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def is_absent(resp) -> bool:
    """True when an endpoint is missing from the running build (404 no route)."""
    if resp.status != 404:
        return False
    body = resp.json()
    return isinstance(body, dict) and body.get("error", {}).get("code") == "NOT_FOUND"


def result(
    check_id: str,
    workstream: str,
    criterion: str,
    test_method: str,
    status: str,
    detail: str = "",
    artifact: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": check_id,
        "workstream": workstream,
        "criterion": criterion,
        "test_method": test_method,
        "status": status,
        "detail": detail,
        "artifact": artifact,
    }
    if reason is not None:
        row["reason"] = reason
    return row


# Import the per-workstream check functions and build the registry.
from . import w1, w2, w3, w4, w5, w6  # noqa: E402

REGISTRY: List[Callable[[CheckContext], Dict[str, Any]]] = [
    w1.check_w1_1,
    w1.check_w1_2,
    w2.check_w2_1,
    w2.check_w2_2,
    w2.check_w2_3,
    w2.check_w2_4,
    w3.check_w3_1,
    w3.check_w3_2,
    w3.check_w3_3,
    w4.check_w4_1,
    w4.check_w4_2,
    w5.check_w5_1,
    w5.check_w5_2,
    w5.check_w5_3,
    w6.check_w6_1,
    w6.check_w6_2,
]
