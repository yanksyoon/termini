"""Shared value types for the AI-operator remediation loop.

These are plain dataclasses (no I/O, no side effects) so the detector, chooser,
and executor stay independently unit-testable. The loop (`loop.py`) is the only
piece that touches the database and the LXD REST client together.

The `type` vocabulary (oom | disk_fail | disk_degrade | latency) is the
detector's health condition. The `rule_id` is the frozen traceability id from
`docs/contracts/node-telemetry.md` §5:
    oom-guard-01, disk-fail-01, disk-degrade-01, gpu-oos-01 (plus the
    harness-only latency-01, which thresholds net[].latency_ms).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HealthEvent:
    """A structured health event emitted by the detector: the offending node,
    the frozen rule that fired, and the evidence window that justified it."""

    event_id: str
    node_id: str
    rule_id: str
    type: str                       # oom | disk_fail | disk_degrade | latency
    metric: str                     # e.g. ram_used_bytes, disk_free_bytes, net_latency_ms
    value: float | None             # the offending value (percent/bytes/ms or slope)
    threshold: str                  # human-readable threshold description
    fired_at: str                   # RFC3339 UTC (ms)
    evidence: list = field(default_factory=list)  # ordered sample dicts (oldest first)
    breach_started_at: str | None = None  # ts of the first breaching sample (latency base)
    episode: int = 0                # trigger fire counter for this (node, rule)


@dataclass
class RemediationAction:
    """The action selected by the policy/chooser from a declarative playbook."""

    kind: str                       # instance.restart | image.prune | ...
    params: dict = field(default_factory=dict)
    reason: str = ""
    playbook: str = ""              # playbook file that produced this action
    rule_id: str = ""


@dataclass
class ExecResult:
    """Outcome of a single executor operation against the LXD REST API."""

    result: str                     # ok | error | dry_run | already_applied
    detail: str = ""
    method: str = ""                # HTTP method that was (or would be) issued
    path: str = ""                  # LXD REST path
    body: str = ""                  # JSON body that was (or would be) sent
    status: int = 0                 # HTTP status when actually executed
    operation: str = ""             # LXD operation id, when returned
