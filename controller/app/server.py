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
    POST /v1/operator/lease                       (project_token)    -> 200
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
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import db, errors, governance, health, inventory, registration, role, store, validate, ws as wsmod
from . import commands as cmd_mod
from . import ulid
from . import sshca as sshca_mod
from .operator import store as opstore
from .operator import target as op_target
from .operator import triggers as op_triggers

log = logging.getLogger("termini.controller")


def _stripped_sample_keys(raw_samples: Any, clean_samples: Any) -> set[str]:
    """Top-level sample keys removed by deny-by-default stripping.

    Stripping unknown fields is deliberate (governance.md §3 — it is what keeps a
    newer node's unknown C0 field from being rejected by an older controller). But
    a SILENT strip means a node whose redactor is broken never learns its field was
    discarded, and no operator sees C2 attempting to egress. This lets the ingest
    path log the drop, which is what makes the boundary observable.
    """
    dropped: set[str] = set()
    if not isinstance(raw_samples, list) or not isinstance(clean_samples, list):
        return dropped
    for raw, clean in zip(raw_samples, clean_samples):
        if isinstance(raw, dict) and isinstance(clean, dict):
            dropped |= set(raw) - set(clean)
    return dropped

MAX_BODY = 1_048_576  # 1 MiB, controller-api.md §1.4
WS_CLOSE_UNAUTHORIZED = 4401


class ControllerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg, project, broker, operator_logs, inventory, terminal_manager,
                 ca: sshca_mod.SSHCA):
        super().__init__(addr, ControllerHandler)
        self.cfg = cfg
        self.project = project
        self.project_token_hash = project["project_token_hash"]
        self.broker = broker
        self.operator_logs = operator_logs
        self.inventory = inventory
        self.terminal_manager = terminal_manager
        self.ca = ca
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
        if (method == "POST" and len(segs) == 4 and segs[:2] == ["v1", "projects"]
                and segs[3] == "ssh-key"):
            return self._ssh_key_sign(conn, segs[2])
        if (method == "POST" and len(segs) == 5 and segs[:2] == ["v1", "projects"]
                and segs[3] == "ssh-key" and segs[4] == "revoke"):
            return self._ssh_key_revoke(conn, segs[2])
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
        if method == "POST" and segs[:2] == ["v1", "operator"] and len(segs) == 3 and segs[2] == "lease":
            return self._operator_lease(conn)
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
            ca_public_key_line=self.server.ca.public_key_line,
            key_comment=self.server.cfg.ssh_key_comment,
            client_key_path=self.server.cfg.ssh_client_key_path,
        )

    def _ssh_key_sign(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/ssh-key — sign a user public key into a
        short-lived, principal-bound certificate (D4, controller-api.md §4.3).
        The user's private key is never received or generated here."""
        self._require_project(conn, project_id)
        if not self.server.ca.available:
            raise errors.err(503, "INTERNAL", "SSH CA is not configured")
        body = self._read_body()
        principal = body.get("principal") or self.server.cfg.ssh_cert_principal
        try:
            cert = self.server.ca.sign(
                body.get("public_key"),
                principal=principal,
                validity_s=body.get("validity_s"),
                identity=body.get("identity") or principal,
            )
        except sshca_mod.SSHCAError as exc:
            raise errors.invalid_argument(str(exc))
        store.insert_ssh_certificate(conn, {
            "cert_id": cert["cert_id"],
            "project_id": project_id,
            "principal": cert["principal"],
            "key_id": cert["key_id"],
            "key_algorithm": cert["key_algorithm"],
            "key_fingerprint": cert["key_fingerprint"],
            "valid_from": cert["valid_from"],
            "valid_until": cert["valid_until"],
            "issued_at": store.now_iso(),
        })
        return 200, {
            "cert_id": cert["cert_id"],
            "certificate": cert["certificate"],
            "ca_fingerprint": cert["ca_fingerprint"],
            "principal": cert["principal"],
            "valid_from": cert["valid_from"],
            "valid_until": cert["valid_until"],
            "algorithm": cert["key_algorithm"],
            "key_fingerprint": cert["key_fingerprint"],
        }

    def _ssh_key_revoke(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/ssh-key/revoke — revoke an issued cert by the
        public key fingerprint (which ssh-keygen reports for both key and cert)."""
        self._require_project(conn, project_id)
        body = self._read_body()
        fingerprint = body.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise errors.invalid_argument("fingerprint is required")
        status = store.revoke_ssh_certificate(conn, fingerprint.strip(), store.now_iso())
        if status is None:
            raise errors.not_found(f"no certificate issued for fingerprint {fingerprint}")
        return 200, {"fingerprint": fingerprint.strip(),
                     "revoked": status == "revoked", "status": status}

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
        """Materialise an accepted plan: one LXD instance per committed VM.

        This used to be a no-op that published `state: "provisioning"` forever. It
        now drives each allocation's resolved node's LXD REST API (per-node endpoint
        via `operator.target`, global fallback for an older node) and returns a
        TERMINAL state.

        The `vm_id` collected here is the LXD **instance name** — the deterministic,
        node-local identity of the created workload (`termini-<plan>-<index>-<slot>`).
        It is deliberately not a freshly minted ULID: a stable name derived from the
        plan is what makes a replayed apply idempotent (LXD rejects the duplicate
        name, which `create_instance` maps to `already_applied`), and it is the id an
        operator can actually find on the node. (This diverges from the ULID-typed
        `vm_ids` item in vm-allocation.schema.json; the plan-level contract text is
        updated to match.)

        Failure semantics — capacity must never claim an instance that does not
        exist. If any create of an allocation fails or times out, that allocation's
        partially-created instances are rolled back and its committed capacity is
        released (`store.release_allocation`), the allocation is reported `failed`
        with the error, and the job state is terminal `failed`. A create that
        succeeds (or is a duplicate = `already_applied`) keeps its commit.
        """
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

        cfg = self.server.cfg
        image_alias = cfg.lxd_workload_image
        job_id = ulid.new_ulid()
        node_cache: dict[str, dict | None] = {}
        alloc_results: list[dict] = []
        vm_ids: list[str] = []
        any_failed = False

        for alloc in plan["allocations"]:
            index = alloc.get("index")
            status = alloc.get("status")
            if status not in ("accepted", "clamped"):
                # The plan is all-or-nothing at validation time, so an accepted
                # plan has no rejected classes; keep this defensive anyway.
                alloc_results.append({
                    "index": index, "status": "skipped", "node_id": alloc.get("node_id"),
                    "vm_ids": [], "detail": f"allocation status {status!r}",
                })
                continue
            if alloc.get("realized_as") == "host_service":
                # inference_vm is a REFERENCE to the node's always-on host inference
                # service (D5): recorded, never launched, so no VM and no vm_id.
                alloc_results.append({
                    "index": index, "status": "ready", "node_id": alloc.get("node_id"),
                    "realized_as": "host_service", "vm_ids": [],
                    "detail": "host_service reference; no VM materialised",
                })
                continue

            resolved = alloc.get("resolved") or {}
            node_id = alloc.get("node_id")
            count = max(0, int(resolved.get("count") or 0))
            vcpus = int(resolved.get("vcpus") or 0)
            ram_gb = int(resolved.get("ram_gb") or 0)
            disk_gb = int(resolved.get("disk_gb") or 0)

            if node_id not in node_cache:
                node_cache[node_id] = store.get_node(conn, node_id) if node_id else None
            node = node_cache[node_id]
            if node is None:
                any_failed = True
                alloc_results.append({
                    "index": index, "status": "failed", "node_id": node_id, "vm_ids": [],
                    "detail": "resolved node not found; nothing committed to release",
                })
                continue

            executor = op_target.executor_for_node(cfg, node)
            created: list[str] = []
            failure: str | None = None
            already = 0
            for slot in range(count):
                name = op_target.instance_name(plan_id, index, slot)
                r = executor.create_instance(
                    name, image_alias=image_alias,
                    cpu=vcpus or None,
                    memory=f"{ram_gb}GiB" if ram_gb else None,
                    timeout_s=cfg.lxd_create_timeout_s,
                )
                if r.result == "ok":
                    created.append(name)
                elif r.result == "already_applied":
                    already += 1
                    created.append(name)
                else:
                    failure = f"create {name} failed: {r.detail}"
                    break

            if failure is None:
                alloc["vm_ids"] = created           # record the real ids on the plan
                vm_ids.extend(created)
                alloc_results.append({
                    "index": index, "status": "ready", "node_id": node_id,
                    "realized_as": alloc.get("realized_as", "vm"), "vm_ids": created,
                    "created": len(created) - already, "already_applied": already,
                })
                continue

            # Roll back this allocation: delete any instance we did create, then
            # release the class's committed capacity. The plan must not keep
            # capacity (or a vm_id) for an instance that does not exist.
            any_failed = True
            for name in created:
                executor.delete_instance(name)
            released = store.release_allocation(
                conn, project_id, plan_id, node_id,
                count * vcpus, count * ram_gb, count * disk_gb,
            )
            alloc_results.append({
                "index": index, "status": "failed", "node_id": node_id,
                "realized_as": alloc.get("realized_as", "vm"), "vm_ids": [],
                "detail": failure, "rolled_back": len(created), "capacity_released": released,
            })

        state = "failed" if any_failed else "ready"
        event = {
            "job_id": job_id, "plan_id": plan_id, "state": state,
            "vm_ids": vm_ids, "allocations": alloc_results,
        }
        self.server.broker.publish("vm.provisioned", event, project_id)

        # Audit trail in the operator log stream (dashboard.md §2) so a failed or
        # partial apply is observable, not just a broker frame a late subscriber
        # missed.
        logs = getattr(self.server, "operator_logs", None)
        if logs is not None:
            logs.append(
                conn, project_id, severity=("error" if any_failed else "info"),
                actor="human",
                message=f"apply plan {plan_id}: {state} ({len(vm_ids)} vm(s))",
                remediation=event,
            )

        return 202, {"job_id": job_id, "plan_id": plan_id, "state": state,
                     "vm_ids": vm_ids, "allocations": alloc_results}

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
        """Map a ValidationResult to the AllocationPlan shape (controller-api.md §4.2).

        Runs §5.2 step 6 (per-node capacity clamp) + step 7 (commit) on top of
        the deterministic validator output: every accepted `agent_vm` class is
        clamped against its resolved node's allocatable capacity (registered −
        reserve − allocated) and, if the whole plan is safe, committed.
        """
        nodes = store.list_nodes(conn, project_id)
        plan_id = ulid.new_ulid()
        request_id = ulid.new_ulid()
        outcome = "accepted" if result.safe else "rejected"
        allocations = []
        reject_reasons = []
        for r in result.rejections:
            reject_reasons.append({"index": r.index, "reason": r.reason, "detail": r.detail})

        # Committed capacity per node, loaded lazily so a role-check-only caller
        # (no DB connection) can still drive the plan; plus this request's
        # in-flight accounting so multiple classes on one node share the ceiling.
        committed: dict[str, dict] = {}
        pending: dict[str, dict] = {}  # node_id -> committed deltas to persist (step 7)

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
                # Surface role-level rejections in the top-level reject_reasons
                # (the 422 details[]) so clients can audit why an otherwise
                # valid spec was refused at the role guardrail (W7 F3).
                reject_reasons.append({
                    "index": i,
                    "reason": role_reason,
                    "detail": _ROLE_REJECT_DETAILS.get(role_reason, role_reason),
                })
                continue
            clamped = [d.to_dict() for d in result.decisions if d.index == i]
            resolved = spec.to_dict()
            status = "accepted"

            # §5.2 step 6: per-node capacity clamp (agent_vm only; inference_vm
            # is a reference to the host service and reserves no VM capacity).
            # Skipped when the resolved node reported no hardware capacity.
            hw_raw = resolved_node.get("hardware_json") if resolved_node else None
            if spec.type == "agent_vm" and resolved_node is not None and hw_raw:
                node_id = resolved_node["id"]
                if node_id not in committed:
                    committed[node_id] = (
                        store.allocated_capacity(conn, node_id) if conn is not None
                        else {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0}
                    )
                alloc = committed[node_id]
                hw = json.loads(hw_raw) if hw_raw else {}
                cap = health.capacity_of(
                    hw, alloc["cpu_allocated"], alloc["ram_allocated_gb"], alloc["disk_allocated_gb"],
                )
                clamped_resolved, cap_decisions, cap_reject = health.clamp_to_node_capacity(
                    spec.count, spec.vcpus, spec.ram_gb, spec.disk_gb, cap, index=i,
                )
                clamped.extend(cap_decisions)
                # Merge the clamped per-VM fields back into the full spec shape
                # (count is never changed by the capacity clamp).
                resolved["vcpus"] = clamped_resolved["vcpus"]
                resolved["ram_gb"] = clamped_resolved["ram_gb"]
                resolved["disk_gb"] = clamped_resolved["disk_gb"]
                if cap_reject:
                    allocations.append({
                        "index": i, "status": "rejected", "resolved": resolved,
                        "guardrail": {"clamped_fields": clamped,
                                      "reject_reasons": [{"reason": cap_reject}]},
                        "node_id": node_id, "vm_ids": [],
                    })
                    reject_reasons.append({
                        "index": i,
                        "reason": cap_reject,
                        "detail": _CAPACITY_REJECT_DETAILS.get(cap_reject, cap_reject),
                    })
                    outcome = "rejected"
                    continue
                status = "clamped" if cap_decisions else "accepted"
                delta = {
                    "cpu_allocated": resolved["count"] * resolved["vcpus"],
                    "ram_allocated_gb": resolved["count"] * resolved["ram_gb"],
                    "disk_allocated_gb": resolved["count"] * resolved["disk_gb"],
                }
                committed[node_id] = {
                    "cpu_allocated": alloc["cpu_allocated"] + delta["cpu_allocated"],
                    "ram_allocated_gb": alloc["ram_allocated_gb"] + delta["ram_allocated_gb"],
                    "disk_allocated_gb": alloc["disk_allocated_gb"] + delta["disk_allocated_gb"],
                }
                p = pending.setdefault(
                    node_id, {"cpu_allocated": 0, "ram_allocated_gb": 0, "disk_allocated_gb": 0})
                for k in delta:
                    p[k] += delta[k]

            realized_as = "host_service" if spec.type == "inference_vm" else "vm"
            allocations.append({
                "index": i,
                "status": status,
                "resolved": resolved,
                "guardrail": {"clamped_fields": clamped, "reject_reasons": []},
                "node_id": resolved_node["id"] if resolved_node else None,
                "realized_as": realized_as,
                "vm_ids": [],
            })

        # §5.2 step 7: commit accepted/clamped agent_vm capacity. All-or-nothing —
        # any rejected class commits nothing and the plan is rejected upstream.
        if outcome != "rejected":
            now = store.now_iso()
            for node_id, delta in pending.items():
                store.commit_allocation(
                    conn, project_id, plan_id, node_id,
                    delta["cpu_allocated"], delta["ram_allocated_gb"], delta["disk_allocated_gb"],
                    now,
                )

        plan = {
            "plan_id": plan_id,
            "request_id": request_id,
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
        if spec.type == "inference_vm":
            # Reference-class resolution (controller-api.md §5.2 step 3): `auto`
            # resolves to the (single) inference node hosting the always-on
            # service; no inference node registered -> None -> REJECT upstream.
            for n in nodes:
                if n["role"] == "inference":
                    return n
            return None
        # auto (agent_vm): node with the most free allocatable RAM (tie-break: lowest node_id)
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

        # Make deny-by-default stripping observable: a stripped field is data the
        # node believed it was allowed to send. Silent discard hides both a broken
        # node-side redactor and any C2 that reached this far under a name the
        # C2 matcher did not recognise.
        stripped = _stripped_sample_keys(body.get("samples"), samples)
        if stripped:
            log.warning(
                "telemetry: stripped non-allowlisted field(s) node=%s call_id=%s fields=%s",
                node_id, clean.get("call_id"), sorted(stripped),
            )

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

    def _operator_lease(self, conn) -> tuple[int, dict]:
        """POST /v1/operator/lease — acquire/renew the single-owner operator lease.

        Both the in-process loop (via `opstore.acquire_lease` in `loop.py`) and the
        standalone CLI (this endpoint) acquire the same per-project lease before
        remediating, so only one operator instance ever acts at a time.
        """
        self._require_project(conn, self.server.project["id"])
        body = self._read_body()
        owner_id = body.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise errors.invalid_argument("owner_id is required")
        ttl_s = float(getattr(self.server.cfg, "operator_lease_ttl_s", 10.0))
        now = op_triggers.now_iso_ms()
        owned = opstore.acquire_lease(
            conn, self.server.project["id"], owner_id.strip(), now, ttl_s
        )
        lease = opstore.get_lease(conn, self.server.project["id"])
        return 200, {
            "owner": owned,
            "owner_id": lease["owner_id"] if lease else None,
            "acquired_at": lease["acquired_at"] if lease else None,
            "expires_at": lease["expires_at"] if lease else None,
        }

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
        alloc = store.allocated_capacity(conn, n["id"])
        capacity = health.capacity_of(hw, alloc["cpu_allocated"], alloc["ram_allocated_gb"], alloc["disk_allocated_gb"])
        return {
            "node_id": n["id"],
            "registration_id": n["registration_id"],
            "role": n["role"],
            "status": status,
            "last_seen_at": n["last_seen_at"],
            "uptime_sec": health.uptime_sec(sample),
            "hardware": hw,
            "capacity": capacity,
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
            user = self.server.inventory.ssh_user
            port = self.server.inventory.ssh_port
            backend = self.server.cfg.terminal_backend
            identity_key_path = None
            cert_path = None
            if backend == "ssh":
                if not self.server.ca.available:
                    raise sshca_mod.SSHCAError("SSH CA is not configured")
                identity_key_path, cert_path, _meta = self.server.ca.issue_operator_certificate(
                    self.server.cfg.ssh_operator_key_path,
                    principal=self.server.cfg.ssh_cert_principal,
                    validity_s=self.server.cfg.ssh_cert_default_validity_s,
                )
            master_fd, proc, shell = terminal_mod.spawn_shell(
                backend, identity_key_path=identity_key_path, cert_path=cert_path,
                user=user, host=public_ip, port=port, cols=120, rows=40,
            )
        except (OSError, FileNotFoundError, sshca_mod.SSHCAError) as exc:
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
    """Frozen role guardrails (controller-api.md §5.2 steps 3/5, architecture.md R16)."""
    if spec.type == "inference_vm":
        # R16: inference_vm is a REFERENCE to the always-on Node C host inference
        # service, not a launchable VM. No GPU passthrough requirement (it is a
        # structural rejection upstream, not here). It must resolve to the
        # inference node; zero VMs are materialized for it.
        if resolved_node is None or resolved_node.get("role") != "inference":
            return False, "invalid_target_node_type"
        return True, None
    # agent_vm
    if spec.passthrough_gpu:
        return False, "gpu_not_allowed_for_agent"
    if resolved_node is None:
        return False, "invalid_target_node"
    return True, None


# Human-readable detail for each role-level rejection reason (surfaced in the
# top-level 422 details[] alongside the machine-readable code). Values are the
# same frozen codes as vm-allocation.schema.json §clamped_field.reason.
_ROLE_REJECT_DETAILS = {
    "invalid_target_node_type": "inference_vm must resolve to the inference node",
    "invalid_target_node": "agent_vm target node could not be resolved to a compute node",
    "gpu_not_allowed_for_agent": "GPU passthrough is not permitted on agent_vm",
}

# Human-readable detail for each per-node capacity rejection reason
# (controller-api.md §5.2 step 6), surfaced alongside the machine-readable code.
_CAPACITY_REJECT_DETAILS = {
    "exceeds_node_ram_ceiling": "aggregate RAM exceeds the node's free capacity",
    "exceeds_node_cpu_ceiling": "aggregate vCPU exceeds the node's overcommit ceiling",
    "exceeds_node_disk_ceiling": "aggregate disk exceeds the node's free capacity",
}
