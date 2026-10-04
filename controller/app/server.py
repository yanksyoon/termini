"""HTTP service — stdlib `http.server.ThreadingHTTPServer` + a small router.

Endpoints implemented (controller core, per controller-api.md §4.1/§4.2 and the
task scope):

    POST /v1/nodes/register                       (bootstrap_token)  -> 201
    GET  /v1/projects/{pid}/nodes                 (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}           (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}/metrics   (project_token)    -> 200
    POST /v1/nodes/{nid}/telemetry                (node_token)       -> 202
    GET  /v1/nodes/{nid}/telemetry/recent         (node_token)       -> 200
    GET  /v1/telemetry/recent?node=&limit=        (project_token)    -> 200

Not in scope for this card (owned by other W1 tasks): heartbeat, reconnect,
commands/acks, intents + guardrail validator, VMs, ssh-key, WS streams/terminal.

Transaction discipline: handlers RETURN (status, body); the dispatcher commits
the transaction and only then writes the HTTP response, so a subsequent request
can never observe uncommitted state (the deterministic role cap depends on this).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import db, errors, governance, registration, role, store, validate
from .operator import store as opstore

log = logging.getLogger("termini.controller")

MAX_BODY = 1_048_576  # 1 MiB, controller-api.md §1.4


class ControllerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg, project: dict):
        super().__init__(addr, ControllerHandler)
        self.cfg = cfg
        self.project = project
        self.project_token_hash = project["project_token_hash"]


class ControllerHandler(BaseHTTPRequestHandler):
    server: ControllerServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 (http.server API)
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _send_json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Termini-Version", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, e: errors.ApiError) -> None:
        self._send_json(e.status, errors.envelope(e.code, e.message, e.details))

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _bootstrap_token(self) -> str | None:
        # Accept the canonical Bearer form and the legacy X-Bootstrap-Token header.
        return self._bearer() or self.headers.get("X-Bootstrap-Token")

    def _check_version(self) -> None:
        v = self.headers.get("X-Termini-Version")
        if v and v != "1":
            raise errors.unsupported_version([{"field": "X-Termini-Version", "reason": v}])

    def _read_body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if len(raw) > MAX_BODY:
            raise errors.ApiError(400, "INVALID_ARGUMENT", "request body exceeds 1 MiB")
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise errors.ApiError(400, "INVALID_ARGUMENT", f"malformed JSON body: {exc}")
        if not isinstance(obj, dict):
            raise errors.ApiError(400, "INVALID_ARGUMENT", "body must be a JSON object")
        return obj

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # ------------------------------------------------------------------ dispatch
    def _route(self, method: str) -> None:
        self._check_version()
        path = urlsplit(self.path).path
        segs = [s for s in path.split("/") if s]

        conn = db.connect(self.server.cfg.db_path)
        try:
            status, body = self._dispatch(conn, method, segs, path)
            conn.commit()  # commit BEFORE the response is written
        except errors.ApiError as e:
            try:
                conn.rollback()
            finally:
                conn.close()
            self._send_error(e)
            return
        except Exception:
            try:
                conn.rollback()
            finally:
                conn.close()
            log.exception("unhandled error")
            self._send_error(errors.err(500, "INTERNAL", "controller internal error"))
            return
        conn.close()
        self._send_json(status, body)

    def _dispatch(self, conn, method: str, segs: list[str], path: str) -> tuple[int, dict]:
        if method == "POST" and segs[:3] == ["v1", "nodes", "register"] and len(segs) == 3:
            return self._register(conn)
        if method == "POST" and len(segs) == 4 and segs[:2] == ["v1", "nodes"] and segs[3] == "telemetry":
            return self._ingest_telemetry(conn, segs[2])
        if method == "GET" and len(segs) == 4 and segs[:2] == ["v1", "projects"] and segs[3] == "nodes":
            return self._list_nodes(conn, segs[2])
        if method == "GET" and len(segs) == 5 and segs[:2] == ["v1", "projects"] and segs[3] == "nodes":
            return self._get_node(conn, segs[2], segs[4])
        if (method == "GET" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "nodes" and segs[5] == "metrics"):
            return self._get_metrics(conn, segs[2], segs[4])
        if (method == "GET" and len(segs) == 5 and segs[:2] == ["v1", "nodes"]
                and segs[3] == "telemetry" and segs[4] == "recent"):
            return self._node_recent(conn, segs[2])
        if method == "GET" and segs[:2] == ["v1", "telemetry"] and segs[2:] == ["recent"]:
            return self._recent(conn)
        if method == "GET" and segs[:2] == ["v1", "operator"] and len(segs) == 3:
            if segs[2] == "events":
                return self._operator_events(conn)
            if segs[2] == "audits":
                return self._operator_audits(conn)
            if segs[2] == "decisions":
                return self._operator_decisions(conn)
        raise errors.not_found(f"no route: {method} {path}")

    def do_POST(self) -> None:
        self._route("POST")

    def do_GET(self) -> None:
        self._route("GET")

    # ------------------------------------------------------------------ auth
    def _require_bootstrap(self, conn) -> str:
        """Return the presented bootstrap token after validating it is known and
        unused. The token is *burned* only after successful registration."""
        token = self._bootstrap_token()
        if not token:
            raise errors.unauthenticated("missing bootstrap token")
        h = store.sha256(token)
        row = conn.execute(
            "SELECT used_at FROM bootstrap_tokens WHERE token_hash = ?", (h,)
        ).fetchone()
        if row is None:
            raise errors.unauthenticated("unknown bootstrap token")
        if row["used_at"] is not None:
            raise errors.unauthenticated("bootstrap token already used")
        return token

    def _require_project(self, conn, project_id: str) -> None:
        token = self._bearer()
        if not token:
            raise errors.unauthenticated()
        if project_id != self.server.project["id"]:
            raise errors.permission_denied("project_id does not match token scope")
        if not _const_eq(store.sha256(token), self.server.project_token_hash):
            raise errors.permission_denied("invalid project token")

    def _require_node(self, conn, node_id: str) -> dict:
        token = self._bearer()
        if not token:
            raise errors.unauthenticated()
        node = store.get_node_by_token_hash(conn, store.sha256(token))
        if node is None:
            raise errors.permission_denied("invalid node token")
        if node["id"] != node_id:
            raise errors.permission_denied("node token does not match node_id in path")
        return node

    # ------------------------------------------------------------------ endpoints
    def _register(self, conn) -> tuple[int, dict]:
        token = self._require_bootstrap(conn)
        body = self._read_body()
        reg = registration.parse_registration(body)

        now = store.now_iso()
        project = self.server.project
        inference_count = store.count_inference_nodes(conn, project["id"])
        role_name = role.assign_role(
            reg["gpu_vram_bytes"], inference_count,
            self.server.cfg.max_inference_nodes, self.server.cfg.gpu_vram_min_bytes,
        )

        node_token = self.server.cfg.new_secret()
        source_ip = self.client_address[0] if self.client_address else None
        node_id, created = store.upsert_node(
            conn, project["id"], reg, role_name, store.sha256(node_token), source_ip, now
        )
        # Burn the single-use token only after a successful registration.
        store.burn_bootstrap_token(conn, token, node_id, now)

        host = self.headers.get("Host") or "controller.termini.example"
        resp = {
            "schema_version": "1.0",
            "node_id": node_id,
            "project_id": project["id"],
            "role": role_name,
            "node_token": node_token,
            # identity (client cert) and cluster (LXD join token) are issued by the
            # infra/bootstrap task; the core controller does not mint mTLS certs.
            "identity": None,
            "cluster": None,
            "telemetry": {
                "interval_ms": self.server.cfg.telemetry_interval_ms,
                "channel": f"wss://{host}/node/v1/{node_id}/channel",
            },
            "config": {
                "heartbeat_interval_s": self.server.cfg.heartbeat_interval_s,
                "telemetry_interval_ms": self.server.cfg.telemetry_interval_ms,
                "telemetry_batch_max": self.server.cfg.telemetry_batch_max,
                "command_channel": "websocket",
            },
            "controller_time": now,
            "registration_ttl_s": 3600,
        }
        return (201 if created else 200), resp

    def _list_nodes(self, conn, project_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        nodes = store.list_nodes(conn, project_id)
        return 200, {"nodes": [self._node_view(n) for n in nodes]}

    def _get_node(self, conn, project_id: str, node_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        node = store.get_node(conn, node_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {node_id} not found")
        view = self._node_view(node)
        view["metrics_latest"] = store.latest_sample(conn, node_id)
        return 200, view

    def _get_metrics(self, conn, project_id: str, node_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        node = store.get_node(conn, node_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {node_id} not found")

        q = self._query()
        start = _parse_rfc3339(q["start"]) if q.get("start") else None
        end = _parse_rfc3339(q["end"]) if q.get("end") else None
        resolution = q.get("resolution", "5s")
        if resolution not in ("raw", "5s", "1m", "5m"):
            raise errors.invalid_argument("resolution must be one of raw|5s|1m|5m")
        names = [m for m in q.get("metrics", "").split(",") if m] or list(store.METRICS.keys())
        bad = [m for m in names if m not in store.METRICS]
        if bad:
            raise errors.invalid_argument(f"unknown metric(s): {', '.join(bad)}")

        return 200, store.metric_series(conn, node_id, start, end, names, resolution)

    def _ingest_telemetry(self, conn, node_id: str) -> tuple[int, dict]:
        node = self._require_node(conn, node_id)
        body = self._read_body()

        # Governance gates BEFORE schema validation: fail-closed on C2/C1/redactor.
        c2 = governance.scan_c2(body)
        if c2:
            raise errors.governance_reject("C2 content detected", [{"reason": v} for v in c2])
        c1 = governance.check_c1_transforms(body)
        if c1:
            raise errors.governance_reject("untransformed C1 field", [{"reason": v} for v in c1])

        red = body.get("_redaction") if isinstance(body.get("_redaction"), dict) else {}
        ver_err = governance.assert_redactor_version(
            red.get("version"), self.server.cfg.min_redactor_version
        )
        if ver_err:
            raise errors.governance_reject(ver_err)

        # Envelope-level node_id must match the path (node-telemetry.md §7.2).
        if isinstance(body.get("node_id"), str) and body["node_id"] != node_id:
            raise errors.permission_denied("envelope node_id does not match path")

        clean, verrors = validate.validate_envelope(body)
        if verrors:
            raise errors.invalid_argument("telemetry schema violation", [{"reason": v} for v in verrors])

        clean["node_id"] = node_id
        samples = clean["samples"]

        now = store.now_iso()
        inserted, dropped = store.insert_samples(conn, node_id, clean["call_id"], samples)
        store.insert_batch(conn, clean, node_id, inserted, dropped, now)
        conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node_id))

        return 202, {"accepted": inserted, "rejected": dropped, "backpressure": "none"}

    def _node_recent(self, conn, node_id: str) -> tuple[int, dict]:
        self._require_node(conn, node_id)
        limit = self._limit_param()
        return 200, {"node_id": node_id, "samples": store.recent_samples(conn, node_id, limit)}

    def _recent(self, conn) -> tuple[int, dict]:
        q = self._query()
        node_id = q.get("node")
        self._require_project(conn, self.server.project["id"])
        limit = self._limit_param()
        if node_id:
            node = store.get_node(conn, node_id)
            if node is None:
                raise errors.not_found(f"node {node_id} not found")
            return 200, {"node_id": node_id, "samples": store.recent_samples(conn, node_id, limit)}
        out = {}
        for n in store.list_nodes(conn, self.server.project["id"]):
            out[n["id"]] = store.recent_samples(conn, n["id"], limit)
        return 200, {"nodes": out}

    def _operator_events(self, conn) -> tuple[int, dict]:
        self._require_project(conn, self.server.project["id"])
        node_id = self._query().get("node")
        limit = self._limit_param()
        return 200, {"events": opstore.list_health_events(conn, node_id, limit)}

    def _operator_audits(self, conn) -> tuple[int, dict]:
        self._require_project(conn, self.server.project["id"])
        node_id = self._query().get("node")
        limit = self._limit_param()
        return 200, {"audits": opstore.list_audits(conn, node_id, limit)}

    def _operator_decisions(self, conn) -> tuple[int, dict]:
        self._require_project(conn, self.server.project["id"])
        limit = self._limit_param()
        return 200, {"decisions": opstore.list_decisions(conn, limit)}

    def _limit_param(self, default: int = 50) -> int:
        raw = self._query().get("limit", str(default))
        if not re.fullmatch(r"\d{1,4}", raw):
            raise errors.invalid_argument("limit must be an integer 1..1000")
        n = int(raw)
        if not (1 <= n <= 1000):
            raise errors.invalid_argument("limit must be an integer 1..1000")
        return n

    def _node_view(self, n: dict) -> dict:
        hw = json.loads(n["hardware_json"]) if n["hardware_json"] else {}
        return {
            "node_id": n["id"],
            "registration_id": n["registration_id"],
            "role": n["role"],
            "status": n["status"],
            "last_seen_at": n["last_seen_at"],
            "hardware": hw,
            "capacity": {
                "cpu_cores": _cpu_cores(hw),
                "ram_gb": _ram_gb(hw),
                "disk_gb": _disk_gb(hw),
                "gpu_free": 0,
                "cpu_allocated": 0,
                "ram_allocated_gb": 0,
            },
        }


def _cpu_cores(hw: dict) -> int:
    cpu = hw.get("cpu") if isinstance(hw.get("cpu"), dict) else {}
    return cpu.get("threads") or cpu.get("logical_cores") or cpu.get("cores") or 0


def _ram_gb(hw: dict) -> int:
    mem = hw.get("memory") if isinstance(hw.get("memory"), dict) else {}
    b = mem.get("total_bytes")
    return b // (1024 ** 3) if isinstance(b, int) and not isinstance(b, bool) else 0


def _disk_gb(hw: dict) -> int:
    total = 0
    for d in (hw.get("disks") or hw.get("disk") or []):
        if isinstance(d, dict):
            b = d.get("size_bytes") if d.get("size_bytes") else (d.get("size_gb", 0) * (1024 ** 3))
            if isinstance(b, (int, float)) and not isinstance(b, bool):
                total += int(b)
    return total // (1024 ** 3)


def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _parse_rfc3339(s: str) -> float:
    try:
        return validate.parse_rfc3339_epoch(s)
    except ValueError:
        raise errors.invalid_argument(f"invalid RFC3339 timestamp {s!r}")
