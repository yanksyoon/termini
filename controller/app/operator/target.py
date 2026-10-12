"""Per-node LXD targeting for apply-plan materialisation.

Until now there was exactly one LXD base URL (``TERMINI_LXD_BASE_URL``), which
cannot name one node among a fleet: each node runs its own LXD on its own underlay
address (``10.10.0.11-13:8443``, ``docs/contracts/infra-decisions.md`` D2). A node
now records its own endpoint at registration (``nodes.lxd_endpoint``); this module
turns an allocation's resolved node into an :class:`LxdExecutor` bound to THAT
node.

Fallback rule (frozen): a node that reported no endpoint uses the global
``TERMINI_LXD_BASE_URL``. That is what keeps the stub-based demo working, and it is
the *only* case in which the global URL is used — a node with its own endpoint is
never silently redirected to the global address ("materialise the VMs on the wrong
host" is the failure that rule exists to prevent).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from .executor import LxdExecutor

log = logging.getLogger("termini.operator.target")

_SLUG = re.compile(r"[^a-z0-9-]+")


def endpoint_base_url(endpoint: str | None, *, fallback: str,
                      default_scheme: str = "https") -> str:
    """Base URL for a node endpoint, or `fallback` when the node reported none.

    `endpoint` is a node-reported ``host:port`` or a full ``scheme://host:port``.
    A bare ``host:port`` is given ``default_scheme`` (real nodes serve LXD over
    HTTPS); a full URL is honoured verbatim so an http:// demo endpoint still
    works.
    """
    ep = (endpoint or "").strip()
    if not ep:
        return (fallback or "").rstrip("/")
    if "://" in ep:
        return ep.rstrip("/")
    scheme = (default_scheme or "https").strip().lower()
    return f"{scheme}://{ep}"


def executor_for_node(cfg, node: dict | None, *, dry_run: bool = False) -> LxdExecutor:
    """Build an LxdExecutor bound to `node`'s own endpoint (or the global fallback).

    Client credentials: a per-node ``lxd_client_cert``/``lxd_client_key`` on the
    node record wins when present (provisioned out-of-band — the registration wire
    never carries a private key, node-bootstrap.md §11); otherwise the global
    ``TERMINI_LXD_CLIENT_CERT``/``_KEY`` are used.
    """
    node = node or {}
    endpoint = node.get("lxd_endpoint")
    base_url = endpoint_base_url(
        endpoint, fallback=getattr(cfg, "lxd_base_url", ""),
        default_scheme=getattr(cfg, "lxd_node_scheme", "https"),
    )
    if not endpoint:
        log.info(
            "node %s reported no LXD endpoint; using global fallback %s",
            node.get("id"), base_url,
        )
    cert = node.get("lxd_client_cert") or getattr(cfg, "lxd_client_cert", None)
    key = node.get("lxd_client_key") or getattr(cfg, "lxd_client_key", None)
    return LxdExecutor(
        base_url,
        dry_run=dry_run,
        timeout_s=getattr(cfg, "lxd_request_timeout_s", 10.0),
        retry_max=getattr(cfg, "lxd_retry_max", 3),
        backoff_s=getattr(cfg, "lxd_retry_backoff_s", 0.5),
        client_cert=cert,
        client_key=key,
    )


def instance_name(plan_id: str, index: int, slot: int = 0) -> str:
    """Deterministic LXD instance name for one VM of one plan allocation.

    Derived from the plan id + allocation index (+ a per-VM slot so a class whose
    `count` fans out to N VMs gets N distinct, stable names). Determinism is what
    makes a replayed apply idempotent: re-issuing the same create hits LXD's
    duplicate-name rule, which `create_instance` maps to `already_applied`.

    The plan id is lowercased and slugged to stay a valid LXD instance name
    (lowercase alphanumerics + hyphens).
    """
    slug = _SLUG.sub("-", str(plan_id).lower()).strip("-")[:24] or "plan"
    return f"termini-{slug}-{int(index)}-{int(slot)}"
