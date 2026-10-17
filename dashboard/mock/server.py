#!/usr/bin/env python3
"""Termini dashboard mock server — stdlib only.

Serves the dashboard static bundle AND a contract-faithful mock of the
controller endpoints the dashboard consumes, so the whole UI can be exercised
(and verified) without the real controller running.

    python3 dashboard/mock/server.py --port 8099
    open http://localhost:8099/

Routes implemented (docs/contracts/dashboard.md / controller-api.md §4.2):

    GET  /v1/projects/{pid}/nodes
    GET  /v1/projects/{pid}/nodes/{nid}
    GET  /v1/projects/{pid}/nodes/{nid}/metrics
    GET  /v1/projects/{pid}/nodes/{nid}/ssh
    GET  /v1/projects/{pid}/logs                      (SSE, §2.2/§2.3)
    GET  /v1/telemetry/recent?node=&limit=            (as-built fallback)
    POST /v1/projects/{pid}/nodes/{nid}/actions/reboot
    POST /v1/projects/{pid}/nodes/{nid}/actions/drain
    POST /v1/projects/{pid}/intents
    POST /v1/projects/{pid}/vms/apply
    POST /v1/projects/{pid}/remediations/{cid}/approve
    POST /v1/projects/{pid}/remediations/{cid}/deny

`/config.js` is generated at runtime so the dashboard boots with no query args.
The mock emits evolving telemetry, live status transitions and a streaming
operator log (including a pending_approval remediation to demo approve/deny).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import queue
import random
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx  # noqa: E402
import assistant as assistant_fixture  # noqa: E402

DASHBOARD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_RING = 10000


# --------------------------------------------------------------------- world

class World:
    """Mutable demo state: telemetry, node liveness, logs, commands."""

    def __init__(self, project_id: str, token: str):
        self.project_id = project_id
        self.token = token
        self.lock = threading.RLock()
        self.nodes = list(fx.NODES)
        self.booted = time.time() - 3600  # nodes appear to have booted ~1h ago
        self.seq = {n["node_id"]: int(time.time()) for n in self.nodes}
        self.last_seen = {n["node_id"]: fx_now_ms() for n in self.nodes}
        self.stall_until = {n["node_id"]: 0 for n in self.nodes}
        self.history = {n["node_id"]: deque(maxlen=240) for n in self.nodes}
        self.logs = deque(maxlen=LOG_RING)
        self.subscribers: set[queue.Queue] = set()
        self.commands: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        # One operator bundle for the process lifetime: decision/event/audit ids
        # must be STABLE across polls, otherwise every 3 s refresh looks like a
        # brand-new decision set and the assistant's citations (and the panel's
        # citation jumps) point at rows the UI no longer holds.
        self.operator = fx.operator_decisions(self.nodes)
        self._stop = threading.Event()
        self._seed()

    # -- telemetry ---------------------------------------------------------
    def current_sample(self, node: dict, advance: bool = True, t_sec: float | None = None) -> dict:
        with self.lock:
            nid = node["node_id"]
            if advance:
                self.seq[nid] += 1
            t = t_sec if t_sec is not None else time.time()
            uptime = int(t - self.booted)
            s = fx.sample(node, t, self.seq[nid], uptime)
            self.history[nid].append(s)
            if advance and fx_now_ms() >= self.stall_until.get(nid, 0):
                self.last_seen[nid] = fx_now_ms()
            return s

    def latest(self, node: dict) -> dict:
        h = self.history[node["node_id"]]
        return h[-1] if h else self.current_sample(node)

    def view(self, node: dict) -> dict:
        s = self.current_sample(node)
        now = fx_now_ms()
        with self.lock:
            seen = self.last_seen[node["node_id"]]
        v = fx.node_view(node, s, now, seen)
        v["metrics_latest"] = s
        return v

    def _seed(self) -> None:
        base = time.time()
        for node in self.nodes:
            for i in range(120):
                self.current_sample(node, advance=False, t_sec=base - (120 - i) * 5)
        self.emit(fx.log_line(None, "info", "ai_operator",
                              "AI operator online; reconcile loop attached to project telemetry."))
        self.emit(fx.log_line(self.nodes[0]["node_id"], "info", "ai_operator",
                              "Threshold rules armed: oom-guard-01, disk-fail-01, disk-degrade-01, gpu-oos-01."))

    # -- logs --------------------------------------------------------------
    def emit(self, line: dict) -> None:
        with self.lock:
            self.logs.append(line)
            for q in list(self.subscribers):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass

    def replay_since(self, since: str | None) -> list[dict]:
        with self.lock:
            if not since:
                return list(self.logs)[-60:]
            ids = [l["log_id"] for l in self.logs]
            if since in ids:
                idx = ids.index(since)
                return list(self.logs)[idx + 1:]
            # cursor fell out of the buffer — resume from the tail with a notice
            notice = fx.log_line(None, "warn", "ai_operator",
                                 "log tail truncated; resuming from oldest retained line")
            notice["truncated"] = True
            return [notice] + list(self.logs)[-60:]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(q)

    # -- background --------------------------------------------------------
    def start_background(self) -> None:
        threading.Thread(target=self._ticker, daemon=True).start()

    def _ticker(self) -> None:
        n = 0
        while not self._stop.is_set():
            time.sleep(2.5)
            n += 1
            try:
                self._tick(n)
            except Exception:  # pragma: no cover - demo resilience
                pass

    def _tick(self, n: int) -> None:
        now = fx_now_ms()
        # 1) exercise a live health transition on the flappy nodes: drop
        #    heartbeats for 20-55 s so the dashboard moves online -> degraded
        #    (and sometimes offline), then recovers.
        for node in self.nodes:
            if not node.get("flappy"):
                continue
            nid = node["node_id"]
            if n % 24 == 0 and self.stall_until.get(nid, 0) <= now:
                self.stall_until[nid] = now + random.choice([20000, 30000, 55000])
                self.emit(fx.log_line(nid, "warn", "ai_operator",
                                      f"Heartbeat gap on {node['registration_id']}; health re-evaluating."))
            elif self.stall_until.get(nid, 0) and now >= self.stall_until[nid]:
                self.stall_until[nid] = 0
                self.last_seen[nid] = now
                self.emit(fx.log_line(nid, "info", "ai_operator",
                                      f"{node['registration_id']} heartbeats resumed; status online."))

        node = self.nodes[n % len(self.nodes)]
        # 2) a periodic pending_approval remediation so approve/deny is demoable
        if n % 9 == 0:
            cid = fx.ulid()
            rem = {"command_id": cid, "type": "node.drain", "status": "pending_approval"}
            self.commands[cid] = {
                "command_id": cid,
                "node_id": node["node_id"],
                "type": "node.drain",
                "approval": "required",
                "state": "pending_approval",
                "issued_by": "ai_operator",
                "issued_at": fx.now_iso(),
                "timeout_s": 300,
            }
            self.pending[cid] = rem
            self.emit(
                fx.log_line(
                    node["node_id"], "warn", "ai_operator",
                    f"disk-fail-01 fired on {node['registration_id']}: predicted disk failure. Issuing node.drain — awaiting human approval.",
                    rem,
                )
            )
            return

        # 3) routine operator chatter
        pool = [
            ("debug", "Telemetry sample accepted; ingest ring buffer healthy."),
            ("info", f"Health flags recomputed for {node['registration_id']}."),
            ("info", "oom-guard-01 evaluated: ram_used_bytes below threshold."),
            ("warn", f"disk-degrade-01 observed on {node['registration_id']}: write_latency_ms elevated (flag + alert only)."),
        ]
        sev, msg = random.choice(pool)
        self.emit(fx.log_line(node["node_id"], sev, "ai_operator", msg))

    # -- commands ----------------------------------------------------------
    def issue(self, node_id: str, ctype: str, approval: str, issued_by: str = "human") -> dict:
        cid = fx.ulid()
        cmd = {
            "command_id": cid,
            "type": ctype,
            "approval": approval,
            "state": "queued" if approval == "auto" else "pending_approval",
            "issued_by": issued_by,
            "issued_at": fx.now_iso(),
            "timeout_s": 60,
        }
        with self.lock:
            self.commands[cid] = cmd
        return cmd

    def decide(self, command_id: str, action: str):
        with self.lock:
            cmd = self.commands.get(command_id)
            if cmd is None:
                return fx.err("NOT_FOUND", f"command {command_id} not found", 404)
            # dashboard.md §4.4 guardrail: only `required` + `pending_approval`
            if cmd.get("approval") != "required" or cmd.get("state") != "pending_approval":
                return fx.err(
                    "CONFLICT",
                    f"command {command_id} is {cmd.get('state')} (approval={cmd.get('approval')}); "
                    "only pending required approvals may be decided",
                    409,
                )
            decided_state = "approved" if action == "approve" else "denied"
            cmd["state"] = decided_state
            if action == "approve":
                cmd["approved_by"] = "human"
                cmd["approved_at"] = fx.now_iso()
            else:
                cmd["denied_by"] = "human"
                cmd["denied_at"] = fx.now_iso()
            self.pending.pop(command_id, None)
            resp = {"command_id": command_id, "state": decided_state}
            resp["approved_by" if action == "approve" else "denied_by"] = "human"
            resp["approved_at" if action == "approve" else "denied_at"] = fx.now_iso()
        # release an approved command through the state machine
        if action == "approve":
            threading.Thread(target=self._release, args=(command_id,), daemon=True).start()
        nid = self.commands[command_id].get("node_id")
        self.emit(fx.log_line(nid, "info", "human",
                              f"Human {action}d AI remediation {command_id}.",
                              {"command_id": command_id, "type": cmd["type"], "status": decided_state}))
        return 200, resp

    def _release(self, command_id: str) -> None:
        time.sleep(0.6)
        with self.lock:
            cmd = self.commands.get(command_id)
            if cmd is None or cmd.get("state") != "approved":
                return
            cmd["state"] = "queued"
        self._run_command(command_id)

    def _run_command(self, command_id: str) -> None:
        for state, delay in (("acked", 0.4), ("running", 0.6), ("succeeded", 1.6)):
            time.sleep(delay)
            with self.lock:
                cmd = self.commands.get(command_id)
                if cmd is None:
                    return
                cmd["state"] = state
                nid = cmd.get("node_id")
            self.emit(fx.log_line(nid, "info", "ai_operator",
                                  f"{cmd['type']} {state}.", {"command_id": command_id, "type": cmd["type"], "status": state}))


def fx_now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------- handler

API_RE = re.compile(r"^/v1/(?P<rest>.*)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "termini-dashboard-mock/1"

    world: World  # set on the server class

    # ------------------------------------------------------------- plumbing
    def log_message(self, format, *args):  # noqa: A002 - http.server API
        if os.environ.get("MOCK_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Termini-Version, Idempotency-Key, Last-Event-ID",
        )

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Termini-Version", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self, query: dict) -> bool:
        token = self.world.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        presented = header[7:].strip() if header.startswith("Bearer ") else ""
        presented = presented or query.get("token", "")
        return presented == token

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    # -------------------------------------------------------------- verbs
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if path.startswith("/v1/"):
            self._api_get(path, query)
        elif path == "/config.js":
            self._serve_config()
        elif path == "/healthz":
            self._json(200, {"ok": True, "nodes": len(self.world.nodes)})
        else:
            self._static(path)

    def do_POST(self):  # noqa: N802
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if path.startswith("/v1/"):
            self._api_post(path, query)
        else:
            self._json(404, {"error": {"code": "NOT_FOUND", "message": f"no route: POST {path}"}})

    # ----------------------------------------------------------------- api
    def _api_get(self, path: str, query: dict):
        w = self.world

        # Client view of ONE node (task t_5649d117): that node's own credential in
        # the Authorization header. Checked BEFORE the project-token gate because
        # on the real controller this route is node_token-authenticated and
        # project-scoped nowhere. A credential in the query string is refused here
        # on purpose, so the dashboard cannot regress to the ?token=… pattern even
        # against the mock.
        m = re.fullmatch(r"/v1/nodes/([^/]+)/telemetry/recent/?", path)
        if m:
            node_id = m.group(1)
            if query.get("token"):
                self._json(400, fx.err(
                    "INVALID_ARGUMENT",
                    "credentials must not travel in the query string; use the Authorization header",
                    400)[1])
                return
            node = self._find_node(node_id)
            if node is None:
                self._json(404, fx.err("NOT_FOUND", "node not found", 404)[1])
                return
            header = self.headers.get("Authorization", "")
            presented = header[7:].strip() if header.startswith("Bearer ") else ""
            if not presented:
                self._json(401, fx.err("UNAUTHENTICATED", "missing node token", 401)[1])
                return
            # The mock keeps ONE credential universe (mock/README.md): a Bearer
            # value that is not the mock's token is refused, which is the shape of
            # the real controller's "this credential cannot read that node" 403.
            if presented != w.token:
                self._json(403, fx.err("PERMISSION_DENIED", "this credential cannot read that node", 403)[1])
                return
            limit = int(query.get("limit", "5") or 5)
            self._json(200, {"node_id": node_id, "samples": list(w.history[node_id])[-limit:]})
            return

        if not self._auth_ok(query):
            self._json(401, {"error": {"code": "UNAUTHENTICATED", "message": "invalid project token", "details": [], "request_id": fx.ulid()}})
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/nodes/?", path)
        if m:
            # dashboard.md §1.1 amendment: the default listing is the REAL fleet;
            # `?include=synthetic` adds the seeder-fabricated rows (labelled).
            listing = [w.view(n) for n in w.nodes]
            if (query.get("include") or "") == "synthetic":
                listing = listing + [fx.synthetic_node(m.group(1))]
            self._json(200, {"nodes": listing})
            return

        # Operator decision surface (operator-decision.md §11). Token-scoped, not
        # project-scoped — mirrors the controller's /v1/operator/* routes.
        m = re.fullmatch(r"/v1/operator/(decisions|events|audits)/?", path)
        if m:
            kind = m.group(1)
            payload = w.operator[kind]
            try:
                limit = max(1, min(500, int(query.get("limit", "50"))))
            except (TypeError, ValueError):
                limit = 50
            self._json(200, {kind: payload[:limit]})
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/nodes/([^/]+)/metrics/?", path)
        if m:
            node = self._find_node(m.group(2))
            if node is None:
                self._json(404, fx.err("NOT_FOUND", "node not found", 404)[1])
                return
            names = [x for x in (query.get("metrics") or "").split(",") if x]
            if not names:
                names = list(fx.METRIC_UNITS.keys())
            bad = [x for x in names if x not in fx.METRIC_UNITS]
            if bad:
                self._json(400, fx.err("INVALID_ARGUMENT", f"unknown metric(s): {', '.join(bad)}", 400)[1])
                return
            resolution = query.get("resolution", "5s")
            if resolution not in ("raw", "5s", "1m", "5m"):
                self._json(400, fx.err("INVALID_ARGUMENT", "resolution must be raw|5s|1m|5m", 400)[1])
                return
            end = self._epoch(query.get("end")) or time.time()
            start = self._epoch(query.get("start")) or (end - 900)
            self._json(200, fx.series(node, start, end, resolution, names))
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/nodes/([^/]+)/ssh/?", path)
        if m:
            node = self._find_node(m.group(2))
            if node is None:
                self._json(404, fx.err("NOT_FOUND", "node not found", 404)[1])
                return
            self._json(200, fx.ssh_view(node))
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/nodes/([^/]+)/?", path)
        if m:
            node = self._find_node(m.group(2))
            if node is None:
                self._json(404, fx.err("NOT_FOUND", "node not found", 404)[1])
                return
            self._json(200, w.view(node))
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/logs/?", path)
        if m:
            self._sse(query)
            return

        if re.fullmatch(r"/v1/telemetry/recent/?", path):
            node_id = query.get("node")
            if node_id:
                node = self._find_node(node_id)
                if node is None:
                    self._json(404, fx.err("NOT_FOUND", "node not found", 404)[1])
                    return
                self._json(200, {"node_id": node_id, "samples": list(w.history[node_id])[-int(query.get("limit", "50")):]})
            else:
                self._json(200, {"nodes": {n["node_id"]: list(w.history[n["node_id"]])[-5:] for n in w.nodes}})
            return

        self._json(404, fx.err("NOT_FOUND", f"no route: GET {path}", 404)[1])

    def _api_post(self, path: str, query: dict):
        w = self.world
        if not self._auth_ok(query):
            self._json(401, {"error": {"code": "UNAUTHENTICATED", "message": "invalid project token", "details": [], "request_id": fx.ulid()}})
            return
        body = self._body()

        m = re.fullmatch(r"/v1/projects/([^/]+)/assistant/ask/?", path)
        if m:
            status, payload = assistant_fixture.assistant_answer(body, w.operator, w.nodes)
            self._json(status, payload)
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/nodes/([^/]+)/actions/(reboot|drain)/?", path)
        if m:
            node = self._find_node(m.group(2))
            if node is None:
                self._json(404, fx.err("NOT_FOUND", "node not found", 404)[1])
                return
            if node["node_id"] in w.stall_until and fx_now_ms() < w.stall_until[node["node_id"]]:
                self._json(503, fx.err("NODE_OFFLINE", f"node {node['node_id']} is offline", 503)[1])
                return
            action = m.group(3)
            ctype = "node.reboot" if action == "reboot" else "node.drain"
            # human-issued actions are already authorised -> queued, not pending
            cmd = w.issue(node["node_id"], ctype, "auto", "human")
            cmd["node_id"] = node["node_id"]
            w.emit(fx.log_line(node["node_id"], "info", "human",
                               f"Human issued {ctype} for {node['registration_id']}.",
                               {"command_id": cmd["command_id"], "type": ctype, "status": cmd["state"]}))
            self._json(202, {"command_id": cmd["command_id"], "type": ctype, "state": cmd["state"], "issued_by": "human"})
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/intents/?", path)
        if m:
            prompt = str(body.get("prompt") or "")
            plan = fx.intent_plan(prompt)
            if plan["outcome"] != "accepted":
                payload = dict(plan)
                payload["error"] = {"code": "GUARDRAIL_REJECTED", "message": "plan rejected by guardrail", "details": [], "request_id": plan["request_id"]}
                self._json(422, payload)
            else:
                self._json(200, plan)
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/vms/apply/?", path)
        if m:
            self._json(202, {"job_id": fx.ulid(), "state": "provisioning", "vm_ids": [fx.ulid(), fx.ulid(), fx.ulid()]})
            return

        m = re.fullmatch(r"/v1/projects/([^/]+)/remediations/([^/]+)/(approve|deny)/?", path)
        if m:
            status, payload = w.decide(m.group(2), m.group(3))
            self._json(status, payload)
            return

        self._json(404, fx.err("NOT_FOUND", f"no route: POST {path}", 404)[1])

    # ----------------------------------------------------------------- sse
    def _sse(self, query: dict):
        since = query.get("since") or self.headers.get("Last-Event-ID")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b"retry: 3000\n\n")
        try:
            self.wfile.flush()
        except Exception:
            pass

        for line in self.world.replay_since(since):
            if not self._write_event(line):
                return

        q = self.world.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        return
                    continue
                if not self._write_event(line):
                    return
        finally:
            self.world.unsubscribe(q)

    def _write_event(self, line: dict) -> bool:
        try:
            data = json.dumps(line)
            self.wfile.write(f"event: log\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except Exception:
            return False

    # -------------------------------------------------------------- static
    def _serve_config(self):
        cfg = (
            "window.TERMINI_CONFIG = "
            + json.dumps(
                {
                    "apiBase": "/v1",
                    "projectId": self.world.project_id,
                    "token": self.world.token,
                }
            )
            + ";\n"
        )
        body = cfg.encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        safe = posixpath.normpath(path).lstrip("/")
        full = os.path.join(DASHBOARD_ROOT, safe)
        if not os.path.abspath(full).startswith(os.path.abspath(DASHBOARD_ROOT)):
            self._json(403, {"error": {"code": "PERMISSION_DENIED", "message": "path traversal"}})
            return
        # Directory index, the way Firebase Hosting serves /proto/ from
        # /proto/index.html. Without this the local dev server 404s a path that
        # works in production, which is exactly the kind of difference that eats
        # an hour during evidence capture (task t_4458a089).
        if os.path.isdir(full):
            index = os.path.join(full, "index.html")
            if os.path.isfile(index):
                full = index
                safe = posixpath.join(safe, "index.html") if safe else "index.html"
        if not os.path.isfile(full):
            self._json(404, {"error": {"code": "NOT_FOUND", "message": f"not found: {path}"}})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --------------------------------------------------------------- helpers
    def _find_node(self, node_id: str):
        for n in self.world.nodes:
            if n["node_id"] == node_id or n["registration_id"] == node_id:
                return n
        return None

    @staticmethod
    def _epoch(value):
        if not value:
            return None
        try:
            return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except Exception:
            pass
        try:
            cleaned = value.replace("Z", "+00:00")
            import datetime

            return datetime.datetime.fromisoformat(cleaned).timestamp()
        except Exception:
            return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Termini dashboard mock server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--project", default=fx.PROJECT_ID)
    ap.add_argument("--token", default=fx.PROJECT_TOKEN)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    random.seed(args.seed)
    world = World(args.project, args.token)
    world.start_background()

    Handler.world = world
    httpd = Server((args.host, args.port), Handler)
    print(f"termini dashboard mock on http://{args.host}:{args.port}/  (project {args.project})")
    print(f"  project_token: {args.token}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
