#!/usr/bin/env python3
"""Mock OpenAI-compatible llama.cpp server for the direct-route load test.

The real Termini demo path is `llama-server` on Node C (docs/contracts/
infra-decisions.md D5). This mock reproduces the two properties the route test
cares about:

  1. Streaming chat-completions over Server-Sent Events, exactly the wire shape
     POST /v1/chat/completions returns with "stream": true.
  2. It reads the ENTIRE request body before it emits a single byte. That is what
     makes an MTU black hole observable: if the oversized prompt upload stalls in
     the overlay, the client never receives a response and the connection drops.

Only the stdlib is used so it runs on the baked agent image with no extra deps.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_TOKENS = 64
DEFAULT_TOKENS_PER_SECOND = 60.0


class _Server(ThreadingHTTPServer):
    """Threaded server that stays quiet when a client aborts mid-stream."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "termini-mock-llama/1.0"

    # Injected by make_server(); declared here for type checkers.
    tokens: int = DEFAULT_TOKENS
    tokens_per_second: float = DEFAULT_TOKENS_PER_SECOND
    model: str = "deepseek-flash"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    # -- helpers ---------------------------------------------------------------

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        remaining = length
        data = bytearray()
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        return bytes(data)

    # -- routes ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path.rstrip("/") in ("/health", "/v1/health"):
            self._json(200, {"status": "ok"})
        elif self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": self.model, "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return

        raw = self._read_body()  # blocks until the whole prompt has arrived
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        stream = bool(request.get("stream", False))
        if not stream:
            self._json(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
            return

        delay = 1.0 / self.tokens_per_second if self.tokens_per_second > 0 else 0.0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        created = int(time.time())
        for index in range(self.tokens):
            payload = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": created,
                "model": self.model,
                "choices": [{"index": 0, "delta": {"content": f"t{index} "}, "finish_reason": None}],
            }
            frame = f"data: {json.dumps(payload)}\n\n".encode()
            self._write_chunk(frame)
            if delay:
                time.sleep(delay)

        self._write_chunk(b"data: [DONE]\n\n")
        self._write_chunk(b"")  # terminating zero-length chunk

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


def make_server(host: str, port: int, *, tokens: int, rate: float, model: str) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"tokens": tokens, "tokens_per_second": rate, "model": model},
    )
    return _Server((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible streaming llama.cpp server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tokens", type=int, default=DEFAULT_TOKENS)
    parser.add_argument("--tokens-per-second", type=float, default=DEFAULT_TOKENS_PER_SECOND)
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args()

    server = make_server(
        args.host, args.port, tokens=args.tokens, rate=args.tokens_per_second, model=args.model
    )
    print(f"mock-llama listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
