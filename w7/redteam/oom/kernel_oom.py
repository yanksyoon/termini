#!/usr/bin/env python3
"""Real kernel-OOM timing probe (Termini W7, task t_3dc57f17).

Answers the calibration question the whole OOM race hinges on:

    after guest RAM crosses the frozen OOM threshold (>= 95 % of the memory the
    kernel will let the workload use), how long does the kernel actually leave
    before it fires the OOM-killer?

Method (host-safe):
  * run the allocator inside a **transient systemd unit with `MemoryMax`** so the
    OOM-killer acts inside that cgroup only — the host and every other process
    are never at risk;
  * poll the unit's cgroup (`memory.current`, `memory.events`) at high frequency
    and record (a) the instant usage crosses 95 % of the limit (breach onset) and
    (b) the instant `oom_kill` increments (the kernel's `oom_reaper` reaping);
  * capture the raw kernel log lines for the kill (`dmesg`).

`--rate-sweep` runs several allocation rates so the report can state the window
as a function of ramp speed, not one anecdote.

Privileges: needs root to create the transient unit and read `dmesg`; it uses
`sudo` (non-interactive). If sudo/root is unavailable it prints a clear BLOCKED
result instead of pretending to have measured anything.

Usage:
    python3 w7/redteam/oom/kernel_oom.py --runs 5 --out /tmp/oom-evidence/kernel_oom.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
LIMIT = 256 * 1024 * 1024           # 256 MiB cgroup limit (host-safe)
BREACH_FRAC = 0.95
_OOM_RE = re.compile(r"out of memory|oom-kill|killed process|oom_reaper", re.I)


def _run(argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 255, f"{type(exc).__name__}: {exc}"


def _dmesg_lines() -> list[str]:
    rc, out = _run(["sudo", "dmesg"])
    if rc != 0:
        return []
    return out.splitlines()


def _read_int(path: pathlib.Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_events(path: pathlib.Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition(" ")
            out[k.strip()] = int(v.strip())
    except (OSError, ValueError):
        pass
    return out


def one_run(alloc_cmd: list[str], *, limit: int, sample_ms: int, hold_s: float,
            unit: str, tag: str, log=print) -> dict:
    cg = pathlib.Path(f"/sys/fs/cgroup/system.slice/{unit}.service")
    before_lines = _dmesg_lines()
    rc, out = _run([
        "sudo", "systemd-run", f"--unit={unit}",
        f"-p", f"MemoryMax={limit}",
        f"-p", "MemorySwapMax=0",
        f"-p", "MemoryAccounting=yes",
        "--",
    ] + alloc_cmd)
    if rc != 0:
        return {"error": f"systemd-run failed: {out.strip()}", "tag": tag}

    try:
        # wait for the cgroup to appear
        cur = cg / "memory.current"
        for _ in range(40):
            if cur.exists():
                break
            time.sleep(0.05)

        base_events = _read_events(cg / "memory.events")
        base_kill = base_events.get("oom_kill", 0)
        t0 = time.monotonic()
        breach_at = None
        kill_at = None
        exited = False
        max_cur = 0
        trace = []
        deadline = t0 + hold_s + 12.0
        while time.monotonic() < deadline:
            c = _read_int(cur)
            ev = _read_events(cg / "memory.events")
            now = time.monotonic() - t0
            if c is not None:
                max_cur = max(max_cur, c)
                trace.append({"t": round(now, 3), "mem": c})
                if breach_at is None and c >= BREACH_FRAC * limit:
                    breach_at = now
            if ev.get("oom_kill", 0) > base_kill:
                kill_at = now
                break
            # The unit exiting on its own is NOT an OOM kill: only the oom_kill
            # counter (or a dmesg line) proves the kernel fired. Record a clean
            # exit separately so it can never be mistaken for a kill.
            active = _run(["systemctl", "is-active", unit])[1].strip()
            if active in ("inactive", "failed") and c is None:
                exited = True
                break
            time.sleep(sample_ms / 1000.0)

        after_lines = _dmesg_lines()
        new_lines = after_lines[len(before_lines):] if len(after_lines) >= len(before_lines) else after_lines
        oom_lines = [l for l in new_lines if _OOM_RE.search(l)]
        if oom_lines and kill_at is None:
            kill_at = time.monotonic() - t0  # dmesg proves it even if we missed the counter
        if oom_lines and breach_at is None:
            # killed faster than the sampling grid: the window is shorter than one
            # sample; report the last observed pre-kill sample as the upper bound.
            pre = [s for s in trace if s["mem"] < BREACH_FRAC * limit]
            if pre:
                breach_at = pre[-1]["t"]

        # Simulate the controller's 1 s debounce over the trace: how many
        # consecutive 1-second samples sit above the threshold before the kill?
        grid = _downsample_1s(trace, t_end=kill_at if kill_at is not None else max((s["t"] for s in trace), default=0))
        run_len = 0
        max_run = 0
        for v in grid:
            if v is not None and v >= BREACH_FRAC * limit:
                run_len += 1
                max_run = max(max_run, run_len)
            else:
                run_len = 0

        window = None
        breach_observed = max_cur >= BREACH_FRAC * limit
        if breach_at is not None and kill_at is not None:
            window = round(kill_at - breach_at, 3)
        return {
            "tag": tag,
            "limit_bytes": limit,
            "breach_at_s": round(breach_at, 3) if breach_at is not None else None,
            "kill_at_s": round(kill_at, 3) if kill_at is not None else None,
            "window_s": window,
            "window_is_upper_bound": bool(oom_lines and not breach_observed),
            "breach_observed_at_sampling": breach_observed,
            "exited_cleanly": exited,
            "max_bytes": max_cur,
            "max_pct_of_limit": round(max_cur / limit * 100, 1),
            "oom_kill_seen": bool(kill_at is not None),
            "consecutive_breach_samples_1s": max_run,
            "operator_would_fire": max_run >= 2,
            "kernel_oom_lines": oom_lines[-4:],
        }
    finally:
        _run(["sudo", "systemctl", "stop", unit])
        _run(["sudo", "systemctl", "reset-failed", unit])


def _downsample_1s(trace: list[dict], t_end: float) -> list[int | None]:
    if not trace:
        return []
    out = []
    t = 0.0
    idx = 0
    # sample the trace at 1s grid points
    while t <= t_end:
        # value at or just before t
        val = None
        while idx < len(trace) and trace[idx]["t"] <= t:
            val = trace[idx]["mem"]
            idx += 1
        out.append(val)
        t += 1.0
    return out


def rate(alloc_cmd_tag: str) -> str:
    return alloc_cmd_tag


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="real kernel-OOM timing probe")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--limit-mb", type=int, default=256)
    ap.add_argument("--sample-ms", type=int, default=20)
    ap.add_argument("--hold-s", type=float, default=0.0,
                    help="0 = derive per rate from the time needed to fill the limit")
    ap.add_argument("--out", default="/tmp/oom-evidence/kernel_oom.json")
    ap.add_argument("--rates", default="80,20,7",
                    help="comma allocation rates in MB/s for the sweep")
    args = ap.parse_args(argv)

    limit = args.limit_mb * 1024 * 1024
    # (label, alloc_cmd, hold_s) for each rate: chunk/interval chosen to hit MB/s
    configs = []
    for r in args.rates.split(","):
        mbps = float(r)
        interval_ms = 100.0
        chunk_mb = max(0.25, mbps * interval_ms / 1000.0)
        hold = args.hold_s or min(45.0, args.limit_mb / max(0.1, mbps) + 5.0)
        alloc = [sys.executable, str(HERE / "alloc.py"),
                 "--chunk-mb", f"{chunk_mb:.2f}", "--interval-ms", f"{interval_ms:.0f}",
                 "--hold-s", str(hold)]
        configs.append((f"{mbps:g}MB/s", alloc, hold))
    # plus the aggressive stress-ng profile the scenario script actually uses
    hold = args.hold_s or 20.0
    configs.append(("stress-ng --vm 1", [
        "/usr/bin/stress-ng", "--vm", "1", "--vm-bytes", "512M", "--vm-keep",
        "--timeout", f"{int(hold)}s", "--quiet"], hold))

    records = []
    for label, cmd, hold in configs:
        for i in range(args.runs):
            unit = f"oomrt-{abs(hash(label)) % 10000}-{i}"
            rec = one_run(cmd, limit=limit, sample_ms=args.sample_ms,
                          hold_s=hold, unit=unit, tag=label)
            rec["run"] = i
            records.append(rec)
            print(f"  {label} run={i}: window={rec.get('window_s')}s "
                  f"oom_kill={rec.get('oom_kill_seen')} "
                  f"consec1s={rec.get('consecutive_breach_samples_1s')} "
                  f"fire={rec.get('operator_would_fire')}")
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, indent=2))
    print(f"\nwrote {out}")
    # summary
    print("summary by rate:")
    labels = sorted({r.get("tag") for r in records})
    for lb in labels:
        rs = [r for r in records if r.get("tag") == lb and r.get("window_s") is not None]
        if not rs:
            print(f"  {lb}: no measurable kill")
            continue
        ws = sorted(r["window_s"] for r in rs)
        fires = sum(1 for r in rs if r.get("operator_would_fire"))
        print(f"  {lb}: n={len(rs)} window_s min={ws[0]} median={ws[len(ws)//2]} "
              f"max={ws[-1]}  operator_would_fire={fires}/{len(rs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
