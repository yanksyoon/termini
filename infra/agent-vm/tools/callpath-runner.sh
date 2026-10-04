#!/usr/bin/env bash
# callpath-runner.sh — IN-VM half of the cross-node acceptance (t_fd8eee6c).
#
# Runs inside an Agent VM on Node A. It exercises the only two network
# destinations an agent VM is allowed to have (agent-runtime.md §15.1): the
# on-prem inference route on Node C. It never contacts the controller.
#
#   callpath-runner.sh <node_c_ip> <port> <max_tokens>
#
# Inputs (pushed by verify-callpath.sh):
#   /tmp/callpath-agent.yaml   validated agent config (base_url = Node C)
#   /tmp/callpath-turns.json   list of user turns for the multi-turn task
# Sources (pushed by verify-callpath.sh):
#   /opt/termini/ws-src/src    current tree's termini-agent package
# Outputs:
#   /tmp/env.json              environment facts (python, vm id, inference url)
#   /tmp/probe.json            Node C reachability over the direct host route
#   /tmp/callpath-report.json  the multi-turn latency/streaming report
set -euo pipefail

NODE_C_IP="${1:?usage: callpath-runner.sh <node_c_ip> <port> <max_tokens>}"
PORT="${2:?}"
MAX_TOKENS="${3:?}"

export PYTHONPATH=/opt/termini/ws-src/src
PY=/opt/termini/venv/bin/python

printf '{"kind":"termini.callpath_env","vm_id":"%s","inference_url":"http://%s:%s","python":"%s"}\n' \
  "$(cat /etc/hostname)" "$NODE_C_IP" "$PORT" "$("$PY" -V 2>&1)" > /tmp/env.json

# Reachability proof of Node C over the frozen direct host route.
"$PY" - "$NODE_C_IP" "$PORT" > /tmp/probe.json <<'PYEOF'
import json, socket, sys, time, urllib.request
host, port = sys.argv[1], int(sys.argv[2])
out = {"host": host, "port": port}
t0 = time.monotonic()
s = socket.create_connection((host, port), timeout=5); s.close()
out["tcp_connect_ms"] = round((time.monotonic() - t0) * 1000, 2)
t0 = time.monotonic()
with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as r:
    out["health_status"] = r.status
    out["health_body"] = r.read(120).decode("utf-8", "replace")
out["health_rtt_ms"] = round((time.monotonic() - t0) * 1000, 2)
print(json.dumps(out))
PYEOF

set +e
"$PY" -m termini_agent.cli multiturn \
  --config /tmp/callpath-agent.yaml \
  --turns-file /tmp/callpath-turns.json \
  --max-tokens "$MAX_TOKENS" \
  --out /tmp/callpath-report.json
rc=$?
set -e
echo "multiturn rc=$rc"
exit "$rc"
