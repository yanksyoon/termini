"""The AI operator brain: a local model, grammar-constrained, replacing playbook.choose().

Public entry point is `choose()`, which is signature-compatible with
`controller/app/operator/playbook.choose()`:

    choose(event, playbooks, instance_names=None, default_instance_name="agent")
        -> RemediationAction | None

so it can be swapped in without editing the loop or the CLI.

NOTE ON `playbooks`: it is accepted and **deliberately ignored**. That is the
point of the spike -- the action is chosen from live evidence by a model, not
looked up from a fault->action table. Passing the playbooks through keeps the
signature compatible while proving nothing reads them.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gbnf  # noqa: E402

# Import the controller's value types if reachable (normal in-repo use); fall
# back to a local stand-in so the spike also runs standalone.
try:
    from app.operator.models import RemediationAction  # type: ignore
except Exception:  # pragma: no cover - standalone fallback
    from dataclasses import dataclass, field

    @dataclass
    class RemediationAction:  # type: ignore
        kind: str
        params: dict = field(default_factory=dict)
        reason: str = ""
        playbook: str = ""
        rule_id: str = ""


LLM_URL = os.environ.get("TERMINI_AGENT_URL", "http://127.0.0.1:8099")
LLM_MODEL = os.environ.get("TERMINI_AGENT_MODEL", "local")
LLM_TIMEOUT = float(os.environ.get("TERMINI_AGENT_TIMEOUT", "180"))
MAX_TOKENS = int(os.environ.get("TERMINI_AGENT_MAX_TOKENS", "300"))
# Bound the digest regardless of the operator's evidence window (--limit).
MAX_SAMPLES = int(os.environ.get("TERMINI_AGENT_MAX_SAMPLES", "8"))
# Optional JSONL trace of every model decision, for debugging a live run.
DEBUG_PATH = os.environ.get("TERMINI_AGENT_DEBUG")

SYSTEM = (
    "You are the Termini operations agent for a small on-premise AI cluster. "
    "A monitoring rule has fired on a node. Decide what to do about it.\n"
    "Choose exactly one action:\n"
    "  instance.restart - restart a workload to reclaim memory or clear a stuck process\n"
    "  instance.stop    - stop a workload to free the node (disk failure/degradation)\n"
    "  instance.delete  - remove a workload that should not be running\n"
    "  image.prune      - reclaim disk by removing unused images\n"
    "  escalate_to_human - no safe automatic action; a person must decide\n"
    "  no_action        - the signal is not worth acting on\n"
    "Call instance.stop only when the node itself is unsafe; prefer the least "
    "destructive action that addresses the cause. "
    "Reply with one JSON object and nothing else."
)


def _summ(vals, recent=4):
    """Bounded summary of a series: never send the whole window to the model.

    The operator's window is ~100 samples. Emitting every point makes prompt
    processing dominate (measured: ~18 tokens/s prefill on this CPU), so the
    digest must be O(1) in window length.
    """
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    out = {"n": len(vals), "min": round(min(vals), 2), "max": round(max(vals), 2)}
    if len(vals) > 1:
        out["first"] = round(vals[0], 2)
        out["last"] = round(vals[-1], 2)
    out["recent"] = [round(v, 2) for v in vals[-recent:]]
    return out


def _digest(event) -> dict:
    """Compact, C0-only state digest. Never includes free text or payloads."""
    ev = list(event.evidence or [])[-MAX_SAMPLES:]
    latest = ev[-1] if ev else {}

    def series(name, conv=lambda v: v):
        out = []
        for s in ev:
            v = s.get(name)
            if v is not None:
                out.append(conv(v))
        return out

    def nested(arr_name, field):
        out = []
        for s in ev:
            arr = s.get(arr_name) or []
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                v = arr[0].get(field)
                if v is not None:
                    out.append(v)
        return out

    instances = []
    seen = set()
    for s in ev:
        for inst in (s.get("lxd_instances") or []):
            if isinstance(inst, dict) and inst.get("vm_id") not in seen:
                seen.add(inst.get("vm_id"))
                instances.append({
                    "vm_id": inst.get("vm_id"),
                    "state": inst.get("state"),
                    "cpu_util_pct": inst.get("cpu_util_pct"),
                    "ram_used_bytes": inst.get("ram_used_bytes"),
                    "ram_limit_bytes": inst.get("ram_limit_bytes"),
                    "disk_used_bytes": inst.get("disk_used_bytes"),
                })

    # GPU / inference state -- needed for faults whose remedy is not obviously
    # an LXD verb (the novel-fault case the agent exists to cover).
    gpu = []
    for s in ev:
        arr = s.get("gpu") or []
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            g0 = arr[0]
            gpu.append({
                "util_pct": g0.get("util_pct"),
                "vram_used_bytes": g0.get("vram_used_bytes"),
                "vram_total_bytes": g0.get("vram_total_bytes"),
                "temp_celsius": g0.get("temp_celsius"),
            })
    inference = []
    for s in ev:
        inf = s.get("inference")
        if isinstance(inf, dict):
            inference.append({"server_up": inf.get("server_up"), "model": inf.get("model")})

    ram_total = latest.get("ram_total_bytes")
    ram_used = latest.get("ram_used_bytes")
    return {
        "fired_rule": event.rule_id,
        "condition_type": event.type,
        "metric": event.metric,
        "offending_value": event.value,
        "threshold": event.threshold,
        "samples_in_window": len(ev),
        "observe": {
            "cpu_util_pct": _summ(series("cpu_util_pct")),
            "ram_used_pct": _summ([
                round(100.0 * u / ram_total, 1)
                for u in series("ram_used_bytes")
            ]) if ram_total else None,
            "disk_free_pct": _summ([
                round(100.0 * f / t, 1)
                for f, t in zip(nested("disk", "free_bytes"), nested("disk", "total_bytes"))
            ]),
            "disk_write_latency_ms": _summ(nested("disk", "write_latency_ms")),
            "disk_mount_ro": any(nested("disk", "mount_ro")),
            "net_latency_ms": _summ(nested("net", "latency_ms")),
            "gpu_latest": gpu[-1] if gpu else None,
            "inference_latest": inference[-1] if inference else None,
        },
        "workloads": instances,
    }


def _candidates(event) -> list[str]:
    """vm_ids of running workloads in the evidence window (grammar enum)."""
    out, seen = [], set()
    for s in (event.evidence or []):
        for inst in (s.get("lxd_instances") or []):
            if not isinstance(inst, dict):
                continue
            vm = inst.get("vm_id")
            if vm and vm not in seen and inst.get("state") == "running":
                seen.add(vm)
                out.append(vm)
    return out


def _call_llm(grammar: str, user: str) -> str:
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "grammar": grammar,          # llama.cpp: sampler is constrained to this
        "temperature": 0.0,          # deterministic decisions
        "max_tokens": MAX_TOKENS,
        "cache_prompt": False,
        "stream": False,
    }
    req = urllib.request.Request(
        LLM_URL.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        payload = json.load(r)
    return payload["choices"][0]["message"]["content"]


def _debug(rec: dict) -> None:
    """Append a decision record to TERMINI_AGENT_DEBUG (JSONL), if set."""
    if not DEBUG_PATH:
        return
    try:
        with open(DEBUG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - tracing must never break the loop
        pass


def choose(event, playbooks=None, instance_names=None, default_instance_name="agent"):
    """Signature-compatible drop-in for playbook.choose(). `playbooks` is ignored."""
    candidates = _candidates(event)
    grammar = gbnf.gbnf_for_tool_call(targets=candidates)
    user = (
        "State snapshot (C0 telemetry only):\n"
        + json.dumps(_digest(event), separators=(",", ":"))
        + "\n\nDecide the single best action."
    )

    _debug({"stage": "prompt", "rule": event.rule_id, "chars": len(user),
            "samples_in_evidence": len(event.evidence or [])})

    try:
        raw = _call_llm(grammar, user)
        decision = json.loads(raw)
        _debug({"stage": "decision", "rule": event.rule_id, "raw": raw[:600]})
    except Exception as exc:  # noqa: BLE001
        _debug({"stage": "llm_error", "rule": event.rule_id,
                "error": f"{type(exc).__name__}: {exc}"})
        # A model/runtime failure is not a reason to take an action.
        return RemediationAction(
            kind="escalate_to_human",
            params={},
            reason=f"agent unavailable: {type(exc).__name__}: {exc}",
            playbook="agent",
            rule_id=event.rule_id,
        )

    kind = str(decision.get("action", ""))
    if kind not in gbnf.AGENT_ACTIONS:
        # Unreachable when the grammar is applied -- kept as a canary.
        return RemediationAction(
            kind="escalate_to_human", params={},
            reason=f"agent emitted non-allowlisted action {kind!r}",
            playbook="agent", rule_id=event.rule_id,
        )

    # Resolve the chosen workload. The grammar already limits the model to real
    # vm_ids, so this is a mapping, not a validation.
    vm_id = decision.get("target")
    if vm_id == gbnf.NONE:
        vm_id = None
    name = (instance_names or {}).get(vm_id, vm_id) if vm_id else None
    if kind == "escalate_to_human":
        name = None
    if kind == "no_action":
        return None

    params = {}
    if name:
        params["instance_name"] = name
        params["_vm_id"] = vm_id
    elif kind in ("instance.restart", "instance.stop", "instance.delete"):
        params["instance_name"] = default_instance_name

    return RemediationAction(
        kind=kind,
        params=params,
        reason=str(decision.get("rationale", ""))[:500],
        playbook="agent",  # provenance: no playbook file was consulted
        rule_id=event.rule_id,
    )
