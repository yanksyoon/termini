#!/usr/bin/env python3
"""Prove every registered node serves its OWN LXD REST API (t_0139006c).

Run ON the controller, where the controller's node table and the node-underlay
network both live:

    python3 infra/gcp/tools/verify-node-lxd-api.py [--db /data/controller.db]

What it asserts, per node:

  1. the endpoint the controller would DIAL for that node — the node-reported
     `nodes.lxd_endpoint` when there is one — is probed over HTTPS;
  2. a node that never reported an endpoint is probed at its `source_ip` and the
     port from `TERMINI_LXD_BASE_URL` (the global fallback that made every node's
     remediation dial node-a), and is reported as `FALLBACK` so the gap is visible;
  3. `GET /1.0` must answer with a NON-ZERO HTTP status. 000 (connection refused,
     DNS failure, TLS handshake never completed) means nothing is listening there
     and every operator action for that node will fail at remediation time.

Exit status is the evidence: 0 only when every node's own API answers. A node with
no listener is exactly the defect this task fixed (`core.https_address` empty and
nothing listening on :8443 after the daemon's database drifted away), so a red run
is the truth to report, never something to paper over.

TLS verification is deliberately off: LXD serves a self-signed certificate and this
tool asserts a LISTENER, not an identity. Trust is the controller's mTLS client
certificate in each node's trust store (`controller/app/lxdtrust.py`); this probe
never adds trust and never writes anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request

LXD_PATH = "/1.0"
DEFAULT_DB = "/data/controller.db"
DEFAULT_TIMEOUT_S = 5.0


def default_db() -> str:
    return os.environ.get("TERMINI_DB_PATH", "").strip() or DEFAULT_DB


def fallback_port() -> int:
    """The port the global `TERMINI_LXD_BASE_URL` uses (8443 when unset)."""
    url = os.environ.get("TERMINI_LXD_BASE_URL", "").strip()
    if not url:
        return 8443
    _, _, tail = url.rpartition(":")
    try:
        port = int(tail.rstrip("/") or 0)
    except ValueError:
        return 8443
    return port if 1 <= port <= 65535 else 8443


def split_endpoint(endpoint: str) -> tuple[str, int]:
    """`host:port`, `scheme://host:port` or a bare host -> (host, port)."""
    raw = endpoint.strip()
    for scheme in ("https://", "http://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
    raw = raw.rstrip("/")
    host, sep, port = raw.rpartition(":")
    if not sep:
        return raw, 8443
    try:
        return host or raw, int(port)
    except ValueError:
        return raw, 8443


def probe(host: str, port: int, timeout: float = DEFAULT_TIMEOUT_S) -> tuple[int, str]:
    """GET /1.0 -> (http_status, detail). 0 means no listener answered."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}:{port}{LXD_PATH}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read(2048)
            return resp.status, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:  # a real HTTP answer, just not 2xx
        return e.code, f"HTTP {e.code} {e.reason}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return 0, f"{type(reason).__name__}: {reason}"


def node_rows(db_path: str, include_offline: bool = False) -> list[dict]:
    """Nodes the controller would actually execute an operator action against.

    Decommissioned rows are history (the release path tombstones them) and an
    offline node is not expected to answer; neither is evidence about the live
    fleet, so both are excluded unless asked for.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, registration_id, source_ip, public_ip, lxd_endpoint, status,"
            " synthetic, decommissioned_at FROM nodes"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        row = dict(r)
        if row.get("synthetic") or row.get("decommissioned_at"):
            continue
        if not include_offline and (row.get("status") or "") != "online":
            continue
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prove every registered node serves its own LXD REST API.")
    ap.add_argument("--db", default=default_db(), help=f"controller DB (default {DEFAULT_DB})")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--include-offline", action="store_true",
                    help="also probe nodes the controller has marked offline/degraded")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"!! controller DB not found: {args.db}", file=sys.stderr)
        return 2

    port_default = fallback_port()
    results: list[dict] = []
    for row in node_rows(args.db, include_offline=args.include_offline):
        reported = (row.get("lxd_endpoint") or "").strip()
        if reported:
            endpoint, origin = reported, "reported"
            host, port = split_endpoint(reported)
        else:
            host = (row.get("source_ip") or "").strip()
            port = port_default
            endpoint = f"{host}:{port}" if host else ""
            origin = "FALLBACK"
        if not host:
            results.append({"node_id": row["id"], "registration_id": row.get("registration_id"),
                            "endpoint": None, "origin": origin, "http_status": 0,
                            "detail": "no endpoint and no source_ip recorded"})
            continue
        status, detail = probe(host, port, args.timeout)
        results.append({"node_id": row["id"], "registration_id": row.get("registration_id"),
                        "endpoint": endpoint, "origin": origin,
                        "http_status": status, "detail": detail})

    bad = [r for r in results if r["http_status"] == 0]
    fallback = [r for r in results if r["origin"] == "FALLBACK"]

    if args.json:
        print(json.dumps({"nodes": results, "failing": len(bad)}, indent=2))
    else:
        if not results:
            print("!! no compute nodes in the controller DB — nothing was proven")
        for r in results:
            mark = "ok  " if r["http_status"] else "FAIL"
            print(f"{mark} {r['node_id']}  {r['registration_id'] or '-':<18} "
                  f"{r['endpoint'] or '-':<22} [{r['origin']}] {r['detail']}")
        print(f"\n{len(results) - len(bad)}/{len(results)} nodes answer their own LXD API "
              f"({len(fallback)} resolved via the global fallback)")
        for r in bad:
            print(f"!! {r['node_id']} ({r['registration_id'] or '-'}) has NO listener on "
                  f"{r['endpoint'] or '-'}: {r['detail']} — its remediations will fail")

    if not results:
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
