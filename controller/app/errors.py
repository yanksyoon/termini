"""HTTP error envelope and exception types.

Every non-2xx response uses the shape frozen in controller-api.md §3:

    { "error": { "code", "message", "details": [...], "request_id" } }
"""
from __future__ import annotations

from typing import Any

from . import ulid

# Codes frozen in controller-api.md §3.
VALID_CODES = {
    "INVALID_ARGUMENT",
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "CONFLICT",
    "GUARDRAIL_REJECTED",
    "GOVERNANCE_REJECT",
    "UNSUPPORTED_VERSION",
    "RATE_LIMITED",
    "NODE_OFFLINE",
    "INTERNAL",
}


class ApiError(Exception):
    """Raised by handlers; converted to the error envelope by the server."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        # Extra response headers a route must set with its error (e.g.
        # `Retry-After` on a 429 the agent refused). Never used for the body.
        self.headers = headers or {}


def err(status: int, code: str, message: str, details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None) -> ApiError:
    return ApiError(status, code, message, details, headers)


def invalid_argument(message: str, details: list[dict[str, Any]] | None = None) -> ApiError:
    return ApiError(422, "INVALID_ARGUMENT", message, details)


def governance_reject(message: str, details: list[dict[str, Any]] | None = None) -> ApiError:
    return ApiError(422, "GOVERNANCE_REJECT", message, details)


def unauthenticated(message: str = "missing or invalid token") -> ApiError:
    return ApiError(401, "UNAUTHENTICATED", message)


def permission_denied(message: str = "token scope does not cover this resource") -> ApiError:
    return ApiError(403, "PERMISSION_DENIED", message)


def not_found(message: str = "resource not found") -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def unsupported_version(details: list[dict[str, Any]] | None = None) -> ApiError:
    return ApiError(426, "UNSUPPORTED_VERSION", "unsupported API version", details)


def envelope(
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": request_id or ulid.new_ulid(),
        }
    }
