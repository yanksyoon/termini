"""Multi-turn ADK agent task path against the on-prem llama.cpp endpoint.

This is the cross-node call path the W4 card wires (task `t_fd8eee6c`): an ADK
`LlmAgent` runs a session of several turns inside an Agent VM and every turn is
served by llama.cpp on Node C over the frozen underlay route
(`http://<nodeC>:8080`, agent-runtime.md D2/D5). The session history is carried by
ADK's `InMemorySessionService`, so turn *n* is a real conversational continuation
of turns 1..n-1 — not N independent requests.

The function returns a JSON-serialisable report containing, per turn, the number
of streamed deltas, first-token latency, total latency, and a tail of the produced
text, plus run totals. The acceptance harness (infra/agent-vm/verify-callpath.sh)
asserts every turn streamed (>= 2 discrete deltas in time) and records the latency.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Sequence

from . import __version__
from .config import AgentConfig
from .stream_metrics import ChunkRecorder

DEFAULT_INSTRUCTION = (
    "You are a Termini operations agent. Answer the operator concisely. "
    "Prefer a single short sentence unless asked to enumerate."
)
DEFAULT_TURNS: tuple[str, ...] = (
    "In one short sentence, name the first letter of the Greek alphabet.",
    "State the second letter of the Greek alphabet.",
    "Summarise in one sentence which Greek letters you have named so far.",
)


def _import_adk():
    """Import ADK lazily so this module stays importable when ADK is absent."""
    from google.adk.agents import LlmAgent
    from google.adk.agents.run_config import RunConfig, StreamingMode
    from google.adk.runners import InMemoryRunner
    from google.genai import types as gtypes

    return LlmAgent, RunConfig, StreamingMode, InMemoryRunner, gtypes


def run_multiturn(
    cfg: AgentConfig,
    turns: Sequence[str],
    *,
    model: str | None = None,
    instruction: str = DEFAULT_INSTRUCTION,
    max_tokens: int = 256,
    temperature: float = 0.2,
    streaming: bool = True,
) -> dict[str, Any]:
    """Run a multi-turn ADK conversation against the configured inference endpoint.

    Blocking wrapper around the async ADK runner; returns the per-turn latency
    report plus a machine-checkable ``ok`` flag.
    """
    return asyncio.run(
        run_multiturn_async(
            cfg, turns, model=model, instruction=instruction,
            max_tokens=max_tokens, temperature=temperature, streaming=streaming,
        )
    )


async def run_multiturn_async(
    cfg: AgentConfig,
    turns: Sequence[str],
    *,
    model: str | None = None,
    instruction: str = DEFAULT_INSTRUCTION,
    max_tokens: int = 256,
    temperature: float = 0.2,
    streaming: bool = True,
) -> dict[str, Any]:
    from .adk_llm import LlamaCppLlm
    from .ulid import new_ulid

    LlmAgent, RunConfig, StreamingMode, InMemoryRunner, gtypes = _import_adk()

    alias = cfg.inference.default_model if model is None else model
    if alias not in cfg.inference.allowed_models:
        raise ValueError(f"model {alias!r} not in allowed_models {cfg.inference.allowed_models}")

    recorder = ChunkRecorder()
    llm = LlamaCppLlm(
        model=alias,
        inference=cfg.inference,
        token=cfg.inference_token() or "termini-local",
        vm_id=cfg.vm_id,
        project_id=cfg.project_id,
        recorder=recorder,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    agent = LlmAgent(name="termini_ops", model=llm, instruction=instruction)
    runner = InMemoryRunner(app_name="termini-agent", agent=agent)
    session = await runner.session_service.create_session(
        app_name="termini-agent", user_id=cfg.vm_id
    )
    run_config = RunConfig(streaming_mode=StreamingMode.SSE if streaming else StreamingMode.NONE)

    report: dict[str, Any] = {
        "kind": "termini.multiturn_report",
        "agent_version": __version__,
        "vm_id": cfg.vm_id,
        "node_id": cfg.node_id,
        "project_id": cfg.project_id,
        "inference_base_url": cfg.inference.base_url,
        "model": alias,
        "instruction": instruction,
        "max_tokens": max_tokens,
        "streaming_mode": "sse" if streaming else "none",
        "turns": [],
    }
    started = time.monotonic()
    for index, user_text in enumerate(turns, 1):
        recorder.start()
        message = gtypes.Content(role="user", parts=[gtypes.Part(text=user_text)])
        turn_t0 = time.monotonic()
        adk_partial_events = 0
        adk_final_text: list[str] = []
        async for event in runner.run_async(
            user_id=cfg.vm_id, session_id=session.id, new_message=message, run_config=run_config
        ):
            if getattr(event, "partial", False):
                adk_partial_events += 1
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        adk_final_text.append(part.text)
        turn_total_ms = (time.monotonic() - turn_t0) * 1000.0
        metrics = recorder.as_dict()
        turn_report = {
            "turn": index,
            "user": user_text,
            "streamed_deltas": metrics["chunks"],
            "content_deltas": metrics["content_chunks"],
            "reasoning_deltas": metrics["reasoning_chunks"],
            "first_token_ms": metrics["first_token_ms"],
            "total_ms": round(turn_total_ms, 3),
            "inter_token_ms": metrics["inter_token_ms"],
            "adk_partial_events": adk_partial_events,
            "answer": metrics["content"],
            # The stock lab server splits `reasoning_content` out; keep a tail so
            # the report shows the model genuinely generated text either way.
            "reasoning_tail": metrics["reasoning"],
            "streamed": recorder.streamed(min_chunks=2),
        }
        report["turns"].append(turn_report)

    report["total_turns"] = len(report["turns"])
    report["total_streamed_deltas"] = sum(t["streamed_deltas"] for t in report["turns"])
    report["total_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    report["first_token_ms"] = next(
        (t["first_token_ms"] for t in report["turns"] if t["first_token_ms"] is not None), None
    )
    # Acceptance: every turn streamed incrementally and the session is non-empty.
    report["ok"] = bool(report["turns"]) and all(t["streamed"] for t in report["turns"])
    return report


def turns_from_spec(spec: Iterable[str] | None) -> tuple[str, ...]:
    """Accept an explicit list of turn prompts, else the built-in default task."""
    if not spec:
        return DEFAULT_TURNS
    out = tuple(s.strip() for s in spec if s and s.strip())
    return out or DEFAULT_TURNS
