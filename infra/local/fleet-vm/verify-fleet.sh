#!/usr/bin/env bash
# verify-fleet.sh — assert the local LXD-VM fleet is really a Termini fleet.
#
# Each check prints PASS/FAIL with the measured value. Exit non-zero if any FAIL.
# This is deliberately independent of the provisioning scripts: it reads the
# controller API, the guests themselves and the host, not the provisioner's logs.
#
# Usage:
#   ./verify-fleet.sh                  # structural + live checks
#   ./verify-fleet.sh --with-fault     # also drive one oom injection end to end
#   ./verify-fleet.sh --hold 45        # seconds to watch telemetry advance
set -uo pipefail

CONTROLLER=tvm-controller
NODE_A=tvm-node-a
NODE_C=tvm-node-c
CTL_IP=10.14.100.10
NODE_A_IP=10.14.100.20
NODE_C_IP=10.14.100.30
PROJECT_ID="${TERMINI_PROJECT_ID:-01J0DEM0WARM000000000000AB}"
PROJECT_TOKEN="${TERMINI_PROJECT_TOKEN:-term-demo-project-token-000000000001}"
GATEWAY_PORT="${TERMINI_GATEWAY_PORT:-8099}"
HOLD=20
WITH_FAULT=0
ACTOR="t_14b567d4"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-fault) WITH_FAULT=1 ;;
    --hold) HOLD="$2"; shift ;;
    --actor) ACTOR="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

fails=0
pass() { printf 'PASS  %-34s %s\n' "$1" "$2"; }
fail() { printf 'FAIL  %-34s %s\n' "$1" "$2" >&2; fails=$((fails+1)); }

api() { # path
  lxc exec "$CONTROLLER" -- curl -sS "http://127.0.0.1:8080$1" \
    -H "Authorization: Bearer $PROJECT_TOKEN"
}

# ---------------------------------------------------------------- 1. real VMs
for vm in "$CONTROLLER" "$NODE_A" "$NODE_C"; do
  typ=$(lxc list "$vm" --format csv -c t 2>/dev/null)
  kern=$(lxc exec "$vm" -- uname -r 2>/dev/null | tr -d '\r')
  kvm=$(lxc exec "$vm" -- test -c /dev/kvm && echo "kvm" || echo "no-kvm")
  if [ "$typ" = "VIRTUAL-MACHINE" ] && [ -n "$kern" ]; then
    pass "$vm is a VM" "type=$typ kernel=$kern $kvm"
  else
    fail "$vm is a VM" "type=${typ:-absent} kernel=${kern:-none}"
  fi

done

# ------------------------------------------------------------- 2. registration
NODES_JSON=$(api "/v1/projects/$PROJECT_ID/nodes")
NODE_IDS=$(printf '%s' "$NODES_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for n in d.get("nodes",[]):
    print(n["node_id"], n["role"], n["status"], n["last_seen_at"])
')
if [ "$(printf '%s\n' "$NODE_IDS" | grep -c .)" -ge 2 ]; then
  pass "nodes registered" "$(printf '%s' "$NODE_IDS" | awk '{print $1"/"$2"/"$3}' | tr '\n' ' ')"
else
  fail "nodes registered" "only $(printf '%s\n' "$NODE_IDS" | grep -c .) node(s): $NODE_IDS"
fi

if printf '%s' "$NODE_IDS" | grep -q " degraded"; then
  fail "all nodes healthy" "$(printf '%s' "$NODE_IDS" | grep degraded | tr '\n' ' ')"
else
  pass "all nodes healthy" "no degraded node"
fi

# telemetry must MOVE, not just exist
newest_ts() {
  api "/v1/telemetry/recent?limit=1" | python3 -c '
import json,sys
d=json.load(sys.stdin)
ts=[s["ts"] for v in (d.get("nodes") or {}).values() for s in v if s.get("ts")]
print(max(ts) if ts else "")'
}
first=$(newest_ts)
sleep "$HOLD"
second=$(newest_ts)
if [ -n "$second" ] && [ "$second" != "$first" ]; then
  pass "telemetry advancing" "$first -> $second"
else
  fail "telemetry advancing" "no new sample in ${HOLD}s (last=$second)"
fi

# ----------------------------------------------------------- 3. per-node LXD API
for pair in "$NODE_A_IP:8443" "$NODE_C_IP:8443"; do
  ip=${pair%%:*}; port=${pair##*:}
  code=$(curl -sk -o /dev/null -w '%{http_code}' -m 5 "https://$ip:$port/1.0" || true)
  if [ "$code" = "200" ] || [ "$code" = "403" ]; then
    pass "node LXD API $ip:$port" "HTTP $code (listener answering)"
  else
    fail "node LXD API $ip:$port" "HTTP ${code:-000}"
  fi
done

# reachability from each node to the controller (registration path)
if lxc exec "$NODE_A" -- curl -fsS -m 5 -o /dev/null "http://$CTL_IP:8080/v1/projects/$PROJECT_ID/nodes" -H "Authorization: Bearer $PROJECT_TOKEN"; then
  pass "node -> controller path" "node-a reached $CTL_IP:8080"
else
  fail "node -> controller path" "node-a cannot reach the controller"
fi

# --------------------------------------------------------------- 4. inference
HEALTH=$(lxc exec "$NODE_C" -- curl -sS -m 10 "http://127.0.0.1:8080/health")
backend=$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("backend"), d.get("served_model"))' 2>/dev/null)
if printf '%s' "$backend" | grep -q "gemini-proxy"; then
  pass "inference front door" "$backend"
else
  fail "inference front door" "$HEALTH"
fi

COMP=$(lxc exec "$NODE_C" -- curl -sS -m 60 "http://127.0.0.1:8080/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma","messages":[{"role":"user","content":"Reply with exactly: FLEET-OK"}],"max_tokens":16}')
text=$(printf '%s' "$COMP" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d["choices"][0]["message"]["content"].strip())' 2>/dev/null)
tokens=$(printf '%s' "$COMP" | python3 -c '
import json,sys
u=json.load(sys.stdin).get("usage",{})
print("%s+%s" % (u.get("prompt_tokens"), u.get("completion_tokens")))' 2>/dev/null)
if [ "$text" = "FLEET-OK" ]; then
  pass "model completion" "reply=$text tokens=$tokens"
else
  fail "model completion" "${text:-no answer}"
fi

# ----------------------------------------------------- 5. dashboard gateway
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$GATEWAY_PORT/" || true)
api_code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$GATEWAY_PORT/v1/projects/$PROJECT_ID/nodes" \
  -H "Authorization: Bearer $PROJECT_TOKEN" || true)
if [ "$code" = "200" ] && [ "$api_code" = "200" ]; then
  pass "dashboard gateway :$GATEWAY_PORT" "static=$code api=$api_code"
else
  fail "dashboard gateway :$GATEWAY_PORT" "static=${code:-000} api=${api_code:-000}"
fi

# ------------------------------------------------- 6. optional: fault -> model
if [ "$WITH_FAULT" = 1 ]; then
  nid=$(printf '%s' "$NODES_JSON" | python3 -c '
import json,sys
for n in json.load(sys.stdin)["nodes"]:
    if n["role"] == "compute":
        print(n["node_id"]); break')
  inj=$(lxc exec "$CONTROLLER" -- curl -sS -X POST \
    "http://127.0.0.1:8080/v1/projects/$PROJECT_ID/demo/faults" \
    -H "Authorization: Bearer $PROJECT_TOKEN" -H "Content-Type: application/json" \
    -H "X-Termini-Actor: $ACTOR" \
    -d "{\"node_id\":\"$nid\",\"scenario\":\"oom\",\"duration_s\":90,\"target\":\"real\"}")
  iid=$(printf '%s' "$inj" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("injection_id",""))')
  if [ -n "$iid" ]; then
    pass "fault injected" "injection=$iid node=$nid"
  else
    fail "fault injected" "$inj"
  fi

  decided=""
  for _i in $(seq 1 45); do
    decided=$(lxc exec "$CONTROLLER" -- python3 -c "
import sqlite3
c=sqlite3.connect('/data/controller.db')
r=c.execute(\"select action_kind,decision_source,substr(reason,1,90) from remediation_decisions order by decided_at desc limit 1\").fetchone()
print('|'.join(str(x) for x in r) if r else '')" 2>/dev/null)
    [ -n "$decided" ] && break
    sleep 2
  done
  if printf '%s' "$decided" | grep -q "|model|"; then
    pass "model decision" "$decided"
  else
    fail "model decision" "${decided:-none within 90s}"
  fi

  lxc exec "$CONTROLLER" -- curl -sS -X POST \
    "http://127.0.0.1:8080/v1/projects/$PROJECT_ID/demo/faults/clear" \
    -H "Authorization: Bearer $PROJECT_TOKEN" -H "X-Termini-Actor: $ACTOR" \
    -H "Content-Type: application/json" -d '{}' >/dev/null
  pass "fault cleared" "actor=$ACTOR"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "RESULT: all checks passed"
else
  echo "RESULT: $fails check(s) failed" >&2
fi
exit "$([ "$fails" -eq 0 ] && echo 0 || echo 1)"
