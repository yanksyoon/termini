"""Streaming-chunk accounting for the agent→llama.cpp path (agent-runtime.md §10.3).

Pure stdlib: importable and unit-testable outside the agent image (no `openai`,
no `google.adk`). The ADK model adapter (`termini_agent.adk_llm`) and the raw
inference client both feed a `ChunkRecorder` so the acceptance harness can prove
that tokens actually *streamed* (incremental arrivals with real inter-token gaps)
rather than arriving as one final blob.

A llama.cpp server configured with the frozen demo flags
(`--reasoning-format none`, agent-runtime.md §8 / t_95528ae8) keeps the R1-distill
`` block inline in `delta.content`. A stock server instead splits it out into
`delta.reasoning_content` and leaves `delta.content` empty until thinking ends.
The recorder therefore tracks the two channels separately and the client merges
them for liveness, so streaming is observable on either server config.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Chunk:
    """One streamed delta as seen by the client."""

    index: int
    elapsed_ms: float
    text: str
    kind: str  # "content" | "reasoning"


@dataclass
class ChunkRecorder:
    """Records per-delta arrival times and derives the latency report."""

    chunks: list[Chunk] = field(default_factory=list)
    _t0: float | None = None
    _content: list[str] = field(default_factory=list)
    _reasoning: list[str] = field(default_factory=list)

    def start(self) -> "ChunkRecorder":
        self.chunks.clear()
        self._content.clear()
        self._reasoning.clear()
        self._t0 = time.monotonic()
        return self

    def record(self, text: str, kind: str = "content") -> Chunk:
        if self._t0 is None:
            raise RuntimeError("ChunkRecorder.start() must be called before record()")
        if not text:
            raise ValueError("record() requires non-empty text")
        if kind not in ("content", "reasoning"):
            raise ValueError(f"unknown kind {kind!r}")
        c = Chunk(index=len(self.chunks), elapsed_ms=(time.monotonic() - self._t0) * 1000.0,
                  text=text, kind=kind)
        self.chunks.append(c)
        (self._content if kind == "content" else self._reasoning).append(text)
        return c

    # -- derived metrics -------------------------------------------------------
    @property
    def total_ms(self) -> float:
        if self._t0 is None:
            return 0.0
        return (time.monotonic() - self._t0) * 1000.0

    @property
    def count(self) -> int:
        return len(self.chunks)

    @property
    def content_count(self) -> int:
        return sum(1 for c in self.chunks if c.kind == "content")

    @property
    def reasoning_count(self) -> int:
        return sum(1 for c in self.chunks if c.kind == "reasoning")

    @property
    def first_token_ms(self) -> float | None:
        return self.chunks[0].elapsed_ms if self.chunks else None

    @property
    def last_token_ms(self) -> float | None:
        return self.chunks[-1].elapsed_ms if self.chunks else None

    def inter_token_ms(self) -> list[float]:
        return [round(self.chunks[i].elapsed_ms - self.chunks[i - 1].elapsed_ms, 3)
                for i in range(1, len(self.chunks))]

    @property
    def content(self) -> str:
        return "".join(self._content)

    @property
    def reasoning(self) -> str:
        return "".join(self._reasoning)

    def streamed(self, min_chunks: int = 2) -> bool:
        """True when the response arrived as several distinct deltas in time."""
        if len(self.chunks) < min_chunks:
            return False
        # Distinct arrival instants: a single blob cannot satisfy this.
        return any(self.chunks[i].elapsed_ms > self.chunks[i - 1].elapsed_ms
                   for i in range(1, len(self.chunks)))

    def as_dict(self, *, max_text: int = 400) -> dict:
        inter = self.inter_token_ms()
        return {
            "chunks": self.count,
            "content_chunks": self.content_count,
            "reasoning_chunks": self.reasoning_count,
            "first_token_ms": round(self.first_token_ms, 3) if self.first_token_ms is not None else None,
            "last_token_ms": round(self.last_token_ms, 3) if self.last_token_ms is not None else None,
            "total_ms": round(self.total_ms, 3),
            "inter_token_ms": {
                "min": round(min(inter), 3) if inter else None,
                "max": round(max(inter), 3) if inter else None,
            },
            "content": self.content[:max_text],
            "reasoning": self.reasoning[-max_text:] if self.reasoning else "",
        }
