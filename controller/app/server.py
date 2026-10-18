"""HTTP service — stdlib `http.server.ThreadingHTTPServer` + a small router.

Endpoints (controller core + W5 dashboard surfaces, per controller-api.md §4/§6/§7
and dashboard.md §1–§4):

    POST /v1/nodes/register                       (bootstrap_token)  -> 201
    POST /v1/bootstrap-tokens                     (project_token, loopback) -> 201
    GET  /install/termini-node.sh                 (public, static)   -> 200
    POST /v1/nodes/{nid}/heartbeat                (node_token)       -> 204/200
    GET  /v1/projects/{pid}/nodes                 (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}           (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}/metrics   (project_token)    -> 200
    GET  /v1/projects/{pid}/nodes/{nid}/ssh       (project_token)    -> 200
    POST /v1/projects/{pid}/nodes/{nid}/actions/{reboot,drain}       -> 202
    POST /v1/projects/{pid}/intents               (project_token)    -> 200/422
    POST /v1/projects/{pid}/vms/apply             (project_token)    -> 202
    POST /v1/projects/{pid}/vms/release           (project_token)    -> 200/404
    POST /v1/projects/{pid}/vms/reconcile         (project_token)    -> 200
    POST /v1/projects/{pid}/nodes/{nid}/decommission (project_token) -> 200/404
    POST /v1/projects/{pid}/remediations/{cid}/{approve,deny}        -> 200
    POST /v1/projects/{pid}/assistant/ask        (project_token)    -> 200
    POST /v1/nodes/{nid}/telemetry                (node_token)       -> 202
    GET  /v1/nodes/{nid}/telemetry/recent         (node_token)       -> 200
    POST /v1/nodes/{nid}/lxd-trust                 (node_token)       -> 200
    GET  /v1/telemetry/recent?node=&limit=        (project_token)    -> 200
    POST /v1/operator/lease                       (project_token)    -> 200
    POST /v1/projects/{pid}/demo/faults           (project_token)    -> 201   operator-only
    GET  /v1/projects/{pid}/demo/faults           (project_token)    -> 200   operator-only
    POST /v1/projects/{pid}/demo/faults/clear     (project_token)    -> 200   operator-only
    DELETE /v1/projects/{pid}/demo/faults/{iid}   (project_token)    -> 200   operator-only
    GET  /v1/projects/{pid}/logs                  (SSE, Bearer auth; ?token= fallback)
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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import db, errors, governance, health, inventory, lxdtrust, registration, role, store, validate, ws as wsmod
from . import agent_tasks as agent_tasks_mod
from . import agent_actions as agent_actions_mod
from . import assistant as assistant_mod
from . import commands as cmd_mod
from . import demo_faults as demo_faults_mod
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

# Headers a reverse proxy adds when it forwards a request. The demo controller is
# reached publicly through the host's reverse proxy (:443 -> 127.0.0.1:8080), so a
# REMOTE request still arrives with a loopback TCP peer; these headers are what
# distinguish it from the seeder's direct loopback call (see `_is_loopback_client`).
PROXY_REQUEST_HEADERS = ("X-Forwarded-For", "X-Real-Ip", "Forwarded", "Via")

# Public client-install surface (docs/contracts/controller-api.md §4.7, task
# t_70bd0f2b). STATIC, UNAUTHENTICATED, and deliberately SECRET-FREE: the node
# installer a customer pipes into `sh`, the client status helper it installs, and
# the two node-runtime files a machine that did not come from the Termini node
# image needs (the image ships them pre-baked).
#
# Lookup is by NAME from this table and nothing else — no part of the request
# path reaches the filesystem, so there is no traversal to escape. Serving them
# from the controller is deliberate: it is the one surface a customer can already
# reach, and it removes the second distribution channel (and its drift) that a
# separate download host would be.
INSTALL_ARTIFACTS = {
    "termini-node.sh": ("client/termini-node-install.sh", "text/x-shellscript"),
    "termini-node-status.py": ("client/termini-node-status.py", "text/x-python"),
    "termini-bootstrap.py": ("node/termini-bootstrap.py", "text/x-python"),
    "termini-bootstrap.service": ("node/termini-bootstrap.service", "text/plain"),
}


def install_root() -> str:
    """Directory holding the install artifacts (`TERMINI_INSTALL_DIR` or the tree).

    Default resolution is the repo tree this controller was deployed from: the
    controller runs from `<tree>/controller/app`, so `<tree>/infra/bootstrap` is
    where the artifacts live. `TERMINI_INSTALL_DIR` overrides it for an install
    that stages them elsewhere.
    """
    override = os.environ.get("TERMINI_INSTALL_DIR", "").strip()
    if override:
        return override
    app_dir = os.path.dirname(os.path.abspath(__file__))          # <tree>/controller/app
    tree = os.path.dirname(os.path.dirname(app_dir))              # <tree>
    return os.path.join(tree, "infra", "bootstrap")


class ControllerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg, project, broker, operator_logs, inventory, terminal_manager,
                 ca: sshca_mod.SSHCA, fault_injector=None, assistant=None):
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
        # Operator-only demo fault driver (controller-api.md §4.7). None when the
        # controller runs with injection disabled — the endpoints then refuse 503
        # rather than pretending to inject.
        self.fault_injector = fault_injector
        # The assistant service (controller-api.md §4.10). None until the first
        # /assistant/ask, then built lazily from cfg (so tests can inject a fake).
        self.assistant = assistant

    def publish_node_events(self, conn, node: dict, sample: dict | None) -> None:
        """Push node.telemetry (≤1/s) and node.health_changed (on transition).

        Server-level (not handler-level) because the demo fault driver fans its
        injected samples out through the same path — an injected sample must light
        the dashboard's live panels exactly like a node-reported one.
        """
        node_id = node["id"]
        project_id = self.project["id"]
        now_epoch = time.time()

        status, _ = health.node_status_health(node, sample, now_epoch)
        if self._last_status.get(node_id) != status:
            self._last_status[node_id] = status
            self.broker.publish(
                "node.health_changed",
                {"node_id": node_id, "status": status, "last_seen_at": node["last_seen_at"]},
                project_id,
            )

        last = self._last_telemetry_at.get(node_id, 0.0)
        if sample and now_epoch - last >= 1.0:
            self._last_telemetry_at[node_id] = now_epoch
            self.broker.publish("node.telemetry", sample, project_id)


def _endpoint_key(raw: str) -> tuple[str, str, int]:
    """Canonical (scheme, host, port) for comparing two LXD endpoints.

    `registration._lxd_endpoint` accepts both `host:port` and
    `scheme://host:port`, so the trust route must not treat the two spellings of
    the SAME listener as different nodes.
    """
    value = (raw or "").strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


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
                         "Authorization, Content-Type, Idempotency-Key, X-Termini-Version, "
                         "Last-Event-ID, Cache-Control, X-Termini-Actor")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _send_json(self, status: int, obj, headers: dict | None = None) -> None:
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
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, e: errors.ApiError) -> None:
        self._send_json(e.status, errors.envelope(e.code, e.message, e.details),
                        headers=getattr(e, "headers", None))

    def _send_bytes(self, status: int, content_type: str, data: bytes) -> None:
        """Raw-body response (the static client-install artifacts, not JSON)."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Termini-Version", "1")
        # A customer may be installing while the provider rolls a new artifact
        # out; never let a proxy or the browser serve a stale installer.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _bootstrap_token(self) -> str | None:
        # Accept the canonical Bearer form and the legacy X-Bootstrap-Token header.
        return self._bearer() or self.headers.get("X-Bootstrap-Token")

    def _actor(self) -> str:
        """Who is acting, from `X-Termini-Actor` (kanban t_9c1362f6).

        The project token is shared by every worker and by the console, so it cannot
        distinguish them. Without this header a peer worker's clear-all was written
        down as `cleared_by=human` — "the operator" — and that is exactly the lie the
        acceptance run tripped over. Absent header → `unattributed_project_token`
        (never `human`); a malformed one is a loud 422, not a silent default.
        """
        raw = (self.headers.get("X-Termini-Actor") or "").strip()
        if not raw:
            return demo_faults_mod.UNATTRIBUTED_ACTOR
        if not demo_faults_mod.ACTOR_RE.match(raw):
            raise errors.invalid_argument(
                "X-Termini-Actor must be 1..64 chars of [A-Za-z0-9_.:@-] "
                "(your card id, e.g. t_9c1362f6, or 'human')",
                [{"field": "X-Termini-Actor", "reason": raw[:80]}],
            )
        return raw

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
        if method == "POST" and segs == ["v1", "bootstrap-tokens"]:
            return self._mint_bootstrap_tokens(conn)
        # Agent task API (t_1a2c913c, agent-runtime.md §5.6/§7.4).
        if (method == "POST" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "agents" and segs[5] == "tasks"):
            return self._agent_task_submit(conn, segs[2], segs[4])
        if (method == "GET" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "agents" and segs[5] == "tasks"):
            return self._agent_task_list(conn, segs[2], segs[4])
        if (method == "GET" and len(segs) == 7 and segs[:2] == ["v1", "projects"]
                and segs[3] == "agents" and segs[5] == "tasks"):
            return self._agent_task_get(conn, segs[2], segs[4], segs[6])
        # Per-agent operations (t_44a2b87b, controller-api.md §4.11): the same
        # command/audit envelope as node actions, executed against one instance.
        if (method == "POST" and len(segs) == 7 and segs[:2] == ["v1", "projects"]
                and segs[3] == "agents" and segs[5] == "actions"):
            return self._agent_action(conn, segs[2], segs[4], segs[6])
        if method == "POST" and len(segs) == 4 and segs[:2] == ["v1", "projects"] and segs[3] == "intents":
            return self._intents(conn, segs[2])
        if method == "POST" and len(segs) == 5 and segs[:2] == ["v1", "projects"] and segs[3] == "vms" and segs[4] == "apply":
            return self._apply_plan(conn, segs[2])
        if method == "POST" and len(segs) == 5 and segs[:2] == ["v1", "projects"] and segs[3] == "vms" and segs[4] == "release":
            return self._release_plan(conn, segs[2])
        if method == "POST" and len(segs) == 5 and segs[:2] == ["v1", "projects"] and segs[3] == "vms" and segs[4] == "reconcile":
            return self._reconcile(conn, segs[2])
        # Assistant API (controller-api.md §4.10, t_bfd2c2f7): grounded Q&A over
        # the controller's records with guardrail-admitted execute authority.
        if (method == "POST" and len(segs) == 5 and segs[:2] == ["v1", "projects"]
                and segs[3] == "assistant" and segs[4] == "ask"):
            return self._assistant_ask(conn, segs[2])
        if (method == "POST" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "nodes" and segs[5] == "decommission"):
            return self._decommission_node(conn, segs[2], segs[4])
        if (method == "POST" and len(segs) == 6 and segs[:2] == ["v1", "projects"]
                and segs[3] == "remediations" and segs[5] in ("approve", "deny")):
            return self._remediation_decision(conn, segs[2], segs[4], segs[5])
        if (method == "GET" and len(segs) == 5 and segs[:2] == ["v1", "nodes"]
                and segs[3] == "telemetry" and segs[4] == "recent"):
            return self._node_recent(conn, segs[2])
        if (method == "POST" and len(segs) == 4 and segs[:2] == ["v1", "nodes"]
                and segs[3] == "lxd-trust"):
            return self._node_lxd_trust(conn, segs[2])
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
        # Operator-only demo fault injection (controller-api.md §4.7): project_token
        # only — a node token never authenticates here (see §4.7 "not reachable
        # from a node context"), and no token means 401.
        if len(segs) >= 5 and segs[:2] == ["v1", "projects"] and segs[3:5] == ["demo", "faults"]:
            if method == "POST" and len(segs) == 5:
                return self._demo_fault_start(conn, segs[2])
            if method == "GET" and len(segs) == 5:
                return self._demo_fault_list(conn, segs[2])
            if method == "POST" and len(segs) == 6 and segs[5] == "clear":
                return self._demo_fault_clear_all(conn, segs[2])
            if method == "DELETE" and len(segs) == 6:
                return self._demo_fault_clear(conn, segs[2], segs[5])
        raise errors.not_found(f"no route: {method} {path}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        # DELETE /v1/projects/{pid}/demo/faults/{injection_id} — clear one injection
        # (controller-api.md §4.7). Only the demo-fault routes accept DELETE; the
        # frozen REST surface is otherwise GET/POST.
        self._route("DELETE")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        segs = [s for s in path.split("/") if s]
        if self._is_websocket():
            self._handle_websocket(segs)
            return
        if len(segs) == 4 and segs[:2] == ["v1", "projects"] and segs[3] == "logs":
            self._handle_sse_logs(segs[2])
            return
        if segs[:1] == ["install"]:
            # Public static client-install surface — no DB connection, no token.
            try:
                self._check_version()
                self._serve_install(segs)
            except errors.ApiError as e:
                self._send_error(e)
            return
        self._route("GET")

    def _is_websocket(self) -> bool:
        return self.headers.get("Upgrade", "").lower() == "websocket"

    # ------------------------------------------------------------------ client install surface
    def _serve_install(self, segs: list[str]) -> None:
        """GET /install/<artifact> — the public, unauthenticated install surface.

        Static files only, chosen by name out of `INSTALL_ARTIFACTS`; no part of
        the request path is used to build a filesystem path. These artifacts carry
        no credential of any kind (that is what the customer's own enrollment
        credential is for), so there is nothing here to protect with a token — and
        requiring one would defeat the point, since the customer does not have
        (and must never hold) the controller's project token.
        """
        name = segs[1] if len(segs) == 2 else ""
        entry = INSTALL_ARTIFACTS.get(name)
        if entry is None:
            raise errors.not_found(
                "no install artifact: GET /install/"
                + (name or "<name>")
                + " (available: " + ", ".join(sorted(INSTALL_ARTIFACTS)) + ")"
            )
        rel, content_type = entry
        path = os.path.join(install_root(), *rel.split("/"))
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            log.error("install artifact %s missing on this controller (%s): %s",
                      name, path, e)
            raise errors.err(
                503, "INTERNAL",
                f"install artifact {name} is not deployed on this controller "
                f"(expected at {path})"
            ) from None
        self._send_bytes(200, content_type, data)

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
        # A heartbeat may carry the node's own LXD endpoint (`lxd.endpoint` nested
        # or the flat `lxd_endpoint`, node-bootstrap.md §5.2/§13). Registration is
        # one-shot: this is how a node whose listener came up later — or whose
        # address changed — keeps the controller's per-node endpoint current, so
        # the executor never falls back to the global URL for a node that has one.
        # An empty body is the old heartbeat and stays valid.
        body = self._read_body()
        endpoint = registration.endpoint_from_heartbeat(body)
        lxd_api = body.get("lxd_api") if isinstance(body.get("lxd_api"), dict) else {}
        now = store.now_iso()
        conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node_id))
        if endpoint and endpoint != node.get("lxd_endpoint"):
            store.set_node_lxd_endpoint(conn, node_id, endpoint)
            log.info("node %s reports its own LXD endpoint: %s (was %s)",
                     node_id, endpoint, node.get("lxd_endpoint"))
        if lxd_api.get("listening") is False:
            log.warning("node %s reports its LXD API is NOT listening on %s (%s): "
                        "remediations for it will fail",
                        node_id, lxd_api.get("endpoint"), lxd_api.get("detail") or "no detail")
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM commands WHERE node_id = ? AND state = 'queued'", (node_id,)
        ).fetchone()["n"]
        self._publish_node_events(conn, node, store.latest_sample(conn, node_id))
        if pending:
            return 200, {"pending_commands": pending}
        return 204, {}

    def _list_nodes(self, conn, project_id: str) -> tuple[int, dict]:
        """GET /v1/projects/{pid}/nodes — the fleet listing (dashboard.md §1.1).

        Default view is the REAL fleet: a seeder-fabricated row (`synthetic`) is
        excluded while any real node is registered, mirroring the placement rule
        (`_selection_pool`, controller-api.md §5.2 step 5). The standing demo's
        fleet is its hardware, so a stopped seeder's stale fabricated rows must
        not make the dashboard contradict the deck. A seeder-only demo (no real
        node registered) still lists its fabricated rows so the seeded demo
        renders, and `?include=synthetic` returns every row (provenance intact).
        """
        self._require_project(conn, project_id)
        nodes = store.list_nodes(conn, project_id)
        include = (self._query().get("include") or "").strip().lower()
        if include != "synthetic" and _real_nodes(nodes):
            nodes = _real_nodes(nodes)
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

    # ------------------------------------------------------------ agent tasks
    def _agent_task_submit(self, conn, project_id: str, vm_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/agents/{vm_id}/tasks — give an agent a task.

        Decision AT-1 (agent-runtime.md §7.4): the controller PROXIES this call onto
        the agent's loopback task surface through the owning node's LXD exec API. The
        client never needs a route to the instance and never receives instance
        credentials; the cost is that task content transits the controller, which is
        recorded, never logged, never model-processed here (see `agent_tasks.py`).

        Every submission is audited exactly like a node action: an `agent.dispatch_task`
        command in the same envelope/allowlist (`commands.py`) + an operator-log line,
        both content-free, plus the enumerable `agent_tasks` row.
        """
        self._require_project(conn, project_id)
        body = self._read_body()

        prompt = body.get("prompt")
        messages = body.get("messages")
        if prompt is None and messages:
            prompt = "\n".join(str(m.get("content", "")) for m in messages
                               if isinstance(m, dict))
        if not isinstance(prompt, str) or not prompt.strip():
            raise errors.err(422, "INVALID_ARGUMENT", "prompt is required",
                             [{"field": "prompt", "reason": "required"}])
        size = len(prompt.encode("utf-8"))
        if size > agent_tasks_mod.MAX_PROMPT_BYTES:
            raise errors.err(
                413, "PROMPT_TOO_LARGE",
                f"prompt is {size} bytes; the per-task limit is "
                f"{agent_tasks_mod.MAX_PROMPT_BYTES}",
                [{"field": "prompt", "reason": "too_large"}])

        max_tokens = body.get("max_tokens", 256)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or not (1 <= max_tokens <= 4096):
            raise errors.err(422, "INVALID_ARGUMENT", "max_tokens must be an integer in [1, 4096]",
                             [{"field": "max_tokens", "reason": "out_of_range"}])
        model_preference = body.get("model_preference") or body.get("model")
        if model_preference is not None and not isinstance(model_preference, str):
            raise errors.err(422, "INVALID_ARGUMENT", "model_preference must be a string",
                             [{"field": "model_preference", "reason": "not_a_string"}])
        actor = body.get("actor") or "human"
        if not isinstance(actor, str):
            raise errors.err(422, "INVALID_ARGUMENT", "actor must be a string",
                             [{"field": "actor", "reason": "not_a_string"}])

        resolution = agent_tasks_mod.resolve_agent_target(
            conn, self.server.cfg, project_id, vm_id)
        if resolution.node is None:
            if resolution.reason == "node_unreachable":
                raise errors.err(
                    503, "NODE_UNREACHABLE",
                    f"the node holding agent {vm_id} cannot be reached, so its state "
                    f"cannot be established",
                    [{"field": "vm_id",
                      "reason": "node_unreachable",
                      "unscanned_nodes": resolution.unscanned_nodes,
                      "attributed_nodes": resolution.telemetry_node_ids}])
            raise errors.err(
                404, "AGENT_NOT_FOUND",
                f"no agent {vm_id} on this project's nodes",
                [{"field": "vm_id",
                  "reason": "unknown",
                  "literal_name": vm_id,
                  "derived_agent_id": agent_tasks_mod.agent_id_for_name(vm_id),
                  "sources": resolution.sources,
                  "known_agent_ids": resolution.known_agent_ids[:20],
                  "unscanned_nodes": resolution.unscanned_nodes}])
        node = resolution.node
        instance_name = resolution.instance_name

        sample = store.latest_sample(conn, node["id"])
        node_status, _ = health.node_status_health(node, sample, time.time())
        if node_status == "offline":
            raise errors.err(503, "NODE_OFFLINE",
                             f"node {node['id']} is offline; agent {vm_id} cannot be reached",
                             [{"field": "vm_id", "reason": "node_offline"}])

        command = cmd_mod.issue_command(
            conn, self.server.operator_logs, project_id, "agent.dispatch_task",
            {"vm_id": vm_id, "prompt_bytes": size,
             "model_preference": model_preference, "max_tokens": max_tokens},
            "human", node_id=node["id"])

        task = agent_tasks_mod.insert_task(
            conn, project_id=project_id, vm_id=vm_id, node_id=node["id"],
            command_id=command["command_id"], actor=actor, prompt=prompt,
            model_preference=model_preference, max_tokens=int(max_tokens),
            timeout_s=agent_tasks_mod.DEFAULT_TASK_TIMEOUT_S)
        # Commit BEFORE dispatch: the record (and its audit command) must survive a
        # dispatch failure, and `_route` rolls the transaction back on any ApiError.
        conn.commit()

        try:
            submitted = agent_tasks_mod.submit_to_agent(
                self.server.cfg, node, task, instance_name=instance_name)
        except agent_tasks_mod.AgentTaskError as exc:
            agent_tasks_mod.fail_task(
                conn, project_id, task["task_id"], code=exc.code,
                message=str(exc), actionable=exc.actionable,
                logs=self.server.operator_logs)
            conn.commit()
            details = [{"field": exc.param or "vm_id",
                        "reason": exc.actionable or str(exc)}]
            headers = ({"Retry-After": str(exc.retry_after)}
                       if getattr(exc, "retry_after", None) else None)
            raise errors.err(exc.status, exc.code, str(exc), details, headers)

        state = submitted.get("state")
        if state not in (agent_tasks_mod.QUEUED, agent_tasks_mod.RUNNING):
            state = agent_tasks_mod.QUEUED
        agent_tasks_mod.update_task(
            conn, task["task_id"], state=state,
            agent_task_id=submitted.get("task_id"),
            started_at=submitted.get("started_at"))
        conn.commit()
        self.server.broker.publish(
            "task.queued",
            {"task_id": task["task_id"], "vm_id": vm_id, "project_id": project_id},
            project_id)
        threading.Thread(
            target=agent_tasks_mod.poll_until_terminal,
            args=(self.server.cfg, project_id, task["task_id"], vm_id, node["id"],
                  self.server.operator_logs),
            kwargs={"instance_name": instance_name},
            name=f"task-poll-{task['task_id'][-6:]}", daemon=True).start()
        return 202, agent_tasks_mod.get_task(conn, task["task_id"], project_id) or {}

    def _agent_task_get(self, conn, project_id: str, vm_id: str,
                        task_id: str) -> tuple[int, dict]:
        """GET /v1/projects/{pid}/agents/{vm_id}/tasks/{task_id} — state + answer."""
        self._require_project(conn, project_id)
        task = agent_tasks_mod.get_task(conn, task_id, project_id)
        if task is None or task["vm_id"] != vm_id:
            raise errors.not_found(f"no task {task_id} for agent {vm_id}")
        return 200, task

    def _agent_task_list(self, conn, project_id: str, vm_id: str) -> tuple[int, dict]:
        """GET /v1/projects/{pid}/agents/{vm_id}/tasks — the enumerable record.

        The answers are omitted here on purpose (a listing is not the read path for
        content); each row carries the states/errors for evidence, and the single-task
        GET returns the body.
        """
        self._require_project(conn, project_id)
        limit = self._query().get("limit", "50")
        try:
            limit_i = int(limit)
        except ValueError:
            raise errors.invalid_argument("limit must be an integer")
        tasks = agent_tasks_mod.list_tasks(conn, project_id, vm_id=vm_id, limit=limit_i)
        return 200, {"vm_id": vm_id, "tasks": tasks, "count": len(tasks)}

    def _agent_action(self, conn, project_id: str, vm_id: str,
                      action: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/agents/{vm_id}/actions/{start|stop|restart}.

        Contract: `controller-api.md` §4.11. The human-issued command, its state
        transitions and its operator-log line are the SAME shape a node
        Restart / Drain produces (`commands.issue_command`); `agent_actions`
        documents why the execution is synchronous. `vm_id` accepts either the
        LXD instance name from apply or the telemetry-derived id the client sees.
        """
        self._require_project(conn, project_id)
        self._read_body()  # no fields today; a body is accepted and ignored
        return agent_actions_mod.perform_action(
            conn, self.server.cfg, project_id, vm_id, action,
            self.server.operator_logs)

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

    # ---------------------------------------------------------------- assistant
    def _assistant_service(self) -> assistant_mod.AssistantService:
        svc = getattr(self.server, "assistant", None)
        if svc is None:
            svc = assistant_mod.AssistantService(
                self.server.cfg, self.server.project,
                logs=self.server.operator_logs)
            self.server.assistant = svc
        return svc

    def _assistant_ask(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/assistant/ask (controller-api.md §4.10)."""
        self._require_project(conn, project_id)
        body = self._read_body()

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise errors.invalid_argument(
                "messages must be a non-empty array of {role, content}",
                [{"field": "messages", "reason": "required"}])
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                raise errors.invalid_argument(
                    "each message must be an object",
                    [{"field": f"messages[{i}]", "reason": "not_an_object"}])
            role = m.get("role")
            if role not in assistant_mod.MESSAGE_ROLES:
                raise errors.invalid_argument(
                    f"messages[{i}].role must be one of "
                    + "|".join(assistant_mod.MESSAGE_ROLES),
                    [{"field": f"messages[{i}].role", "reason": "unknown_role"}])
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                raise errors.invalid_argument(
                    f"messages[{i}].content must be a non-empty string",
                    [{"field": f"messages[{i}].content", "reason": "required"}])
            if len(content) > assistant_mod.MESSAGE_CONTENT_MAX_CHARS:
                raise errors.invalid_argument(
                    f"messages[{i}].content exceeds "
                    f"{assistant_mod.MESSAGE_CONTENT_MAX_CHARS} chars",
                    [{"field": f"messages[{i}].content", "reason": "too_long"}])

        context = body.get("context")
        if context is not None and not isinstance(context, dict):
            raise errors.invalid_argument(
                "context must be an object when present",
                [{"field": "context", "reason": "not_an_object"}])
        kind = (context or {}).get("kind") or "none"
        ctx_id = (context or {}).get("id")

        # conversation_id / message_id anchor the idempotency key (D1). Optional
        # in the body; the Idempotency-Key header (§1.5) is the conversation
        # fallback, and a fresh ULID the message fallback.
        conversation_id = body.get("conversation_id") or self.headers.get("Idempotency-Key") or ulid.new_ulid()
        message_id = body.get("message_id") or ulid.new_ulid()
        for name, value in (("conversation_id", conversation_id), ("message_id", message_id)):
            if not isinstance(value, str) or not value.strip():
                raise errors.invalid_argument(
                    f"{name} must be a non-empty string",
                    [{"field": name, "reason": "invalid"}])

        service = self._assistant_service()
        return 200, service.ask(
            conn, messages, context if context else None,
            conversation_id.strip(), message_id.strip(),
        )

    # Headers a reverse proxy adds when it forwards a request. The public path to
    # the controller goes through the host's Caddy (:443 -> 127.0.0.1:8080), so a
    # REMOTE request still arrives with a loopback TCP peer — `client_address`
    # alone cannot tell it apart from the seeder's direct call. A direct caller
    # sends none of these; a proxied one carries at least X-Forwarded-For.
    def _is_loopback_client(self) -> bool:
        """True only for a DIRECT connection over the controller's own loopback.

        Provisioning surfaces (bootstrap-token minting) are restricted to the
        controller itself: the demo seeder runs beside the controller and talks to
        `127.0.0.1:8080`, so a remote holder of the project token cannot mint. A
        loopback TCP peer is necessary but NOT sufficient — the host's reverse proxy
        also connects from loopback — so a request that carries any proxy-forwarding
        header is treated as remote.
        """
        addr = self.client_address[0] if self.client_address else ""
        if addr not in ("127.0.0.1", "::1", "localhost"):
            return False
        for h in PROXY_REQUEST_HEADERS:
            if self.headers.get(h):
                return False
        return True

    # ------------------------------------------------------ seeder provisioning
    def _mint_bootstrap_tokens(self, conn) -> tuple[int, dict]:
        """POST /v1/bootstrap-tokens — mint fresh single-use bootstrap tokens.

        Why: the demo seeder (`console/seed_demo.py`, systemd unit
        `termini-seeder`) registers synthetic nodes through the same
        `POST /v1/nodes/register` endpoint real hardware uses, which consumes a
        single-use bootstrap token. A token is burned on first use
        (`store.burn_bootstrap_token`), so a seeder *restart* had nothing left to
        authenticate with: starting it needed hand-minted tokens written into the
        unit (and a drop-in that silently overrode them), and a bare
        `systemctl start termini-seeder` failed `401 bootstrap token already used`
        (or `unknown bootstrap token` when the drop-in disagreed with the unit).
        That made a recording a landmine.

        This endpoint makes the seeder self-provisioning: it mints N **unused**
        tokens on demand. They are ordinary `bootstrap_tokens` rows, so the
        single-use property is untouched — the registration path still burns the
        token, and a replayed token is still rejected
        (`_require_bootstrap` -> `401 bootstrap token already used`). Nothing here
        relaxes that check; a REAL node's replayed token must keep failing.

        Trust boundary: minting is loopback-only (see `_is_loopback_client`) and
        still requires the project token.
        """
        self._require_project(conn, self.server.project["id"])
        if not self._is_loopback_client():
            raise errors.permission_denied(
                "bootstrap-token minting is restricted to the controller loopback"
            )
        body = self._read_body()
        count = body.get("count", 2)
        if isinstance(count, bool) or not isinstance(count, int) or not (1 <= count <= 32):
            raise errors.invalid_argument("count must be an integer in [1, 32]")

        now = store.now_iso()
        tokens = [self.server.cfg.new_secret() for _ in range(count)]
        # Insert as UNUSED single-use rows — exactly the rows
        # `TERMINI_BOOTSTRAP_TOKEN` seeds at startup produce.
        store.seed_bootstrap_tokens(conn, tokens, now)

        logs = getattr(self.server, "operator_logs", None)
        if logs is not None:
            logs.append(
                conn, self.server.project["id"], severity="info", actor="human",
                message=f"minted {count} single-use bootstrap token(s) for the demo seeder",
                remediation={"count": count},
            )
        return 201, {
            "schema_version": "1.0",
            "count": count,
            "tokens": tokens,
            "created_at": now,
            "single_use": True,
        }

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
            # Bound the extraction on the USER's side of the request. Without
            # this the round-trip inherited the operator's minutes-long model
            # timeout, and a user request that arrived while the operator held
            # node-c's single model slot waited behind it until the client gave
            # up (HTTP 000, no status code at all). The extraction shares the
            # on-prem model with the operator by design; it must not share its
            # unbounded patience. See config.intent_model_timeout_s.
            result = extract_and_validate(
                body["prompt"], host_caps,
                config=_load_intent_model_config(
                    self.server.cfg.intent_model_timeout_s,
                    self.server.cfg.intent_inference_base_url or None,
                ),
            )
            _raise_if_extraction_failed(result)
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

        Running postcondition — a created instance is left STOPPED by LXD, so a
        successful create is followed by `start_instance` and the instance is only
        reported `ready` once the start operation confirms it is running. A create
        that succeeds but cannot be started is a FAILED allocation under the same
        all-or-nothing rule (its instances are deleted and its capacity released):
        "the agents are running" has to be true, not assumed.
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
        # Instances actually materialised, grouped by node. Recorded onto the
        # plan's commit after the loop so reconciliation can later tell "these
        # instances were created and are now gone" from "committed, never applied".
        instances_by_node: dict[str, list[str]] = {}
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
            start_timeout = float(getattr(
                cfg, "lxd_start_timeout_s",
                getattr(cfg, "lxd_create_timeout_s", 120.0)))
            created: list[str] = []          # instances materialised by THIS apply
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
                if r.result == "already_applied":
                    already += 1
                elif r.result != "ok":
                    failure = f"create {name} failed: {r.detail}"
                    break
                created.append(name)

                # LXD leaves a created instance STOPPED; materialisation promises
                # the agent is up, so start it and wait for the operation to
                # confirm. A start that does not reach "running" is a failure (the
                # name is already in `created`, so it is rolled back too).
                s = executor.start_instance(name, timeout_s=start_timeout)
                if s.result not in ("ok", "already_applied"):
                    failure = f"start {name} failed: {s.detail}"
                    break

            if failure is None:
                alloc["vm_ids"] = created           # record the real ids on the plan
                vm_ids.extend(created)
                if created and node_id:
                    instances_by_node.setdefault(node_id, []).extend(created)
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
                # force=True: an instance we already started is RUNNING, and LXD
                # refuses to delete a running instance — a plain DELETE would
                # silently leave it behind, contradicting the all-or-nothing rule.
                executor.delete_instance(name, force=True)
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

        # Record the instances this plan actually materialised on each node so
        # reconciliation can later release a commit whose VMs are gone (a commit
        # with no recorded instances is never reconciled — see
        # `store.set_allocation_instances` / `reconcile_allocations`).
        for node_id, names in instances_by_node.items():
            store.set_allocation_instances(conn, project_id, plan_id, node_id, names)

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

    def _release_plan(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/vms/release — release a plan's committed capacity.

        The inverse of the §5.2 step 7 commit: it returns to the node's pool the
        capacity a plan reserved, so a later plan can use it (the demo's headline
        prompt is exactly the request that a saturated node refuses). Idempotent —
        a repeat release finds no live commitment and reports `already_released`,
        never a double decrement — while a plan that never committed is a clean
        `404` (as is a malformed `plan_id` a `422`), never a crash. The commit row
        is soft-released, so the audit trail survives (this is why release, not a
        DB wipe, is the recovery path).

        Release also TEARS DOWN what the plan materialised. Returning capacity
        while leaving the containers RUNNING is a silent leak: the accounting says
        the node is free, the node is not, and nothing in `GET /nodes` can show
        it (found live on node-a, 2026-10-17). The teardown keys off exactly the
        names `vm_allocations.vm_ids_json` recorded for this plan on each node —
        never a pattern match — and is best-effort: per-instance failures are
        reported (`failed[]`) and never abort the capacity release or the others.
        """
        self._require_project(conn, project_id)
        body = self._read_body()
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise errors.invalid_argument("plan_id is required")
        plan_id = plan_id.strip()
        if not ulid.is_valid_ulid(plan_id):
            raise errors.invalid_argument("plan_id must be a ULID")
        reason = body.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
            raise errors.invalid_argument("reason must be a string of at most 200 chars")

        # Read the LIVE rows BEFORE the soft-release: after it, `released_at` is
        # set and the record of what to tear down is only historical. A repeat
        # release therefore reads nothing here and deletes nothing a second time.
        pending = store.live_allocation_rows(conn, project_id, plan_id=plan_id)
        result = store.release_allocations(
            conn, project_id, plan_id=plan_id, reason=reason or "released_by_user")
        if not result["had_commit"]:
            raise errors.not_found(f"no committed capacity for plan {plan_id}")

        teardown = {"deleted": [], "failed": [], "unknown_instances": False}
        if result["rows_released"]:
            teardown = self._teardown_plan_instances(conn, pending)

        logs = getattr(self.server, "operator_logs", None)
        if logs is not None and result["rows_released"]:
            r = result["released"]
            message = (f"released plan {plan_id}: {r['ram_allocated_gb']} GiB RAM, "
                       f"{r['cpu_allocated']} vCPU returned to the pool")
            if teardown["deleted"]:
                message += "; tore down " + ", ".join(teardown["deleted"])
            if not teardown["deleted"] and not teardown["unknown_instances"]:
                message += "; no materialised instance recorded"
            if teardown["failed"]:
                message += (f"; {len(teardown['failed'])} instance(s) could NOT be "
                            f"deleted: {', '.join(f['name'] for f in teardown['failed'])}")
            if teardown["unknown_instances"]:
                message += ("; vm_ids unknown (apply never recorded the instances) —"
                            " check the node for leftovers")
            logs.append(
                conn, project_id,
                severity="warn" if (teardown["failed"] or teardown["unknown_instances"]) else "info",
                actor="human", message=message,
                remediation={"plan_id": plan_id, "released": r,
                             "reason": reason or "released_by_user",
                             "teardown": teardown},
            )
        return 200, {
            "plan_id": plan_id,
            "status": "released" if result["rows_released"] else "already_released",
            "released": result["released"],
            "rows_released": result["rows_released"],
            "released_at": result["released_at"],
            "deleted": teardown["deleted"],
            "failed": teardown["failed"],
            "unknown_instances": teardown["unknown_instances"],
        }

    def _teardown_plan_instances(self, conn, rows: list[dict]) -> dict:
        """Delete the LXD instances a just-released plan recorded, best-effort.

        `rows` are the plan's LIVE commits as read before the release; each names
        the node the instances live on and the exact instance names recorded by
        `_apply_plan` (`vm_ids_json`). Deletion is per instance through the
        node's own LXD endpoint with `force=True` — a RUNNING instance cannot be
        deleted otherwise, and a released plan's agents are typically still up.

        Rules this follows, each of them learned from the leak it repairs:

        * **Never guess a name.** Only recorded names are deleted. A name derived
          from the plan id would delete an instance this controller did not create
          (and `termini-<plan>-*` is a shape, not a proof of ownership).
        * **Never abort the release.** Capacity is already back in the pool when
          this runs; a container that refuses to die is collected in `failed[]`
          with its LXD error and the remaining names are still attempted.
        * **Never hide an unknown.** `vm_ids_json IS NULL` means apply was
          interrupted before it recorded what it created, so the names cannot be
          known: report `unknown_instances: true` rather than silently leaving a
          container that no accounting can see.
        * `already_applied` from the executor is LXD's 404 (the instance is
          already gone) — the postcondition holds, so it counts as deleted.
        """
        deleted: list[str] = []
        failed: list[dict] = []
        unknown = False
        cfg = self.server.cfg
        for row in rows:
            if not row.get("vm_ids_recorded"):
                unknown = True
                continue
            names = row.get("vm_ids") or []
            if not names:
                continue
            node = store.get_node(conn, row["node_id"])
            if node is None:
                for name in names:
                    failed.append({"name": name, "node_id": row["node_id"],
                                   "error": "node not found in this project; cannot delete"})
                continue
            try:
                executor = op_target.executor_for_node(cfg, node)
            except Exception as exc:  # noqa: BLE001 - teardown must not abort the release
                for name in names:
                    failed.append({"name": name, "node_id": row["node_id"],
                                   "error": f"{type(exc).__name__}: {exc}"})
                continue
            for name in names:
                try:
                    r = executor.delete_instance(name, force=True)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"name": name, "node_id": row["node_id"],
                                   "error": f"{type(exc).__name__}: {exc}"})
                    continue
                if r.result in ("ok", "already_applied"):
                    deleted.append(name)
                else:
                    failed.append({"name": name, "node_id": row["node_id"],
                                   "error": r.detail or r.result})
        return {"deleted": deleted, "failed": failed, "unknown_instances": unknown}

    def _decommission_node(self, conn, project_id: str, node_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/nodes/{node_id}/decommission — retire a node.

        Retires a node from the fleet: it is dropped from `GET /nodes` (so the
        dashboard cannot show phantom OFFLINE entries that contradict the deck)
        and its live committed capacity is released so a dead node does not keep a
        share of some pool. Idempotent — a repeat call reports
        `already_decommissioned`; an unknown node is a clean `404`. The node row,
        its telemetry and its audit history survive (soft retire).
        """
        self._require_project(conn, project_id)
        body = self._read_body()
        reason = body.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
            raise errors.invalid_argument("reason must be a string of at most 200 chars")

        result = store.decommission_node(conn, project_id, node_id, reason=reason or "retired")
        if result is None:
            raise errors.not_found(f"node {node_id} not found")

        logs = getattr(self.server, "operator_logs", None)
        if logs is not None and result["status"] == "decommissioned":
            logs.append(
                conn, project_id, severity="info", actor="human", node_id=node_id,
                message=(f"decommissioned node {node_id}: released "
                         f"{result['released']['ram_allocated_gb']} GiB RAM, "
                         f"{result['released']['cpu_allocated']} vCPU"),
                remediation={"node_id": node_id, "released": result["released"],
                             "reason": reason or "retired"},
            )
        return 200, result

    def _reconcile(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/vms/reconcile — release capacity for gone VMs.

        On-demand form of the self-heal the controller also runs once at startup:
        release every live commit whose materialised instances no longer exist on
        its node.
        """
        self._require_project(conn, project_id)
        return 200, reconcile_allocations(conn, self.server.cfg, project_id)

    def _host_caps(self, conn, default_caps: dict) -> dict:
        """Per-role host maxima for the clamp validator, from REGISTERED nodes.

        Real nodes are authoritative: when any real node is registered the maxima
        are derived from those alone, so a seeder-fabricated row's inflated facts
        (its declared 64 GiB) can never widen the coarse ceiling a real node's
        request has to fit. Fabricated rows are used only in a seeder-only demo,
        mirroring `_resolve_target`'s fallback.

        The merge is a MAX with the frozen default rather than a last-writer-wins
        assignment: it is order-independent (the old loop let whichever node
        happened to be registered last decide), and the pre-filter never tightens
        below the frozen demo default — the authoritative ceiling for a real
        allocation is the resolved node's own capacity clamp (§5.2 step 6), which
        reads that node's real hardware.
        """
        nodes = _selection_pool(store.list_nodes(conn, self.server.project["id"]))
        caps = {
            "agent_vm": dict(default_caps["agent_vm"]),
            "inference_vm": dict(default_caps["inference_vm"]),
        }
        for n in nodes:
            hw = json.loads(n["hardware_json"]) if n["hardware_json"] else {}
            cap = health.capacity_of(hw)
            if n["role"] == "inference":
                if cap["ram_gb"]:
                    caps["inference_vm"]["ram_gb"] = max(
                        caps["inference_vm"]["ram_gb"], max(0, cap["ram_gb"] - 2))
            else:
                if cap["cpu_cores"]:
                    caps["agent_vm"]["vcpus"] = max(
                        caps["agent_vm"]["vcpus"], max(1, cap["cpu_cores"] * 2))
                if cap["ram_gb"]:
                    caps["agent_vm"]["ram_gb"] = max(
                        caps["agent_vm"]["ram_gb"], max(1, cap["ram_gb"] - 2))
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
            # §5.2 step 3: an `inference_vm` may resolve to the node that serves
            # the agent-facing inference front door even though it reports
            # `compute` (no local GPU on this fleet). Resolution and the role
            # guardrail must agree on that, so compute it once here.
            service_host = _is_inference_service_host(self, resolved_node)
            role_ok, role_reason = _role_check(spec, resolved_node, service_host=service_host)
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
                    "detail": _role_reject_detail(role_reason, resolved_node, self),
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
                        # Name the node and the numbers, not just the rule. A bare
                        # "aggregate RAM exceeds the node's free capacity" tells a
                        # user nothing about which node refused or by how much, so
                        # the natural-language path looked broken (Defect B) when
                        # the guardrail was in fact correctly refusing a request
                        # that could not fit the compute node it resolved to.
                        "detail": _capacity_reject_detail(
                            cap_reject, node_id, cap, resolved,
                            spec.count, spec.vcpus, spec.ram_gb, spec.disk_gb,
                        ),
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
        """Resolve an allocation's target node (controller-api.md §5.2 steps 3/5).

        Placement provenance rule: a SEEDER-FABRICATED node is never preferred over
        a real registered one, and is never chosen at all while a real node is
        registered — for `auto` *or* for an explicit `target_node`. The seeder
        registers through the same endpoint as hardware, so before this rule the
        only signal was reported capacity, which the seeder inflates: `auto` put
        agents on a node that does not exist.
        """
        if not nodes:
            return None
        target = spec.target_node
        if target and target != "auto":
            for n in nodes:
                if n["id"] == target:
                    # Naming a fabricated node is still a placement, so the same
                    # rule applies. Returning None lets the caller surface the
                    # frozen `invalid_target_node[_type]` rejection rather than
                    # silently redirecting the allocation to a different node.
                    if n.get("synthetic") and _real_nodes(nodes):
                        return None
                    return n
            return None
        candidates = _selection_pool(nodes)
        if spec.type == "inference_vm":
            # Reference-class resolution (controller-api.md §5.2 step 3), two steps:
            #
            # 1. (unchanged, frozen §4.1) a node the role rule assigned `inference`
            #    because it reports a >= 12 GiB GPU. Kept first so a real GPU node
            #    still wins if the fleet ever has one again.
            # 2. the node that serves the agent-facing inference FRONT DOOR — the
            #    address the controller itself dials (`TERMINI_INFERENCE_BASE_URL`,
            #    e.g. node-c 10.10.0.13:8080, `backend=gemini-proxy` after the
            #    CPU/proxy pivot). That node reports `compute` (there is no local
            #    GPU — the L4 quota is blocked, t_294d2159), yet it is exactly the
            #    host the reference class means: a host SERVICE, no VM, no GPU.
            #    Without this step EVERY `inference_vm` is rejected on a CPU-only
            #    fleet, so the demo's headline prompt is a guaranteed 422
            #    (t_d7e0bf4e, infra-decisions.md D9 amendment).
            #
            # No match at all -> None -> REJECT upstream (`invalid_target_node_type`).
            for n in candidates:
                if n["role"] == "inference":
                    return n
            return _inference_service_host(self, candidates)

        # auto (agent_vm): the node that serves the always-on model is a SERVICE
        # host, not an agent target (see `agent_placement_excluded_hosts`) — the
        # demo's CPU model host registers `compute`, so without this it wins the
        # max-free-RAM comparison against the real compute node and every agent
        # lands on the machine that is busy running the fleet's model. The
        # exclusion is dropped when it would leave no candidate at all, so a
        # single-node or seeder-only demo stays placeable.
        excluded = _placement_excluded_hosts(self)
        if excluded:
            allowed = [n for n in candidates if node_address_host(n) not in excluded]
            if allowed:
                candidates = allowed
        # auto (agent_vm): node with the most free allocatable RAM (tie-break: lowest node_id)
        best = None
        best_ram = -1
        for n in candidates:
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

        # A running demo fault injection owns this node's series (controller-api.md
        # §4.7): the injected samples are authoritative, so the node's own samples
        # are recorded as received but not stored. Without this, a real node's
        # healthy samples would interleave with the injected fault and the rule
        # would flap instead of holding across its debounce count.
        fault = demo_faults_mod.open_fault_for_node(conn, node["project_id"], node_id)
        if fault is not None:
            store.insert_batch(conn, clean, node_id, 0, len(samples), now)
            conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node_id))
            injector = getattr(self.server, "fault_injector", None)
            if injector is not None:
                injector.supersede_log(conn, node, fault)
            self._publish_node_events(conn, node, store.latest_sample(conn, node_id))
            return 202, {"accepted": 0, "rejected": len(samples), "backpressure": "none"}

        inserted, dropped = store.insert_samples(
            conn, node_id, clean["call_id"], samples, source=store.NODE_SOURCE
        )
        store.insert_batch(conn, clean, node_id, inserted, dropped, now)
        conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node_id))

        latest = store.latest_sample(conn, node_id)
        self._publish_node_events(conn, node, latest)

        return 202, {"accepted": inserted, "rejected": dropped, "backpressure": "none"}

    def _publish_node_events(self, conn, node: dict, sample: dict | None) -> None:
        """Push node.telemetry (≤1/s) and node.health_changed (on transition).

        Delegates to the server so the demo fault driver fans an injected sample
        out through the identical path (see ControllerServer.publish_node_events).
        """
        self.server.publish_node_events(conn, node, sample)

    def _node_recent(self, conn, node_id: str) -> tuple[int, dict]:
        self._require_node(conn, node_id)
        limit = self._limit_param()
        return 200, {"node_id": node_id, "samples": store.recent_samples(conn, node_id, limit)}

    def _node_lxd_trust(self, conn, node_id: str) -> tuple[int, dict]:
        """POST /v1/nodes/{nid}/lxd-trust — the node hands over a one-time
        certificate-add trust token so the controller's CLIENT certificate lands
        in that node's LXD trust store.

        Why this route exists (measured 2026-10-17): LXD authorises per client
        certificate, and trust was installed only at CONTROLLER STARTUP for a
        static list (`TERMINI_LXD_TRUST_NODES` + a shared token). A node that
        enrolled afterwards was authenticated TO the controller and streaming
        telemetry, while the controller was not authorised BY the node: the
        operator's first remediation on a node booted from the pre-baked image
        answered `HTTP 403 Forbidden`, and because the vm_id -> instance-name
        resolution needs `GET /1.0/instances`, the model's choice could not even
        be resolved to a real name. Day-0 has to leave a node the operator can
        ACT on, so the token is pushed by the node at registration instead of
        hoping an operator restart sweeps it in.

        Trust boundary: node token, own node only (`_require_node`); the endpoint
        must match the host:port the node reported, so a compromised node cannot
        point the controller's trust installation at somebody else's LXD; the
        token itself is single-use and is consumed by LXD at the node.
        """
        node = self._require_node(conn, node_id)
        body = self._read_body()
        endpoint = registration.endpoint_from_heartbeat(body)
        if not endpoint:
            raise errors.invalid_argument(
                "endpoint is required (send `lxd.endpoint` or `lxd_endpoint`)",
                [{"field": "lxd_endpoint", "reason": "missing"}],
            )
        token = body.get("trust_token") or body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise errors.invalid_argument(
                "trust_token is required (the node's one-time LXD cert-add token)",
                [{"field": "trust_token", "reason": "missing"}],
            )
        # The endpoint must be the node's own address. A node that reports a
        # different host:port here than the one it registered with is refused:
        # otherwise "install my trust token" becomes "install it over there".
        reported = node.get("lxd_endpoint")
        if reported and _endpoint_key(reported) != _endpoint_key(endpoint):
            raise errors.permission_denied(
                f"endpoint {endpoint} does not match the registered endpoint {reported}",
            )

        cfg = self.server.cfg
        identity = lxdtrust.resolve_identity(
            explicit_cert=cfg.lxd_client_cert,
            explicit_key=cfg.lxd_client_key,
            client_dir=cfg.lxd_client_dir,
            common_name=cfg.lxd_trust_name,
            provision=False,
        )
        if identity is None or not identity.available:
            raise errors.err(
                503, "UNAVAILABLE",
                "the controller has no LXD client identity on disk; trust cannot be installed",
            )
        result = lxdtrust.ensure_trusted(
            endpoint, identity, token.strip(),
            name=cfg.lxd_trust_name, server_ca=cfg.lxd_server_ca,
            timeout_s=cfg.lxd_request_timeout_s,
        )
        logs = getattr(self.server, "operator_logs", None)
        if logs is not None:
            logs.append(
                conn, self.server.project["id"],
                severity="info" if result.ok else "warning",
                actor="node",
                message=(f"LXD trust for {node['registration_id']} at {endpoint}: "
                         f"{result.status} — {result.detail}"),
                remediation={"endpoint": endpoint, "trusted": bool(result.ok),
                             "status": result.status},
            )
        log.info("node %s LXD trust at %s: %s — %s",
                 node_id, endpoint, result.status, result.detail)
        return 200, {
            "node_id": node_id,
            "endpoint": endpoint,
            "trusted": bool(result.ok),
            "status": result.status,
            "detail": result.detail,
        }

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

    # ------------------------------------------------- operator fault console (§4.7)
    def _fault_injector(self):
        injector = getattr(self.server, "fault_injector", None)
        if injector is None:
            raise errors.err(503, "INTERNAL",
                             "demo fault injection is not enabled on this controller")
        return injector

    def _demo_fault_start(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/demo/faults — start one injection.

        Operator-only: `project_token` auth, scoped to its own project. A request
        with no token is `401 UNAUTHENTICATED`; a `node_token` (or any other
        credential) is `403 PERMISSION_DENIED` — the route is not reachable from a
        node context, and nothing here is public (controller-api.md §4.7).
        """
        self._require_project(conn, project_id)
        body = self._read_body()
        return 201, self._fault_injector().start_injection(
            conn, project_id, body, actor=self._actor())

    def _demo_fault_list(self, conn, project_id: str) -> tuple[int, dict]:
        """GET /v1/projects/{pid}/demo/faults — active + recent injections."""
        self._require_project(conn, project_id)
        injector = self._fault_injector()
        q = self._query()
        state = (q.get("state") or "all").strip().lower()
        if state not in ("all", "open"):
            raise errors.invalid_argument("state must be 'all' or 'open'")
        faults = [
            injector.view(row, store.get_node(conn, row["node_id"]),
                          thresholds=injector.resolve_thresholds(conn))
            for row in demo_faults_mod.list_faults(conn, project_id, limit=self._limit_param(50))
        ]
        if state == "open":
            faults = [f for f in faults if f["open"]]
        return 200, {
            "project_id": project_id,
            "scenarios": list(demo_faults_mod.SCENARIO_NAMES),
            "faults": faults,
            "open_count": sum(1 for f in faults if f["open"]),
        }

    def _demo_fault_clear(self, conn, project_id: str, injection_id: str) -> tuple[int, dict]:
        """DELETE /v1/projects/{pid}/demo/faults/{injection_id} — clear one injection.

        Idempotent and never refused on state: a fault must always be clearable
        (§4.7 safety rule 2). Clearing starts the recovery burst — the driver keeps
        writing the healthy baseline for as many samples as the rule's hysteresis
        needs to re-arm — so the caller sees `state: recovering` and the next
        `GET` reports `cleared`.
        """
        self._require_project(conn, project_id)
        reason = (self._query().get("reason") or "cleared_by_operator")[:200]
        return 200, self._fault_injector().clear(
            conn, project_id, injection_id, reason=reason, actor=self._actor())

    def _demo_fault_clear_all(self, conn, project_id: str) -> tuple[int, dict]:
        """POST /v1/projects/{pid}/demo/faults/clear — clear every open injection.

        Body `{"node_id": "<id>", "reason": "<text>"}` (both optional). This is the
        clear path for environments whose reverse proxy drops DELETE; both are in
        the contract and both go through the same recovery burst.
        """
        self._require_project(conn, project_id)
        body = self._read_body()
        node_id = body.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise errors.invalid_argument("node_id must be a string when present")
        reason = str(body.get("reason") or "cleared_by_operator")[:200]
        return 200, self._fault_injector().clear_all(
            conn, project_id, node_id=node_id, reason=reason, actor=self._actor()
        )

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
            # Provenance: True for a row fabricated by the demo seeder. Surfaced so
            # the dashboard can label it and an operator can tell a real node from a
            # fabricated one (the placement rule already ignores it for `auto`).
            "synthetic": bool(n.get("synthetic")),
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
        # Subscribe BEFORE completing the handshake. The broker is not a replay
        # buffer: an event published by another request thread in the gap between
        # the 101 response and broker.subscribe() is dropped, so a client that
        # fires an action the instant it sees the 101 (as the dashboard log-stream
        # test does) can miss the resulting frames and time out. Subscribing first
        # makes the 101 a synchronisation point: by the time the client can act,
        # the subscription is already active.
        sub = self.server.broker.subscribe()
        ws = self._ws_handshake()
        if ws is None:
            self.server.broker.unsubscribe(sub)
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
            # The browser SSE client authenticates with `Authorization: Bearer`
            # (a credential in the URL is forbidden by t_5649d117); `?token=` is
            # kept only as a fallback for the documented curl/CLI clients.
            self._require_project_token(self._bearer() or q.get("token"))
            if project_id != self.server.project["id"]:
                raise errors.permission_denied("project_id does not match token scope")
        except errors.ApiError as e:
            self._send_error(e)
            return

        # Subscribe before flushing the SSE headers so the header flush (the point
        # at which the client can fire an action) implies the subscription is live.
        # Same dropped-event race as the WS stream: the broker has no replay, and
        # test_log_stream_delivers_fresh_event_over_sse fires a reboot the moment
        # its reader sees the headers.
        sub = self.server.broker.subscribe()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            self.server.broker.unsubscribe(sub)
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

def reconcile_allocations(conn, cfg, project_id: str, *, executor_factory=None) -> dict:
    """Release committed capacity whose materialised instances are gone (self-heal).

    The success path of `_apply_plan` records the LXD instance names it created on
    the plan's commit (`store.set_allocation_instances`). For every LIVE commit
    that recorded instances, ask the node's LXD whether any of them still exist and
    release the commit when none do. This is what stops capacity accumulating
    permanently: a node that rebooted, an operator who deleted the instances, or a
    restarted demo no longer leaves a plan claiming VMs that are gone — so the
    headline intent cannot be permanently 422'd by stale state.

    A commit with no recorded instances (committed but never applied) is left
    alone: it has materialised nothing, so no absence can be proven. A node that
    cannot be reached is also SKIPPED, never released — absence must be *proven*,
    not assumed (an unreachable node is not an empty node).

    Returns `{"nodes_checked", "released": [...], "released_count"}`.
    """
    factory = executor_factory or (lambda c, node: op_target.executor_for_node(c, node))
    nodes_checked = 0
    released: list[dict] = []
    for row in store.live_allocations(conn, project_id):
        vm_ids = row.get("vm_ids") or []
        if not vm_ids:
            continue  # committed but never materialised -> not ours to release
        node = store.get_node(conn, row["node_id"])
        if node is None or node.get("decommissioned_at"):
            store.release_allocations(
                conn, project_id, plan_id=row["plan_id"], node_id=row["node_id"],
                reason="node_gone")
            released.append({"plan_id": row["plan_id"], "node_id": row["node_id"],
                             "reason": "node_gone", "vm_ids": vm_ids})
            continue
        nodes_checked += 1
        ok, live_names = factory(cfg, node).list_instance_names()
        if not ok:
            continue
        if not (set(vm_ids) & set(live_names)):
            store.release_allocations(
                conn, project_id, plan_id=row["plan_id"], node_id=row["node_id"],
                reason="instances_gone")
            released.append({"plan_id": row["plan_id"], "node_id": row["node_id"],
                             "reason": "instances_gone", "vm_ids": vm_ids})
    return {"nodes_checked": nodes_checked, "released": released,
            "released_count": len(released)}


def release_orphaned_reservations(conn, project_id: str, *, live_plan_ids) -> dict:
    """Release never-applied reservations this process can never materialise.

    `reconcile_allocations` deliberately leaves a commit with no recorded
    instances alone: it materialised nothing, so no absence can be proven from
    the node. That is right for a *live* plan, but it leaves a hole with no
    recovery route: `POST /intents` commits capacity at plan-acceptance time
    (controller-api.md §5.2 step 7), the accepted plan lives only in the
    controller's in-memory `plans` dict, and a controller restart drops it. The
    client's later `POST /vms/apply` then 404s (`plan … not found`) and the
    reservation is permanent — the node shows `ram_free_gb` it can never actually
    hand out, and the next request that needs those GiB is rejected
    `exceeds_node_ram_ceiling` forever. Live example: node-a carried a
    never-applied 2 vCPU / 2 GiB / 32 GiB reservation from a previous session, so
    its free RAM was 1 GiB and *any* two-agent deploy — even at the minimum 1 GiB
    each — was refused (Defect B).

    The proof here is different from reconcile's, and stronger: the plan_id is
    simply not in the set of plans this process holds, and this process is the
    only thing that can apply a plan (the set is in-memory, never persisted), so
    the reservation cannot become an instance. Called once at startup, when the
    set is necessarily empty, that releases exactly the reservations orphaned by
    a restart — never one whose plan this process is still holding.

    Returns `{"released": [...], "released_count": n}`.
    """
    live = set(live_plan_ids or ())
    released: list[dict] = []
    for row in store.live_allocations(conn, project_id):
        if row.get("vm_ids"):
            continue  # materialised: reconcile owns it, absence is provable there
        if row["plan_id"] in live:
            continue  # this process still holds the plan; it may yet be applied
        store.release_allocations(
            conn, project_id, plan_id=row["plan_id"], node_id=row["node_id"],
            reason="plan_orphaned_restart")
        released.append({
            "plan_id": row["plan_id"], "node_id": row["node_id"],
            "reason": "plan_orphaned_restart",
            "released": {"cpu_allocated": row["cpu_allocated"],
                         "ram_allocated_gb": row["ram_allocated_gb"],
                         "disk_allocated_gb": row["disk_allocated_gb"]},
        })
    return {"released": released, "released_count": len(released)}


def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# ------------------------------------------------------------------ node provenance

def _real_nodes(nodes: list[dict]) -> list[dict]:
    """Nodes registered by real hardware (not seeder-fabricated).

    `synthetic` is set on the registration wire by the demo seeder
    (`console/seed_demo.py`); real hardware never sends it, and every
    pre-existing row defaults to real (migration 0011). `nodes` may also be a
    plain list of dicts that predate the column (tests, older callers), hence
    `.get()`.
    """
    return [n for n in nodes if not n.get("synthetic")]


def _selection_pool(nodes: list[dict]) -> list[dict]:
    """Candidate nodes for *auto* placement and for host-capacity derivation.

    Real registered nodes ALWAYS win: a seeder-fabricated row is a candidate only
    when NO real node is registered, which is the seeder-only demo (otherwise its
    dashboard would go empty). This is the rule that stops `auto` from resolving to
    a node that does not exist: before it, resolution was a pure capacity
    comparison and the seeder's inflated facts (64 GiB vs a real node's real
    1-8 GiB) won every time.
    """
    real = _real_nodes(nodes)
    return real or list(nodes)


def _endpoint_host(value: object) -> str:
    """Bare, lowercased host from a node address (`host:port` or `scheme://...`).

    Returns "" for anything that is not a usable address, so a node that reported
    no endpoint simply never matches an exclusion rather than being silently
    assigned another node's identity.
    """
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if raw.startswith("["):                     # bracketed IPv6 literal
        return raw.split("]", 1)[0].lstrip("[").lower()
    host = raw.rsplit(":", 1)[0] if ":" in raw else raw
    return host.strip().lower()


def node_address_host(node: dict) -> str:
    """Host a node is reachable at for LXD: its own endpoint, else its source IP."""
    return _endpoint_host(node.get("lxd_endpoint")) or _endpoint_host(node.get("source_ip"))


def inference_service_hosts(cfg) -> set[str]:
    """Hosts that serve the agent-facing inference FRONT DOOR (controller-api.md §5.2 step 3).

    The reference class `inference_vm` does not describe a VM that would be
    launched — it describes the always-on host SERVICE that answers agent traffic
    at the frozen agent address. The controller already knows that address
    (`TERMINI_INFERENCE_BASE_URL`, e.g. `http://10.10.0.13:8080`), which is the
    authoritative statement of *which host* serves it, and the operator can name
    further hosts explicitly (`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`, the same
    list §5.2 step 5 uses to keep agents off the model host).

    This is deliberately NOT a role rule: nothing in the frozen role vocabulary
    (GPU-gated, §4.1) says a CPU-only model host is special, which is why a
    CPU-only fleet had no resolvable `inference_vm` at all.
    """
    hosts: set[str] = set()
    if cfg is None:
        return hosts
    h = _endpoint_host(getattr(cfg, "inference_base_url", ""))
    if h:
        hosts.add(h)
    for extra in (getattr(cfg, "agent_placement_exclude_hosts", ()) or ()):
        eh = _endpoint_host(extra)
        if eh:
            hosts.add(eh)
    return hosts


def agent_placement_excluded_hosts(cfg) -> set[str]:
    """Hosts that must not receive *auto* agent placement (controller-api.md §5.2 step 5).

    The node that serves the always-on decision model is a *service* host — it is
    running llama.cpp for the whole fleet, so it is not an agent compute target.
    Nothing in the frozen role rule expresses that: role is GPU-gated
    (`node-bootstrap.md` §5.3 / controller-api.md §4.1), so a CPU-only model host
    like the demo's node C registers `compute` and would otherwise beat the real
    compute node on free RAM. The exclusion is therefore stated in terms of the
    model address the controller already knows (`TERMINI_INFERENCE_BASE_URL`),
    plus any operator-named hosts (`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`).

    It is exactly the front-door host set (`inference_service_hosts`): the machine
    agents must not be *placed on* is the machine the inference reference *is*.
    """
    return inference_service_hosts(cfg)


def _placement_excluded_hosts(handler) -> set[str]:
    """Excluded placement hosts for a request handler (tolerates `self=None`)."""
    cfg = getattr(getattr(handler, "server", None), "cfg", None)
    return agent_placement_excluded_hosts(cfg)


def _inference_front_door_hosts(handler) -> set[str]:
    """Front-door host set for a request handler (tolerates `self=None`)."""
    cfg = getattr(getattr(handler, "server", None), "cfg", None)
    return inference_service_hosts(cfg)


def _inference_service_host(handler, nodes: list[dict]) -> dict | None:
    """The real node that serves the agent-facing inference front door, or None.

    Never a seeder-fabricated row: the reference class names the fleet's REAL
    model host, and the provenance rule (§5.2 step 5) applies to it too — a
    synthetic row must not be resurrected as an inference node
    (t_d7e0bf4e). Deterministic: lowest node_id among matching hosts.
    """
    hosts = _inference_front_door_hosts(handler)
    if not hosts:
        return None
    matches = [
        n for n in nodes
        if not n.get("synthetic") and node_address_host(n) in hosts
    ]
    return min(matches, key=lambda n: n["id"]) if matches else None


def _is_inference_service_host(handler, node) -> bool:
    """True when `node` is the real host of the agent-facing inference service."""
    if node is None or node.get("synthetic"):
        return False
    if not node_address_host(node):
        return False
    return node_address_host(node) in _inference_front_door_hosts(handler)


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


def _load_intent_model_config(timeout_s: float, base_url: str | None = None):
    """The extractor's model config, with the INTENT path's own bound and endpoint.

    The on-prem model config is read from the frozen `TERMINI_INFERENCE_*` env
    (termini/intent/config.py). Both overrides exist so a user-facing synchronous
    extraction cannot be starved by the operator's minutes-long decision:

    * ``timeout_s`` — `TERMINI_INFERENCE_TIMEOUT_S` is shared with the operator's
      decision adapter and is set to minutes on a CPU-only fleet; the intent path
      must not inherit that patience.
    * ``base_url`` — `TERMINI_INTENT_INFERENCE_BASE_URL`, the deployment's
      dedicated extraction endpoint. Even on a multi-slot model server the
      operator's ~12k-token decision prompt fills the llama.cpp batch, so a small
      concurrent request makes no progress until that prefill ends; a separate
      instance gives the extraction its own threads. Empty/None falls back to the
      general endpoint.

    Returns ``None`` when the package is unavailable, in which case the extractor
    falls back to its own defaults (existing behaviour).
    """
    try:
        import dataclasses

        from termini.intent.config import ModelConfig
    except ImportError:
        _add_repo_root()
        try:
            import dataclasses

            from termini.intent.config import ModelConfig
        except ImportError:
            return None
    overrides: dict = {"timeout_s": float(timeout_s)}
    if base_url:
        overrides["base_url"] = base_url
    try:
        return dataclasses.replace(ModelConfig.from_env(), **overrides)
    except Exception:  # noqa: BLE001 — a bad env value must not break extraction
        log.exception("could not build the intent model config; using defaults")
        return None


def _raise_if_extraction_failed(result) -> None:
    """Surface a model-side extraction failure as a clear, retryable 503.

    A guardrail rejection (capacity / role) is a real verdict on a real spec and
    stays `422 GUARDRAIL_REJECTED` with its reason. An *extraction* failure is not
    a verdict at all — the model produced no spec (timeout, connection error,
    unparseable JSON). Reporting that behind `422 GUARDRAIL_REJECTED` hides a
    model/backpressure problem behind a guardrail verdict, which is exactly how
    the seeder-starves-the-controller defect read to a user. Raise it as an
    explicit `503 INTERNAL` with the `extraction_failed` detail instead, so a
    caller can tell "your request was refused" from "the model did not answer".
    """
    try:
        from termini.intent.types import REASON_EXTRACTION_FAILED
    except ImportError:
        _add_repo_root()
        from termini.intent.types import REASON_EXTRACTION_FAILED

    if getattr(result, "outcome", None) != "rejected":
        return
    failures = [r for r in getattr(result, "rejections", []) if r.reason == REASON_EXTRACTION_FAILED]
    if not failures:
        return
    raise errors.err(
        503, "INTERNAL",
        "intent extraction failed: the on-prem model did not return a usable "
        "allocation spec in time",
        [r.to_dict() for r in failures],
    )


def _add_repo_root() -> None:
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)


def _role_check(spec, resolved_node, service_host: bool = False) -> tuple[bool, str | None]:
    """Frozen role guardrails (controller-api.md §5.2 steps 3/5, architecture.md R16)."""
    if spec.type == "inference_vm":
        # R16: inference_vm is a REFERENCE to the always-on host inference
        # service, not a launchable VM. No GPU passthrough requirement (it is a
        # structural rejection upstream, not here). It must resolve either to a
        # node the frozen §4.1 rule assigned `inference` (>= 12 GiB GPU) or to the
        # node that serves the agent-facing inference front door (`service_host`,
        # resolved from TERMINI_INFERENCE_BASE_URL — see D9 amendment in
        # infra-decisions.md: a CPU-only fleet has no GPU node, and rejecting
        # every reference there is what made the headline prompt a 422).
        if resolved_node is None:
            return False, "invalid_target_node_type"
        if service_host or resolved_node.get("role") == "inference":
            return True, None
        return False, "invalid_target_node_type"
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
    "invalid_target_node_type": (
        "inference_vm must resolve to the inference node or to the node serving "
        "the agent-facing inference service"
    ),
    "invalid_target_node": "agent_vm target node could not be resolved to a compute node",
    "gpu_not_allowed_for_agent": "GPU passthrough is not permitted on agent_vm",
}


def _role_reject_detail(reason: str | None, resolved_node, handler) -> str:
    """Human-readable detail for a role-level rejection, naming what was looked for.

    The frozen CODE is what clients switch on (`invalid_target_node_type`); the
    detail is what makes it actionable. For an `inference_vm` that did not resolve
    it must say *both* ways to satisfy the reference class and what actually
    matched — otherwise a CPU-only fleet reads as "the feature is broken" when the
    controller is correctly refusing an unknown host (t_d7e0bf4e).
    """
    base = _ROLE_REJECT_DETAILS.get(reason or "") or str(reason or "")
    if reason != "invalid_target_node_type":
        return base
    hosts = sorted(_inference_front_door_hosts(handler))
    if resolved_node is not None:
        return (
            f"{base}: node {resolved_node.get('id')} is neither an inference node "
            f"(a node reporting a >= 12 GiB GPU) nor the host of the agent-facing "
            f"inference service"
        )
    if not hosts:
        return (
            f"{base}: no inference node is registered and no agent-facing inference "
            f"service address is configured (TERMINI_INFERENCE_BASE_URL)"
        )
    return (
        f"{base}: no inference node is registered and no registered real node "
        f"serves the agent-facing inference service at {', '.join(hosts)}"
    )

# Human-readable detail for each per-node capacity rejection reason
# (controller-api.md §5.2 step 6), surfaced alongside the machine-readable code.
_CAPACITY_REJECT_DETAILS = {
    "exceeds_node_ram_ceiling": "aggregate RAM exceeds the node's free capacity",
    "exceeds_node_cpu_ceiling": "aggregate vCPU exceeds the node's overcommit ceiling",
    "exceeds_node_disk_ceiling": "aggregate disk exceeds the node's free capacity",
}


def _capacity_reject_detail(reason: str, node_id: str, cap: dict, resolved: dict,
                            count: int, req_vcpus: int, req_ram_gb: int,
                            req_disk_gb: int) -> str:
    """A capacity refusal that names the node and the numbers.

    The frozen reason CODE is unchanged (`_CAPACITY_REJECT_DETAILS`); only the
    human-readable `detail` gains the resolved node, what the request asked for
    as an aggregate, and what the node actually had free. That is what turns a
    bare "guardrail said no" into something a dashboard user can act on (and what
    makes a re-sized retry obvious).
    """
    base = _CAPACITY_REJECT_DETAILS.get(reason, reason)
    requested = f"{count}×{req_vcpus} vCPU / {count}×{req_ram_gb} GiB RAM"
    if reason == "exceeds_node_cpu_ceiling":
        free = f"cpu_free={cap.get('cpu_free')} vCPU"
    elif reason == "exceeds_node_disk_ceiling":
        free = f"disk_free_gb={cap.get('disk_free_gb')} GiB"
    else:
        free = f"ram_free_gb={cap.get('ram_free_gb')} GiB"
    return (
        f"{base}: node {node_id} had {free} free and the request needed "
        f"{requested} (already allocated {cap.get('cpu_allocated')} vCPU / "
        f"{cap.get('ram_allocated_gb')} GiB RAM)"
    )
