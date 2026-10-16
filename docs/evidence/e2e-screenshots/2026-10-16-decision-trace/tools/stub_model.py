#!/usr/bin/env python3
"""A stub `chat/completions` endpoint standing in for the operator's model.

WHY THIS EXISTS (and what it is NOT)
------------------------------------
Evidence for the dashboard's incident card needs a decision row that carries a
real §4.2 incident record. The one thing this host cannot supply is a real
*model*: there is no fleet, no GPU and no managed-model credential in this
window. So the model endpoint is the single stub in the chain — everything
around it is the production path:

    real controller process  ->  real OperatorLoop  ->  real §3 DecisionInput
      ->  [this stub answers on POST /v1/chat/completions]
      ->  real adapter.parse  ->  real guardrail G1-G9  ->  real SQLite row
      ->  real GET /v1/operator/decisions  ->  real dashboard bundle

The answer below is DERIVED FROM THE REQUEST the controller sent (the rendered
DecisionInput): it picks the action the frozen playbook would pick for the fired
rule, resolves the target from `target_resource.instances[]`, and quotes only
telemetry fields that are present in the request's own `state`/`evidence`. It is
a stub, but it is not a canned response pasted into the DB: if the controller's
prompt/payload changed, this answer would change with it.

Every request it receives is logged as JSONL (`--log`), so the evidence can show
what the model was asked as well as what it answered.

Usage:
    python3 stub_model.py --port 8099 --log /path/requests.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# rule_id -> the executor action the frozen playbook chooses for that rule.
RULE_ACTION = {
    "oom-guard-01": "instance.restart",
    "latency-01": "instance.restart",
    "disk-degrade-01": "instance.stop",
    "disk-fail-01": "instance.stop",
}

# type -> how the incident reads (C0 phrasing only; never an address, a path or
# anything else the controller's C2 scan would have to reject).
TYPE_PHRASE = {
    "oom": "Memory exhaustion",
    "disk_degrade": "Disk write latency",
    "disk_fail": "Read-only disk",
    "latency": "Sustained network latency",
}

_LOCK = threading.Lock()


def _safe_number(value) -> str:
    """A metric value as text that the C2 scan cannot mistake for PII.

    The controller's G7/G9 scans run over every *string* an answer carries, and
    the phone/card signatures match long digit runs (separators included). A raw
    float like `96.99999999866122` therefore reads as a phone number and the
    whole answer is rejected as a `schema_violation` — which is not a stub
    artefact, it is the guardrail doing its job. So the stub quotes every value
    the way a careful model would: rounded, and in a unit-bearing short form for
    byte counts. Nothing here is invented — the same number, formatted.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        if abs(value) >= 1_000_000:
            return f"{value / (1024 ** 3):.1f} GiB"
        return str(value)
    if abs(value) >= 1_000_000:
        return f"{value / (1024 ** 3):.1f} GiB"
    return f"{round(value, 1)}"



def _first_json_object(text: str) -> dict | None:
    """Pull the DecisionInput out of the rendered user message.

    Accepts a bare JSON body or one embedded in the prompt (fenced or not): the
    adapter's prompt module renders the payload as JSON inside the message, and
    the stub must answer the payload it actually received.
    """
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    for fence in re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.S):
        try:
            obj = json.loads(fence.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "allowed_action_space" in obj:
            return obj
    start = stripped.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(stripped)):
            if stripped[i] == "{":
                depth += 1
            elif stripped[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(stripped[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and "allowed_action_space" in obj:
                        return obj
                    break
        start = stripped.find("{", start + 1)
    return None


def _heaviest(instances: list) -> dict | None:
    best = None
    for inst in instances or []:
        if not isinstance(inst, dict):
            continue
        ram = inst.get("ram_used_bytes")
        if not isinstance(ram, (int, float)) or isinstance(ram, bool):
            continue
        if best is None or ram > best.get("ram_used_bytes", 0):
            best = inst
    return best


def _evidence_from_state(state: dict, heaviest: dict | None) -> list[dict]:
    """Quote the fields the request itself carries — nothing else, nothing new."""
    out: list[dict] = []
    seq = state.get("seq")
    ts = state.get("ts")
    metric = None
    for name in ("ram_used_bytes", "disk_write_latency_ms", "net_latency_ms", "ram_total_bytes"):
        if name in state and isinstance(state[name], (int, float)):
            metric = metric or name
    order = [metric, "ram_total_bytes", "disk_free_bytes", "cpu_util_pct"]
    seen = set()
    for name in order:
        if not name or name in seen:
            continue
        value = state.get(name)
        if not isinstance(value, (int, float, str, bool)) or isinstance(value, bool) and name == "ram_used_bytes":
            continue
        seen.add(name)
        entry: dict = {"field": name, "value": value}
        if isinstance(seq, int) and not isinstance(seq, bool):
            entry["seq"] = seq
        if isinstance(ts, str) and ts:
            entry["ts"] = ts
        out.append(entry)
        if len(out) >= 3:
            break
    if heaviest is not None:
        ram = heaviest.get("ram_used_bytes")
        if isinstance(ram, (int, float)) and not isinstance(ram, bool):
            entry = {"field": "lxd_instances.ram_used_bytes", "value": ram}
            if isinstance(seq, int) and not isinstance(seq, bool):
                entry["seq"] = seq
            out.append(entry)
    return out


def answer(request: dict) -> dict:
    """The §4 DecisionResponse this stub returns for one DecisionInput."""
    allowed = [a for a in (request.get("allowed_action_space") or []) if isinstance(a, dict)]
    schemas = {a.get("action_id"): (a.get("params_schema") or {}) for a in allowed}
    rule_id = request.get("rule_id")
    type_ = request.get("type")
    node_id = request.get("node_id")
    trigger = request.get("trigger") or {}
    state = request.get("state") or {}
    instances = ((request.get("target_resource") or {}).get("instances")) or []

    heaviest = _heaviest(instances)
    target = None
    if heaviest is not None:
        target = heaviest.get("instance_name") or heaviest.get("vm_id")

    action_id = RULE_ACTION.get(rule_id if isinstance(rule_id, str) else "")
    if action_id is None or action_id not in schemas:
        action_id = "instance.restart" if "instance.restart" in schemas else next(iter(schemas), None)

    params: dict = {}
    schema = schemas.get(action_id or "") or {}
    if "instance_name" in schema and target:
        params["instance_name"] = target
    if "force" in schema:
        params["force"] = True
    if "timeout_s" in schema:
        params["timeout_s"] = 30

    metric = trigger.get("metric")
    value = trigger.get("value")
    threshold = trigger.get("threshold")
    ram_pct = None
    if isinstance(state.get("ram_used_bytes"), (int, float)) and isinstance(state.get("ram_total_bytes"), (int, float)):
        total = state["ram_total_bytes"] or 0
        if total:
            ram_pct = round(100.0 * state["ram_used_bytes"] / total, 1)

    heft_pct = None
    if heaviest is not None and isinstance(state.get("ram_total_bytes"), (int, float)):
        total = state["ram_total_bytes"] or 0
        if total and isinstance(heaviest.get("ram_used_bytes"), (int, float)):
            heft_pct = round(100.0 * heaviest["ram_used_bytes"] / total, 1)

    phrase = TYPE_PHRASE.get(type_, "Incident")
    title = f"{phrase} on node {node_id}"

    bits = []
    if ram_pct is not None:
        bits.append(f"ram_used_bytes is at {ram_pct}% of ram_total_bytes")
    elif metric is not None:
        bits.append(f"{metric} is at {value} against the fired threshold {threshold}")
    if heft_pct is not None:
        bits.append(f"one instance holds {heft_pct}% of the node's RAM by itself")
    if rule_id:
        bits.append(f"the {rule_id} rule fired")
    explanation = (
        ", and ".join(bits[:2]) + ". "
        + (
            "That points at a single workload rather than fleet-wide pressure, so acting on that "
            "instance addresses the breach without touching the node."
            if heft_pct is not None and type_ == "oom"
            else "The breach is confined to the resources named above, so the chosen action targets "
            "only the workload responsible instead of the node."
        )
    )

    actions = []
    if action_id:
        act: dict = {"action_kind": action_id}
        if target:
            act["target"] = target
        act["note"] = f"{action_id} is the action the {rule_id} rule maps to; it removes the load named above"
        actions.append(act)

    response = {
        "schema_version": 1,
        "request_id": request.get("request_id"),
        "action_id": action_id,
        "params": params,
        "confidence": 0.9,
        "rationale": (
            f"{metric} at {_safe_number(value)} on node {node_id} crossed the {rule_id} threshold"
            + (f"; {action_id} on {target} reclaims it" if target else f"; {action_id} applies")
        ),
        "incident_title": title,
        "explanation": explanation,
        "evidence": _evidence_from_state(state, heaviest),
        "actions": actions,
    }
    return response


class Handler(BaseHTTPRequestHandler):
    server: "StubServer"  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base-class name
        pass

    def _json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        if self.server.log_path:
            with _LOCK:
                with open(self.server.log_path, "a") as fh:
                    fh.write(json.dumps({"path": self.path, "body": body}) + "\n")

        user = ""
        for msg in reversed(body.get("messages") or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                user = msg.get("content") or ""
                break
        request = _first_json_object(user if isinstance(user, str) else json.dumps(user))
        if request is None:
            return self._json(
                200,
                {"choices": [{"message": {"role": "assistant", "content": "{}"}}]},
            )
        content = json.dumps(answer(request))
        self._json(
            200,
            {
                "id": "stub-1",
                "object": "chat.completion",
                "model": body.get("model") or "stub",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, log_path: str | None):
        super().__init__(addr, Handler)
        self.log_path = log_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stub model endpoint (chat/completions)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--log", default=None, help="JSONL path for the requests received")
    args = ap.parse_args(argv)
    if args.log:
        pathlib.Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    srv = StubServer((args.host, args.port), args.log)
    print(f"stub model endpoint on http://{args.host}:{args.port}/v1/chat/completions", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
