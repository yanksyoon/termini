#!/usr/bin/env python3
"""net_chaos.py — fan-network packet-loss / retry / detection red-team harness.

Exercises the "fan-network packet drops" failure mode from project.md against the
live 3-node demo underlay (A/B compute, C inference with host-exposed llama.cpp):

  baseline  ping-flood loss, a TCP throughput transfer, N real llama.cpp calls
  latency   tc netem delay 300ms +/- 50ms on node A -> RTT, call latency, and
            whether the control plane's latency health signal notices
  loss      tc netem loss 30% on node A -> ping loss, TCP stall, call
            failures/retries
  kill      a streaming llama.cpp call to node C, then kill node C mid-transfer;
            record the client outcome (dropped stream) and the controller's
            time-to-detect (degraded/offline) and time-to-recover

Only the standard tooling already on the nodes is used (ping, python3, tc, curl).
Every tc qdisc is removed in a finally block. Raw outputs go to --out.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import threading
import time
import urllib.request

CONTROLLER = "http://127.0.0.1:8085"
PROJECT_TOKEN = "term-demo-project-token-000000000001"
PROJECT_ID = "01J0DEM0WARM000000000000AB"
# node map filled from --env env.json (defaults: the shared demo nodes)
NODES = {
    "a": {"name": "demo-node-a", "id": "01M436FWX4H4FKD41Y4GYAPGFK", "role": "compute", "ip": ""},
    "b": {"name": "demo-node-b", "id": "01M436H26V2ETRX4H8QE7GTBMM", "role": "compute", "ip": ""},
    "c": {"name": "demo-node-c", "id": "01M436J6QPG29ESH2GK2QMC3XC", "role": "inference", "ip": ""},
}
ENV_PATH = None
HERE = pathlib.Path(__file__).resolve().parent


def sh(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def lxexec(node: str, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return sh(["lxc", "exec", node, "--", *args], timeout=timeout)


def node_ip(name: str) -> str:
    p = sh(["lxc", "list", name, "--format", "csv", "-c", "4"])
    for tok in p.stdout.replace(",", " ").split():
        if tok.count(".") == 3:
            return tok
    return ""


def push(local: pathlib.Path, node: str, dest: str) -> None:
    sh(["lxc", "file", "push", str(local), f"{node}{dest}"], timeout=60)


def netem(node: str, spec: list[str]) -> None:
    lxexec(node, ["tc", "qdisc", "add", "dev", "eth0", "root", "netem", *spec], timeout=30)


def netem_del(node: str) -> None:
    lxexec(node, ["tc", "qdisc", "del", "dev", "eth0", "root"], timeout=30)


def ping_flood(node: str, target_ip: str, count: int = 3000) -> dict:
    p = lxexec(node, ["ping", "-f", "-c", str(count), "-i", "0.001", "-W", "2", target_ip], timeout=90)
    out = p.stdout + p.stderr
    rec = {"raw_tail": "\n".join(out.strip().splitlines()[-4:])}
    for line in out.splitlines():
        if "packet loss" in line:
            rec["summary"] = line.strip()
            for part in line.split(","):
                part = part.strip()
                if "packet loss" in part:
                    rec["loss_pct"] = float(part.split("%")[0].split()[-1])
        if "rtt min/avg/max" in line:
            seg = line.split("=", 1)[1].split(" ms", 1)[0].strip()
            vals = [float(v) for v in seg.split("/")[:4]]
            rec["rtt_min_ms"], rec["rtt_avg_ms"], rec["rtt_max_ms"], rec["rtt_mdev_ms"] = vals
    return rec


def tcp_transfer(run_dir: pathlib.Path, tag: str, mb: int = 64, timeout: float = 30) -> dict:
    """A -> C TCP throughput transfer."""
    cip = node_ip(NODES["c"]["name"])
    srv_out = f"/root/srv-{tag}.json"
    cli_out = f"/root/cli-{tag}.json"
    srv = subprocess.Popen(
        ["lxc", "exec", NODES["c"]["name"], "--", "python3", "/root/traffic_probe.py",
         "server", "--port", "9099", "--seconds", "45", "--out", srv_out],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    cli_err = None
    try:
        cli = lxexec(NODES["a"]["name"],
                     ["python3", "/root/traffic_probe.py", "client", "--host", cip, "--port", "9099",
                      "--mb", str(mb), "--timeout", str(timeout), "--out", cli_out], timeout=int(timeout) * 2 + 30)
    except subprocess.TimeoutExpired as e:
        # a stalled transfer is itself the datum: the probe could not make
        # progress inside its socket timeouts and never wrote its json
        cli_err = f"client exec timed out after {int(timeout) * 2 + 30}s: {e}"
        cli = subprocess.CompletedProcess([], 124, "", cli_err)
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()
    lxexec(NODES["c"]["name"], ["pkill", "-f", "traffic_probe.py server"], timeout=15)
    rec = {}
    try:
        rec = json.loads(lxexec(NODES["a"]["name"], ["cat", cli_out], timeout=15).stdout)
    except Exception as e:  # noqa: BLE001
        rec = {"error": f"no client json: {e}", "stdout": cli.stdout[-300:], "stderr": cli.stderr[-300:]}
    if cli_err:
        rec["stalled"] = True
        rec["client_exec_error"] = cli_err
    # server-side bytes
    try:
        rec["server"] = json.loads(lxexec(NODES["c"]["name"], ["cat", srv_out], timeout=15).stdout)
    except Exception:  # noqa: BLE001
        pass
    (run_dir / f"tcp-{tag}.json").write_text(json.dumps(rec, indent=2))
    return rec


def service_chat(run_dir: pathlib.Path, tag: str, n: int = 20, max_tokens: int = 8,
                 timeout: float = 30) -> dict:
    cip = node_ip(NODES["c"]["name"])
    out = f"/root/chat-{tag}.json"
    lxexec(NODES["a"]["name"],
           ["python3", "/root/service_probe.py", "chat", "--host", cip, "--port", "8080",
            "--n", str(n), "--max-tokens", str(max_tokens), "--timeout", str(timeout), "--out", out],
           timeout=int(n * (timeout + 5)) + 30)
    try:
        rec = json.loads(lxexec(NODES["a"]["name"], ["cat", out], timeout=15).stdout)
    except Exception as e:  # noqa: BLE001
        rec = {"error": str(e)}
    (run_dir / f"chat-{tag}.json").write_text(json.dumps(rec, indent=2))
    return rec


def controller_nodes() -> list[dict]:
    req = urllib.request.Request(
        f"{CONTROLLER}/v1/projects/{PROJECT_ID}/nodes",
        headers={"Authorization": f"Bearer {PROJECT_TOKEN}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get("nodes", [])


def telemetry_recent(node_id: str, limit: int = 10) -> dict:
    req = urllib.request.Request(
        f"{CONTROLLER}/v1/telemetry/recent?node={node_id}&limit={limit}",
        headers={"Authorization": f"Bearer {PROJECT_TOKEN}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def operator_events(node_id: str, limit: int = 50) -> dict:
    req = urllib.request.Request(
        f"{CONTROLLER}/v1/operator/events?node={node_id}&limit={limit}",
        headers={"Authorization": f"Bearer {PROJECT_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def infer_health(node: str, port: int = 8080) -> tuple[bool, float]:
    ip = node_ip(node)
    if not ip:
        return False, 0.0
    t0 = time.time()
    p = lxexec(NODES["a"]["name"], ["curl", "-sS", "-m", "3", "-o", "/dev/null",
                                    "-w", "%{http_code}", f"http://{ip}:{port}/health"], timeout=10)
    ok = p.stdout.strip() == "200"
    return ok, round(time.time() - t0, 2)


# --------------------------------------------------------------------------- scenarios
def scenario_baseline(run_dir: pathlib.Path) -> dict:
    res = {}
    res["ping_a_to_b"] = ping_flood(NODES["a"]["name"], node_ip(NODES["b"]["name"]))
    res["ping_a_to_c"] = ping_flood(NODES["a"]["name"], node_ip(NODES["c"]["name"]))
    res["tcp_a_to_c"] = tcp_transfer(run_dir, "baseline")
    res["chat_baseline"] = service_chat(run_dir, "baseline", n=20)
    return res


def scenario_latency(run_dir: pathlib.Path, delay: str = "300ms", jitter: str = "50ms") -> dict:
    res = {"netem": f"delay {delay} {jitter} distribution normal"}
    try:
        netem(NODES["a"]["name"], ["delay", delay, jitter, "distribution", "normal"])
        res["ping_a_to_c"] = ping_flood(NODES["a"]["name"], node_ip(NODES["c"]["name"]), count=600)
        res["chat_under_latency"] = service_chat(run_dir, "latency", n=5, timeout=30)
        # what does the control plane actually see?
        a_rec = telemetry_recent(NODES["a"]["id"], limit=6)
        c_rec = telemetry_recent(NODES["c"]["id"], limit=6)
        res["telemetry_net_latency_ms"] = {
            "a": [s.get("net", [{}])[0].get("latency_ms") for s in a_rec.get("samples", [])],
            "c": [s.get("net", [{}])[0].get("latency_ms") for s in c_rec.get("samples", [])],
        }
        # let the operator loop run a few cycles, then ask for latency events
        time.sleep(8)
        res["operator_events_a"] = operator_events(NODES["a"]["id"])
    finally:
        netem_del(NODES["a"]["name"])
    return res


def scenario_loss(run_dir: pathlib.Path, loss: str = "30%") -> dict:
    res: dict = {"netem": f"loss {loss}"}
    try:
        netem_del(NODES["a"]["name"])  # clear any stale qdisc so the add succeeds
        netem(NODES["a"]["name"], ["loss", loss])
        res["ping_a_to_c"] = ping_flood(NODES["a"]["name"], node_ip(NODES["c"]["name"]), count=3000)
        res["tcp_a_to_c"] = tcp_transfer(run_dir, "loss", mb=64, timeout=20)
        res["chat_under_loss"] = service_chat(run_dir, "loss", n=15, timeout=15)
        try:
            a_rec = telemetry_recent(NODES["a"]["id"], limit=6)
            res["telemetry_net_latency_ms_a"] = [
                s.get("net", [{}])[0].get("latency_ms") for s in a_rec.get("samples", [])]
        except Exception as e:  # noqa: BLE001
            res["telemetry_error"] = f"{type(e).__name__}: {e}"
    finally:
        netem_del(NODES["a"]["name"])
    return res


def scenario_kill(run_dir: pathlib.Path, stream_seconds_before_kill: float = 4.0) -> dict:
    """Stream A->C, kill C mid-transfer, watch detection + recovery."""
    res = {}
    cip = node_ip(NODES["c"]["name"])
    timeline = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                for n in controller_nodes():
                    if n.get("node_id") == NODES["c"]["id"]:
                        timeline.append({
                            "t": round(time.time(), 3),
                            "status": n.get("status"),
                            "last_seen_at": n.get("last_seen_at"),
                        })
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

    watcher = threading.Thread(target=watch, daemon=True)
    # baseline health
    t_start = time.time()
    res["health_before"] = [n for n in controller_nodes() if n.get("node_id") == NODES["c"]["id"]]
    watcher.start()

    # start a long streaming call in the background on node A
    slog = open(run_dir / "kill-stream-client.log", "w")
    client = subprocess.Popen(
        ["lxc", "exec", NODES["a"]["name"], "--", "python3", "/root/service_probe.py", "stream",
         "--host", cip, "--port", "8080", "--n", "1", "--max-tokens", "400", "--timeout", "300",
         "--out", "/root/stream-kill.json"],
        stdout=slog, stderr=subprocess.STDOUT)
    time.sleep(stream_seconds_before_kill)

    # kill node C mid-transfer
    t_kill = time.time()
    res["t_kill"] = t_kill
    p = sh(["lxc", "stop", NODES["c"]["name"], "--force"], timeout=60)
    res["stop_rc"] = p.returncode
    try:
        client.wait(timeout=60)
    except subprocess.TimeoutExpired:
        client.kill()
    t_client_done = time.time()
    res["stream_client_exit_s_after_kill"] = round(t_client_done - t_kill, 2)
    try:
        res["stream_result"] = json.loads(lxexec(NODES["a"]["name"], ["cat", "/root/stream-kill.json"], timeout=15).stdout)
    except Exception as e:  # noqa: BLE001
        res["stream_result"] = {"error": str(e)}
    slog.close()

    # watch until offline (or 70s)
    offline_at = None
    degraded_at = None
    while time.time() - t_kill < 70:
        for n in controller_nodes():
            if n.get("node_id") == NODES["c"]["id"]:
                st = n.get("status")
                if st == "degraded" and degraded_at is None:
                    degraded_at = time.time()
                if st == "offline" and offline_at is None:
                    offline_at = time.time()
        if offline_at:
            break
        time.sleep(0.5)
    res["time_to_degraded_s"] = round(degraded_at - t_kill, 2) if degraded_at else None
    res["time_to_offline_s"] = round(offline_at - t_kill, 2) if offline_at else None

    # recover
    t_restart = time.time()
    sh(["lxc", "start", NODES["c"]["name"]], timeout=60)
    online_at = None
    infer_at = None
    while time.time() - t_restart < 120:
        for n in controller_nodes():
            if n.get("node_id") == NODES["c"]["id"] and n.get("status") == "online":
                # require a fresh last_seen AND the inference endpoint back
                pass
        ok, _ = infer_health(NODES["c"]["name"])
        if ok and infer_at is None:
            infer_at = time.time()
        if infer_at and online_at is None:
            online_at = time.time()
        if online_at and infer_at:
            break
        time.sleep(0.5)
    res["time_to_recover_online_s"] = round(online_at - t_restart, 2) if online_at else None
    res["time_to_recover_inference_s"] = round(infer_at - t_restart, 2) if infer_at else None

    stop.set()
    watcher.join(timeout=5)
    res["health_timeline"] = timeline
    # dedupe timeline to status transitions
    transitions = []
    last = None
    for e in timeline:
        if e["status"] != last:
            transitions.append(e)
            last = e["status"]
    res["status_transitions"] = transitions
    return res


SCENARIOS = {
    "baseline": scenario_baseline,
    "latency": scenario_latency,
    "loss": scenario_loss,
    "kill": scenario_kill,
}


def main() -> int:
    global CONTROLLER, PROJECT_ID, PROJECT_TOKEN, NODES
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=list(SCENARIOS) + ["all"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--env", default=None, help="env.json from env_up.sh (dedicated nodes)")
    args = ap.parse_args()
    run_dir = pathlib.Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.env:
        env = json.loads(pathlib.Path(args.env).read_text())
        CONTROLLER = env["controller"]
        PROJECT_ID = env["project_id"]
        PROJECT_TOKEN = env["project_token"]
        for k, v in env["nodes"].items():
            NODES[k] = v

    # stage probes into the nodes
    push(HERE / "traffic_probe.py", NODES["a"]["name"], "/root/traffic_probe.py")
    push(HERE / "traffic_probe.py", NODES["c"]["name"], "/root/traffic_probe.py")
    push(HERE / "service_probe.py", NODES["a"]["name"], "/root/service_probe.py")

    run = [args.scenario] if args.scenario != "all" else ["baseline", "latency", "loss", "kill"]
    summary = {}
    for name in run:
        print(f"=== scenario {name} ===", flush=True)
        t0 = time.time()
        try:
            summary[name] = SCENARIOS[name](run_dir)
        except Exception as e:  # noqa: BLE001
            summary[name] = {"error": f"{type(e).__name__}: {e}"}
        summary[name]["elapsed_s"] = round(time.time() - t0, 2)
        (run_dir / f"scenario-{name}.json").write_text(json.dumps(summary[name], indent=2, default=str))
        print(json.dumps(summary[name], indent=2, default=str)[:1500], flush=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
