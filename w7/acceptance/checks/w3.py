"""W3 — Bare-metal bring-up on cloud VMs (LXD cluster + networking).

Independent infra observation via the LXD CLI and cross-node HTTP, never the
implementer's own verify scripts.
"""
from __future__ import annotations

import json

from lib.envhash import lxc_list
from . import CheckContext, result, run

WS = "W3 — Bare-metal bring-up on cloud VMs (LXD cluster + networking)"


def _container_ips() -> dict:
    out = {}
    for c in lxc_list():
        out[c.get("name")] = c.get("ipv4")
    return out


def check_w3_1(ctx: CheckContext) -> dict:
    criterion = "a fresh node registers with the controller in <5s"
    method = "measure controller registration latency (from the same synthetic registration as W2.1)"
    fresh = ctx.state.get("fresh_node") or {}
    latency = fresh.get("latency_ms")
    if latency is None:
        art = ctx.ev.record("W3.1", "W3.1 registration latency (skipped)",
                            "POST /v1/nodes/register", "no registration performed")
        return result("W3.1", WS, criterion, method, "skipped", "no registration performed",
                      art, reason="registration skipped (no bootstrap token); cannot measure <5s")
    raw = json.dumps({"machine_id": fresh.get("machine_id"),
                      "latency_ms": round(latency, 1), "status": fresh.get("status"),
                      "role": fresh.get("role")}, indent=2)
    art = ctx.ev.record("W3.1", "W3.1 fresh-node registration latency",
                        "POST /v1/nodes/register (timed)", raw)
    ok = latency < 5000.0
    status = fresh.get("status")
    # A fast failure is not a registration. Gating on latency alone reported PASS for
    # a registration that never happened: observed status=401 (bootstrap token already
    # consumed) with latency=2.6ms -> "pass". Require the registration to have
    # actually succeeded before timing it means anything.
    succeeded = status in (200, 201) and bool(fresh.get("role"))
    ok = ok and succeeded
    if not ok and not succeeded:
        reason = (f"registration did not succeed (status={status!r}, role={fresh.get('role')!r}) — "
                  f"latency={latency:.0f}ms measures a failed call, not a registration")
    else:
        reason = None if ok else f"registration took {latency:.0f}ms >= 5s"
    return result("W3.1", WS, criterion, method,
                  "pass" if ok else "fail",
                  f"registration status={status!r} role={fresh.get('role')!r} latency={latency:.0f}ms (budget 5000ms)",
                  art, reason=reason)


def check_w3_2(ctx: CheckContext) -> dict:
    criterion = "VMs on Node A and Node C share one CIDR"
    method = "inspect guest instances inside Node A / Node C via LXD; compare their IPv4 subnets"
    ips = _container_ips()
    a_ip = ips.get("demo-node-a")
    c_ip = ips.get("demo-node-c")

    guest_a = run("timeout 15 lxc exec demo-node-a -- lxc list 2>&1")
    guest_c = run("timeout 15 lxc exec demo-node-c -- lxc list 2>&1")

    raw = f"node_a_ip={a_ip}\nnode_c_ip={c_ip}\n\n--- lxc list inside demo-node-a ---\n{guest_a}\n\n--- lxc list inside demo-node-c ---\n{guest_c}"
    art = ctx.ev.record("W3.2", "W3.2 guest VM CIDR", "lxc list (nested)", raw)

    # Parse guest IPv4s from the nested lists (best effort). An empty list means
    # no guest instances exist on that node.
    import re
    ips_a = re.findall(r"(\d+\.\d+\.\d+\.\d+)", guest_a)
    ips_c = re.findall(r"(\d+\.\d+\.\d+\.\d+)", guest_c)

    # No guest instances on either node -> cannot verify shared CIDR.
    if not ips_a and not ips_c:
        reason = "no guest VMs provisioned on Node A or Node C yet"
        if "Installing LXD snap" in guest_a or "Installing LXD snap" in guest_c:
            reason = "nodes' internal LXD is still installing; no guest VMs provisioned"
        return result("W3.2", WS, criterion, method, "skipped",
                      f"guest IPv4s: A={ips_a} C={ips_c}", art, reason=reason)

    # Guests on one node only -> no shared CIDR to compare.
    if not ips_a or not ips_c:
        return result("W3.2", WS, criterion, method, "skipped",
                      f"guests on one node only: A={ips_a} C={ips_c}",
                      art, reason="guest VMs present on only one of Node A / Node C; no shared CIDR to compare")

    def cidr(ip): return ".".join(ip.split(".")[:3]) + ".0/24"
    cidrs_a = {cidr(i) for i in ips_a}
    cidrs_c = {cidr(i) for i in ips_c}
    shared = bool(cidrs_a & cidrs_c)

    return result("W3.2", WS, criterion, method,
                  "pass" if shared else "fail",
                  f"node_a guest CIDRs={cidrs_a} node_c guest CIDRs={cidrs_c}",
                  art, reason=None if shared else "no common guest CIDR found between Node A and Node C")


def check_w3_3(ctx: CheckContext) -> dict:
    criterion = "llama.cpp on Node C is reachable from Node A over HTTP"
    method = "from inside Node A container, HTTP GET http://<node-c-ip>:8080/health"
    ips = _container_ips()
    a_ip = ips.get("demo-node-a")
    c_ip = ips.get("demo-node-c")

    if not a_ip or not c_ip:
        raw = f"container ips: {ips}"
        art = ctx.ev.record("W3.3", "W3.3 llama reachability (skipped)", "lxc list", raw)
        return result("W3.3", WS, criterion, method, "skipped", f"cannot resolve node IPs: {ips}",
                      art, reason="could not resolve Node A / Node C container IPs")

    out = run(f"timeout 20 lxc exec demo-node-a -- curl -s -m 10 -w '\\nHTTP_CODE=%{{http_code}}\\n' http://{c_ip}:8080/health 2>&1")
    raw = f"from_node_a={a_ip} -> node_c={c_ip}:8080/health\n\n{out}"
    art = ctx.ev.record("W3.3", "W3.3 llama.cpp reachability from Node A",
                        f"lxc exec demo-node-a -- curl http://{c_ip}:8080/health", raw)

    ok = "HTTP_CODE=200" in out and '"status":"ok"' in out
    if ok:
        return result("W3.3", WS, criterion, method, "pass",
                      f"Node A ({a_ip}) reached llama.cpp on Node C ({c_ip}) -> 200 ok", art)
    return result("W3.3", WS, criterion, method, "fail", out[:300], art,
                  reason="llama.cpp /health on Node C was not reachable from Node A over HTTP")
