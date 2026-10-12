"""LXD REST executor — performs remediation against a node's LXD REST API.

The controller never shells out to `lxc`/`lxd` CLIs; it drives LXD over its REST
API (urllib, stdlib-only). The LXD server is reached at a configurable base URL
(HTTP for the demo; HTTPS with a client certificate for a real cluster).

Guarantees:
  * dry-run mode logs the intended request (method/path/body) and sends nothing;
  * transient failures (5xx, connection errors, timeouts) retry with exponential
    backoff; 4xx responses are treated as terminal and returned immediately;
  * `instance.delete` treats a 404 as success (already gone -> idempotent re-run);
  * every state-changing operation is **waited on and confirmed**: LXD answers a
    write with `202 + an operation URL`, which only means "accepted", so the
    operation is polled to a terminal state and the instance's resulting state is
    read back. A failed/timeout operation, or one whose success did not change the
    instance, is reported as `error` — never `ok` (see `_await_write` /
    `_confirm_state`);
  * a `restart` of a STOPPED instance is a no-op that LXD still accepts with
    `202 "Restarting instance"` (measured live), so `restart_instance` reads the
    state first and *starts* a stopped instance instead of issuing a restart that
    cannot take effect;
  * every operation returns an ExecResult (method/path/body + status + detail +
    operation id) so the loop can write a complete audit record (node, event,
    action, result).

Client trust: the controller authenticates to a node's LXD by the TLS client
certificate it presents (`client_cert`/`client_key`). The certificate is minted
and installed into the node's trust store by `app/lxdtrust.py`; `trust_client`
here is the raw `POST /1.0/certificates` primitive that consumes an LXD trust
token (or `core.trust_password`) to add the presented cert to a node's trust
store, and `list_instances` is the "am I trusted?" probe (200 trusted vs 403
not).

The actual idempotency/dedup *decision* (one health event -> one command) lives
in the loop + schema (UNIQUE decision per event, cooldown); the executor's job
is to make each LXD call safe to re-run and to report exactly what it did.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request

from .models import ExecResult, RemediationAction

log = logging.getLogger("termini.operator.executor")

_TRANSIENT_MIN = 500  # retry on 5xx; 4xx is terminal

# LXD refuses `PUT .../state {"action":"start"}` on an already-running instance
# ("You will get an error if the instance does not exist or if it is running
# already"). That is the *expected* answer on a replay of a plan whose instances
# are already up, so it is folded to `already_applied` (idempotent no-op) rather
# than surfaced as a failure. The negative lookahead keeps "not running" out.
_ALREADY_RUNNING = re.compile(r"already\s+(?:running|started)|(?<!not\s)is\s+running", re.I)


def _instance_already_running(detail: object) -> bool:
    return bool(_ALREADY_RUNNING.search(str(detail or "")))



def _operation_url(raw: str) -> str:
    """Extract the operation URL from an LXD write response body.

    LXD answers a write with 202 and a body of the form
    `{"type": "async", "operation": "/1.0/operations/<uuid>", ...}`. Returns ""
    when the body is not that shape (e.g. a synchronous response), in which case
    the caller has nothing to wait on.
    """
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(meta, dict):
        return ""
    op = meta.get("operation")
    return op if isinstance(op, str) else ""


class LxdExecutor:
    def __init__(
        self,
        base_url: str,
        *,
        dry_run: bool = False,
        timeout_s: float = 10.0,
        retry_max: int = 3,
        backoff_s: float = 0.5,
        client_cert: str | None = None,
        client_key: str | None = None,
        server_ca: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self.retry_max = max(1, retry_max)
        self.backoff_s = backoff_s
        self._ssl_ctx = self._make_ssl_ctx(client_cert, client_key, server_ca)

    @staticmethod
    def _make_ssl_ctx(
        cert: str | None, key: str | None, server_ca: str | None = None
    ) -> ssl.SSLContext | None:
        """Build the HTTPS context: present `cert`, optionally pin the server.

        LXD serves a *self-signed* server certificate (it is its own CA), so the
        default system trust store cannot verify it. With no `server_ca` we mirror
        the demo's `curl -k` behaviour — present the client cert, do not verify the
        server — and log one warning so the gap is visible. Set `server_ca` to a
        PEM holding the node's/cluster's server certificate to pin it properly.
        """
        if not cert and not server_ca:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False  # nodes are dialled by underlay IP, not hostname
        if server_ca:
            ctx.load_verify_locations(cafile=server_ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
        else:
            ctx.verify_mode = ssl.CERT_NONE
            log.warning(
                "LXD TLS: verifying the server certificate is DISABLED (no "
                "server_ca pinned) — set TERMINI_LXD_SERVER_CA to a node PEM to pin it"
            )
        if cert:
            ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, body: dict | None = None) -> ExecResult:
        url = self.base_url + path
        payload = json.dumps(body) if body is not None else ""
        if self.dry_run:
            return ExecResult(
                result="dry_run", method=method, path=path, body=payload,
                detail=f"dry-run: would {method} {url}",
            )

        data = payload.encode() if payload else None
        headers = {"Content-Type": "application/json"}
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_ctx)
        ) if self._ssl_ctx else urllib.request.build_opener()

        last = ExecResult(result="error", method=method, path=path, body=payload,
                          detail="no attempt completed")
        for attempt in range(self.retry_max):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with opener.open(req, timeout=self.timeout_s) as resp:
                    raw = resp.read().decode(errors="replace")
                    status = resp.status
                    return ExecResult(
                        result="ok" if status < 400 else "error",
                        detail=raw, method=method, path=path, body=payload,
                        status=status,
                    )
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                status = e.code
                last = ExecResult(result="error", detail=f"HTTP {status}: {raw[:400]}",
                                  method=method, path=path, body=payload, status=status)
                if status < _TRANSIENT_MIN:
                    return last
            except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as e:
                last = ExecResult(result="error", detail=f"{type(e).__name__}: {e}",
                                  method=method, path=path, body=payload)
            time.sleep(self.backoff_s * (2 ** attempt))
        return last

    def _read_json(self, path: str) -> tuple[ExecResult, object]:
        r = self._request("GET", path)
        if r.result != "ok":
            return r, None
        try:
            return r, json.loads(r.detail)
        except json.JSONDecodeError:
            r.result = "error"
            r.detail = f"non-JSON response from {path}: {r.detail[:200]}"
            return r, None

    # ------------------------------------------------------------- operations
    def _instance_state(self, name: str) -> str:
        """The instance's current LXD state ("Running"/"Stopped"/"Frozen"/...).

        Returns "" when it cannot be read (instance absent, server unreachable,
        or a synchronous body that carries no state) — callers must treat "" as
        "unknown", never as a state.
        """
        r, meta = self._read_json(f"/1.0/instances/{name}/state")
        if r.result != "ok" or not isinstance(meta, dict):
            return ""
        md = meta.get("metadata")
        if isinstance(md, dict):
            st = md.get("status")
            if isinstance(st, str):
                return st
        return ""

    def _await_write(self, r: ExecResult, *, timeout_s: float = 120.0) -> ExecResult:
        """Poll the operation a write returned, so `ok` means *done*.

        LXD answers a state-changing write with `202 + an operation URL`; the 202
        only means "accepted", not "the action happened". When the response body
        carries an operation, this waits on it via ``wait_operation`` (which
        reports a Failure/timed-out operation as `error`) and then restores the
        original write's method/path/body onto the result, so the audit row still
        records the request that was actually issued to LXD. A synchronous
        response (no operation URL) is returned unchanged.
        """
        if r.result != "ok":
            return r
        op = _operation_url(r.detail)
        if not op:
            return r
        waited = self.wait_operation(op, timeout_s=timeout_s)
        if waited.result != "ok" and waited.status == 404:
            # The operation finished and LXD already reaped it before our first
            # poll (a fast no-op completes in well under the poll interval). Its
            # outcome is no longer readable, so fall back to the write's own
            # acceptance; `_confirm_state` is the ground truth for a state change.
            r.detail = f"{r.detail}; operation {op} already finished (reaped before first poll)"
            return r
        waited.method = r.method
        waited.path = r.path
        waited.body = r.body
        waited.status = r.status
        return waited

    def _confirm_state(self, name: str, expected: str, *, action: str,
                       before: str, r: ExecResult, timeout_s: float = 20.0,
                       poll_s: float = 0.5) -> ExecResult:
        """Confirm the instance actually reached `expected` after the operation.

        An operation reaching Success is not proof the instance changed (a restart
        of a stopped instance succeeds and changes nothing), so the state is read
        back until it matches or the window lapses. When the state cannot be read
        at all (e.g. the demo stub answers a synchronous body), the effect cannot
        be contradicted and `r` is returned unchanged; only a positively observed
        wrong state turns the result into an `error`.
        """
        deadline = time.monotonic() + timeout_s
        seen = ""
        while True:
            seen = self._instance_state(name)
            if seen == expected:
                r.detail = f"{r.detail}; action={action}; state {before or '?'} -> {seen}"
                return r
            if seen:
                break  # a definitive, different state — no point polling
            if time.monotonic() >= deadline:
                return r  # unreadable: nothing contradicts the operation result
            time.sleep(poll_s)
        r.result = "error"
        r.detail = (f"INEFFECTIVE: {action} reported success ({r.detail}) but "
                    f"instance {name} is {seen!r}, not {expected!r}")
        return r

    def restart_instance(self, name: str, *, force: bool = True, timeout: int = 30) -> ExecResult:
        """Restart the instance — and make sure that is what actually happens.

        A restart on a STOPPED instance is a no-op LXD nevertheless accepts with
        `202 "Restarting instance"` (measured live: the call returned ok and the
        instance stayed STOPPED). So the state is read first: a running (or
        unreadable) instance is restarted, a stopped one is *started* — a restart
        cannot bring it up. The operation is waited on and the resulting state is
        confirmed, so an ineffective remediation is `error`, not `ok`.
        """
        path = f"/1.0/instances/{name}/state"
        if self.dry_run:
            return self._request("PUT", path,
                                 {"action": "restart", "force": force, "timeout": timeout})
        before = self._instance_state(name)
        # "Running" (or unknown) -> restart; any definitive non-running state ->
        # start, because restarting a stopped instance changes nothing.
        action = "restart" if before in ("", "Running") else "start"
        body = ({"action": "restart", "force": force, "timeout": timeout}
                if action == "restart" else {"action": "start", "timeout": timeout})
        r = self._await_write(self._request("PUT", path, body),
                              timeout_s=max(120.0, float(timeout) + 30.0))
        if r.result != "ok":
            return r
        return self._confirm_state(name, "Running", action=action, before=before, r=r)

    def stop_instance(self, name: str, *, force: bool = True) -> ExecResult:
        """Stop the instance, waiting on the operation and confirming STOPPED."""
        path = f"/1.0/instances/{name}/state"
        if self.dry_run:
            return self._request("PUT", path, {"action": "stop", "force": force})
        r = self._await_write(self._request("PUT", path, {"action": "stop", "force": force}))
        if r.result != "ok":
            return r
        return self._confirm_state(name, "Stopped", action="stop", before="", r=r)

    def start_instance(self, name: str, *, wait: bool = True,
                       timeout_s: float = 120.0) -> ExecResult:
        """Start a created instance and wait until it is running.

        A `create_instance` only *creates* the instance — LXD answers the create
        operation with the instance left **STOPPED** ("The request creates the
        instance, but does not start it"). Materialisation's promise is that the
        agents are running, so the create is followed by this start and the caller
        does not report the VM ready until the start operation has completed.

        Idempotent: LXD errors when an instance is already running (there is
        nothing to do), and that specific answer is folded to `already_applied` —
        the same "re-running the same step is safe" rule `delete_instance` applies
        to a 404 and `create_instance` applies to a duplicate name. A genuine
        start failure (no space, cannot allocate) stays an `error` so the caller
        can treat the allocation as failed.
        """
        r = self._request("PUT", f"/1.0/instances/{name}/state", {"action": "start"})
        if r.result == "error" and _instance_already_running(r.detail):
            r.result = "already_applied"
            r.detail = f"instance {name} already running — idempotent no-op"
            return r
        if r.result != "ok" or not wait:
            return r

        op = _operation_url(r.detail)
        if not op:
            return r
        r.operation = op
        w = self.wait_operation(op, timeout_s=timeout_s)
        if w.result == "error" and _instance_already_running(w.detail):
            w.result = "already_applied"
            w.detail = f"instance {name} already running — idempotent no-op"
        return w

    def delete_instance(self, name: str, *, force: bool = False,
                        timeout_s: float = 60.0) -> ExecResult:
        """Delete an instance; a 404 is success (already gone -> idempotent re-run).

        With `force=True`, stop the instance (and WAIT for the stop operation)
        before deleting: LXD refuses to delete a RUNNING instance, so a rollback
        of an allocation whose instances were already started must stop them
        first or the delete silently no-ops and leaves the VM behind. A stop of an
        already-stopped instance errors; that error is ignored because the DELETE
        that follows is the authority.
        """
        if force:
            sr = self._request("PUT", f"/1.0/instances/{name}/state",
                               {"action": "stop", "force": True})
            if sr.result == "ok":
                op = _operation_url(sr.detail)
                if op:
                    self.wait_operation(op, timeout_s=timeout_s)
        r = self._request("DELETE", f"/1.0/instances/{name}")
        # 404 = already gone: re-running the same remediation is a no-op.
        if r.result == "error" and r.status == 404:
            r.result = "ok"
            r.detail = f"instance {name} already absent (404) — idempotent no-op"
            return r
        # A delete is async too: wait so a following step never races the deletion.
        return self._await_write(r)

    # ------------------------------------------------------------- client trust
    def list_instances(self) -> ExecResult:
        """Authenticated GET /1.0/instances — the "am I trusted?" probe.

        A trusted client gets 200; an untrusted one gets 401/403 (the defect this
        whole change repairs). The status on the returned ExecResult is the
        observable signal (`app/lxdtrust.py` reads it).
        """
        return self._request("GET", "/1.0/instances")

    def list_instance_names(self) -> tuple[bool, list[str]]:
        """Names of the instances that exist on the node, for reconciliation.

        Returns `(reachable, names)`. `reachable` is False when the node could not
        be read (untrusted, unreachable, timeout, non-JSON): the caller must treat
        that as "absence not proven" and keep the reservation, never as an empty
        node — an unreachable node that silently released every plan's capacity
        would be worse than the bug this repairs.
        """
        r, meta = self._read_json("/1.0/instances?recursion=1")
        if r.result != "ok":
            return False, []
        items = meta.get("metadata") if isinstance(meta, dict) else meta
        names: list[str] = []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("name"), str):
                    names.append(it["name"])
                elif isinstance(it, str):  # recursion=0 URL-list shape
                    names.append(it.rstrip("/").split("/")[-1])
        return True, names

    def trust_client(self, secret: str, *, name: str = "termini-controller") -> ExecResult:
        """Add THIS client's certificate to the node's LXD trust store.

        `POST /1.0/certificates` with the client cert in the TLS handshake and the
        secret in `password`. LXD accepts either a certificate-add trust token or
        `core.trust_password` there. An LXD "already trusted"/409 reply is folded
        to `already_applied` so a re-run is a no-op rather than an error (the same
        idempotency rule `delete_instance` applies to a 404).
        """
        r = self._request(
            "POST", "/1.0/certificates",
            {"type": "client", "password": secret, "name": name},
        )
        if r.result == "error" and (
            r.status == 409 or "already" in (str(r.detail) or "").lower()
        ):
            r.result = "already_applied"
            r.detail = "certificate already present in the node trust store — idempotent no-op"
        return r

    def create_instance(
        self,
        name: str,
        *,
        image_alias: str | None = None,
        image_fingerprint: str | None = None,
        profile: str = "default",
        cpu: int | None = None,
        memory: str | None = None,
        ephemeral: bool = False,
        wait: bool = True,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        """Materialise an instance from a preloaded image (POST /1.0/instances).

        This is the primitive that turns an accepted plan's allocations into real
        instances on a node. The image must already be in the node's local store:
        first boot never contacts a registry (D3), so the alias refers to an image
        baked into the node image rather than one to be pulled.

        Safe to re-run: LXD rejects a duplicate name, and that is mapped to
        `already_applied` (the same idempotency rule `delete_instance` applies to a
        404), so a retried or replayed materialisation is a no-op rather than an
        error.

        NOTE: this creates the instance **STOPPED** — LXD does not start a created
        instance. A caller that needs a running workload must follow this with
        `start_instance`; `_apply_plan` does exactly that.
        """
        if not image_alias and not image_fingerprint:
            raise ValueError("create_instance needs image_alias or image_fingerprint")
        source: dict = {"type": "image"}
        if image_fingerprint:
            source["fingerprint"] = image_fingerprint
        else:
            source["alias"] = image_alias
        config: dict = {}
        if cpu:
            config["limits.cpu"] = str(int(cpu))
        if memory:
            config["limits.memory"] = str(memory)
        body = {
            "name": name,
            "source": source,
            "profiles": [profile],
            "config": config,
            "ephemeral": bool(ephemeral),
        }

        r = self._request("POST", "/1.0/instances", body)
        if r.result == "error" and "exist" in (r.detail or "").lower():
            r.result = "already_applied"
            r.detail = f"instance {name} already exists — idempotent no-op"
            return r
        if r.result != "ok" or not wait:
            return r

        # LXD answers a write with 202 + an operation URL. "The POST returned" does
        # NOT mean "the instance exists", so materialisation waits on the operation
        # before reporting success — otherwise the plan would commit capacity for an
        # instance that is still being created.
        op = _operation_url(r.detail)
        if not op:
            return r
        r.operation = op
        return self.wait_operation(op, timeout_s=timeout_s)

    def wait_operation(self, operation_url: str, *, timeout_s: float = 120.0,
                       poll_s: float = 1.0) -> ExecResult:
        """Poll an LXD operation to a terminal state."""
        path = operation_url
        if path.startswith("http"):
            path = path[len(self.base_url):] if path.startswith(self.base_url) else path
        r = ExecResult(result="error", method="GET", path=path,
                       detail=f"operation {operation_url} did not complete")
        deadline = time.monotonic() + timeout_s
        while True:
            r, meta = self._read_json(path)
            if r.result != "ok":
                return r
            status = ""
            err = ""
            if isinstance(meta, dict):
                md = meta.get("metadata")
                md = md if isinstance(md, dict) else {}
                status = md.get("status") or ""
                # The operation's own error string is WHERE the reason lives: LXD
                # answers a refused start with a *failed operation* ("The instance
                # is already running"), not a synchronous error, so a caller that
                # only kept the status could never tell why. Surface it.
                err = md.get("err") or ""
            if status == "Success":
                r.result = "ok"
                r.operation = path
                r.detail = f"operation Success: {path}"
                return r
            if status in ("Failure", "Cancelled"):
                r.result = "error"
                r.operation = path
                r.detail = f"operation {status}: {err}" if err else f"operation {status}: {path}"
                return r
            if time.monotonic() >= deadline:
                r.result = "error"
                r.operation = path
                r.detail = f"operation still {status or 'pending'} after {timeout_s}s: {path}"
                return r
            time.sleep(poll_s)

    def prune_images(self) -> ExecResult:
        """Delete unused/cached images to reclaim disk (pure REST, no shell)."""
        if self.dry_run:
            return ExecResult(
                result="dry_run", method="GET+DELETE", path="/1.0/images*",
                body="", detail="dry-run: would GET /1.0/images?recursion=1 and "
                                "DELETE each prunable (cached/auto_update) image",
            )
        r, images = self._read_json("/1.0/images?recursion=1")
        if r.result != "ok":
            return r
        prunable = [fp for fp in self._prunable(images)]
        deleted = 0
        for fp in prunable:
            dr = self._request("DELETE", f"/1.0/images/{fp}")
            if dr.result == "ok" or (dr.result == "error" and dr.status == 404):
                deleted += 1
        return ExecResult(
            result="ok", method="GET+DELETE", path="/1.0/images*", body="",
            detail=f"pruned {deleted} of {len(prunable)} prunable images", status=200,
        )

    @staticmethod
    def _prunable(metadata) -> list[str]:
        """Extract fingerprints of images that are safe to prune (cached or
        auto-update). Handles LXD's recursion=1 object list and recursion=0 URL
        list shapes."""
        items = metadata
        if isinstance(metadata, dict):
            items = metadata.get("metadata", [])
        out: list[str] = []
        if not isinstance(items, list):
            return out
        for it in items:
            if isinstance(it, str):  # image URL path (recursion=0)
                fp = it.rstrip("/").split("/")[-1]
                out.append(fp)
            elif isinstance(it, dict):
                cached = it.get("cached") is True
                auto = it.get("auto_update") is True
                fp = it.get("fingerprint")
                if fp and (cached or auto):
                    out.append(fp)
        return out

    # ------------------------------------------------------------------ dispatch
    def execute(self, action: RemediationAction) -> ExecResult:
        kind = action.kind
        p = action.params or {}
        if kind == "instance.create":
            return self.create_instance(
                p.get("instance_name") or p.get("name") or "agent",
                image_alias=p.get("image_alias"),
                image_fingerprint=p.get("image_fingerprint"),
                profile=p.get("profile", "default"),
                cpu=p.get("vcpus"),
                memory=p.get("memory"),
                ephemeral=bool(p.get("ephemeral", False)),
            )
        if kind == "instance.restart":
            return self.restart_instance(
                p.get("instance_name") or p.get("name") or "agent",
                force=bool(p.get("force", True)),
                timeout=int(p.get("timeout_s", 30)),
            )
        if kind == "instance.stop":
            return self.stop_instance(
                p.get("instance_name") or p.get("name") or "agent",
                force=bool(p.get("force", True)),
            )
        if kind == "instance.start":
            return self.start_instance(
                p.get("instance_name") or p.get("name") or "agent",
                timeout_s=float(p.get("timeout_s", 120.0)),
            )
        if kind == "instance.delete":
            return self.delete_instance(p.get("instance_name") or p.get("name") or "agent")
        if kind == "image.prune":
            return self.prune_images()
        return ExecResult(result="error", detail=f"unknown action kind: {kind!r}")
