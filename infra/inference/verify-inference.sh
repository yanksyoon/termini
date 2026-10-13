#!/usr/bin/env bash
# verify-inference.sh — acceptance harness for the Termini inference endpoint.
#
# Owner: kanban task t_95528ae8 ("Expose host llama.cpp on GPU Node C over HTTP
# to Node A"). Proves, against the LIVE footprint, the frozen contract:
#   D5  — host-exposed llama.cpp, OpenAI-compatible, on Node C :8080
#   D2  — reached from Node A at http://<node-c-underlay-ip>:8080
# and reports GPU device visibility on Node C honestly.
#
# Acceptance (verbatim): "a request from Node A to Node C returns a completion;
# streaming works."
#
# Inputs (env, overridable; otherwise read from the shared inventory):
#   INVENTORY   path to inventory.json   (default /home/ubuntu/.termini/inventory.json)
#   SSH_KEY     private key for the nodes (default ~/.ssh/id_ed25519)
#   NODE_A_PUB  Node A public IP  (SSH entry point)
#   NODE_C_PUB  Node C public IP  (SSH, for the GPU/host checks)
#   NODE_C_IP   Node C underlay IP (what Node A curls)
#   NODE_C_UNDERLAY / PORT also overridable.
#
# Exit 0 only when: Node A -> Node C /health is 200, a non-streaming completion
# returns non-empty content, and a streaming request yields >=2 incremental delta
# chunks terminated by [DONE].
set -uo pipefail

INVENTORY="${INVENTORY:-/home/ubuntu/.termini/inventory.json}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PORT="${PORT:-8080}"
MODEL_ALIAS="${MODEL_ALIAS:-gemma}"

inv() { python3 -c "import json,sys; d=json.load(open('$INVENTORY')); n={x['name']:x for x in d['nodes']}; print(n['$1']['$2'])" 2>/dev/null; }

NODE_A_PUB="${NODE_A_PUB:-$(inv node-a public_ip)}"
NODE_C_PUB="${NODE_C_PUB:-$(inv node-c public_ip)}"
NODE_C_IP="${NODE_C_IP:-$(inv node-c private_ip)}"

SSHOPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes)
say() { printf '%s\n' "$*"; }
hdr() { printf '\n===== %s =====\n' "$*"; }

FAILURES=0
check() { # check <name> <0/1-ok> [detail]
  if [ "$2" = "0" ]; then say "PASS  $1${3:+ — $3}"; else say "FAIL  $1${3:+ — $3}"; FAILURES=$((FAILURES+1)); fi
}

say "Termini inference endpoint acceptance — $(date -u +%FT%TZ)"
say "Node A (caller): $NODE_A_PUB   Node C (endpoint): ${NODE_C_IP}:${PORT}   (ssh $NODE_C_PUB)"

# --- 0. GPU device visibility on Node C (reported, not gating) ---------------
hdr "Node C GPU device visibility"
GPU_OUT=$(ssh "${SSHOPTS[@]}" ubuntu@"$NODE_C_PUB" '
  echo "nvidia-smi: $(command -v nvidia-smi || echo absent)";
  echo "lspci-nvidia: $(lspci 2>/dev/null | grep -ci -e nvidia -e "3d controller" || true)";
  echo "dev-nodes: $(ls /dev/nvidia* 2>/dev/null | tr "\n" " " || echo none)";
  echo "modules: $(lsmod | grep -o -e nvidia_drm -e nvidia_uvm -e nvidia -e nouveau | sort -u | tr "\n" " " || echo none)"
' 2>&1)
say "$GPU_OUT"
if printf '%s' "$GPU_OUT" | grep -q 'nvidia-smi: absent' && printf '%s' "$GPU_OUT" | grep -q 'lspci-nvidia: 0'; then
  say "NOTE  Node C has no GPU device (CPU-only footprint). Host-exposed llama.cpp on CPU is the sanctioned D5 fallback; see infra/gcp/README.md for GPU re-enable."
else
  say "NOTE  a GPU device is visible on Node C"
fi

# --- 1. /health reachable from Node A ---------------------------------------
hdr "Node A -> Node C:${PORT} /health"
HEALTH_CODE=$(ssh "${SSHOPTS[@]}" ubuntu@"$NODE_A_PUB" \
  "curl -s -m 10 -o /dev/null -w '%{http_code}' http://${NODE_C_IP}:${PORT}/health" 2>&1)
say "http_code=$HEALTH_CODE"
check "A->C /health returns 200" "$([ "$HEALTH_CODE" = "200" ] && echo 0 || echo 1)" "got $HEALTH_CODE"

# --- 2. served model identity ------------------------------------------------
hdr "Node A -> Node C:${PORT} /v1/models"
MODELS=$(ssh "${SSHOPTS[@]}" ubuntu@"$NODE_A_PUB" \
  "curl -s -m 10 http://${NODE_C_IP}:${PORT}/v1/models" 2>&1)
say "$MODELS"
if printf '%s' "$MODELS" | grep -q "\"$MODEL_ALIAS\""; then
  check "served model alias is '$MODEL_ALIAS'" 0
else
  check "served model alias is '$MODEL_ALIAS'" 1 "alias not found in /v1/models"
fi

# --- 3. non-streaming completion --------------------------------------------
hdr "Node A -> Node C:${PORT} non-streaming completion"
NONSTREAM=$(ssh "${SSHOPTS[@]}" ubuntu@"$NODE_A_PUB" \
  "curl -s -m 180 -w '\nHTTP=%{http_code}\n' http://${NODE_C_IP}:${PORT}/v1/chat/completions -H 'Content-Type: application/json' -d '{\"model\":\"${MODEL_ALIAS}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: termini inference ok\"}],\"max_tokens\":32,\"temperature\":0}'" 2>&1)
say "$NONSTREAM"
NS_JSON=$(printf '%s' "$NONSTREAM" | sed -n '/^{/p' | head -1)
NS_OK=$(printf '%s' "$NS_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(1 if d["choices"][0]["message"].get("content") else 0)' 2>/dev/null || echo 0)
if printf '%s' "$NONSTREAM" | grep -q 'HTTP=200' && [ "$NS_OK" = "1" ]; then
  check "non-streaming completion returns HTTP 200 with non-empty content" 0
else
  check "non-streaming completion returns HTTP 200 with non-empty content" 1
fi

# --- 4. streaming (SSE) — incremental deltas + [DONE], timed -----------------
hdr "Node A -> Node C:${PORT} streaming completion (SSE)"
STREAM=$(ssh "${SSHOPTS[@]}" ubuntu@"$NODE_A_PUB" 'python3 - '"$NODE_C_IP" "$PORT" "$MODEL_ALIAS" <<'PY' 2>&1
import http.client, json, sys, time
host, port, alias = sys.argv[1], int(sys.argv[2]), sys.argv[3]
body = json.dumps({
    "model": alias,
    "messages": [{"role": "user", "content": "Count from one to five, one number per line."}],
    "max_tokens": 40, "temperature": 0, "stream": True,
})
conn = http.client.HTTPConnection(host, port, timeout=120)
t0 = time.monotonic()
conn.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
resp = conn.getresponse()
print(f"http_status={resp.status}")
deltas, done, first = [], False, None
text = ""
while True:
    line = resp.readline()
    if not line:
        break
    line = line.decode().strip()
    if not line.startswith("data:"):
        continue
    payload = line[5:].strip()
    if payload == "[DONE]":
        done = True
        break
    try:
        chunk = json.loads(payload)
    except Exception:
        continue
    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
    c = delta.get("content")
    if c:
        if first is None:
            first = time.monotonic() - t0
        deltas.append(c)
        text += c
        print(f"delta[{len(deltas)}] t=+{(time.monotonic()-t0):.3f}s {c!r}")
print(f"incremental_delta_chunks={len(deltas)}")
print(f"first_token_s={first if first is not None else -1:.3f}")
print(f"total_s={time.monotonic()-t0:.3f}")
print(f"done_marker={done}")
print(f"assembled={text!r}")
PY
)
say "$STREAM"
NDELTA=$(printf '%s' "$STREAM" | sed -n 's/^incremental_delta_chunks=//p')
DONEOK=$(printf '%s' "$STREAM" | sed -n 's/^done_marker=//p')
if printf '%s' "$STREAM" | grep -q 'http_status=200' && [ "${NDELTA:-0}" -ge 2 ] && [ "$DONEOK" = "True" ]; then
  check "streaming returns incremental SSE deltas and [DONE]" 0 "$NDELTA delta chunks"
else
  check "streaming returns incremental SSE deltas and [DONE]" 1 "deltas=${NDELTA:-0} done=$DONEOK"
fi

hdr "RESULT"
if [ "$FAILURES" -eq 0 ]; then
  say "RESULT: PASS (inference endpoint reachable A->C with completion + streaming)"
  exit 0
else
  say "RESULT: FAIL ($FAILURES check(s) failed)"
  exit 1
fi
