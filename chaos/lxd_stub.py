#!/usr/bin/env python3
"""Stub LXD REST endpoint for the chaos harness.

Impersonates enough of the LXD 1.0 REST API for a remediation executor to talk
to it without a live LXD cluster, and records every request so a test can prove
a *real* HTTP command was issued (not just a log line).

It is intentionally permissive: any method/path is accepted and answered with a
LXD-style success envelope; mutations are recorded and assigned an operation id.
Only `GET /__chaos__/*` is special (the harness control plane).

Response shape (default, `--mutation-status sync`):

    {"type": "sync", "status": "Success", "status_code": 200,
     "operation": "/1.0/operations/<id>", "metadata": {...}}

Run it directly or let chaos/run.py manage it:

    python3 chaos/lxd_stub.py --port 18443 --log /tmp/lxd.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # allow `python3 chaos/lxd_stub.py`

from chaos import common  # noqa: E402

MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class StubState:
    def __init__(self, log_path: str | None, mutation_status: str = "sync") -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.log_path = log_path
        self.mutation_status = mutation_status
        self.seq = 0

    def record(self, entry: dict) -> None:
        with self.lock:
            self.seq += 1
            entry["op_seq"] = self.seq
            self.requests.append(entry)
            if self.log_path:
                common.append_jsonl(self.log_path, entry)

    def mutations(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] in MUTATION_METHODS]

    def reset(self) -> None:
        with self.lock:
            self.requests = []
        if self.log_path and pathlib.Path(self.log_path).exists():
            pathlib.Path(self.log_path).unlink()


class LxdStubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, state: StubState):
        super().__init__(addr, LxdStubHandler)
        self.state = state


def _instance_name(path: str) -> str | None:
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 3 and segs[0] == "1.0" and segs[1] == "instances":
        return segs[2]
    return None


class LxdStubHandler(BaseHTTPRequestHandler):
    server: LxdStubServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # keep the stub quiet; the JSONL log is the record

    # ------------------------------------------------------------------ plumbing
    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    def _auth_scheme(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        return auth.split(" ", 1)[0] if auth else None

    # ------------------------------------------------------------------ routing
    def _route(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}

        if path.startswith("/__chaos__/"):
            return self._control(method, path)

        body = self._read_body() if method in MUTATION_METHODS else None
        operation_id = common.new_ulid()
        entry = {
            "ts": common.rfc3339_now(),
            "method": method,
            "path": path,
            "query": query,
            "body": body,
            "instance": _instance_name(path),
            "auth_scheme": self._auth_scheme(),
            "token_present": bool(self.headers.get("Authorization")),
            "operation_id": operation_id,
        }

        if method == "GET":
            payload = self._sync(self._get_metadata(path))
            entry["response_status"] = 200
        else:
            payload = self._mutation(operation_id)
            entry["response_status"] = 200
        self.server.state.record(entry)
        self._send(entry["response_status"], payload)

    def _get_metadata(self, path: str):
        if path in ("/1.0", "/1.0/"):
            return {
                "api_extensions": [],
                "api_status": "stable",
                "api_version": "1.0",
                "auth": "trusted",
                "public": False,
                "environment": {
                    "server_name": "termini-lxd-stub",
                    "server_version": "5.21.3",
                    "storage": "zfs",
                },
            }
        if path.rstrip("/").endswith("/instances"):
            return []
        name = _instance_name(path)
        if name:
            return {"name": name, "status": "Running", "type": "virtual-machine",
                    "state": {"status": "Running", "status_code": 101}}
        return {}

    def _sync(self, metadata) -> dict:
        return {"type": "sync", "status": "Success", "status_code": 200,
                "operation": "", "error_code": 0, "error": "", "metadata": metadata}

    def _mutation(self, operation_id: str) -> dict:
        op_url = f"/1.0/operations/{operation_id}"
        operation = {
            "id": operation_id, "class": "task", "description": "chaos stub operation",
            "status": "Success", "status_code": 200, "err": "", "resources": {},
        }
        # Default `sync`: a 200 Success envelope whose metadata is the (already
        # complete) operation, plus an operation URL for clients that poll.
        return {"type": "sync", "status": "Success", "status_code": 200,
                "operation": op_url, "error_code": 0, "error": "", "metadata": operation}

    def _control(self, method: str, path: str) -> None:
        sub = path[len("/__chaos__/"):].strip("/")
        if sub == "health":
            return self._send(200, {"ok": True, "requests": len(self.server.state.requests)})
        if sub == "requests":
            return self._send(200, {"requests": list(self.server.state.requests)})
        if sub == "commands":
            return self._send(200, {"commands": self.server.state.mutations()})
        if sub == "last":
            muts = self.server.state.mutations()
            return self._send(200, {"command": muts[-1] if muts else None})
        if sub == "reset":
            self.server.state.reset()
            return self._send(200, {"ok": True})
        self._send(404, {"type": "error", "error": "unknown control endpoint", "error_code": 404})

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def do_DELETE(self) -> None:
        self._route("DELETE")


def serve(host: str, port: int, log_path: str | None, mutation_status: str = "sync") -> LxdStubServer:
    state = StubState(log_path, mutation_status)
    return LxdStubServer((host, port), state)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stub LXD REST endpoint (records commands)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18443)
    ap.add_argument("--log", default=None, help="JSONL path for recorded requests")
    ap.add_argument("--mutation-status", default="sync", choices=["sync", "async"])
    args = ap.parse_args(argv)
    srv = serve(args.host, args.port, args.log, args.mutation_status)
    print(f"[lxd_stub] listening on {args.host}:{args.port} log={args.log}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
