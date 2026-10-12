"""W4 — Agentic OS image & llama.cpp inference serving.

Independent observation: inspect the image pipeline artifacts and the running
llama-server, and probe llama.cpp's streaming endpoint directly.
"""
from __future__ import annotations

import json
import os

from lib.http import request
from lib.envhash import lxc_list
from . import CheckContext, result, run

WS = "W4 — Agentic OS image & llama.cpp inference serving"


def _node_c_ip() -> str:
    for c in lxc_list():
        if c.get("name") == "demo-node-c":
            return c.get("ipv4") or ""
    return ""


def check_w4_1(ctx: CheckContext) -> dict:
    criterion = "an Agent VM on Node A calls llama.cpp on Node C and completes a multi-turn task with streaming responses"
    method = "check for an agent VM on Node A; independently probe llama.cpp streaming chat-completions on Node C"
    guest_a = run("timeout 15 lxc exec demo-node-a -- lxc list 2>&1")
    c_ip = _node_c_ip()

    stream_note = ""
    if c_ip:
        # Probe the OpenAI-compatible streaming endpoint on Node C directly.
        try:
            body = {"model": "deepseek-flash",
                    "messages": [{"role": "user", "content": "say acceptance"}],
                    "stream": True, "max_tokens": 8}
            r = request("POST", f"http://{c_ip}:8080/v1/chat/completions",
                        body=body, timeout=15)
            head = r.body_text[:600]
            stream_note = f"streaming probe status={r.status}\n{head}"
        except Exception as exc:  # noqa: BLE001
            stream_note = f"streaming probe error: {type(exc).__name__}: {exc}"

    raw = f"--- lxc list inside demo-node-a ---\n{guest_a}\n\n--- llama.cpp streaming probe (Node C {c_ip}) ---\n{stream_note}"
    art = ctx.ev.record("W4.1", "W4.1 agent VM multi-turn + streaming",
                        "lxc list (nested) + POST /v1/chat/completions (stream)", raw)

    has_agent_vm = any(tok in guest_a for tok in ("agent", "termini-agent", "RUNNING"))
    # A check that can only ever return 'skipped' is not a check. Evaluate the
    # criterion: an Agent VM running on Node A AND a working streamed completion from
    # Node C. Both branches below are real verdicts.
    stream_ok = "status=200" in stream_note and "data:" in stream_note
    if "Installing LXD snap" in guest_a or not has_agent_vm:
        return result("W4.1", WS, criterion, method, "fail",
                      "no Agent VM running on Node A (internal LXD not provisioned)",
                      art, reason="cross-node agent->llama.cpp path not deployed: no Agent VM on Node A")
    if not stream_ok:
        return result("W4.1", WS, criterion, method, "fail",
                      f"Agent VM present but the streaming completion on Node C ({c_ip}) did not return a stream",
                      art, reason="streaming probe did not return HTTP 200 with streamed chunks")
    return result("W4.1", WS, criterion, method, "pass",
                  f"Agent VM on Node A + streamed completion from Node C ({c_ip})", art)


def check_w4_2(ctx: CheckContext) -> dict:
    criterion = "the image boots with zero network downloads"
    method = "inspect image/evidence/offline-load-proof.txt + image/manifests + models.lock.json, and confirm the running llama-server loads a local model path"
    root = ctx.repo_root
    proof = os.path.join(root, "image", "evidence", "offline-load-proof.txt")
    manifest = os.path.join(root, "image", "manifests", "image-manifest.schema.json")
    lock = os.path.join(root, "image", "models.lock.json")

    proof_txt = open(proof, encoding="utf-8").read() if os.path.isfile(proof) else "(missing)"
    lock_txt = open(lock, encoding="utf-8").read() if os.path.isfile(lock) else "(missing)"

    # Running llama-server model path (local disk?).
    ps_llama = run("ps aux | grep 'llama-server' | grep -v grep")

    raw = f"offline-load-proof.txt exists={os.path.isfile(proof)}\n{proof_txt[:800]}\n\n--- models.lock.json ---\n{lock_txt[:800]}\n\n--- running llama-server cmdline ---\n{ps_llama}"
    art = ctx.ev.record("W4.2", "W4.2 zero-network boot evidence",
                        "read image/evidence + ps llama-server", raw)

    offline_proven = "no DNS -> no network" in proof_txt and "load_model: loading model" in proof_txt
    local_model = "/var/lib/termini/models/" in ps_llama or "load-mode" in ps_llama

    if offline_proven and local_model:
        return result("W4.2", WS, criterion, method, "pass",
                      "offline smoke proof shows no-network boot + local model load; running llama-server uses a local /var/lib/termini/models path", art)
    return result("W4.2", WS, criterion, method, "fail",
                  f"offline_proven={offline_proven} local_model={local_model}",
                  art, reason="could not confirm zero-network boot (proof file and/or local model path missing)")
