#!/usr/bin/env python3
"""Sustained streaming load client for an OpenAI-compatible llama.cpp endpoint.

Speaks the real `POST /v1/chat/completions` + SSE wire protocol so it can be
pointed either at the baked `llama-server` on Node C or at the lab mock
(`mock_llama_server.py`). It uploads a deliberately large prompt body so an
MTU black hole on the request path shows up as a stalled/dropped connection
instead of a silent success.

Exit code is 0 only when every request completed (zero dropped connections),
which is the acceptance condition for the direct route.
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

TERMINATOR = b"[DONE]"


class Metrics:
    """Thread-safe counters for one load-test run."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.completed = 0
        self.dropped = 0
        self.aborted = 0
        self.tokens = 0
        self.bytes_in = 0
        self.first_token_ms: list[float] = []
        self.inter_token_ms: list[float] = []
        self.total_ms: list[float] = []
        self.errors: list[str] = []

    def add_request(self) -> None:
        with self.lock:
            self.requests += 1

    def add_success(self, *, tokens: int, bytes_in: int, first_ms: float, inter: list[float], total_ms: float) -> None:
        with self.lock:
            self.completed += 1
            self.tokens += tokens
            self.bytes_in += bytes_in
            self.first_token_ms.append(first_ms)
            self.inter_token_ms.extend(inter)
            self.total_ms.append(total_ms)

    def add_drop(self, error: str) -> None:
        with self.lock:
            self.dropped += 1
            self.errors.append(error)

    def add_abort(self) -> None:
        # A request cut short only because the load window ended is not a drop.
        with self.lock:
            self.aborted += 1


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[rank]


def one_request(
    url: str, prompt_body: bytes, timeout: float, metrics: Metrics, stop_at: float
) -> None:
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    metrics.add_request()

    started = time.monotonic()
    # A request ends at whichever comes first: its own socket timeout, or the end
    # of the load window. Only the former counts as a dropped connection.
    hard_deadline = min(started + timeout, stop_at + 0.25)
    socket_timeout = max(0.25, hard_deadline - time.monotonic())
    tokens = 0
    bytes_in = 0
    inter: list[float] = []
    first_ms = 0.0
    done = False
    try:
        conn = http.client.HTTPConnection(host, port, timeout=socket_timeout)
        conn.request(
            "POST",
            path,
            body=prompt_body,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"http {response.status}")

        last_token_at: float | None = None
        while True:
            line = response.readline()
            if not line:
                break
            bytes_in += len(line)
            if not line.startswith(b"data:"):
                continue
            payload = line[len(b"data:") :].strip()
            if payload.startswith(TERMINATOR):
                done = True
                break
            try:
                frame = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = frame.get("choices") or []
            if not choices or not choices[0].get("delta", {}).get("content"):
                continue
            now = time.monotonic()
            if first_ms == 0.0:
                first_ms = (now - started) * 1000.0
            elif last_token_at is not None:
                inter.append((now - last_token_at) * 1000.0)
            last_token_at = now
            tokens += 1
        conn.close()
        if not done:
            raise RuntimeError("stream truncated before [DONE]")
        metrics.add_success(
            tokens=tokens,
            bytes_in=bytes_in,
            first_ms=first_ms,
            inter=inter,
            total_ms=(time.monotonic() - started) * 1000.0,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is a dropped connection here
        if time.monotonic() >= stop_at:
            metrics.add_abort()
        else:
            metrics.add_drop(f"{type(exc).__name__}: {exc}")


def worker(url: str, body: bytes, timeout: float, metrics: Metrics, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        one_request(url, body, timeout, metrics, stop_at)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sustained streaming load test for llama.cpp")
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds to drive load")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--prompt-bytes", type=int, default=65536, help="size of the chat prompt body")
    parser.add_argument("--request-timeout", type=float, default=10.0, help="per-request socket timeout")
    parser.add_argument("--warmup", type=float, default=2.0, help="seconds of warmup to exclude")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args()

    filler = "The quick brown fox jumps over the lazy dog. " * (
        args.prompt_bytes // 45 + 1
    )
    body = json.dumps(
        {
            "model": "deepseek-flash",
            "stream": True,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": filler[: args.prompt_bytes]}],
        }
    ).encode()

    metrics = Metrics()
    stop_at = time.monotonic() + args.warmup + args.duration
    threads = [
        threading.Thread(target=worker, args=(args.url, body, args.request_timeout, metrics, stop_at))
        for _ in range(max(1, args.concurrency))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    report: dict[str, Any] = {
        "url": args.url,
        "prompt_bytes": len(body),
        "duration_s": args.duration,
        "concurrency": args.concurrency,
        "requests": metrics.requests,
        "completed": metrics.completed,
        "dropped": metrics.dropped,
        "aborted": metrics.aborted,
        "tokens": metrics.tokens,
        "bytes_in": metrics.bytes_in,
        "first_token_ms": {
            "p50": round(_percentile(metrics.first_token_ms, 50), 2),
            "p95": round(_percentile(metrics.first_token_ms, 95), 2),
            "max": round(max(metrics.first_token_ms, default=0.0), 2),
        },
        "inter_token_ms": {
            "p50": round(statistics.median(metrics.inter_token_ms), 2) if metrics.inter_token_ms else 0.0,
            "p95": round(_percentile(metrics.inter_token_ms, 95), 2),
            "max": round(max(metrics.inter_token_ms, default=0.0), 2),
        },
        "request_total_ms": {
            "p50": round(_percentile(metrics.total_ms, 50), 2),
            "p95": round(_percentile(metrics.total_ms, 95), 2),
            "max": round(max(metrics.total_ms, default=0.0), 2),
        },
        "errors": metrics.errors[:10],
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    return 0 if metrics.dropped == 0 and metrics.completed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
