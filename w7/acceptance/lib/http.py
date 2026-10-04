"""Minimal independent HTTP client for probing the Termini controller.

Deliberately written from scratch (stdlib urllib) so the harness never reuses
the implementer's own request helpers or assertions.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple


class Response:
    __slots__ = ("status", "headers", "body_bytes", "body_text")

    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = headers
        self.body_bytes = body
        self.body_text = body.decode("utf-8", errors="replace")

    def json(self) -> Optional[Any]:
        if not self.body_bytes:
            return None
        try:
            return json.loads(self.body_text)
        except (json.JSONDecodeError, ValueError):
            return None

    def __repr__(self) -> str:
        return f"Response(status={self.status}, body={self.body_text[:200]!r})"


def request(
    method: str,
    url: str,
    *,
    body: Any = None,
    token: Optional[str] = None,
    headers: Optional[dict] = None,
    timeout: float = 15.0,
) -> Response:
    """Send an HTTP request and return a Response (never raises on HTTP errors).

    ``body`` is JSON-encoded when it is not None and not already bytes.
    """
    data: Optional[bytes] = None
    hdrs = {"X-Termini-Version": "1"}
    if headers:
        hdrs.update(headers)
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if body is not None:
        if isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, dict(e.headers), e.read())
    except urllib.error.URLError as e:
        raise ConnectionError(f"request failed: {e.reason}") from e


def http(
    method: str,
    base: str,
    path: str,
    **kwargs: Any,
) -> Response:
    """Convenience: request against ``base`` + ``path``."""
    return request(method, base.rstrip("/") + path, **kwargs)
