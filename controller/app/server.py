"""HTTP service — stdlib `http.server.ThreadingHTTPServer` + a small router.

Endpoints (controller core + W5 dashboard surfaces, per controller-api.md §4/§6/§7
and dashboard.md §1–§4):

    POST /v1/nodes/register                       (bootstrap_token)  -> 201
    POST /v1/nodes/{nid}/heartbeat                (node_token)       -> 204/200
    GET  /v1/projects/{pid}/nodes                 (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}           (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}/metrics   (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}/ssh       (project_token)    -> 200
    POST /v1/projects/{pid}/nodes/{nid}/actions/{reboot,drain}       -> 202
    POST /v1/projects/{pid}/intents               (project_token)    -> 200/422
    POST /v1/projects/{pid}/vms/apply             (project_token)    -> 202
    POST /v1/projects/{pid}/remediations/{cid}/{approve,deny}        -> 200
    POST /v1/nodes/{nid}/telemetry                (node_token)       -> 202
    GET  /v1/nodes/{nid}/telemetry/recent         (node_token)       -> 200
    GET  /v1/telemetry/recent?node=&limit=        (project_token)    -> 200
    GET  /v1/projects/{pid}/logs                  (SSE, token in ?token)
    GET  /stream/v1?token=...                     (WebSocket event stream)
    GET  /terminal/v1/nodes/{node_id}?token=...    (WebSocket terminal)

Transaction discipline: REST handlers RETURN (status, body); the dispatcher
commits the transaction and only then writes the HTTP response, so a subsequent
request can never observe uncommitted state (the deterministic role cap depends
on this). WebSocket/SSE endpoints are long-lived and manage their own short-lived
DB connections.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import db, errors, governance, health, inventory, registration, role, store, validate, ws as wsmod
from . import commands as cmd_mod
from . import ulid
from .operator import store as opstore

log = logging.getLogger("termini.controller")

MAX_BODY = 1_048_576  # 1 MiB, controller-api.md §1.4
WS_CLOSE_UNAUTHORIZED = 4401


class ControllerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg, project, broker, operator_logs, inventory, terminal_manager,
                 public_key_line: str | None):
        super().__init__(addr, ControllerHandler)
        self.cfg = cfg
        self.project = project
        self.project_token_hash = project["project_token_hash"]
        self.broker = broker
        self.operator_logs = operator_logs
        self.inventory = inventory
        self.terminal_manager = terminal_manager
        self.public_key_line = public_key_line
        self.plans: dict[str, dict] = {}        # plan_id -> accepted plan (in-memory, demo)
        self._last_telemetry_at: dict[str, float] = {}
        self._last_status: dict[str, str] = {}


class ControllerHandler(BaseHTTPRequestHandler):
    server: ControllerServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 (http.server API)
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _cors_origin(self) -> str:
        origins = self.server.cfg.dashboard_origins
        if not origins:
            return "*"
        origin = self.headers.get("Origin", "")
        return origin if origin in origins else origins[0]

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, Idempotency-Key, X-Termini-Version, Last-Event-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, status: int, obj) -> None:
        if status == 204:
            self.send_response(204)
            self._cors_headers()
            self.send_header("X-Termini-Version", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
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
        if method == "POST" and len(segs) == 4 and segs[:2] == ["v1", "nodes"]:
            if segs[3] == "telemetry":
                return self._ingest_telemetry(conn, segs[2])
            if segs[3] == "heartbeat":
                return self._heartbeat(conn, segs[2])
        if method == "GET" and len(segs) == 4 and segs[:2] == ["v1", "projects"] and segs[3] == "nodes":
            return self._list_nodes(conn, segs[2])
        if method == "GET" and len(segs) == 5 and segs[:2] == ["v1", "projects"] and segs[3] == "nodes":
            return self._get_node(conn, segs[2], segs[4])
        if (method == "GET" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "nodes" and segs[5] == "metrics"):
            return self._get_metrics(conn, segs[2], segs[4])
        if (method == "GET" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "nodes" and segs[5] == "ssh"):
            return self._get_ssh(conn, segs[2], segs[4])
        if (method == "POST" and len(segs) == 7 and segs[:2] == ["v1", "projects"]
                and segs[3] == "nodes" and segs[5] == "actions"):
            return self._node_action(conn, segs[2], segs[4], segs[6])
        if method == "POST" and len(segs) == 4 and segs[:2] == ["v1", "projects"] and segs[3] == "intents":
            return self._intents(conn, segs[2])
        if method == "POST" and len(segs) == 5 and segs[:2] == ["v1", "projects"] and segs[3] == "vms" and segs[4] == "apply":
            return self._apply_plan(conn, segs[2])
        if (method == "POST" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "remediations" and segs[5] in ("approve", "deny")):
            return self._remediation_decision(conn, segs[2], segs[4], segs[5])
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

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        self._route("POST")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        segs = [s for s in path.split("/") if s]
        if self._is_websocket():
            self._handle_websocket(segs)
            return
        if len(segs) == 4 and segs[:2] == ["v1", "projects"] and segs[3] == "logs":
            self._handle_sse_logs(segs[2])
            return
        self._route("GET")

    def _is_websocket(self) -> bool:
        return self.headers.get("Upgrade", "").lower() == "websocket"

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

    def _require_project_token(self, token: str | None) -> None:
        if not token:
            raise errors.unauthenticated()
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

    def _node_in_project(self, conn, project_id: str, node_id: str) -> dict:
        node = store.get_node(conn, node_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {node_id} not found")
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
        # Resolve public_ip from the infra inventory (registration_id/role match).
        node = store.get_node(conn, node_id)
        if node is not None and self.server.inventory.loaded:
            pub_ip = self.server.inventory.public_ip_for(node)
            if pub_ip:
                store.set_node_public_ip(conn, node_id, pub_ip)
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

    def _heartbeat(self, conn, node_id: str) -> tuple[int, dict]:
        node = self._require_node(conn, node_id)
        now = store.now_iso()
        conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node_id))
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM commands WHERE node_id = ? AND state = 'queued'", (node_id,)
        ).fetchone()["n"]
        self._publish_node_events(conn, node, store.latest_sample(conn, node_id))
        if pending:
            return 200, {"pending_commands": pending}
        return 204, {}

    def _list_nodes(self, conn, project_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        nodes = store.list_nodes(conn, project_id)
        return 200, {"nodes": [self._node_view(conn, n) for n in nodes]}

    def _get_node(self, conn, project_id: str, node_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        node = store.get_node(conn, node_id)
        if node is None or node["project_id"] != project_id:
            raise errors.not_found(f"node {node_id} not found")
        sample = store.latest_sample(conn, node_id)
        view = self._node_view(conn, node, sample)
        view["metrics_latest"] = sample
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

    def _get_ssh(self, conn, project_id: str, node_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        node = self._node_in_project(conn, project_id, node_id)
        return 200, inventory.ssh_command_for(
            node, self.server.inventory,
            public_key_line=self.server.public_key_line,
            key_comment=self.server.cfg.ssh_key_comment,
            client_key_path=self.server.cfg.ssh_client_key_path,
        )

    def _node_action(self, conn, project_id: str, node_id: str, action: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        node = self._node_in_project(conn, project_id, node_id)
        now_epoch = time.time()
        sample = store.latest_sample(conn, node_id)
        status, _ = health.node_status_health(node, sample, now_epoch)

        if action == "reboot":
            if status == "offline":
                raise errors.err(503, "NODE_OFFLINE", f"node {node_id} is offline")
            body = self._read_body()
            delay = body.get("delay_s", 0)
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
                raise errors.invalid_argument("delay_s must be a non-negative number")
            cmd = cmd_mod.issue_command(
                conn, self.server.operator_logs, project_id, "node.reboot",
                {"delay_s": int(delay)}, "human", node_id=node_id,
            )
            return 202, {"command_id": cmd["command_id"], "type": "node.reboot",
                         "state": cmd["state"], "issued_by": "human"}

        if action == "drain":
            body = self._read_body()
            reason = body.get("reason")
            if reason not in cmd_mod.DRAIN_REASONS:
                raise errors.invalid_argument(
                    "reason must be one of " + "|".join(cmd_mod.DRAIN_REASONS))
            evacuate_to = body.get("evacuate_to")
            if evacuate_to is not None:
                if not isinstance(evacuate_to, str) or not ulid.is_valid_ulid(evacuate_to):
                    raise errors.invalid_argument("evacuate_to must be a node ULID")
                if store.get_node(conn, evacuate_to) is None:
                    raise errors.invalid_argument("evacuate_to is not a known node")
            payload = {"reason": reason}
            if evacuate_to:
                payload["evacuate_to"] = evacuate_to
            cmd = cmd_mod.issue_command(
                conn, self.server.operator_logs, project_id, "node.drain",
                payload, "human", node_id=node_id,
            )
            return 202, {"command_id": cmd["command_id"], "type": "node.drain",
                         "state": cmd["state"], "issued_by": "human"}

        raise errors.not_found(f"unknown action {action}")

    def _remediation_decision(self, conn, project_id: str, command_id: str, decision: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        if decision == "approve":
            body = self._read_body()
            return 200, cmd_mod.approve_command(
                conn, self.server.operator_logs, project_id, command_id)
        # deny
        body = self._read_body()
        reason = body.get("reason", "")
        if reason and not isinstance(reason, str):
            raise errors.invalid_argument("reason must be a string")
        return 200, cmd_mod.deny_command(
            conn, self.server.operator_logs, project_id, command_id, reason or "")

    # ------------------------------------------------------------------ intent / plans
    def _intents(self, conn, project_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        body = self._read_body()
        clamp_validate, default_caps = _load_intent_validator()

        host_caps = self._host_caps(conn, default_caps)

        if "prompt" in body:
            if not isinstance(body["prompt"], str) or not body["prompt"].strip():
                raise errors.invalid_argument("prompt must be a non-empty string")
            extract_and_validate = _load_intent_extractor()
            if extract_and_validate is None:
                raise errors.err(503, "INTERNAL", "intent extractor unavailable")
            result = extract_and_validate(body["prompt"], host_caps)
            raw_allocations = None
        elif "spec" in body:
            spec = body["spec"]
            if not isinstance(spec, dict):
                raise errors.invalid_argument("spec must be an object")
            allocations = spec.get("allocations")
            if not isinstance(allocations, list):
                raise errors.invalid_argument("spec.allocations must be an array")
            result = clamp_validate(allocations, host_caps)
            raw_allocations = allocations
        else:
            raise errors.invalid_argument("body must contain 'prompt' or 'spec'")

        plan = self._build_plan(conn, project_id, result, raw_allocations)
        if plan["outcome"] == "rejected":
            raise errors.ApiError(422, "GUARDRAIL_REJECTED",
                                  "allocation plan rejected by guardrail", plan.get("reject_reasons", []))
        self.server.plans[plan["plan_id"]] = plan
        return 200, plan

    def _apply_plan(self, conn, project_id: str) -> tuple[int, dict]:
        self._require_project(conn, project_id)
        body = self._read_body()
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, str):
            raise errors.invalid_argument("plan_id is required")
        plan = self.server.plans.get(plan_id)
        if plan is None:
            raise errors.not_found(f"plan {plan_id} not found")
        if plan["outcome"] != "accepted":
            raise errors.err(409, "CONFLICT", "plan is not accepted")
        job_id = ulid.new_ulid()
        # Materialization (LXD fan-out via the node command channel) is the node
        # channel / LXD task's scope; this returns the accepted job handle.
        self.server.broker.publish(
            "vm.provisioned",
            {"job_id": job_id, "plan_id": plan_id, "state": "provisioning", "vm_ids": []},
            project_id,
        )
        return 202, {"job_id": job_id, "state": "provisioning", "vm_ids": []}

    def _host_caps(self, conn, default_caps: dict) -> dict:
        """Per-role host maxima for the clamp validator, from registered nodes."""
        nodes = store.list_nodes(conn, self.server.project["id"])
        caps = {
            "agent_vm": dict(default_caps["agent_vm"]),
            "inference_vm": dict(default_caps["inference_vm"]),
        }
        for n in nodes:
            hw = json.loads(n["hardware_json"]) if n["hardware_json"] else {}
            cap = health.capacity_of(hw)
            if n["role"] == "inference":
                if cap["ram_gb"]:
                    caps["inference_vm"]["ram_gb"] = max(0, cap["ram_gb"] - 2)
            else:
                if cap["cpu_cores"]:
                    caps["agent_vm"]["vcpus"] = max(1, cap["cpu_cores"] * 2)
                if cap["ram_gb"]:
                    caps["agent_vm"]["ram_gb"] = max(1, cap["ram_gb"] - 2)
        return caps

    def _build_plan(self, conn, project_id: str, result, raw_allocations) -> dict:
        """Map a ValidationResult to the AllocationPlan shape (controller-api.md §4.2)."""
        nodes = store.list_nodes(conn, project_id)
        outcome = "accepted" if result.safe else "rejected"
        allocations = []
        reject_reasons = []
        for r in result.rejections:
            reject_reasons.append({"index": r.index, "reason": r.reason, "detail": r.detail})

        for i, spec in enumerate(result.allocations):
            resolved_node = self._resolve_target(spec, nodes)
            role_ok, role_reason = _role_check(spec, resolved_node)
            if role_ok is False:
                allocations.append({
                    "index": i, "status": "rejected", "resolved": spec.to_dict(),
                    "guardrail": {"clamped_fields": [], "reject_reasons": [{"reason": role_reason}]},
                    "node_id": None, "vm_ids": [],
                })
                outcome = "rejected"
                continue
            clamped = [d.to_dict() for d in result.decisions if d.index == i]
            allocations.append({
                "index": i,
                "status": "accepted",
                "resolved": spec.to_dict(),
                "guardrail": {"clamped_fields": clamped, "reject_reasons": []},
                "node_id": resolved_node["id"] if resolved_node else None,
                "vm_ids": [],
            })

        plan = {
            "plan_id": ulid.new_ulid(),
            "request_id": ulid.new_ulid(),
            "project_id": project_id,
            "outcome": outcome,
            "created_at": store.now_iso(),
            "allocations": allocations,
            "audit": {"actor": "human", "guardrail": "termini.intent.clamp_validate"},
        }
        if reject_reasons:
            plan["reject_reasons"] = reject_reasons
        return plan

    def _resolve_target(self, spec, nodes: list[dict]) -> dict | None:
        if not nodes:
            return None
        target = spec.target_node
        if target and target != "auto":
            for n in nodes:
                if n["id"] == target:
                    return n
            return None
        # auto: node with the most free allocatable RAM (tie-break: lowest node_id)
        best = None
        best_ram = -1
        for n in nodes:
            hw = json.loads(n["hardware_json"]) if n["hardware_json"] else {}
            cap = health.capacity_of(hw)
            if cap["ram_gb"] > best_ram or (cap["ram_gb"] == best_ram and best is not None and n["id"] < best["id"]):
                best_ram = cap["ram_gb"]
                best = n
        return best

    # ------------------------------------------------------------------ telemetry
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

        latest = store.latest_sample(conn, node_id)
        self._publish_node_events(conn, node, latest)

        return 202, {"accepted": inserted, "rejected": dropped, "backpressure": "none"}

    def _publish_node_events(self, conn, node: dict, sample: dict | None) -> None:
        """Push node.telemetry (≤1/s) and node.health_changed (on transition)."""
        node_id = node["id"]
        project_id = self.server.project["id"]
        now_epoch = time.time()

        status, _ = health.node_status_health(node, sample, now_epoch)
        if self.server._last_status.get(node_id) != status:
            self.server._last_status[node_id] = status
            self.server.broker.publish(
                "node.health_changed",
                {"node_id": node_id, "status": status, "last_seen_at": node["last_seen_at"]},
                project_id,
            )

        last = self.server._last_telemetry_at.get(node_id, 0.0)
        if sample and now_epoch - last >= 1.0:
            self.server._last_telemetry_at[node_id] = now_epoch
            self.server.broker.publish("node.telemetry", sample, project_id)

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

    def _node_view(self, conn, n: dict, sample: dict | None = None) -> dict:
        if sample is None:
            sample = store.latest_sample(conn, n["id"])
        hw = json.loads(n["hardware_json"]) if n["hardware_json"] else {}
        status, health_obj = health.node_status_health(n, sample, time.time())
        return {
            "node_id": n["id"],
            "registration_id": n["registration_id"],
            "role": n["role"],
            "status": status,
            "last_seen_at": n["last_seen_at"],
            "uptime_sec": health.uptime_sec(sample),
            "hardware": hw,
            "capacity": health.capacity_of(hw),
            "health": health_obj,
        }

    # ------------------------------------------------------------------ WebSocket
    def _handle_websocket(self, segs: list[str]) -> None:
        if segs[:1] == ["stream"] and len(segs) >= 2 and segs[1] == "v1":
            self._ws_stream()
            return
        if len(segs) == 4 and segs[:3] == ["terminal", "v1", "nodes"]:
            self._ws_terminal(segs[3])
            return
        self._send_error(errors.not_found(f"no websocket route: {self.path}"))
        self.close_connection = True

    def _ws_handshake(self) -> wsmod.WebSocket | None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_error(errors.invalid_argument("missing Sec-WebSocket-Key"))
            return None
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsmod.accept_key(key))
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return None
        return wsmod.WebSocket(self.rfile, self.connection)

    def _ws_stream(self) -> None:
        q = self._query()
        try:
            self._require_project_token(q.get("token"))
        except errors.ApiError as e:
            self._ws_reject(e)
            return
        ws = self._ws_handshake()
        if ws is None:
            return
        project_id = self.server.project["id"]
        # Backfill: replay buffered log lines after `since`, then stream live.
        since = q.get("since")
        conn = db.connect(self.server.cfg.db_path)
        try:
            if since:
                oldest = self.server.operator_logs.oldest_retained(conn, project_id)
                if oldest is None or since < oldest:
                    ws.send_text(json.dumps({
                        "event": "log.line", "ts": store.now_iso(), "project_id": project_id,
                        "data": {"log_id": oldest or "", "ts": store.now_iso(), "severity": "warn",
                                 "actor": "ai_operator", "node_id": None,
                                 "message": "log tail truncated; resuming from oldest retained line",
                                 "remediation": None, "truncated": True},
                    }))
                for line in self.server.operator_logs.replay(conn, project_id, since):
                    ws.send_text(json.dumps({"event": "log.line", "ts": line["ts"],
                                             "project_id": project_id, "data": line}))
        finally:
            conn.close()

        sub = self.server.broker.subscribe()
        try:
            self._ws_pump(ws, sub)
        finally:
            self.server.broker.unsubscribe(sub)
            self.close_connection = True

    def _ws_pump(self, ws: wsmod.WebSocket, sub) -> None:
        """Forward broker events; answer client pings."""
        import queue
        import select as _select
        while True:
            try:
                msg = sub.get(timeout=0.5)
                ws.send_text(json.dumps(msg))
            except queue.Empty:
                pass
            except OSError:
                break
            # Drain inbound frames without blocking the pump; answer ping -> pong.
            try:
                r, _, _ = _select.select([ws.fileno()], [], [], 0)
                if r:
                    text = ws.recv_message()
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("type") == "ping":
                        ws.send_text(json.dumps({"type": "pong"}))
            except (wsmod.ConnectionClosed, wsmod.WebSocketError, OSError):
                break

    def _ws_terminal(self, node_id: str) -> None:
        q = self._query()
        try:
            self._require_project_token(q.get("token"))
        except errors.ApiError as e:
            self._ws_reject(e)
            return
        conn = db.connect(self.server.cfg.db_path)
        try:
            node = store.get_node(conn, node_id)
            if node is None or node["project_id"] != self.server.project["id"]:
                self._ws_terminal_error(node_id, "NOT_FOUND", "node not found")
                return
            sample = store.latest_sample(conn, node_id)
            status, _ = health.node_status_health(node, sample, time.time())
            public_ip = node.get("public_ip") or self.server.inventory.public_ip_for(node)
            if status == "offline":
                self._ws_terminal_error(node_id, "NODE_OFFLINE", "node not reachable")
                return
            if self.server.cfg.terminal_backend == "ssh" and not public_ip:
                self._ws_terminal_error(node_id, "NODE_OFFLINE", "node has no routable address")
                return
        finally:
            conn.close()

        ws = self._ws_handshake()
        if ws is None:
            return

        # One session per node: replace any existing session.
        existing = self.server.terminal_manager.replace(node_id)
        if existing is not None:
            try:
                existing.ws.send_text(json.dumps(
                    {"type": "error", "code": "SESSION_REPLACED", "message": "session replaced"}))
            except OSError:
                pass
            existing.close()

        try:
            from . import terminal as terminal_mod
            key_path = self.server.cfg.ssh_key_path
            user = self.server.inventory.ssh_user
            port = self.server.inventory.ssh_port
            backend = self.server.cfg.terminal_backend
            master_fd, proc, shell = terminal_mod.spawn_shell(
                backend, key_path=key_path, user=user, host=public_ip,
                port=port, cols=120, rows=40,
            )
        except (OSError, FileNotFoundError) as exc:
            try:
                ws.send_text(json.dumps(
                    {"type": "error", "code": "INTERNAL", "message": f"failed to open shell: {exc}"}))
            except OSError:
                pass
            ws.send_close(1011, "shell spawn failed")
            self.close_connection = True
            return

        session = terminal_mod.TerminalSession(
            ws, node_id=node_id, user=user, shell=shell,
            master_fd=master_fd, proc=proc,
            idle_timeout_s=self.server.cfg.terminal_idle_timeout_s,
            on_close=self.server.terminal_manager.on_close,
        )
        self.server.terminal_manager.register(session)
        session.run()
        self.close_connection = True

    def _ws_reject(self, e: errors.ApiError) -> None:
        """Refuse a WebSocket per dashboard.md §3.1: complete the handshake, then
        close with code 4401 + reason 'unauthorized' (no JSON frames exchanged)."""
        ws = self._ws_handshake()
        if ws is None:
            return
        reason = "unauthorized"
        try:
            ws.send_close(WS_CLOSE_UNAUTHORIZED, reason)
        except OSError:
            pass
        self.close_connection = True

    def _ws_terminal_error(self, node_id: str, code: str, message: str) -> None:
        """Send the terminal error frame after a successful handshake."""
        ws = self._ws_handshake()
        if ws is None:
            return
        try:
            ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
        except OSError:
            pass
        ws.send_close(1011, message)
        self.close_connection = True

    # ------------------------------------------------------------------ SSE logs
    def _handle_sse_logs(self, project_id: str) -> None:
        q = self._query()
        try:
            self._require_project_token(q.get("token"))
            if project_id != self.server.project["id"]:
                raise errors.permission_denied("project_id does not match token scope")
        except errors.ApiError as e:
            self._send_error(e)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return

        since = q.get("since") or self.headers.get("Last-Event-ID")
        conn = db.connect(self.server.cfg.db_path)
        try:
            if since:
                oldest = self.server.operator_logs.oldest_retained(conn, project_id)
                if oldest is None or since < oldest:
                    self._sse_write("log", {
                        "log_id": oldest or "", "ts": store.now_iso(), "node_id": None,
                        "severity": "warn", "actor": "ai_operator",
                        "message": "log tail truncated; resuming from oldest retained line",
                        "remediation": None, "truncated": True,
                    })
                for line in self.server.operator_logs.replay(conn, project_id, since):
                    self._sse_write("log", line)
        finally:
            conn.close()

        sub = self.server.broker.subscribe()
        try:
            self._sse_pump(sub)
        finally:
            self.server.broker.unsubscribe(sub)
            self.close_connection = True

    def _sse_write(self, event: str, data: dict) -> bool:
        try:
            payload = json.dumps(data)
            self.wfile.write(f"id: {data.get('log_id', '')}\n".encode())
            self.wfile.write(f"event: {event}\n".encode())
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _sse_pump(self, sub) -> None:
        import queue
        while True:
            try:
                msg = sub.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg.get("event") == "log.line":
                if not self._sse_write("log", msg["data"]):
                    break


# ------------------------------------------------------------------ module helpers

def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _parse_rfc3339(s: str) -> float:
    from . import validate
    try:
        return validate.parse_rfc3339_epoch(s)
    except ValueError:
        raise errors.invalid_argument(f"invalid RFC3339 timestamp {s!r}")


def _load_intent_validator():
    """Import termini.intent.clamp_validate + DEFAULT_HOST_CAPS (repo-root package)."""
    try:
        from termini.intent import DEFAULT_HOST_CAPS, clamp_validate
    except ImportError:
        _add_repo_root()
        from termini.intent import DEFAULT_HOST_CAPS, clamp_validate
    return clamp_validate, DEFAULT_HOST_CAPS


def _load_intent_extractor():
    try:
        from termini.intent import extract_and_validate
        return extract_and_validate
    except ImportError:
        _add_repo_root()
        try:
            from termini.intent import extract_and_validate
            return extract_and_validate
        except ImportError:
            return None


def _add_repo_root() -> None:
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)


def _role_check(spec, resolved_node) -> tuple[bool, str | None]:
    """Frozen role guardrails (controller-api.md §5.2 steps 3/5)."""
    if spec.type == "inference_vm":
        if not spec.passthrough_gpu:
            return False, "gpu_required_for_inference"
        if resolved_node is None:
            return False, "invalid_target_node_type"
        if resolved_node.get("role") != "inference":
            return False, "invalid_target_node_type"
        return True, None
    # agent_vm
    if spec.passthrough_gpu:
        return False, "gpu_not_allowed_for_agent"
    if resolved_node is None:
        return False, "invalid_target_node"
    return True, None
