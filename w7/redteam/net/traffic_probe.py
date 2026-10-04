#!/usr/bin/env python3
"""traffic_probe.py — minimal TCP throughput probe for the fan-network red-team.

Runs *inside* a Termini node container (no third-party deps). One process is the
server, one is the client; the client streams a fixed volume and reports achieved
throughput, completion, and any stall. Combined with `ping -f` (raw packet loss)
and `ss` it characterises the underlay path under tc/netem chaos.

  server: python3 traffic_probe.py server --port 9099 --seconds 60 --out /root/srv.json
  client: python3 traffic_probe.py client --host <ip> --port 9099 --mb 64 --timeout 30 --out /root/cli.json
"""
from __future__ import annotations

import argparse
import json
import socket
import time


def server(port: int, seconds: float, out: str) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)
    srv.settimeout(1.0)
    end = time.time() + seconds
    conns = 0
    total = 0
    while time.time() < end:
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        conns += 1
        c.settimeout(5.0)
        try:
            while True:
                data = c.recv(1 << 20)
                if not data:
                    break
                total += len(data)
        except OSError:
            pass
        finally:
            c.close()
    srv.close()
    json.dump({"role": "server", "port": port, "connections": conns, "bytes": total}, open(out, "w"))
    print(f"server done conns={conns} bytes={total}")
    return 0


def client(host: str, port: int, mb: int, timeout: float, out: str) -> int:
    total_bytes = mb * 1024 * 1024
    chunk = b"x" * 65536
    t0 = time.time()
    sent = 0
    err = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        while sent < total_bytes:
            n = s.send(chunk)
            sent += n
        s.shutdown(socket.SHUT_WR)
        # wait for server close
        try:
            s.recv(1)
        except OSError:
            pass
        s.close()
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0
    rec = {
        "role": "client", "host": host, "port": port,
        "target_bytes": total_bytes, "sent_bytes": sent,
        "elapsed_s": round(elapsed, 3),
        "mbps": round((sent * 8 / 1e6) / elapsed, 2) if elapsed > 0 else None,
        "completed": err is None and sent >= total_bytes,
        "error": err,
    }
    json.dump(rec, open(out, "w"))
    print(json.dumps(rec))
    return 0 if rec["completed"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("server")
    sp.add_argument("--port", type=int, default=9099)
    sp.add_argument("--seconds", type=float, default=60)
    sp.add_argument("--out", default="/tmp/srv.json")
    cp = sub.add_parser("client")
    cp.add_argument("--host", required=True)
    cp.add_argument("--port", type=int, default=9099)
    cp.add_argument("--mb", type=int, default=64)
    cp.add_argument("--timeout", type=float, default=30)
    cp.add_argument("--out", default="/tmp/cli.json")
    args = ap.parse_args()
    if args.cmd == "server":
        return server(args.port, args.seconds, args.out)
    return client(args.host, args.port, args.mb, args.timeout, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
