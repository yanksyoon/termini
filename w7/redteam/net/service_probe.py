#!/usr/bin/env python3
"""service_probe.py — the real inter-node service call used by the fan-network
red-team: an agent VM / node on Node A calling the host-exposed llama.cpp
OpenAI endpoint on Node C (D5), per docs/contracts/agent-runtime.md.

Modes:
  chat    N sequential non-streaming /v1/chat/completions calls, with the
          contract's retry policy (connect timeout 2 s, retry <= 2 with jitter)
  stream  one (or more) streaming /v1/chat/completions calls; records whether the
          SSE stream completed ([DONE]) or was cut mid-flight (kill-node test)

Runs inside a node container (stdlib only).

  python3 service_probe.py chat --host <ip> --port 8080 --n 20 --max-tokens 8 --out /root/chat.json
  python3 service_probe.py stream --host <ip> --port 8080 --n 3 --max-tokens 32 --out /root/stream.json
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import time
import urllib.error
import urllib.request

MODEL = "deepseek-flash"


def _one_chat(host: str, port: int, max_tokens: int, timeout: float) -> dict:
    url = f"http://{host}:{port}/v1/chat/completions"
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": "reply with one word"}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            dt = time.time() - t0
            ok = r.status == 200
            tokens = 0
            try:
                tokens = len(json.loads(payload).get("choices", [{}])[0].get("message", {}).get("content", "").split())
            except Exception:  # noqa: BLE001
                pass
            return {"ok": ok, "http": r.status, "latency_s": round(dt, 3), "tokens": tokens, "error": None}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "latency_s": round(time.time() - t0, 3),
                "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "http": None, "latency_s": round(time.time() - t0, 3),
                "error": f"{type(e).__name__}: {e}"}


def cmd_chat(host, port, n, max_tokens, timeout, out):
    calls = []
    for i in range(n):
        attempts = []
        final = None
        for attempt in range(3):  # 1 try + <=2 retries (agent-runtime.md)
            res = _one_chat(host, port, max_tokens, timeout)
            attempts.append(res)
            if res["ok"]:
                final = res
                break
            if attempt < 2:
                time.sleep(random.uniform(0.1, 0.3))
        calls.append({"i": i, "attempts": len(attempts), "final": final or attempts[-1],
                      "per_attempt": attempts})
    ok = sum(1 for c in calls if c["final"]["ok"])
    retried = sum(1 for c in calls if c["attempts"] > 1)
    failed = n - ok
    lat = [c["final"]["latency_s"] for c in calls if c["final"]["ok"]]
    rec = {"role": "chat", "host": host, "n": n, "ok": ok, "failed": failed,
           "retried_calls": retried,
           "latency_s": ({"min": min(lat), "max": max(lat),
                          "mean": round(sum(lat) / len(lat), 3)} if lat else {}),
           "calls": calls}
    json.dump(rec, open(out, "w"), indent=2)
    print(json.dumps({k: rec[k] for k in ("role", "n", "ok", "failed", "retried_calls", "latency_s")}))
    return 0


def _one_stream(host: str, port: int, max_tokens: int, timeout: float) -> dict:
    url = f"http://{host}:{port}/v1/chat/completions"
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "stream": True,
                       "messages": [{"role": "user", "content": "count slowly from 1 to 40"}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    chunks = 0
    saw_done = False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                line = r.readline()
                if not line:
                    break
                chunks += 1
                if b"[DONE]" in line:
                    saw_done = True
                    break
        return {"completed": saw_done, "chunks": chunks, "latency_s": round(time.time() - t0, 3), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"completed": False, "chunks": chunks, "latency_s": round(time.time() - t0, 3),
                "error": f"{type(e).__name__}: {e}"}


def cmd_stream(host, port, n, max_tokens, timeout, out):
    runs = []
    for i in range(n):
        res = _one_stream(host, port, max_tokens, timeout)
        res["i"] = i
        runs.append(res)
        print(f"stream {i}: completed={res['completed']} chunks={res['chunks']} "
              f"latency={res['latency_s']}s err={res['error']}")
    cut = sum(1 for r in runs if not r["completed"])
    rec = {"role": "stream", "host": host, "n": n, "completed": n - cut, "cut": cut, "runs": runs}
    json.dump(rec, open(out, "w"), indent=2)
    print(json.dumps({k: rec[k] for k in ("role", "n", "completed", "cut")}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("chat", "stream"):
        p = sub.add_parser(name)
        p.add_argument("--host", required=True)
        p.add_argument("--port", type=int, default=8080)
        p.add_argument("--n", type=int, default=10)
        p.add_argument("--max-tokens", type=int, default=8)
        p.add_argument("--timeout", type=float, default=30)
        p.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "chat":
        return cmd_chat(args.host, args.port, args.n, args.max_tokens, args.timeout, args.out)
    return cmd_stream(args.host, args.port, args.n, args.max_tokens, args.timeout, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
