"""Per-agent operations — start / stop / restart one agent instance.

Owner: kanban task `t_44a2b87b` (the client dashboard's Agents panel). The panel
must be able to act on the agents a client owns with the same human-audit shape
as the node Restart / Drain controls (`controller/app/commands.py`), so this
module deliberately does NOT invent a second command/audit path:

  1. resolve the agent identifier to the node + the LXD instance name
     (`agent_tasks.resolve_agent_target` — the identifier may be the instance
     name recorded at apply time OR the telemetry `vm_id` of
     node-telemetry.md §4.6, which is a one-way hash of the name);
  2. issue an ordinary command (`vm.start` / `vm.stop` / `vm.restart`) through
     `commands.issue_command` — the same allowlist, approval policy, `commands`
     row and operator-log line a human-issued `node.reboot` gets;
  3. execute it through the node's own LXD endpoint (`operator.target`), wait for
     the operation, and CONFIRM the resulting state (`LxdExecutor._confirm_state`
     is already part of every primitive) so "the agent restarted" is a measured
     fact, not a 202;
  4. close the command row (`running` -> `succeeded` / `failed`) and write one
     content-free operator-log line carrying the command id and the result.

Why execute synchronously when `node.reboot` only queues: a node reboot is
carried out by the host itself, while an instance state change is one LXD call
the controller can make and confirm on the node it already drives. The client
pressing "Restart" must see the state change, and the operator-log record must
say what actually happened (`failed` is a real outcome, not a timeout).

Audit shape, exactly as node actions: `commands.state` transition
queued -> running -> succeeded|failed, `commands.result_json` holding
`{result, detail, action}`, one `operator_logs` row with `actor=human` and the
`remediation` block (`command_id`, type, status). No instance name is written to
the command payload or a log line — only the client-known identifier (`vm_id`).
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import agent_tasks, commands as cmd_mod, errors, health, store

# The three operations the Agents panel exposes. The command type is what makes
# the action auditable in the frozen allowlist (`commands.COMMAND_ALLOWLIST`).
ACTIONS = {
    "start": "vm.start",
    "stop": "vm.stop",
    "restart": "vm.restart",
}


def perform_action(
    conn,
    cfg,
    project_id: str,
    vm_id: str,
    action: str,
    logs,
    *,
    executor_factory: Callable[[Any, dict], Any] | None = None,
) -> tuple[int, dict]:
    """Issue and execute one start/stop/restart for agent `vm_id`.

    Returns `(202, body)` on a completed attempt. A refused/ineffective LXD call
    is NOT a 5xx: the command was issued and is human-audited, so the attempt is
    reported with `state: "failed"` and its detail — the same way an operator
    audit row records `result=error`. Only "we cannot even name the target"
    (unknown agent, unreachable/offline node) is an API error.
    """
    if action not in ACTIONS:
        raise errors.not_found(f"unknown agent action {action}")

    resolution = agent_tasks.resolve_agent_target(
        conn, cfg, project_id, vm_id, executor_factory=executor_factory)
    if resolution.node is None:
        if resolution.reason == "node_unreachable":
            raise errors.err(
                503, "NODE_UNREACHABLE",
                f"the node holding agent {vm_id} cannot be reached, so its state "
                f"cannot be established",
                [{"field": "vm_id",
                  "reason": "node_unreachable",
                  "unscanned_nodes": resolution.unscanned_nodes,
                  "attributed_nodes": resolution.telemetry_node_ids}])
        raise errors.err(
            404, "AGENT_NOT_FOUND",
            f"no agent {vm_id} on this project's nodes",
            [{"field": "vm_id",
              "reason": "unknown",
              "literal_name": vm_id,
              "derived_agent_id": agent_tasks.agent_id_for_name(vm_id),
              "sources": resolution.sources,
              "known_agent_ids": resolution.known_agent_ids[:20],
              "unscanned_nodes": resolution.unscanned_nodes}])
    node = resolution.node
    instance_name = resolution.instance_name

    sample = store.latest_sample(conn, node["id"])
    node_status, _ = health.node_status_health(node, sample, time.time())
    if node_status == "offline":
        raise errors.err(
            503, "NODE_OFFLINE",
            f"node {node['id']} is offline; agent {vm_id} cannot be {action}ed",
            [{"field": "vm_id", "reason": "node_offline"}])

    command_type = ACTIONS[action]
    command = cmd_mod.issue_command(
        conn,
        logs,
        project_id,
        command_type,
        # Only the client-known identifier: an instance name may carry customer
        # data and is never written to a command payload or a log line.
        {"vm_id": vm_id, "action": action},
        "human",
        node_id=node["id"],
    )

    from .operator import target as op_target

    factory = executor_factory or op_target.executor_for_node
    executor = factory(cfg, node)

    store.transition_command(conn, command["command_id"], state="running",
                             acked_at=store.now_iso())
    conn.commit()

    started = time.monotonic()
    try:
        result = _run(executor, action, instance_name or vm_id, cfg)
    except Exception as exc:  # noqa: BLE001 — an executor fault must be recorded
        result = _ErrorResult(f"{type(exc).__name__}: {exc}")
    latency_ms = int((time.monotonic() - started) * 1000)

    ok = getattr(result, "result", "error") in ("ok", "already_applied")
    state = "succeeded" if ok else "failed"
    detail = getattr(result, "detail", "") or ""
    payload = {
        "result": getattr(result, "result", "error"),
        "detail": detail[:600],
        "action": action,
        "latency_ms": latency_ms,
    }
    store.transition_command(
        conn, command["command_id"], state=state, completed_at=store.now_iso(),
        result_json=json.dumps(payload))
    logs.append(
        conn,
        project_id,
        node_id=node["id"],
        severity="info" if ok else "warn",
        actor="human",
        message=(f"{command_type} {vm_id} -> {payload['result']} "
                 f"({latency_ms} ms)"),
        remediation={"command_id": command["command_id"],
                     "type": command_type, "status": state},
    )
    conn.commit()

    return 202, {
        "command_id": command["command_id"],
        "type": command_type,
        "state": state,
        "issued_by": "human",
        "agent_id": vm_id,
        "node_id": node["id"],
        "action": action,
        "result": payload,
        "completed_at": store.now_iso(),
    }


class _ErrorResult:
    def __init__(self, detail: str):
        self.result = "error"
        self.detail = detail


def _run(executor, action: str, instance_name: str, cfg):
    """The LXD primitive for one action, with the configured timeouts.

    `restart_instance` already reads the state first: a restart on a STOPPED
    instance is a no-op LXD accepts, so the primitive starts it instead — a
    "restart" from the panel therefore always ends RUNNING, which is what the
    button promises.
    """
    if action == "start":
        return executor.start_instance(
            instance_name,
            timeout_s=float(getattr(cfg, "lxd_start_timeout_s",
                                    getattr(cfg, "lxd_create_timeout_s", 120.0))))
    if action == "stop":
        return executor.stop_instance(instance_name, force=True)
    return executor.restart_instance(instance_name, force=True)
