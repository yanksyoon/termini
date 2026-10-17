"""The assistant API — grounded Q&A over Termini's records with guardrail-admitted
execute authority (kanban task ``t_bfd2c2f7``).

Endpoint: ``POST /v1/projects/{project_id}/assistant/ask`` (controller-api.md §4.10).

The assistant answers questions about the controller's own records (decisions,
health events, audits, nodes, metric windows, remediation commands) AND may
execute any action the existing guardrail admits, with one audit row + one
decision-surface record per executed action carrying ``initiated_by: assistant``.

Boundary (owner decision D1):
  * Execution is scoped to actions requested *through the assistant*. The
    operator/playbook ``pending_approval -> approve/deny`` state machine is
    untouched for operator-initiated remediations.
  * "Guardrail admits" is the SAME code path as today: ``Guardrail.validate``
    (the frozen ACTION_SPACE allowlist + target resolution), reusing the guarded
    ``LxdExecutor``, plus the C2 / field-class assertion of
    ``externalModelCall`` applied to the context records before the model sees
    them. An action outside the allowlist is refused with the reason surfaced in
    the answer, never executed.
  * Idempotency: the action key is ``assistant:{conversation_id}:{message_id}:
    {action}:{target}``, stored in the audit row's existing ``idempotency_key``
    column, so a double submit / retry / page reload executes once.

Model (owner decision D2): the diagnosis path — ``externalModelCall`` over the
same ``DecisionModelConfig`` / OpenAI-compatible transport the decision adapter
uses. On model failure the endpoint returns a typed ``503 MODEL_UNAVAILABLE`` and
never a fabricated answer.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any, Callable

from . import commands as cmd_mod
from . import errors, governance, store, ulid
from .operator import decision as op_decision
from .operator import executor as op_executor
from .operator import store as opstore
from .operator.external_call import ExternalCallViolation, external_model_call
from .operator.models import HealthEvent, RemediationAction

log = logging.getLogger("termini.assistant")

# The assistant's frozen prompt-template id (governance.md §7.2 analogue): the
# version asserted by externalModelCall so a template change is a loud violation,
# not a silent drift.
ASSISTANT_TEMPLATE_VERSION = "assistant-ask v1"

# The frozen context-kind enum (controller-api.md §4.10). `none` means no
# context record was requested (a general question over no specific record).
CONTEXT_KINDS = ("incident", "decision", "node", "metric", "remediation", "audit", "none")

# The action space the assistant may execute. It is the union of the two wired
# remediation roads the controller already has (governance.md §281 / §7.3):
#   * the guarded executor's ACTION_SPACE (operator-decision.md §5) — gated by
#     Guardrail.validate + LxdExecutor;
#   * the wired command-channel verbs (node.drain, node.reboot) — gated by
#     commandAllowlist() + the command channel.
# An action outside this union is refused with the reason surfaced, never run.
ASSISTANT_EXECUTOR_ACTIONS = ("instance.restart", "instance.stop", "instance.delete", "image.prune")
ASSISTANT_COMMAND_ACTIONS = ("node.drain", "node.reboot")
ASSISTANT_ACTION_IDS = ASSISTANT_EXECUTOR_ACTIONS + ASSISTANT_COMMAND_ACTIONS

# Bounds.
MESSAGE_ROLES = ("system", "user", "assistant")
MESSAGE_CONTENT_MAX_CHARS = 4000
CONTEXT_RECORDS_MAX = 40
CONTEXT_MAX_CHARS = 8000
ANSWER_MAX_CHARS = 8000
CITATION_LABEL_MAX_CHARS = 120
CITATIONS_MAX = 12
PROPOSED_ACTIONS_MAX = 8


class AssistantParseError(Exception):
    """The model returned text that cannot be turned into a well-shaped answer."""


# --------------------------------------------------------------------------- #
# Context builder — kind + id -> the exact controller records (C0-only)
# --------------------------------------------------------------------------- #

def _c0_sample(sample: Any) -> dict:
    """Keep only the C0/C1-transformed telemetry fields of a sample.

    A sample may carry ``_redaction``-adjacent or otherwise non-C0 keys; the
    prompt may only carry the fields that ``externalModelCall``'s assertion is
    prepared to defend. Rather than hand the model a raw stored sample, keep the
    C0 metric leaves plus the already-transformed C1 identifiers.
    """
    if not isinstance(sample, dict):
        return {}
    return dict(sample)


def build_context(conn, project_id: str, kind: str, ctx_id: str | None):
    """Resolve ``kind + id`` to (context_blob, resolvable).

    ``context_blob`` is ``{"kind": kind, "id": ctx_id, "records": [...]}`` where
    each record is a controller record the model may read. ``resolvable`` is
    ``{kind: {ids}}`` — the ONLY (kind, id) pairs an answer may cite.

    Raises ``errors.not_found`` for an unknown id, ``errors.invalid_argument``
    for an unknown kind. ``kind == "none"`` returns no records and an empty
    resolvable set.
    """
    kind = kind or "none"
    if kind not in CONTEXT_KINDS:
        raise errors.invalid_argument(
            f"context.kind must be one of {'|'.join(CONTEXT_KINDS)}",
            [{"field": "context.kind", "reason": "unknown_kind"}])
    if kind == "none":
        return {"kind": "none", "id": None, "records": []}, {}

    if not isinstance(ctx_id, str) or not ctx_id.strip():
        raise errors.invalid_argument(
            "context.id is required when context.kind != 'none'",
            [{"field": "context.id", "reason": "required"}])
    ctx_id = ctx_id.strip()

    if kind == "node":
        node = store.get_node(conn, ctx_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {ctx_id} not found")
        sample = store.latest_sample(conn, ctx_id) or {}
        import time as _t
        records = [{
            "kind": "node", "node_id": node["id"], "registration_id": node["registration_id"],
            "role": node["role"], "synthetic": bool(node.get("synthetic")),
            "status": node.get("status"), "last_seen_at": node.get("last_seen_at"),
            "hardware": json.loads(node["hardware_json"]) if node.get("hardware_json") else {},
            "metrics_latest": _c0_sample(sample),
        }]
        return {"kind": kind, "id": ctx_id, "records": records}, {"node": {ctx_id}, "metric": {ctx_id}}

    if kind == "metric":
        node = store.get_node(conn, ctx_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {ctx_id} not found")
        samples = store.recent_samples(conn, ctx_id, 20)
        records = [{"kind": "metric", "node_id": ctx_id,
                    "samples": [_c0_sample(s) for s in samples]}]
        return {"kind": kind, "id": ctx_id, "records": records}, {"metric": {ctx_id}, "node": {ctx_id}}

    if kind == "decision":
        row = conn.execute(
            "SELECT * FROM remediation_decisions WHERE id = ?", (ctx_id,)
        ).fetchone()
        if row is None:
            raise errors.not_found(f"decision {ctx_id} not found")
        d = dict(row)
        params = d.get("action_params")
        try:
            params = json.loads(params) if isinstance(params, str) else (params or {})
        except json.JSONDecodeError:
            params = {}
        records = [{
            "kind": "decision", "id": d["id"], "node_id": d["node_id"],
            "rule_id": d["rule_id"], "action_kind": d["action_kind"],
            "action_params": params, "reason": d["reason"],
            "decision_source": d["decision_source"], "decided_at": d["decided_at"],
        }]
        resolvable = {"decision": {ctx_id}}
        if d.get("node_id"):
            resolvable.setdefault("node", set()).add(d["node_id"])
        if d.get("event_id"):
            resolvable.setdefault("incident", set()).add(d["event_id"])
        return {"kind": kind, "id": ctx_id, "records": records}, resolvable

    if kind == "incident":
        row = conn.execute("SELECT * FROM health_events WHERE id = ?", (ctx_id,)).fetchone()
        if row is None:
            raise errors.not_found(f"incident {ctx_id} not found")
        e = dict(row)
        evidence = e.get("evidence_json")
        try:
            evidence = json.loads(evidence) if isinstance(evidence, str) else []
        except json.JSONDecodeError:
            evidence = []
        records = [{
            "kind": "incident", "id": e["id"], "node_id": e["node_id"],
            "rule_id": e["rule_id"], "type": e["type"], "metric": e["metric"],
            "value": e["value"], "threshold": e["threshold"], "fired_at": e["fired_at"],
            "evidence": [_c0_sample(s) for s in (evidence or [])],
        }]
        resolvable = {"incident": {ctx_id}}
        if e.get("node_id"):
            resolvable.setdefault("node", set()).add(e["node_id"])
        return {"kind": kind, "id": ctx_id, "records": records}, resolvable

    if kind == "remediation":
        cmd = store.get_command(conn, ctx_id)
        if cmd is None or cmd.get("project_id") != project_id:
            raise errors.not_found(f"remediation command {ctx_id} not found")
        records = [{
            "kind": "remediation", "id": cmd["command_id"], "node_id": cmd.get("node_id"),
            "type": cmd["type"], "state": cmd.get("state"), "issued_by": cmd.get("issued_by"),
            "issued_at": cmd.get("issued_at"), "payload": cmd.get("payload") or {},
        }]
        return {"kind": kind, "id": ctx_id, "records": records}, {"remediation": {ctx_id}}

    if kind == "audit":
        row = conn.execute("SELECT * FROM remediation_audits WHERE id = ?", (ctx_id,)).fetchone()
        if row is None:
            raise errors.not_found(f"audit {ctx_id} not found")
        a = dict(row)
        records = [{
            "kind": "audit", "id": a["id"], "node_id": a["node_id"], "rule_id": a["rule_id"],
            "action_kind": a["action_kind"], "result": a["result"],
            "detail": (a.get("detail") or "")[:500], "executed_at": a["executed_at"],
        }]
        resolvable = {"audit": {ctx_id}}
        if a.get("node_id"):
            resolvable.setdefault("node", set()).add(a["node_id"])
        return {"kind": kind, "id": ctx_id, "records": records}, resolvable

    raise errors.not_found(f"unknown context kind {kind!r}")


# --------------------------------------------------------------------------- #
# Class assertion (the assistant's externalModelCall gate)
# --------------------------------------------------------------------------- #

_ASSISTANT_PAYLOAD_KEYS = {"schema_version", "request_id", "messages", "context"}


def assert_assistant_classes(payload: Any) -> list[str]:
    """C0-only assertion for the assistant payload (governance.md §7.2 analogue).

    ``messages`` is the user's own free text — the sanctioned input of a Q&A
    surface (owner decision D2), bounded to a closed role enum and a content cap,
    NOT class-asserted. ``context.records`` are controller records and MUST be
    C0-only: any C2 field name, C2 signature, or untransformed C1 identifier is a
    violation that refuses the payload BEFORE the model sees it.
    """
    violations: list[str] = []
    if not isinstance(payload, dict):
        return ["assistant payload must be an object"]
    for key in payload:
        if key not in _ASSISTANT_PAYLOAD_KEYS:
            violations.append(f"{key}: not a frozen assistant field")

    messages = payload.get("messages")
    if not isinstance(messages, list):
        violations.append("messages: expected a list")
    else:
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                violations.append(f"messages[{i}]: not an object")
                continue
            if m.get("role") not in MESSAGE_ROLES:
                violations.append(f"messages[{i}].role: not in {'|'.join(MESSAGE_ROLES)}")
            if not isinstance(m.get("content"), str):
                violations.append(f"messages[{i}].content: not a string")

    context = payload.get("context")
    if not isinstance(context, dict):
        violations.append("context: expected an object")
    else:
        if context.get("kind") not in CONTEXT_KINDS:
            violations.append(f"context.kind: unknown kind {context.get('kind')!r}")
        records = context.get("records")
        if not isinstance(records, list):
            violations.append("context.records: expected a list")
        else:
            for i, rec in enumerate(records):
                if not isinstance(rec, dict):
                    violations.append(f"context.records[{i}]: not an object")
                    continue
                violations.extend(f"context.records[{i}]: {v}" for v in governance.scan_c2(rec))
                violations.extend(f"context.records[{i}]: {v}" for v in governance.check_c1_transforms(rec))
    return violations


# --------------------------------------------------------------------------- #
# Prompt rendering + reply parsing
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "You are Termini's assistant, answering questions about a controller's own "
    "operational records. Reply with ONE JSON object and nothing else:\n"
    '{"answer": string, "citations": [{"kind": string, "id": string, "label": string}], '
    '"proposed_actions": [{"action": string, "target": string}]}\n'
    "- answer: a concise grounded answer. Cite records by (kind, id) from the "
    "context; never cite an id you were not given.\n"
    "- citations: the records your answer rests on. kind is one of "
    "incident|decision|node|metric|remediation|audit. id must be an id actually "
    "present in the context below.\n"
    "- proposed_actions: ONLY when the user asked you to perform an action. "
    "action is one of instance.restart|instance.stop|instance.delete|image.prune; "
    "target is the instance vm_id to act on (or \"\" for image.prune). "
    "Leave it [] when the user only asked a question.\n"
    "If you cannot ground the answer, say so in `answer` rather than inventing facts."
)


def render_assistant_messages(messages: list[dict], context: dict) -> list[dict]:
    """Build the OpenAI-style message list (system + context + conversation)."""
    out: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    out.append({"role": "system",
                "content": "CONTEXT:\n" + json.dumps(context, default=str)})
    for m in messages:
        if isinstance(m, dict) and m.get("role") in MESSAGE_ROLES:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def parse_assistant_reply(content: Any) -> dict:
    """Parse the model's completion into ``{answer, citations, proposed_actions}``.

    Raises :class:`AssistantParseError` on anything unusable (never fabricates).
    """
    if not isinstance(content, str):
        raise AssistantParseError(f"model content is {type(content).__name__}, expected a string")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssistantParseError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssistantParseError(f"model output is {type(data).__name__}, expected an object")

    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise AssistantParseError("model output has no non-empty answer")
    answer = answer[:ANSWER_MAX_CHARS]

    citations: list[dict] = []
    raw_citations = data.get("citations")
    if isinstance(raw_citations, list):
        for c in raw_citations[:CITATIONS_MAX]:
            if not isinstance(c, dict):
                continue
            if not isinstance(c.get("kind"), str) or not isinstance(c.get("id"), str):
                continue
            label = c.get("label")
            if not isinstance(label, str):
                label = ""
            citations.append({"kind": c["kind"], "id": c["id"],
                              "label": label[:CITATION_LABEL_MAX_CHARS]})

    proposed: list[dict] = []
    raw_actions = data.get("proposed_actions")
    if isinstance(raw_actions, list):
        for a in raw_actions[:PROPOSED_ACTIONS_MAX]:
            if not isinstance(a, dict):
                continue
            action = a.get("action")
            if not isinstance(action, str):
                continue
            target = a.get("target")
            if not isinstance(target, str):
                target = ""
            proposed.append({"action": action, "target": target[:200]})

    return {"answer": answer, "citations": citations, "proposed_actions": proposed}


def validate_citations(citations: list[dict], resolvable: dict) -> tuple[list[dict], list[dict]]:
    """Keep only citations whose (kind, id) is in ``resolvable``; report the rest.

    A citation that does not resolve is surfaced in ``errors`` (never a silent
    footnote): the answer may cite only ids that exist in the fetched context.
    """
    kept: list[dict] = []
    errors_: list[dict] = []
    for c in citations:
        kind = c["kind"]
        ids = resolvable.get(kind) or set()
        if c["id"] in ids:
            kept.append(c)
        else:
            errors_.append({"reason": "citation_unresolvable",
                            "kind": kind, "id": c["id"]})
    return kept, errors_


# --------------------------------------------------------------------------- #
# Execution — Guardrail + guarded executor, one audit row + one decision record
# --------------------------------------------------------------------------- #

def _synthetic_event(node_id: str, now_iso: str) -> HealthEvent:
    """A labelled synthetic health event anchoring an assistant decision.

    ``remediation_decisions.event_id`` is NOT NULL UNIQUE -> health_events(id),
    so an assistant-initiated action (which has no detector-fired event) records
    against a clearly-labelled synthetic event. It appears on the events surface
    as ``type: assistant``, so the decision surface stays complete and
    attributable.
    """
    return HealthEvent(
        event_id=ulid.new_ulid(),
        node_id=node_id,
        rule_id="assistant",
        type="assistant",
        metric="assistant",
        value=None,
        threshold="assistant-initiated action",
        fired_at=now_iso,
    )


def _validate_action(action: str, target: str, node_id: str, conn,
                     instance_names: dict, default_instance_name: str):
    """Run the proposed action through the SAME Guardrail the operator uses."""
    sample = store.latest_sample(conn, node_id) or {}
    instances = sample.get("lxd_instances") if isinstance(sample, dict) else None
    instances = list(instances) if isinstance(instances, list) else []

    response = op_decision.DecisionResponse(
        action_id=action,
        params={"instance_name": target} if target else {},
        confidence=1.0,
        rationale="assistant-initiated action",
        schema_version=1,
    )
    event = _synthetic_event(node_id, store.now_iso())
    ctx = op_decision.DecisionContext(
        event=event,
        state=dict(sample) if sample else {},
        evidence=[],
        instances=instances,
        instance_names=instance_names or {},
        default_instance_name=default_instance_name,
        allowed_actions=list(ASSISTANT_EXECUTOR_ACTIONS),
        project_id="",
    )
    guardrail = op_decision.Guardrail(
        min_confidence=0.0,
        allowed_actions=list(ASSISTANT_EXECUTOR_ACTIONS),
        instance_names=instance_names or {},
        default_instance_name=default_instance_name,
    )
    return guardrail.validate(response, ctx), event


def _resolve_node_id(conn, project_id: str, kind: str, ctx_id: str | None, target: str) -> str | None:
    """The node an action targets: context node first, then the target's owner."""
    if kind in ("node", "metric") and ctx_id:
        node = store.get_node(conn, ctx_id)
        if node is not None and node["project_id"] == project_id:
            return ctx_id
    if target:
        for n in store.list_nodes(conn, project_id):
            sample = store.latest_sample(conn, n["id"]) or {}
            insts = sample.get("lxd_instances") if isinstance(sample, dict) else None
            if isinstance(insts, list):
                for inst in insts:
                    if isinstance(inst, dict) and (inst.get("vm_id") == target or inst.get("name") == target):
                        return n["id"]
    nodes = store.list_nodes(conn, project_id)
    return nodes[0]["id"] if nodes else None


def build_executor(cfg) -> op_executor.LxdExecutor:
    """The guarded executor, built exactly as the operator loop builds its own."""
    return op_executor.LxdExecutor(
        cfg.lxd_base_url,
        dry_run=cfg.operator_dry_run,
        timeout_s=cfg.lxd_request_timeout_s,
        retry_max=cfg.lxd_retry_max,
        backoff_s=cfg.lxd_retry_backoff_s,
        client_cert=cfg.lxd_client_cert,
        client_key=cfg.lxd_client_key,
        server_ca=getattr(cfg, "lxd_server_ca", None),
    )


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class AssistantService:
    """Wires the model call + guarded execution for the assistant endpoint.

    ``model_call(messages, request_id) -> str`` and ``executor`` are injectable
    so tests exercise the real orchestration with no network; the defaults reuse
    the diagnosis transport (``DecisionModelConfig`` / ``default_client``) and
    the loop's executor construction.
    """

    def __init__(self, cfg, project: dict, *, executor=None, model_call=None,
                 egress_probe=None, logs=None):
        self.cfg = cfg
        self.project = project
        self.project_id = project["id"]
        self._executor = executor
        self._model_call = model_call
        self._egress_probe = egress_probe
        self.logs = logs
        self._model_name = ""
        self._model_base_url = ""

    # -- model transport -----------------------------------------------------
    def _default_model(self):
        from termini.decision import DecisionModelConfig
        from termini.decision.adapter import default_client

        config = DecisionModelConfig.from_env()
        base_url = getattr(self.cfg, "inference_base_url", "") or config.base_url
        if base_url:
            config = dataclasses.replace(config, base_url=base_url)
        client = default_client(config)
        return client, config

    def _call_model(self, payload: dict, request_id: str) -> str:
        client, config = None, None
        if self._model_call is not None:
            def _send():
                return self._model_call(
                    render_assistant_messages(payload["messages"], payload["context"]),
                    request_id,
                )
            destination = getattr(self.cfg, "inference_base_url", "")
            model_name = ""
        else:
            client, config = self._default_model()
            self._model_name = config.model
            self._model_base_url = config.base_url

            def _send():
                return client(render_assistant_messages(payload["messages"], payload["context"]),
                              request_id=request_id)

            destination = config.base_url
            model_name = config.model

        egress = None
        if self._egress_probe is not None:
            try:
                egress = self._egress_probe()
            except Exception:  # noqa: BLE001 — labelling must not break the call
                egress = None

        result = external_model_call(
            payload,
            _send,
            template_version_expected=ASSISTANT_TEMPLATE_VERSION,
            template_version_actual=ASSISTANT_TEMPLATE_VERSION,
            destination=destination,
            request_id=request_id,
            egress=egress,
            class_assertion=assert_assistant_classes,
        )
        self._model_name = self._model_name or model_name
        return result

    # -- executor ------------------------------------------------------------
    def _executor_for(self, conn, node_id: str) -> op_executor.LxdExecutor:
        ex = self._executor or build_executor(self.cfg)
        node = store.get_node(conn, node_id)
        endpoint = (node or {}).get("lxd_endpoint")
        if endpoint and str(endpoint).startswith("http"):
            ex = ex.with_base_url(str(endpoint))
        return ex

    # -- one proposed action ------------------------------------------------
    def _execute(self, conn, proposed: dict, conversation_id: str, message_id: str,
                 kind: str, ctx_id: str | None, instance_names: dict,
                 default_instance_name: str) -> dict:
        action = proposed.get("action", "")
        target = proposed.get("target", "")
        base = {"action": action, "target": target, "command_id": None,
                "audit_id": None, "state": "rejected"}

        if action not in ASSISTANT_ACTION_IDS:
            base["state"] = "rejected"
            base["reason"] = f"action {action!r} not in the assistant action allowlist"
            return base

        if action in ASSISTANT_COMMAND_ACTIONS:
            return self._execute_command(conn, proposed, conversation_id, message_id,
                                         kind, ctx_id)

        node_id = _resolve_node_id(conn, self.project_id, kind, ctx_id, target)
        if node_id is None:
            base["state"] = "rejected"
            base["reason"] = "no node resolves for this action"
            return base

        result, event = _validate_action(
            action, target, node_id, conn, instance_names, default_instance_name)
        if not result.ok or result.action is None:
            base["state"] = "rejected"
            base["reason"] = result.reject_reason or "guardrail rejected"
            return base

        raction: RemediationAction = result.action

        idem_key = f"assistant:{conversation_id}:{message_id}:{action}:{target}"
        prior = opstore.audit_by_idempotency_key(conn, idem_key)
        if prior is not None and prior["result"] in ("ok", "dry_run"):
            return {"action": action, "target": target,
                    "command_id": prior.get("decision_id"),
                    "audit_id": prior["id"], "state": "already_applied"}

        ex = self._executor_for(conn, node_id)
        executed = ex.execute(raction)
        dry_run = bool(getattr(ex, "dry_run", False))

        now = store.now_iso()
        decision_id = ulid.new_ulid()
        audit_id = ulid.new_ulid()
        # Write the synthetic event + decision + audit in ONE transaction (the
        # server commits the connection after _dispatch returns), so an executed
        # action is never left unattributable by a partial write.
        opstore.insert_health_event(conn, event)
        opstore.insert_decision(
            conn, decision_id, event, raction, now,
            source="assistant", incident=None, trace=None,
            initiated_by="assistant", conversation_id=conversation_id,
            message_id=message_id,
        )
        opstore.insert_audit(
            conn, audit_id, decision_id, event, raction, executed, idem_key, now,
            dry_run, lxd_endpoint=getattr(ex, "base_url", ""),
            initiated_by="assistant", conversation_id=conversation_id,
            message_id=message_id,
        )
        return {"action": action, "target": target, "command_id": decision_id,
                "audit_id": audit_id, "state": executed.result}

    # -- a command-channel action (node.drain / node.reboot) ---------------
    def _execute_command(self, conn, proposed: dict, conversation_id: str,
                         message_id: str, kind: str, ctx_id: str | None) -> dict:
        action = proposed.get("action", "")
        target = proposed.get("target", "")
        base = {"action": action, "target": target, "command_id": None,
                "audit_id": None, "state": "rejected"}

        # Guardrail: the frozen command allowlist (governance.md §281). Only the
        # wired command verbs are executable; anything else has no execution path.
        if action not in cmd_mod.COMMAND_ALLOWLIST:
            base["reason"] = f"action {action!r} not in the command allowlist"
            return base

        node_id = target if target else (ctx_id if kind in ("node", "metric") else None)
        node = store.get_node(conn, node_id) if node_id else None
        if node is None or node.get("project_id") != self.project_id:
            base["reason"] = f"target node {node_id!r} not found"
            return base

        idem_key = f"assistant:{conversation_id}:{message_id}:{action}:{node_id}"
        prior = opstore.audit_by_idempotency_key(conn, idem_key)
        if prior is not None and prior["result"] in ("ok", "dry_run"):
            return {"action": action, "target": node_id,
                    "command_id": prior.get("decision_id"),
                    "audit_id": prior["id"], "state": "already_applied"}

        payload = {"reason": "assistant_requested"} if action == "node.drain" else {}
        # Issue the command straight to queued (assistant bypasses approval, D1).
        cmd = cmd_mod.issue_command(
            conn, self.logs, self.project_id, action, payload, "assistant",
            node_id=node_id,
        )

        now = store.now_iso()
        decision_id = ulid.new_ulid()
        audit_id = ulid.new_ulid()
        event = _synthetic_event(node_id, now)
        raction = RemediationAction(
            kind=action, params=dict(payload), reason="assistant-initiated",
            playbook="assistant", rule_id="assistant",
        )
        issued = op_executor.ExecResult(
            result="ok", detail=f"{action} issued (command_id={cmd['command_id']})",
            method="POST", path=f"/v1/projects/{self.project_id}/nodes/{node_id}/actions",
            body=json.dumps(payload),
        )
        opstore.insert_health_event(conn, event)
        opstore.insert_decision(
            conn, decision_id, event, raction, now,
            source="assistant", incident=None, trace=None,
            initiated_by="assistant", conversation_id=conversation_id,
            message_id=message_id,
        )
        opstore.insert_audit(
            conn, audit_id, decision_id, event, raction, issued, idem_key, now, False,
            lxd_endpoint="", initiated_by="assistant", conversation_id=conversation_id,
            message_id=message_id,
        )
        return {"action": action, "target": node_id,
                "command_id": cmd["command_id"], "audit_id": audit_id,
                "state": cmd["state"]}

    # -- the endpoint --------------------------------------------------------
    def ask(self, conn, messages: list[dict], context: dict | None,
            conversation_id: str, message_id: str) -> dict:
        t0 = time.monotonic()
        kind = (context or {}).get("kind") or "none"
        ctx_id = (context or {}).get("id")
        context_blob, resolvable = build_context(conn, self.project_id, kind, ctx_id)

        truncated = False
        truncation_note = ""
        serialized = json.dumps(context_blob, default=str)
        if len(serialized) > CONTEXT_MAX_CHARS:
            truncated = True
            # Drop the heaviest record (the metric window) and re-measure; the
            # flag + note are what make the truncation visible instead of silent.
            context_blob["records"] = context_blob["records"][:5]
            serialized = json.dumps(context_blob, default=str)
            truncation_note = f"context truncated to {len(serialized)} chars"

        request_id = ulid.new_ulid()
        payload = {
            "schema_version": 1,
            "request_id": request_id,
            "messages": messages,
            "context": context_blob,
        }

        try:
            content = self._call_model(payload, request_id)
        except ExternalCallViolation as exc:
            raise errors.governance_reject(
                "assistant context is not C0-safe; the model was not called",
                [{"reason": v} for v in exc.violations],
            ) from None
        except Exception as exc:  # noqa: BLE001 — transport/latency -> typed 503
            log.warning("assistant model call failed: %s", exc)
            raise errors.err(
                503, "MODEL_UNAVAILABLE",
                f"the model did not answer: {type(exc).__name__}: {exc}",
            ) from None

        try:
            parsed = parse_assistant_reply(content)
        except AssistantParseError as exc:
            raise errors.err(
                503, "MODEL_UNAVAILABLE",
                f"the model returned an unusable answer: {exc}",
            ) from None

        citations, cite_errors = validate_citations(parsed["citations"], resolvable)
        errors_ = list(cite_errors)

        instance_names = dict(getattr(self.cfg, "lxd_instance_names", {}) or {})
        default_instance_name = getattr(self.cfg, "lxd_default_instance_name", "agent") or "agent"

        executed: list[dict] = []
        for prop in parsed["proposed_actions"]:
            try:
                executed.append(self._execute(
                    conn, prop, conversation_id, message_id, kind, ctx_id,
                    instance_names, default_instance_name))
            except Exception as exc:  # noqa: BLE001 — a failed write must fail the action
                log.exception("assistant action execution failed")
                executed.append({"action": prop.get("action"), "target": prop.get("target"),
                                 "command_id": None, "audit_id": None, "state": "error",
                                 "reason": f"{type(exc).__name__}: {exc}"})

        if truncation_note:
            errors_.append({"reason": "prompt_truncated", "detail": truncation_note})

        return {
            "answer": parsed["answer"],
            "citations": citations,
            "executed": executed,
            "proposed_actions": parsed["proposed_actions"],
            "model": self._model_name or "gemini",
            "decided_by": "assistant",
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
            "truncated": truncated,
            "errors": errors_,
        }


__all__ = [
    "ASSISTANT_TEMPLATE_VERSION",
    "CONTEXT_KINDS",
    "ASSISTANT_ACTION_IDS",
    "ASSISTANT_EXECUTOR_ACTIONS",
    "ASSISTANT_COMMAND_ACTIONS",
    "AssistantParseError",
    "AssistantService",
    "assert_assistant_classes",
    "build_context",
    "build_executor",
    "parse_assistant_reply",
    "render_assistant_messages",
    "validate_citations",
]
