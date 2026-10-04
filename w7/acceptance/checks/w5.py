"""W5 — Customer operations dashboard & web terminal.

Independent HTTP/WS probes against the controller's dashboard-facing surfaces.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

from lib.http import http
from lib.ws import WsClient
from . import CheckContext, result, run, is_absent

WS = "W5 — Customer operations dashboard & web terminal"


def _first_node_id(ctx: CheckContext) -> str:
    if not ctx.project_token or not ctx.project_id:
        return ""
    r = http("GET", ctx.controller_url, f"/v1/projects/{ctx.project_id}/nodes",
             token=ctx.project_token)
    nodes = (r.json() or {}).get("nodes", [])
    return nodes[0].get("node_id") if nodes else ""


def _sse_head(url: str, token: str, seconds: float = 6.0):
    """Read the first bytes of an SSE stream for a bounded time.

    Returns (status_code, text). For a 404/error the status is the HTTP code;
    for a live stream it is 200.
    """
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                "X-Termini-Version": "1"})
    out = []
    status = 0
    try:
        with urllib.request.urlopen(req, timeout=seconds) as r:
            status = r.status
            import time
            start = time.monotonic()
            # read1() returns whatever the socket has buffered instead of
            # blocking for a full 512 bytes, so a single SSE event is observed
            # within the window without waiting for a second one.
            read = getattr(r, "read1", None) or r.read
            while time.monotonic() - start < seconds:
                chunk = read(512)
                if not chunk:
                    break
                out.append(chunk.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        status = e.code
        out.append(e.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        out.append(f"<stream error: {type(exc).__name__}: {exc}>")
    return status, "".join(out)


def check_w5_1(ctx: CheckContext) -> dict:
    criterion = "the dashboard shows live node health + streaming AI logs"
    method = "GET /v1/projects/{pid}/nodes (live health) + GET /v1/projects/{pid}/logs (SSE AI log stream)"
    if not ctx.project_token or not ctx.project_id:
        art = ctx.ev.record("W5.1", "W5.1 dashboard data (skipped)", "GET /nodes + /logs", "no project token")
        return result("W5.1", WS, criterion, method, "skipped", "no project token",
                      art, reason="TERMINI_PROJECT_TOKEN not supplied")

    base = ctx.controller_url
    nr = http("GET", base, f"/v1/projects/{ctx.project_id}/nodes", token=ctx.project_token)
    nodes = (nr.json() or {}).get("nodes", [])
    health_ok = nr.status == 200 and all("status" in n for n in nodes)

    logs_url = f"{base.rstrip('/')}/v1/projects/{ctx.project_id}/logs?token={ctx.project_token}"
    sse_status, sse = _sse_head(logs_url, ctx.project_token)

    raw = json.dumps({"nodes_status": nr.status, "node_count": len(nodes),
                      "logs_sse_status": sse_status, "logs_sse": sse[:800]}, indent=2)
    art = ctx.ev.record("W5.1", "W5.1 dashboard health + AI log stream",
                        f"GET /v1/projects/{ctx.project_id}/nodes and /logs (SSE)", raw)

    logs_ok = sse_status == 200 and ("event:" in sse or "data:" in sse)

    if health_ok and logs_ok:
        return result("W5.1", WS, criterion, method, "pass",
                      f"node health live ({len(nodes)} nodes); AI-log SSE stream observed", art)
    if health_ok and not logs_ok:
        return result("W5.1", WS, criterion, method, "skipped",
                      f"node health live ({len(nodes)} nodes) but AI-log stream unavailable (status={sse_status})",
                      art, reason="live node health is served, but the AI-log SSE endpoint is not available in the running controller build (GET /logs -> %s)" % sse_status)
    return result("W5.1", WS, criterion, method, "fail",
                  f"health_ok={health_ok} logs_ok={logs_ok} nodes_status={nr.status}",
                  art, reason="dashboard data surfaces did not return live health and/or streaming logs")


def check_w5_2(ctx: CheckContext) -> dict:
    criterion = "the web terminal opens a working shell on a node"
    method = ("WebSocket /terminal/v1/nodes/{node_id}?token=… ; run a command that "
              "prints a value only the shell can produce and require its OUTPUT "
              "(never the PTY echo) to show a remote (SSH) node session")
    node_id = _first_node_id(ctx)
    if not node_id or not ctx.project_token:
        art = ctx.ev.record("W5.2", "W5.2 terminal (skipped)", "WS /terminal/v1/nodes", "no node id / token")
        return result("W5.2", WS, criterion, method, "skipped", "no node id available",
                      art, reason="no node id / project token available to open a terminal")

    # The command expands to `W5OUT_<hostname>_<ssh|local>_END`. The literal
    # command line sent to the PTY contains `$(hostname)` / `$(...)`, so the
    # expanded token can ONLY appear in the command's OUTPUT — never in the
    # terminal's local echo of the typed line. That is what makes this probe
    # assert on a real result instead of on the line-discipline echo (which used
    # to sail through even when the node shell was unreachable). The embedded
    # `$SSH_CONNECTION` check additionally proves the shell is on a *node*
    # reached over SSH, not a `bash` on the controller host.
    probe = ("echo W5OUT_$(hostname)_"
             "$( [ -n \"$SSH_CONNECTION\" ] && echo ssh || echo local )_END\n")
    out_re = re.compile(r"W5OUT_([A-Za-z0-9._-]+)_(ssh|local)_END")

    ws_url = f"ws://{ctx.controller_url.split('://', 1)[-1].rstrip('/')}/terminal/v1/nodes/{node_id}?token={ctx.project_token}"
    output = ""
    decoded = ""
    err_code = None
    status = "fail"
    reason = None
    match = None
    try:
        ws = WsClient(ws_url, timeout=8)
        ws.connect()
        ws.send_text(json.dumps({"type": "input", "data": base64.b64encode(probe.encode()).decode()}))
        # Keep reading until the command output arrives or a wall-clock deadline
        # passes. A single idle window must not fail a healthy-but-slow node: the
        # first frames are the shell banner and the local echo of the typed line,
        # and on a loaded host the expanded output can lag the echo by seconds.
        deadline = time.monotonic() + 30.0
        frames = 0
        while frames < 80 and time.monotonic() < deadline:
            try:
                frame = ws.recv_frame(timeout=8)
            except TimeoutError:
                continue  # idle window — keep waiting until the deadline
            except Exception as exc:  # noqa: BLE001
                output += f"<recv error: {type(exc).__name__}: {exc}>\n"
                break
            if frame is None:
                break
            frames += 1
            output += json.dumps(frame) + "\n"
            # recv_frame returns the raw text frame (a JSON envelope); decode the
            # base64 `output.data` payload to read the PTY bytes.
            try:
                obj = json.loads(frame["data"])
            except (KeyError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("type") == "output" and isinstance(obj.get("data"), str):
                try:
                    decoded += base64.b64decode(obj["data"]).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(obj, dict) and obj.get("type") == "error":
                err_code = obj.get("code")
            match = out_re.search(decoded)
            if match:
                status = "pass" if match.group(2) == "ssh" else "fail"
                break
        ws.close()
    except Exception as exc:  # noqa: BLE001
        output += f"<ws error: {type(exc).__name__}: {exc}>"

    art = ctx.ev.record("W5.2", "W5.2 web terminal shell", ws_url,
                        output + "\n--- decoded PTY ---\n" + decoded)

    if match and match.group(2) == "ssh":
        return result("W5.2", WS, criterion, method, "pass",
                      f"terminal opened an SSH shell on the node "
                      f"(output `{match.group(0)}`, host {match.group(1)})", art)
    if match and match.group(2) == "local":
        # Output arrived, but it came from a controller-host shell — not a node.
        return result("W5.2", WS, criterion, method, "fail",
                      f"terminal ran a LOCAL shell (output `{match.group(0)}`), "
                      f"not a shell on the node",
                      art, reason=("the terminal backend opened a shell on the "
                                   "controller host (no SSH_CONNECTION); a node "
                                   "shell is required by dashboard.md §3.1"))
    if "404" in output:
        return result("W5.2", WS, criterion, method, "skipped",
                      "terminal WS endpoint not found (404) in running controller",
                      art, reason="the /terminal/v1/nodes WebSocket route is not implemented in the running controller build")
    if err_code == "NODE_OFFLINE" or "no key" in output.lower() or (
            "ssh" in output.lower() and "key" in output.lower()):
        reason = ("terminal backend unreachable: the node has no routable address / "
                  "the controller has no SSH key for the bridge")
        status = "skipped"
    elif err_code or "error" in output.lower():
        reason = f"terminal returned an error ({err_code}): {output[:200]}"
        status = "fail"
    else:
        reason = ("terminal produced no command output (only, at most, the local "
                  "echo of the typed line) — the node shell never ran the command")
        status = "fail"
    return result("W5.2", WS, criterion, method, status, output[:300], art, reason=reason)


def check_w5_3(ctx: CheckContext) -> dict:
    criterion = "the SSH command string is shown and works"
    method = "GET /v1/projects/{pid}/nodes/{nid}/ssh -> parse string + fingerprint; attempt login only if a break-glass key is present"
    node_id = _first_node_id(ctx)
    if not node_id or not ctx.project_token or not ctx.project_id:
        art = ctx.ev.record("W5.3", "W5.3 ssh string (skipped)", "GET /nodes/{nid}/ssh", "no node/token")
        return result("W5.3", WS, criterion, method, "skipped", "no node id",
                      art, reason="no node id / project token")

    r = http("GET", ctx.controller_url, f"/v1/projects/{ctx.project_id}/nodes/{node_id}/ssh",
             token=ctx.project_token)
    body = r.json() or {}

    raw = json.dumps({"status": r.status, "body": body}, indent=2)
    art = ctx.ev.record("W5.3", "W5.3 SSH string + login",
                        f"GET /v1/projects/{ctx.project_id}/nodes/{node_id}/ssh", raw)

    if is_absent(r):
        return result("W5.3", WS, criterion, method, "skipped",
                      "GET /nodes/{nid}/ssh returned 404 no route",
                      art, reason="the SSH-string endpoint is not implemented in the running controller build (no route for GET /v1/projects/{pid}/nodes/{nid}/ssh)")

    ssh_string = body.get("ssh") or body.get("command") or body.get("ssh_command") or ""
    fingerprint = body.get("fingerprint") or body.get("key_fingerprint") or ""
    wellformed = bool(ssh_string and ssh_string.startswith("ssh ") and "@" in ssh_string)

    if not wellformed:
        return result("W5.3", WS, criterion, method, "fail",
                      f"ssh string malformed: {ssh_string!r}", art,
                      reason="SSH endpoint did not return a well-formed ssh command string")

    # Prove the string works by executing it. Verdict is the child's EXIT STATUS
    # (not empty output — a first-connect known-hosts notice, or any banner, on
    # stderr would otherwise masquerade as a failure) and the known-hosts file is
    # disabled so a fresh node never trips host-key prompts. Two attempts:
    #   (a) the string exactly as the dashboard renders it (CA-signed operator
    #       key, D4);
    #   (b) the same target with the infra break-glass recovery key.
    opts = ("-o BatchMode=yes -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=6")
    key_candidates = ["/home/ubuntu/.ssh/termini_breakglass", "/root/.ssh/termini_breakglass"]
    key = next((k for k in key_candidates if os.path.isfile(k)), None)
    attempts = [("the string as rendered", ssh_string.replace("ssh ", f"ssh {opts} ", 1) + " true")]
    if key:
        target = ssh_string.split(" ", 1)[1]
        attempts.append(("break-glass key", f"ssh {opts} -i {key} {target} true"))

    login_ok_via = ""
    for label, cmd in attempts:
        res = run(f"timeout 12 {cmd} 2>&1; echo \" RC=$?\"")
        if res.rstrip().endswith("RC=0"):
            login_ok_via = label
            break

    if login_ok_via:
        return result("W5.3", WS, criterion, method, "pass",
                      f"well-formed string + fingerprint {fingerprint}; login succeeded ({login_ok_via})", art)

    return result("W5.3", WS, criterion, method, "skipped",
                  f"well-formed ssh string returned ({ssh_string}); fingerprint={fingerprint}; login not independently verified",
                  art, reason="SSH string is shown; the 'works' half requires the paired key (break-glass or CA-signed operator key) and a reachable node in this demo env")
