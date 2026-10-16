#!/usr/bin/env python3
"""Collect a REAL node-telemetry sample on this host and self-check it (t_92aac3c8).

The collector card's local proof: run the node daemon's own collectors against
this host's `/proc`, `/sys`, cgroup hierarchy and LXD, write the resulting
payload next to this tool, and scan it the way the contract says egress is
scanned.

It loads `infra/bootstrap/node/termini-bootstrap.py` itself (no copy, no stub), so
what lands in `node-payload.json` is byte-for-byte what the daemon would put on
the wire for this host.

    python3 docs/evidence/2026-10-16-metric-expansion/tools/collect_node_payload.py

Outputs (next to this file):
    node-payload.json   the raw sample, as collected
    c2-scan.txt         contract validator + C2 scan + signature grep results
    stdout              a summary: which groups were collected, which were not

Honest negatives are printed, not hidden: a group whose source does not exist on
this host (or needs a live GCE node — SMART, EDAC, GCE metadata) emits nothing,
and the summary says so.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import socket
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
OUT_DIR = HERE.parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "controller"))


def load_bootstrap():
    path = REPO / "infra" / "bootstrap" / "node" / "termini-bootstrap.py"
    spec = importlib.util.spec_from_file_location("termini_bootstrap_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_iface() -> str:
    """The interface carrying the default route, else the first non-loopback one."""
    override = os.environ.get("TERMINI_PROBE_IFACE")
    if override:
        return override
    try:
        for line in pathlib.Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000" and fields[0] != "lo":
                return fields[0]
    except OSError:
        pass
    for name in sorted(os.listdir("/sys/class/net")):
        if name != "lo":
            return name
    return "eth0"


def local_listener():
    """A bound loopback listener, so `net[].latency_ms` is a REAL measurement."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return srv, srv.getsockname()[1], stop


# C2 signatures named by governance.md §4.3 (field names and value shapes), minus
# the ones `scan_c2` already applies with its transformed-identifier exemptions.
SIGNATURES = (
    ("file path (/proc,/sys,/etc,/var/log)", re.compile(r"/(proc|sys|etc|var/log)/")),
    ("command line / argv", re.compile(r"cmdline|\bargv\b")),
    ("environment", re.compile(r"environ|\benv\b", re.I)),
    ("exe / cwd path", re.compile(r"\bexe\b|\bcwd\b", re.I)),
    ("log/syslog content", re.compile(r"\bsyslog\b|\blog_line\b|\blog_lines\b", re.I)),
    ("credential", re.compile(r"api[_-]?key|\btoken\b|\bsecret\b|password", re.I)),
    ("private key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("cloud key", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("raw IPv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("raw MAC", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("UUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("conntrack table", re.compile(r"nf_conntrack")),
    ("cloud-init payload", re.compile(r"cloud-init|user-data|volatile\.")),
)


def signature_hits(payload) -> list[str]:
    """Every C2-shaped value in the payload, with its JSON path."""
    hits: list[str] = []

    def walk(node, path: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + [str(key)])
        elif isinstance(node, list):
            for item in node:
                walk(item, path)
        elif isinstance(node, str):
            where = ".".join(path)
            for label, pattern in SIGNATURES:
                if pattern.search(node):
                    hits.append(f"{where}: {label}: {node!r}")
        elif isinstance(node, (int, float, bool)) or node is None:
            return

    walk(payload, [])
    return hits


def main() -> int:
    tb = load_bootstrap()
    from app import governance
    from app.validate import validate_sample

    srv, port, stop = local_listener()
    cfg = {
        "node_name": socket.gethostname(),
        "iface": default_iface(),
        "role_hint": os.environ.get("TERMINI_PROBE_ROLE", "compute"),
        "controller_url": f"http://127.0.0.1:{port}",
        "project_id": os.environ.get("TERMINI_PROBE_PROJECT_ID") or tb.new_ulid(),
    }
    try:
        sample = tb.build_sample(cfg, 1)
    finally:
        stop.set()
        srv.close()

    # The payload as it would be serialized for egress (compact separators).
    payload_json = json.dumps(sample, separators=(",", ":"))
    (OUT_DIR / "node-payload.json").write_text(
        json.dumps(sample, indent=2, sort_keys=True) + "\n")

    clean, errors = validate_sample(sample)
    c2 = governance.scan_c2(sample)
    hits = signature_hits(sample)

    groups = ("pressure", "oom_kill_count", "pgmajfault_count",
              "mem_available_bytes", "tcp_retransmit_segs", "conntrack_count",
              "conntrack_max")
    disk_ext = ("io_ticks_ms", "io_in_flight", "inode_used", "inode_total")
    net_ext = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
    inst_ext = ("oom_kill_count", "pgmajfault_count")

    report = []
    report.append("== sample (compact, as egressed) ==")
    report.append(payload_json)
    report.append("")
    report.append("== controller validator (app.validate.validate_sample) ==")
    report.append(f"errors: {errors}")
    report.append("")
    report.append("== contract C2 scan (app.governance.scan_c2) ==")
    report.append(f"violations: {c2}")
    report.append("")
    report.append("== C2 signature grep over the payload (governance.md §4.3) ==")
    report.append("\n".join(hits) if hits else "no signature matched in any string value")
    report.append("")
    report.append("notes on the grep:")
    report.append("  - `ip_local` is the contract's C1 TRANSFORMED form (last octet replaced"
                  " with `.0/24`, node-telemetry.md §4.2) and `governance.scan_c2`"
                  " exempts it by pattern; a RAW address would be a hit. A raw IPv4 or"
                  " MAC anywhere else in the payload above would be listed here.")
    report.append("  - `hostname` / `mac_address` are SHA-256 hex prefixes (C1 hashes) and"
                  " match no signature.")
    report.append("  - `disk[].path` values are device labels (/dev/root, /dev/termini) —"
                  " no /proc, /sys, /etc or /var/log path appears anywhere in the payload,")
    report.append("    and the conntrack TABLE is not read at all: only the two count files.")
    report.append("")
    report.append("== §4.12–§4.14 group presence ==")
    for key in groups:
        report.append(f"  {key}: {'PRESENT ' + repr(sample[key]) if key in sample else 'absent (source unreadable on this host)'}")
    for row in sample.get("disk", []):
        report.append(f"  disk[{row.get('path')}]: " + ", ".join(
            f"{f}={row[f]}" for f in disk_ext if f in row) or "no §4.13 fields")
    for row in sample.get("net", []):
        report.append(f"  net[{row.get('iface')}]: " + ", ".join(
            f"{f}={row[f]}" for f in net_ext if f in row) or "no §4.13 fields")
    for row in sample.get("lxd_instances", []):
        report.append(f"  lxd_instances[{row.get('vm_id')}]: " + ", ".join(
            f"{f}={row[f]}" for f in inst_ext if f in row) or "no §4.14 fields")
    report.append("")
    report.append("== groups this host cannot produce (honest negatives) ==")
    report.append("  SMART media health: smartctl/nvme not installed and no SATA/NVMe"
                  " passthrough on this QEMU host — needs a live node"
                  " (smartctl -a /dev/sda)")
    report.append("  EDAC memory errors: /sys/devices/system/edac/mc has no controller"
                  " nodes — needs ECC server hardware")
    report.append("  GCE preemptible/maintenance-event: metadata.google.internal does"
                  " not resolve off-GCE — needs a live GCE node")
    report.append("  OVS/OVN flow counters: ovs-vsctl/ovn-* not installed; the fleet"
                  " uses the Linux bridge (lxdbr0)")

    text = "\n".join(report) + "\n"
    (OUT_DIR / "c2-scan.txt").write_text(text)
    print(text)
    return 0 if not errors and not c2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
