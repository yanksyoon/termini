"""Egress identity of the model endpoint the operator's decision step calls.

Why this exists
---------------
`controller/app/operator/external_call.py` records the ``destination`` of every
model call — but the *controller's* destination is the node's own underlay
address (``http://10.10.0.13:8080``), which says nothing about where the tokens
actually came from. With the Node C inference proxy, that endpoint may be
serving from **local weights** or **forwarding to a managed model**, and the two
must never be confused: the second crosses the premises boundary.

This module answers the question the audit row needs:

  * :func:`classify_destination` — is a URL inside the VPC or on the internet?
  * :class:`EgressIdentity` — backend, model, destination, egress class.
  * :class:`CachedEgressProbe` — asks the endpoint what it is (its ``/health``),
    bounded and cached so a probe can never slow the decision path down.

The probe is deliberately "ask the endpoint, believe its answer, record what it
said": the proxy reports its own backend (`backend=gemini-proxy`,
`destination=aiplatform.googleapis.com`), so the audit row is derived from the
service's self-description rather than from an assumption in the controller. A
probe that fails yields ``backend="unknown"`` — recorded as unknown, never
silently upgraded to "local".
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger("termini.operator.egress")

CLASS_IN_VPC = "in-vpc"
CLASS_EXTERNAL = "external"
CLASS_UNKNOWN = "unknown"

BACKEND_LOCAL = "local"
BACKEND_FORWARDED = "gemini-proxy"

ANSWERED_BY_FORWARDED = "forwarded-managed-model"
ANSWERED_BY_LOCAL = "local-weights"

# Hosts that are Google-managed model endpoints. Named explicitly so a
# misreading of DNS (or a CNAME to something new) still classifies as external.
_KNOWN_EXTERNAL_SUFFIXES = (
    "googleapis.com",
    "googleusercontent.com",
    "openai.com",
    "anthropic.com",
)

# Sanity bound: the audit must not wait on a slow endpoint. The decision call
# itself is bounded by the operator's decision timeout; this probe is not allowed
# to become the reason a remediation is late.
DEFAULT_PROBE_TIMEOUT_S = 2.0
DEFAULT_PROBE_TTL_S = 60.0


def classify_destination(url: str) -> str:
    """`in-vpc` | `external` | `unknown` for an endpoint URL or bare host.

    Private/loopback/link-local addresses and `.internal`/`.local` names are
    inside the premises; a name that resolves outside the model's own cluster —
    anything with a public suffix — is foreign. An empty/unparsable value is
    ``unknown`` rather than a guess.
    """
    host = host_of(url).lower().strip("[]")
    if not host:
        return CLASS_UNKNOWN
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return CLASS_IN_VPC
        return CLASS_EXTERNAL
    if host in ("localhost",) or host.endswith((".internal", ".local", ".localhost")):
        return CLASS_IN_VPC
    if host.endswith(_KNOWN_EXTERNAL_SUFFIXES):
        return CLASS_EXTERNAL
    if "." in host:
        return CLASS_EXTERNAL
    return CLASS_UNKNOWN


@dataclass(frozen=True)
class EgressIdentity:
    """What the model endpoint says it is, as recorded on the audit row."""

    backend: str = CLASS_UNKNOWN
    model: str = ""
    alias: str = ""
    destination: str = ""
    destination_class: str = CLASS_UNKNOWN
    answered_by: str = CLASS_UNKNOWN
    source: str = "unknown"          # health-probe | unavailable | configured
    endpoint: str = ""
    detail: str = ""

    @property
    def forwarded(self) -> bool:
        return self.backend == BACKEND_FORWARDED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "alias": self.alias,
            "destination": self.destination,
            "destination_class": self.destination_class,
            "answered_by": self.answered_by,
            "source": self.source,
            "endpoint": self.endpoint,
            "detail": self.detail,
            "forwarded": self.forwarded,
        }


def identity_from_health(endpoint: str, health: Dict[str, Any]) -> EgressIdentity:
    """Build the identity from the inference endpoint's ``/health`` body.

    The proxy answers with its backend, the model actually serving and (when
    forwarding) the external destination. A plain llama.cpp `/health`
    (``{"status":"ok"}``) carries none of that, which is exactly the point: an
    endpoint that cannot label itself is recorded as unknown, not as local.
    """
    if not isinstance(health, dict):
        return EgressIdentity(source="unavailable", endpoint=endpoint,
                              detail="health body is not an object")
    backend = str(health.get("backend") or "").strip().lower()
    destination = str(health.get("destination") or "").strip()
    if not backend and not destination:
        return EgressIdentity(source="unavailable", endpoint=endpoint,
                              detail=("endpoint does not label its backend "
                                      "(stock llama.cpp or an older node image)"))
    if backend not in (BACKEND_LOCAL, BACKEND_FORWARDED):
        backend = CLASS_UNKNOWN
    # The EGRESS class is a fact about the destination the endpoint named, not
    # about the endpoint's self-description: a "forwarded" backend pointed at a
    # private address is still inside the premises, and must not be recorded as
    # leaving it.
    destination_class = classify_destination(destination) if destination else CLASS_UNKNOWN
    return EgressIdentity(
        backend=backend,
        model=str(health.get("served_model") or health.get("model") or ""),
        alias=str(health.get("model") or health.get("alias") or ""),
        destination=destination,
        destination_class=destination_class,
        answered_by=str(health.get("answered_by") or CLASS_UNKNOWN),
        source="health-probe",
        endpoint=endpoint,
    )


def probe(endpoint: str, timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> EgressIdentity:
    """Ask `endpoint` what it is. Never raises; failure is an explicit unknown."""
    url = endpoint.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            health = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        return EgressIdentity(source="unavailable", endpoint=endpoint,
                              destination_class=CLASS_UNKNOWN,
                              detail=f"{type(exc).__name__}: {exc}"[:200])
    except Exception as exc:  # noqa: BLE001 - the probe must not break a call
        return EgressIdentity(source="unavailable", endpoint=endpoint,
                              destination_class=CLASS_UNKNOWN,
                              detail=f"{type(exc).__name__}: {exc}"[:200])
    return identity_from_health(endpoint, health)


@dataclass
class CachedEgressProbe:
    """A TTL-cached, thread-safe probe for one endpoint.

    The operator loop is a single thread but the controller also serves requests;
    the lock keeps the cache coherent, and a failed probe is cached for a short
    while so a dead endpoint cannot slow every decision down.
    """

    endpoint: str
    ttl_s: float = DEFAULT_PROBE_TTL_S
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S
    _identity: Optional[EgressIdentity] = field(default=None, init=False)
    _at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __call__(self) -> Dict[str, Any]:
        return self.identity().as_dict()

    def identity(self, *, refresh: bool = False) -> EgressIdentity:
        now = time.monotonic()
        with self._lock:
            fresh = self._identity is not None and (now - self._at) < self.ttl_s
            if fresh and not refresh:
                return self._identity  # type: ignore[return-value]
        identity = probe(self.endpoint, timeout_s=self.timeout_s)
        with self._lock:
            self._identity = identity
            self._at = time.monotonic()
        return identity


def classify_and_describe(destination: str) -> Dict[str, Any]:
    """A destination-only identity (no probe) — the fallback when there is none."""
    destination_class = classify_destination(destination)
    return EgressIdentity(
        backend=CLASS_UNKNOWN,
        destination=host_of(destination),
        destination_class=destination_class,
        source="configured",
    ).as_dict()


def host_of(value: str) -> str:
    """The host part of a URL (or of a bare `host[:port]`) — never the port."""
    if not value or not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw if "//" in raw else "//" + raw)
    return parsed.hostname or ""


def audit_message(record: Dict[str, Any]) -> str:
    """One dashboard line for an ``externalModelCall()`` audit record.

    Keeps the external destination VISIBLE in the operator's log stream: the row
    a judge reads must not imply a local model answered when the tokens came from
    a forwarded managed model.
    """
    egress = record.get("egress") or {}
    backend = egress.get("backend") or record.get("backend") or CLASS_UNKNOWN
    destination_class = egress.get("destination_class") or CLASS_UNKNOWN
    when = "before" if record.get("phase") == "before" else "after"
    parts = [
        f"externalModelCall {when}",
        f"endpoint={record.get('destination', '')}",
        f"backend={backend}",
        f"egress={destination_class}",
    ]
    if egress.get("model"):
        parts.append(f"model={egress['model']}")
    if destination_class == CLASS_EXTERNAL:
        parts.append(f"destination={egress.get('destination', '')}")
        parts.append("answered_by=forwarded-managed-model")
    elif egress.get("answered_by"):
        parts.append(f"answered_by={egress['answered_by']}")
    parts.append(f"outcome={record.get('outcome', '')}")
    parts.append(f"request_id={record.get('request_id', '')}")
    return " ".join(part for part in parts if not part.endswith("="))


__all__ = [
    "ANSWERED_BY_FORWARDED",
    "ANSWERED_BY_LOCAL",
    "BACKEND_FORWARDED",
    "BACKEND_LOCAL",
    "CLASS_EXTERNAL",
    "CLASS_IN_VPC",
    "CLASS_UNKNOWN",
    "CachedEgressProbe",
    "EgressIdentity",
    "audit_message",
    "classify_and_describe",
    "classify_destination",
    "host_of",
    "identity_from_health",
    "probe",
]
