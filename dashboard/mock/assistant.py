"""Deterministic stand-in for the assistant endpoint (controller-api.md §4.10).

Used by the dashboard mock server so the chat panel can be exercised — and
screenshotted — with no model and no controller. Every response is shaped
exactly like the controller's:

    {answer, citations[], executed[], proposed_actions[], model, decided_by,
     elapsed_ms, truncated, errors[]}

The three honest states the panel renders are all reachable from here:

* an unknown id answers 404, a bad `context.kind` or empty `messages` 422;
* the word "unavailable" in the question answers the typed
  `503 MODEL_UNAVAILABLE` (never a fabricated answer);
* a question that asks for an action returns `executed[]` carrying the REAL
  `command_id` (the decision record id, §4.10) and `audit_id` of the fixture
  row, so the panel's audit link lands on an audit row that exists.
"""

from __future__ import annotations

import fixtures as fx

ASSISTANT_CONTEXT_KINDS = ("incident", "decision", "node", "metric", "remediation", "audit", "none")

_MESSAGE_ROLES = ("system", "user", "assistant")
_CONTENT_MAX = 4000


def _err(code: str, message: str, status: int, details=None):
    return fx.err(code, message, status, details)


def assistant_answer(body: dict, bundle: dict, nodes: list[dict]):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _err("INVALID_ARGUMENT", "messages must be a non-empty array of {role, content}", 422,
                    [{"field": "messages", "reason": "required"}])
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            return _err("INVALID_ARGUMENT", "each message must be an object", 422,
                        [{"field": f"messages[{i}]", "reason": "not_an_object"}])
        if m.get("role") not in _MESSAGE_ROLES:
            return _err("INVALID_ARGUMENT",
                        f"messages[{i}].role must be one of system|user|assistant", 422,
                        [{"field": f"messages[{i}].role", "reason": "unknown_role"}])
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            return _err("INVALID_ARGUMENT", f"messages[{i}].content must be a non-empty string", 422,
                        [{"field": f"messages[{i}].content", "reason": "required"}])
        if len(content) > _CONTENT_MAX:
            return _err("INVALID_ARGUMENT", f"messages[{i}].content exceeds {_CONTENT_MAX} chars", 422,
                        [{"field": f"messages[{i}].content", "reason": "too_long"}])

    context = body.get("context") or {"kind": "none"}
    if not isinstance(context, dict):
        return _err("INVALID_ARGUMENT", "context must be an object when present", 422,
                    [{"field": "context", "reason": "not_an_object"}])
    kind = context.get("kind") or "none"
    ctx_id = context.get("id")
    if kind not in ASSISTANT_CONTEXT_KINDS:
        return _err("INVALID_ARGUMENT",
                    "context.kind must be one of " + "|".join(ASSISTANT_CONTEXT_KINDS), 422,
                    [{"field": "context.kind", "reason": "unknown_kind"}])
    if kind != "none" and not (isinstance(ctx_id, str) and ctx_id.strip()):
        return _err("INVALID_ARGUMENT", "context.id is required when context.kind != 'none'", 422,
                    [{"field": "context.id", "reason": "required"}])
    ctx_id = (ctx_id or "").strip()

    question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")

    def name_of(node_id):
        return next((n["registration_id"] for n in nodes if n["node_id"] == node_id), node_id)

    by_decision = next((d for d in bundle["decisions"] if d["id"] == ctx_id), None)
    by_event = next((e for e in bundle["events"] if e["id"] == ctx_id), None)
    by_audit = next((a for a in bundle["audits"] if a["id"] == ctx_id), None)
    node = next((n for n in nodes if n["node_id"] == ctx_id or n["registration_id"] == ctx_id), None)

    if kind in ("node", "metric") and node is None:
        return _err("NOT_FOUND", f"node {ctx_id} not found", 404)
    if kind == "decision" and by_decision is None:
        return _err("NOT_FOUND", f"decision {ctx_id} not found", 404)
    if kind == "incident" and by_event is None:
        return _err("NOT_FOUND", f"incident {ctx_id} not found", 404)
    if kind == "audit" and by_audit is None:
        return _err("NOT_FOUND", f"audit {ctx_id} not found", 404)
    if kind == "remediation":
        return _err("NOT_FOUND", f"remediation command {ctx_id} not found", 404)

    if "unavailable" in question.lower():
        return _err("MODEL_UNAVAILABLE",
                    "the model did not answer: ConnectionError: inference endpoint refused the request",
                    503)

    citations: list[dict] = []
    answer = ""

    if kind in ("node", "metric") and node is not None:
        answer = (
            f"{name_of(node['node_id'])} ({node['role']}) is {node.get('status', 'unknown')} in the "
            "mock fleet. Its latest sample is what the charts are drawn from; the metric window on "
            "this node is what I can ground an answer in."
        )
        citations = [{"kind": kind, "id": node["node_id"], "label": name_of(node["node_id"])}]
    elif kind == "decision" and by_decision is not None:
        d = by_decision
        audit = next((a for a in bundle["audits"] if a["decision_id"] == d["id"]), None)
        answer = (
            f"Decision {d['id']} ({d['decision_source']}) answered rule {d['rule_id']} on "
            f"{name_of(d['node_id'])} by choosing {d['action_kind']}. The record's reason: "
            f"{d['reason']} The audit row says the fleet answered "
            f"\"{audit['result'] if audit else 'unknown'}\"."
        )
        citations = [
            {"kind": "decision", "id": d["id"], "label": f"{d['rule_id']} → {d['action_kind']}"},
            {"kind": "node", "id": d["node_id"], "label": name_of(d["node_id"])},
        ]
        if audit:
            citations.append({"kind": "audit", "id": audit["id"], "label": f"audit {audit['result']}"})
    elif kind == "incident" and by_event is not None:
        e = by_event
        d = next((x for x in bundle["decisions"] if x["event_id"] == e["id"]), None) or {
            "action_kind": "no action",
            "decision_source": "unknown",
        }
        answer = (
            f"Incident {e['id']}: rule {e['rule_id']} fired on {name_of(e['node_id'])} with "
            f"{e['metric']} = {e['value']} against \"{e['threshold']}\". The decision that answered "
            f"it was {d['action_kind']} ({d['decision_source']})."
        )
        citations = [{"kind": "incident", "id": e["id"], "label": f"{e['rule_id']} on {e['metric']}"}]
        if d.get("id"):
            citations.append(
                {"kind": "decision", "id": d["id"], "label": f"{e['rule_id']} → {d['action_kind']}"})

    elif kind == "audit" and by_audit is not None:
        a = by_audit
        answer = (
            f"Audit {a['id']}: {a['action_kind']} on {name_of(a['node_id'])} returned "
            f"\"{a['result']}\" ({a['detail']}). It is the audit row for decision "
            f"{a['decision_id']} under rule {a['rule_id']}."
        )
        citations = [
            {"kind": "audit", "id": a["id"], "label": f"{a['action_kind']} · {a['result']}"},
            {"kind": "decision", "id": a["decision_id"], "label": a["rule_id"]},
        ]
    else:
        model = sum(1 for r in bundle["decisions"] if r["decision_source"] == "model")
        fallback = sum(1 for r in bundle["decisions"] if r["decision_source"] == "fallback")
        answer = (
            f"The mock fleet holds {len(nodes)} nodes and {len(bundle['decisions'])} decisions "
            f"({model} model, {fallback} fallback). Attach an incident, decision, node, metric, "
            "remediation or audit row to ground a question in one record."
        )
        citations = [{"kind": "node", "id": n["node_id"], "label": n["registration_id"]} for n in nodes]

    executed: list[dict] = []
    proposed: list[dict] = []
    acts = ("restart it", "stop it", "do it", "prune", "drain it", "execute")
    if any(w in question.lower() for w in acts) and by_decision is not None:
        d = by_decision
        audit = next((a for a in bundle["audits"] if a["decision_id"] == d["id"]), None)
        target = (d.get("action_params") or {}).get("instance_name", d["node_id"])
        executed = [{
            "action": d["action_kind"],
            "target": target,
            # §4.10: for executor actions command_id is the decision record id.
            "command_id": d["id"],
            "audit_id": audit["id"] if audit else None,
            "state": "ok" if audit and audit["result"] == "ok" else "error",
        }]
        proposed = [{"action": d["action_kind"], "target": target}]
        answer = (
            f"I ran {d['action_kind']} on the target after the guardrail admitted it. "
            "See executed[] for the command_id and audit_id."
        )

    return 200, {
        "answer": answer,
        "citations": citations,
        "executed": executed,
        "proposed_actions": proposed,
        "model": "mock-assistant",
        "decided_by": "assistant",
        "elapsed_ms": 12,
        "truncated": False,
        "errors": [],
    }
