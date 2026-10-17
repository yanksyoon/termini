"""Remediation / operation command store + state machine (dashboard.md §4.4,
controller-api.md §5/§6).

The command types are the frozen ``commandAllowlist()`` set — the AI operator may
select among these only; free-form shell is never executed. Each type carries an
``approval`` policy (dashboard.md §4.4, frozen):

    auto       vm.resize, vm.migrate, vm.start, vm.stop, vm.restart,
               ssh.inject_key, agent.dispatch_task, agent.cancel_task
    required   vm.destroy, node.drain, node.reboot

Human-issued commands (the dashboard intervention controls) are pre-authorized
and go straight to ``queued``; AI-operator-issued ``required`` commands land in
``pending_approval`` until a human approves/denies them. Assistant-issued
commands (``issued_by: assistant``, t_bfd2c2f7) also go straight to ``queued``:
the assistant's authority is guardrail admission, not a human decision (owner
decision D1) — the operator approval gate is unchanged.
"""
from __future__ import annotations

from typing import Any

from . import errors, store, ulid

COMMAND_ALLOWLIST = {
    "vm.resize", "vm.migrate", "vm.stop", "vm.start", "vm.restart", "vm.destroy",
    "node.drain", "node.reboot",
    "agent.dispatch_task", "agent.cancel_task",
}

# dashboard.md §4.4 — frozen approval policy.
APPROVAL_POLICY = {
    "vm.resize": "auto",
    "vm.migrate": "auto",
    "vm.start": "auto",
    "vm.stop": "auto",
    "vm.restart": "auto",
    "vm.destroy": "required",
    "node.drain": "required",
    "node.reboot": "required",
    "agent.dispatch_task": "auto",
    "agent.cancel_task": "auto",
}

DRAIN_REASONS = ("disk_degradation", "disk_failure", "maintenance", "manual")

# State machine (dashboard.md §4.4). Transitions are validated explicitly.
PENDING = "pending_approval"
APPROVED = "approved"
DENIED = "denied"
QUEUED = "queued"


def approval_for(command_type: str) -> str:
    return APPROVAL_POLICY.get(command_type, "auto")


def initial_state(command_type: str, issued_by: str) -> str:
    """Human- and assistant-issued commands skip approval; AI commands honor the policy.

    Assistant-initiated commands (owner decision D1, t_bfd2c2f7) carry
    ``issued_by: assistant`` and execute without the ``pending_approval`` gate —
    the assistant's authority is the guardrail admission, not a human decision.
    The operator/playbook ``pending_approval -> approve/deny`` flow is unchanged
    for ``ai_operator``-issued commands.
    """
    if issued_by in ("human", "assistant"):
        return QUEUED
    return PENDING if approval_for(command_type) == "required" else QUEUED


def issue_command(
    conn,
    logs,
    project_id: str,
    command_type: str,
    payload: dict,
    issued_by: str,
    *,
    node_id: str | None = None,
    trigger: dict | None = None,
    timeout_s: int = 60,
) -> dict:
    """Insert a command, record its log line, and broadcast the remediation event."""
    if command_type not in COMMAND_ALLOWLIST:
        raise errors.invalid_argument(
            f"unknown command type {command_type!r}",
            [{"field": "type", "reason": "not_in_command_allowlist"}],
        )
    if issued_by not in ("ai_operator", "human", "system", "assistant"):
        issued_by = "human"

    state = initial_state(command_type, issued_by)
    cmd = {
        "command_id": ulid.new_ulid(),
        "project_id": project_id,
        "node_id": node_id,
        "type": command_type,
        "payload": payload,
        "approval": approval_for(command_type),
        "state": state,
        "issued_by": issued_by,
        "issued_at": store.now_iso(),
        "timeout_s": timeout_s,
        "trigger": trigger,
    }
    store.insert_command(conn, cmd)
    remediation = {"command_id": cmd["command_id"], "type": command_type, "status": state}
    logs.append(
        conn, project_id,
        node_id=node_id,
        severity="info",
        actor=issued_by if issued_by in ("ai_operator", "human", "assistant") else "ai_operator",
        message=f"{command_type} issued" + (f" on node {node_id}" if node_id else ""),
        remediation=remediation,
    )
    logs.broker.publish("remediation.queued", _command_view(cmd), project_id)
    return cmd


def approve_command(conn, logs, project_id: str, command_id: str, by: str = "human") -> dict:
    """Approve a pending_approval command. Returns the approval response."""
    cmd = store.get_command(conn, command_id)
    if cmd is None or cmd["project_id"] != project_id:
        raise errors.not_found(f"command {command_id} not found")
    if cmd["approval"] != "required":
        raise errors.err(409, "CONFLICT", "command does not require approval")
    if cmd["state"] != PENDING:
        raise errors.err(409, "CONFLICT", f"command already {cmd['state']}")

    now = store.now_iso()
    store.transition_command(
        conn, command_id, state=APPROVED, approved_at=now, approved_by=by
    )
    remediation = {"command_id": command_id, "type": cmd["type"], "status": APPROVED}
    logs.append(
        conn, project_id,
        node_id=cmd["node_id"],
        severity="info",
        actor="human",
        message=f"approved {cmd['type']} remediation",
        remediation=remediation,
    )
    return {
        "command_id": command_id,
        "state": APPROVED,
        "approved_by": by,
        "approved_at": now,
    }


def deny_command(conn, logs, project_id: str, command_id: str, reason: str, by: str = "human") -> dict:
    """Deny a pending_approval command. Returns the denial response."""
    cmd = store.get_command(conn, command_id)
    if cmd is None or cmd["project_id"] != project_id:
        raise errors.not_found(f"command {command_id} not found")
    if cmd["approval"] != "required":
        raise errors.err(409, "CONFLICT", "command does not require approval")
    if cmd["state"] != PENDING:
        raise errors.err(409, "CONFLICT", f"command already {cmd['state']}")

    now = store.now_iso()
    store.transition_command(
        conn, command_id, state=DENIED, denied_at=now, denied_by=by, deny_reason=reason
    )
    remediation = {"command_id": command_id, "type": cmd["type"], "status": DENIED}
    logs.append(
        conn, project_id,
        node_id=cmd["node_id"],
        severity="warn",
        actor="human",
        message=f"denied {cmd['type']} remediation" + (f": {reason}" if reason else ""),
        remediation=remediation,
    )
    return {
        "command_id": command_id,
        "state": DENIED,
        "denied_by": by,
        "denied_at": now,
    }


def expire_pending(conn, logs, project_id: str, timeout_s: int) -> int:
    """Auto-deny pending_approval commands older than ``timeout_s`` (dashboard.md
    §4.4). Returns the number of commands denied."""
    from .validate import parse_rfc3339_epoch

    import time as _time
    cutoff = _time.time() - timeout_s
    count = 0
    for cmd in store.pending_approval_commands(conn, project_id):
        try:
            age = _time.time() - parse_rfc3339_epoch(cmd["issued_at"])
        except (ValueError, KeyError):
            continue
        if age >= timeout_s:
            now = store.now_iso()
            store.transition_command(
                conn, cmd["command_id"], state=DENIED, denied_at=now,
                denied_by="system", deny_reason="approval_timeout",
            )
            remediation = {"command_id": cmd["command_id"], "type": cmd["type"], "status": DENIED}
            logs.append(
                conn, project_id,
                node_id=cmd["node_id"],
                severity="warn",
                actor="ai_operator",
                message=f"{cmd['type']} remediation auto-denied (approval timeout)",
                remediation=remediation,
            )
            count += 1
    return count


def _command_view(cmd: dict) -> dict:
    return {
        "command_id": cmd["command_id"],
        "type": cmd["type"],
        "payload": cmd.get("payload") or {},
        "issued_by": cmd["issued_by"],
        "issued_at": cmd["issued_at"],
        "timeout_s": cmd.get("timeout_s", 60),
        "approval": cmd.get("approval"),
        "state": cmd.get("state"),
        "node_id": cmd.get("node_id"),
        "trigger": cmd.get("trigger"),
    }
