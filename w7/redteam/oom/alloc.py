#!/usr/bin/env python3
"""Controlled memory allocator for the kernel-OOM red-team probe.

Allocates `--chunk-mb` MiB every `--interval-ms`, touching every page so the
pages are really resident (anon memory, not just reservation), and holds the
total for `--hold-s`. Run it inside a memory-limited cgroup (via `systemd-run
-p MemoryMax=...`) so the kernel OOM-killer fires *inside that cgroup* and the
host is never at risk.

The allocation rate (`chunk_mb / interval_ms`) is what determines how long the
process spends between the 95 %-of-limit mark and the hard limit — i.e. the
detection window the controller actually has.
"""
from __future__ import annotations

import argparse
import sys
import time


def touch(buf: bytearray) -> None:
    step = 4096
    for i in range(0, len(buf), step):
        buf[i] = 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="controlled allocator (cgroup-OOM probe)")
    ap.add_argument("--chunk-mb", type=float, default=4.0)
    ap.add_argument("--interval-ms", type=float, default=100.0)
    ap.add_argument("--hold-s", type=float, default=20.0)
    args = ap.parse_args(argv)

    chunk = int(args.chunk_mb * 1024 * 1024)
    interval = max(0.0, args.interval_ms / 1000.0)
    bufs: list[bytearray] = []
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.hold_s:
            b = bytearray(chunk)
            touch(b)
            bufs.append(b)
            if interval:
                time.sleep(interval)
    except MemoryError:
        # Even without the kernel killer, fail safe.
        print("[alloc] MemoryError", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
