"""LXD REST executor — performs remediation against a node's LXD REST API.

The controller never shells out to `lxc`/`lxd` CLIs; it drives LXD over its REST
API (urllib, stdlib-only). The LXD server is reached at a configurable base URL
(HTTP for the demo; HTTPS with a client certificate for a real cluster).

Guarantees:
  * dry-run mode logs the intended request (method/path/body) and sends nothing;
  * transient failures (5xx, connection errors, timeouts) retry with exponential
    backoff; 4xx responses are treated as terminal and returned immediately;
  * `instance.delete` treats a 404 as success (already gone -> idempotent re-run);
  * every operation returns an ExecResult (method/path/body + status + detail)
    so the loop can write a complete audit record (node, event, action, result).

The actual idempotency/dedup *decision* (one health event -> one command) lives
in the loop + schema (UNIQUE decision per event, cooldown); the executor's job
is to make each LXD call safe to re-run and to report exactly what it did.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request

from .types import ExecResult, RemediationAction

log = logging.getLogger("termini.operator.executor")

_TRANSIENT_MIN = 500  # retry on 5xx; 4xx is terminal


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
    ):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self.retry_max = max(1, retry_max)
        self.backoff_s = backoff_s
        self._ssl_ctx = self._make_ssl_ctx(client_cert, client_key)

    @staticmethod
    def _make_ssl_ctx(cert: str | None, key: str | None) -> ssl.SSLContext | None:
        if not cert:
            return None
        ctx = ssl.create_default_context()
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
    def restart_instance(self, name: str, *, force: bool = True, timeout: int = 30) -> ExecResult:
        return self._request("PUT", f"/1.0/instances/{name}/state",
                             {"action": "restart", "force": force, "timeout": timeout})

    def stop_instance(self, name: str, *, force: bool = True) -> ExecResult:
        return self._request("PUT", f"/1.0/instances/{name}/state",
                             {"action": "stop", "force": force})

    def delete_instance(self, name: str) -> ExecResult:
        r = self._request("DELETE", f"/1.0/instances/{name}")
        # 404 = already gone: re-running the same remediation is a no-op.
        if r.result == "error" and r.status == 404:
            r.result = "ok"
            r.detail = f"instance {name} already absent (404) — idempotent no-op"
        return r

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
        if kind == "instance.delete":
            return self.delete_instance(p.get("instance_name") or p.get("name") or "agent")
        if kind == "image.prune":
            return self.prune_images()
        return ExecResult(result="error", detail=f"unknown action kind: {kind!r}")
