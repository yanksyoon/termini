"""Agent task API — give an agent a task and read its answer (agent-runtime.md §5.6/§7.4).

Owner: kanban task `t_1a2c913c`. This module is the controller half of the task
surface the in-VM runtime exposes: it takes a task for a named agent `vm_id`,
records it, hands it to the agent, and follows it to a terminal state.

Decision **AT-1 — the controller PROXIES; it does not hand the client a connect
handle.** The alternative (return `{host, port, token}` and let the client talk to
the instance) was rejected, and the consequence of choosing the proxy is stated
here because the card asks for it:

  * a connect handle would require the client to reach the guest network. In this
    deployment there is no route from outside the node to an instance's bridge
    (`10.174.247.0/24`), and the agent's surface is deliberately bound to
    `127.0.0.1` inside the instance — exposing it would mean binding it to the
    guest bridge and distributing a per-instance credential to the browser. That
    is a strictly larger attack surface than the controller the client already
    authenticates to with its project token.
  * the cost of the proxy is that task content (C2) transits the controller. It is
    handled exactly as agent-runtime.md §7.1 already requires for the inbound
    direction: relayed transiently, never written to a log line, never sent to an
    external model. The task RECORD is the client's own read path for the answer
    (the client's dashboard reads it back through the same project token, and the
    runtime also leaves the body on the premises at
    `/var/lib/termini/agent/results/<task_id>.json`, §7.3) — the controller is not
    a second home for the content, it is the only channel the client has.
  * the client never receives instance credentials and never needs a route to the
    instance; the transport is the controller's own authenticated path to the
    node's LXD (`controller-api.md` §5/§6: the controller drives the resolved
    node's LXD REST API), running one `exec` inside the instance to speak to the
    agent's loopback surface.

Failure semantics (`agent-runtime.md` §7.4): every non-`done` task is an `error`
with a distinct code, so a slow model call can never be read as a finished task.
Transport-level failures carry `AGENT_UNREACHABLE` / `AGENT_NOT_FOUND` /
`NODE_OFFLINE`; anything the in-VM surface itself refused keeps the runtime's own
code (`TASK_BOUND_EXCEEDED`, `MODEL_NOT_ALLOWED`, `INFERENCE_UNREACHABLE`, ...).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Callable

from . import db, errors, store, ulid

log = logging.getLogger("termini.agent_tasks")

# State machine (agent-runtime.md §7.4). `done`/`error` are terminal.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"
TERMINAL = (DONE, ERROR)

AGENT_SURFACE_PORT = 9101          # the runtime's loopback listener (agent-runtime.md §5.1)
DEFAULT_TASK_TIMEOUT_S = 300.0     # controller-side deadline for one task
DEFAULT_POLL_INTERVAL_S = 2.0
EXEC_TIMEOUT_S = 15.0
MAX_PROMPT_BYTES = 8192

# The in-instance client. Run through the node's LXD `exec` API, so it must be a
# program the baked image already has: /usr/bin/python3 (3.12) with stdlib only.
REMOTE_CLIENT = r'''
import json, sys, urllib.request, urllib.error
method, url, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
payload = sys.argv[4] if len(sys.argv) > 4 else None
data = payload.encode("utf-8") if payload else None
req = urllib.request.Request(url, data=data, method=method,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        print(json.dumps({"status": r.status, "body": r.read().decode("utf-8", "replace"),
                          "retry_after": r.headers.get("Retry-After")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "body": e.read().decode("utf-8", "replace"),
                      "retry_after": (e.headers.get("Retry-After") if e.headers else None)}))
except Exception as e:                                  # connect refused / timeout / bad json
    print(json.dumps({"status": 0, "body": json.dumps(
        {"error": type(e).__name__ + ": " + str(e)})}))
'''


class AgentTaskError(RuntimeError):
    """Raised when a task cannot even be handed over (mapped to an API error)."""

    def __init__(self, code: str, message: str, *, status: int = 400,
                 actionable: str | None = None, param: str | None = None,
                 retry_after: str | int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.actionable = actionable
        self.param = param
        # `Retry-After` the agent answered with, forwarded unchanged so an
        # over-bound client is told WHEN to retry instead of guessing (§5.6).
        self.retry_after = retry_after


# --------------------------------------------------------------------------- store
def insert_task(conn, *, project_id: str, vm_id: str, node_id: str | None,
                command_id: str | None, actor: str, prompt: str,
                model_preference: str | None, max_tokens: int,
                timeout_s: float, now: str | None = None) -> dict:
    """Insert one task and return the FULL record (content included).

    The full record is what drives dispatch/polling; the HTTP layer answers with
    `get_task`/`list_tasks`, whose views are the API shape (no prompt). Keeping the
    two apart is deliberate: the content exists for the relay but is not something a
    listing hands out.
    """
    task_id = ulid.new_ulid()
    now = now or store.now_iso()
    conn.execute(
        "INSERT INTO agent_tasks (task_id, project_id, vm_id, node_id, command_id,"
        " state, actor, prompt, model_preference, max_tokens, submitted_at, deadline_s)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, vm_id, node_id, command_id, QUEUED, actor, prompt,
         model_preference, int(max_tokens), now, float(timeout_s)),
    )
    row = conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)).fetchone()
    return {k: row[k] for k in row.keys()}


def get_task(conn, task_id: str, project_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_tasks WHERE task_id = ? AND project_id = ?",
        (task_id, project_id),
    ).fetchone()
    return _row_view(row) if row else None


def list_tasks(conn, project_id: str, *, vm_id: str | None = None,
               limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    if vm_id:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE project_id = ? AND vm_id = ?"
            " ORDER BY task_id DESC LIMIT ?", (project_id, vm_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE project_id = ?"
            " ORDER BY task_id DESC LIMIT ?", (project_id, limit)).fetchall()
    return [_row_view(r, include_answer=False) for r in rows]


def update_task(conn, task_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE agent_tasks SET {cols} WHERE task_id = ?",
                 (*fields.values(), task_id))
    conn.commit()


def sweep_orphans(conn, project_id: str, *, now: float | None = None) -> int:
    """Fail tasks this process inherited as non-terminal (a restart dropped the poller).

    A row stuck in `queued`/`running` after a restart would otherwise look like a
    task that is merely slow — exactly the confusion the contract forbids. It is
    marked `error` with an actionable code instead.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT task_id FROM agent_tasks WHERE project_id = ? AND state IN (?, ?)",
        (project_id, QUEUED, RUNNING)).fetchall()
    for row in rows:
        update_task(conn, row["task_id"], state=ERROR,
                    error_code="TASK_ORPHANED",
                    error_message="the controller restarted while this task was in flight",
                    ended_at=store.now_iso())
    return len(rows)


def _row_view(row, *, include_answer: bool = True) -> dict:
    out = {
        "task_id": row["task_id"],
        "vm_id": row["vm_id"],
        "node_id": row["node_id"],
        "state": row["state"],
        "actor": row["actor"],
        "model_preference": row["model_preference"],
        "model": row["model"],
        "max_tokens": row["max_tokens"],
        "command_id": row["command_id"],
        "agent_task_id": row["agent_task_id"],
        "submitted_at": row["submitted_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_ms": row["duration_ms"],
        "provenance": _json(row["provenance_json"], {}),
        "usage": _json(row["usage_json"], {}),
        "error": _json(row["error_json"], None),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "polls": row["polls"],
    }
    if include_answer:
        out["answer"] = row["answer"]
    return out


def _json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


# ------------------------------------------------------------------------ resolve
def resolve_agent(conn, cfg, project_id: str, vm_id: str, *,
                  executor_factory: Callable[[Any, dict], Any] | None = None
                  ) -> tuple[dict | None, str | None, list[str]]:
    """Which node holds this agent instance? Returns `(node, reason, known_vm_ids)`.

    `vm_id` **is** the LXD instance name (`controller-api.md` §5: "`vm_ids` are the
    LXD instance names of the created workloads"), so the authoritative answer is
    the node whose LXD actually knows the instance. The live-allocation record is
    consulted first because it is free and covers everything the controller
    materialised; when it does not mention the id, each registered node's LXD is
    asked — an instance that was launched by hand, migrated, or materialised by an
    earlier controller process is still addressable, which is what makes
    "a client can give an agent a task" true rather than "…only to agents this
    process launched".

    Returns a reason string on failure: `unknown_vm` (no node has it) or
    `node_unreachable` (the owning node's LXD cannot be reached, so its state —
    including the instance's existence — cannot be established).
    """
    import json as _json

    from .operator import target as op_target

    factory = executor_factory or op_target.executor_for_node
    known: list[str] = []
    node_id = None
    for row in store.live_allocations(conn, project_id):
        ids = row.get("vm_ids") or []
        if not isinstance(ids, list):
            continue
        known.extend(str(i) for i in ids)
        if vm_id in ids:
            node_id = row["node_id"]
            break
    if node_id:
        node = store.get_node(conn, node_id)
        if node is not None:
            return node, None, known

    for node in store.list_nodes(conn, project_id):
        if node.get("synthetic"):
            continue
        executor = factory(cfg, node)
        try:
            ok, live = executor.list_instance_names()
        except Exception:  # noqa: BLE001 — an unreachable node is not an empty node
            ok, live = False, []
        if not ok:
            continue
        known.extend(str(n) for n in live)
        if vm_id in live:
            return node, None, sorted(set(known))

    # Nothing claimed it. Distinguish "the node that should have it is down" from
    # "no such agent", because the operator's next action differs.
    if any(vm_id == k for k in known):
        return None, "node_unreachable", sorted(set(known))
    return None, "unknown_vm", sorted(set(known))


# ----------------------------------------------------------------------- dispatch
def _call_agent_surface(cfg, node, vm_id: str, *, method: str, path: str,
                        payload: dict | None = None) -> tuple[int, dict, str | None]:
    """One request against the agent's loopback surface, through the node's LXD.

    Returns `(status, body, retry_after)` — the last is the agent's own
    `Retry-After` header, carried out so a caller can forward it unchanged.
    """
    from .operator import target as op_target

    executor = op_target.executor_for_node(cfg, node)
    if not hasattr(executor, "exec_instance"):
        raise AgentTaskError(
            "AGENT_UNREACHABLE",
            "this controller cannot exec into instances (executor without exec support)",
            status=503,
        )
    command = ["/usr/bin/python3", "-c", REMOTE_CLIENT, method,
               f"http://127.0.0.1:{AGENT_SURFACE_PORT}{path}",
               str(EXEC_TIMEOUT_S)]
    if payload is not None:
        command.append(json.dumps(payload))
    result = executor.exec_instance(vm_id, command, timeout_s=EXEC_TIMEOUT_S * 2)
    if result.result != "ok":
        raise AgentTaskError(
            "AGENT_UNREACHABLE",
            f"cannot reach agent {vm_id} on node {node.get('id')}: {result.detail}",
            status=503,
            actionable=("the instance is up but its agent runtime did not answer; "
                        "check `systemctl status termini-agent` inside it"),
        )
    try:
        outer = json.loads(result.detail or "{}")
    except json.JSONDecodeError as exc:
        raise AgentTaskError(
            "AGENT_UNREACHABLE",
            f"agent {vm_id} returned an unreadable answer: {exc}",
            status=502,
        ) from exc
    status = int(outer.get("status") or 0)
    if status == 0:
        raise AgentTaskError(
            "AGENT_UNREACHABLE",
            f"agent {vm_id} has no task surface on 127.0.0.1:{AGENT_SURFACE_PORT}: "
            f"{outer.get('body')}",
            status=503,
            actionable=("the instance's termini-agent must expose the task surface "
                        "(agent-runtime.md §5.6); an instance booted from an image "
                        "that predates it will answer 503 here"),
        )
    try:
        body = json.loads(outer.get("body") or "{}")
    except json.JSONDecodeError:
        body = {"raw": outer.get("body")}
    return status, body, outer.get("retry_after")


def submit_to_agent(cfg, node, task: dict) -> dict:
    """Hand one recorded task to the agent. Raises AgentTaskError if refused."""
    payload = {"prompt": task["prompt"], "actor": task["actor"],
               "max_tokens": task["max_tokens"]}
    if task["model_preference"]:
        payload["model_preference"] = task["model_preference"]
    status, body, retry_after = _call_agent_surface(
        cfg, node, task["vm_id"], method="POST", path="/v1/tasks", payload=payload)
    if status >= 400:
        err = (body or {}).get("error") or {}
        # Preserve the agent's own refusal semantics where they are actionable: a
        # bound refusal must stay a 429 (retry later), an oversize prompt a 413, a
        # refused alias a 400 — never flattened into one generic error.
        mapped = status if status in (400, 413, 429) else 503
        raise AgentTaskError(
            str(err.get("code") or "AGENT_REJECTED"),
            str(err.get("message") or f"agent refused the task (HTTP {status})"),
            status=mapped,
            actionable=err.get("actionable"),
            # The agent names the offending field (`model_preference` for a
            # refused alias, `prompt` for an oversize body); carry it through so
            # `details[0].field` is not always `vm_id` for a request-shape error.
            param=err.get("param"),
            # §5.6 promises `Retry-After` on the bound refusal; the agent sends
            # it, so it must survive the proxy rather than dying here.
            retry_after=retry_after,
        )
    return body


def read_agent_task(cfg, node, vm_id: str, agent_task_id: str) -> tuple[int, dict]:
    status, body, _ = _call_agent_surface(cfg, node, vm_id, method="GET",
                                         path=f"/v1/tasks/{agent_task_id}")
    return status, body


# ------------------------------------------------------------------------- poller
def poll_until_terminal(cfg, project_id: str, task_id: str, vm_id: str, node_id: str,
                        logs=None, *, timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
                        interval_s: float = DEFAULT_POLL_INTERVAL_S,
                        sleep: Callable[[float], None] = time.sleep) -> None:
    """Follow one dispatched task to `done`/`error` and record the outcome.

    Runs on its own thread with its own DB connection: the agent's answer is the
    customer's content and a slow model call must not hold a request open. Every
    transition updates the task row (the enumerable record) and writes ONE
    content-free operator-log line at the end, so the submission is human-audited
    exactly like a node action.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        sleep(interval_s)
        try:
            with db.session(cfg.db_path) as conn:
                task = get_task(conn, task_id, project_id)
                if task is None or task["state"] in TERMINAL:
                    return
                node = store.get_node(conn, node_id)
                if node is None:
                    _finish(conn, project_id, task_id, state=ERROR,
                            error_code="NODE_GONE",
                            error_message=f"node {node_id} is no longer registered",
                            logs=logs)
                    return
                try:
                    status, body = read_agent_task(cfg, node, vm_id,
                                                   task["agent_task_id"] or "")
                except AgentTaskError as exc:
                    update_task(conn, task_id, polls=task["polls"] + 1,
                                last_error=str(exc))
                    conn.commit()
                    continue
                state = str(body.get("state") or "")
                if state in TERMINAL:
                    _finish_from_body(conn, project_id, task_id, body, logs=logs)
                    return
                new_state = RUNNING if state == RUNNING else task["state"]
                update_task(conn, task_id, state=new_state,
                            started_at=task["started_at"] or body.get("started_at"),
                            agent_task_id=body.get("task_id") or task["agent_task_id"],
                            polls=task["polls"] + 1)
                conn.commit()
                if new_state != task["state"]:
                    _publish(logs, "task.state_changed",
                             {"task_id": task_id, "vm_id": vm_id, "state": new_state},
                             project_id)
        except sqlite3.OperationalError as exc:
            # The record store itself is unreadable (db removed / volume gone). The
            # task cannot be followed any further and spinning would only burn CPU;
            # a task row that no longer exists is not something to keep polling.
            log.warning("task poll stopped for %s: %s", task_id, exc)
            return
        except Exception:  # noqa: BLE001 — a poller must never die silently mid-task
            log.exception("task poll failed for %s", task_id)
            continue

    with db.session(cfg.db_path) as conn:
        task = get_task(conn, task_id, project_id)
        if task is not None and task["state"] not in TERMINAL:
            _finish(conn, project_id, task_id, state=ERROR,
                    error_code="TASK_TIMEOUT",
                    error_message=(f"task did not reach a terminal state within "
                                   f"{timeout_s:.0f}s"),
                    logs=logs,
                    actionable="the agent accepted the task but never finished it")


def _finish_from_body(conn, project_id: str, task_id: str, body: dict, *,
                      logs=None) -> None:
    state = body.get("state") or ERROR
    err = body.get("error") or {}
    _finish(conn, project_id, task_id, state=state,
            answer=body.get("answer"),
            model=body.get("model"),
            provenance=body.get("provenance"),
            usage=body.get("usage"),
            started_at=body.get("started_at"),
            duration_ms=body.get("duration_ms"),
            error_code=err.get("code"),
            error_message=err.get("message"),
            error=err or None,
            result_ref=body.get("result_ref"),
            logs=logs)


def fail_task(conn, project_id: str, task_id: str, *, code: str, message: str,
              actionable: str | None = None, logs=None) -> None:
    """Record a task that never reached the agent (transport-level failure).

    Called by the HTTP handler when the dispatch itself was refused or unreachable,
    so the failure is enumerable and audited instead of existing only in the error
    response the caller saw.
    """
    _finish(conn, project_id, task_id, state=ERROR, error_code=code,
            error_message=message,
            error={"code": code, "message": message,
                   **({"actionable": actionable} if actionable else {})},
            logs=logs)


def _finish(conn, project_id: str, task_id: str, *, state: str, answer: str | None = None,
            model: str | None = None, provenance: dict | None = None,
            usage: dict | None = None, started_at: str | None = None,
            duration_ms: int | None = None, error_code: str | None = None,
            error_message: str | None = None, error: dict | None = None,
            result_ref: str | None = None, logs=None, actionable: str | None = None) -> None:
    task = get_task(conn, task_id, project_id)
    if task is None or task["state"] in TERMINAL:
        return
    if error and actionable and not error.get("actionable"):
        error = dict(error, actionable=actionable)
    update_task(
        conn, task_id, state=state, answer=answer, model=model,
        provenance_json=json.dumps(provenance) if provenance else None,
        usage_json=json.dumps(usage) if usage else None,
        started_at=started_at or task["started_at"], ended_at=store.now_iso(),
        duration_ms=duration_ms, error_code=error_code, error_message=error_message,
        error_json=json.dumps(error) if error else None,
    )
    _mark_command(conn, task.get("command_id"), state)
    if logs is not None:
        # Content-free by construction: ids, state, model, timing — never the
        # prompt or the answer (governance §4; agent-runtime.md §13.1).
        try:
            logs.append(
                conn, project_id,
                node_id=task.get("node_id"),
                severity="info" if state == DONE else "warn",
                actor="human",
                message=(f"agent task {task_id} on {task['vm_id']} {state}"
                         + (f" (model {model}, {duration_ms}ms)" if state == DONE else
                            f" ({error_code}: {error_message})")),
                remediation={"command_id": task.get("command_id"),
                             "type": "agent.dispatch_task", "status": state},
            )
        except Exception:  # noqa: BLE001 — the record is already updated
            log.exception("could not write the task audit line for %s", task_id)
        _publish(logs, "task.completed",
                 {"task_id": task_id, "vm_id": task["vm_id"], "state": state,
                  "duration_ms": duration_ms, "model": model},
                 project_id)


def _mark_command(conn, command_id: str | None, state: str) -> None:
    """Fold the audited dispatch command to its terminal state (dashboard.md §4.4)."""
    if not command_id:
        return
    cmd_state = "succeeded" if state == DONE else "failed"
    try:
        store.transition_command(conn, command_id, state=cmd_state,
                                 completed_at=store.now_iso())
        conn.commit()
    except Exception:  # noqa: BLE001 — never fail the task over its audit row
        log.exception("could not close command %s", command_id)


def _publish(logs, event: str, data: dict, project_id: str) -> None:
    broker = getattr(logs, "broker", None)
    if broker is not None:
        try:
            broker.publish(event, data, project_id)
        except Exception:  # noqa: BLE001
            log.exception("could not publish %s", event)
