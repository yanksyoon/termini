#!/usr/bin/env bash
# env_up.sh — provision a *dedicated* 3-node Termini underlay for the
# fan-network red-team, isolated from the shared demo nodes (which other
# workers start/stop). Creates rt-net-a (compute), rt-net-b (compute) and
# rt-net-c (inference with the model bind-mounted and llama.cpp resident),
# all registering to a private controller on :8094.
#
#   bash w7/redteam/net/env_up.sh <OUT_DIR>
# writes <OUT_DIR>/env.json  (controller url, project id/token, node names/ids/ips)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="${1:?usage: env_up.sh OUT_DIR}"
mkdir -p "$OUT"
PORT=${RT_PORT:-$(( (RANDOM % 300) + 8400 ))}
HOST=10.14.100.1
PROJECT_ID="$(python3 "$HERE/gen_ulid.py")"
PROJECT_TOKEN="term-rtnet-project-token"
MODELS_DIR=/var/lib/termini/models
TOK_A="rtnet-a-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TOK_B="rtnet-b-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TOK_C="rtnet-c-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')"

echo "### project=$PROJECT_ID controller=:$PORT"

# --- controller -----------------------------------------------------------------
# a stale DB/project row (or stale listener) would break project-scoped auth
[ -f "$OUT/controller.pid" ] && kill "$(cat "$OUT/controller.pid")" 2>/dev/null || true
rm -f "$OUT/controller.db" "$OUT/controller.db-wal" "$OUT/controller.db-shm"
if ! ss -ltn | grep -q ":$PORT "; then
  ( cd "$REPO/controller" && TERMINI_HOST=0.0.0.0 TERMINI_PORT="$PORT" \
      TERMINI_DB_PATH="$OUT/controller.db" \
      TERMINI_BOOTSTRAP_TOKEN="$TOK_A,$TOK_B,$TOK_C" \
      TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" TERMINI_PROJECT_ID="$PROJECT_ID" \
      TERMINI_MIN_REDACTOR_VERSION=3 TERMINI_TELEMETRY_INTERVAL_MS=1000 \
      TERMINI_OPERATOR_ENABLED=1 \
      nohup python3 -m app.main > "$OUT/controller.log" 2>&1 & echo $! > "$OUT/controller.pid" )
fi
# wait until the controller answers a project-scoped route for THIS project
for _ in $(seq 1 60); do
  code=$(curl -sS -m 2 -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$PORT/v1/projects/$PROJECT_ID/nodes" \
    -H "Authorization: Bearer $PROJECT_TOKEN" 2>/dev/null)
  [ "$code" = "200" ] && break
  sleep 0.5
done
echo "### controller ready (http=$code)"

node_userdata() { # name role token gpu
  cat <<EOF
#cloud-config
write_files:
  - path: /etc/termini/config.yaml
    permissions: '0600'
    owner: root:root
    content: |
      controller_url: http://$HOST:$PORT
      node_name: $1
      role_hint: $2
      telemetry_interval_ms: 1000
      inference_port: 8080
      model_alias: deepseek-flash
      image_version: 2026.10.0
      project_id: $PROJECT_ID
      gpu_vram_bytes: $4
  - path: /var/lib/termini/enrollment.token
    permissions: '0600'
    owner: root:root
    content: |
      $3
runcmd:
  - [ systemctl, restart, termini-bootstrap.service ]
  - [ systemctl, restart, llama-cpp.service ]
EOF
}

make_node() { # name snapshot role token gpu
  local name="$1" snap="$2" role="$3" token="$4" gpu="$5"
  for _ in $(seq 1 10); do
    lxc delete --force "$name" >/dev/null 2>&1 && break
    sleep 3
  done
  echo "### copy $snap -> $name"
  lxc copy "$snap" "$name" </dev/null >/dev/null 2>&1
  lxc config set "$name" cloud-init.user-data "$(node_userdata "$name" "$role" "$token" "$gpu")"
  if [ "$role" = inference ]; then
    lxc config set "$name" limits.cpu 4
    lxc config set "$name" limits.memory 8GiB
    lxc config device add "$name" models disk source="$MODELS_DIR" path="$MODELS_DIR" readonly=true >/dev/null 2>&1 || true
  else
    lxc config set "$name" limits.cpu 2
    lxc config set "$name" limits.memory 1GiB
  fi
  lxc start "$name"
}

wait_online() { # name budget
  local name="$1" budget="$2"
  for _ in $(seq 1 "$budget"); do
    if lxc exec "$name" -- cat /var/lib/termini/state.json 2>/dev/null | grep -q '"state": "ONLINE"'; then return 0; fi
    sleep 1
  done
  return 1
}

wait_health() { # name budget
  local name="$1" budget="$2" ip h
  for _ in $(seq 1 "$budget"); do
    ip=$(lxc list "$name" --format csv -c 4 | awk '{print $1}')
    h=$(lxc exec "$name" -- curl -sS -m 3 -o /dev/null -w '%{http_code}' "http://$ip:8080/health" 2>/dev/null)
    [ "$h" = "200" ] && return 0
    sleep 2
  done
  return 1
}

make_node rt-net-a demo-node-a/unregistered compute "$TOK_A" 0
make_node rt-net-b demo-node-a/unregistered compute "$TOK_B" 0
make_node rt-net-c demo-node-c/unregistered inference "$TOK_C" 25769803776

for n in rt-net-a rt-net-b rt-net-c; do
  wait_online "$n" 60 && echo "### $n ONLINE" || echo "### $n NOT online"
done
wait_health rt-net-c 90 && echo "### rt-net-c inference healthy" || echo "### rt-net-c inference NOT healthy"

# emit env.json
node_id() { lxc exec "$1" -- cat /var/lib/termini/identity/node.json 2>/dev/null | jq -r '.node_id'; }
node_ip() { lxc list "$1" --format csv -c 4 | awk '{print $1}'; }
cat > "$OUT/env.json" <<EOF
{
  "controller": "http://127.0.0.1:$PORT",
  "controller_node_url": "http://$HOST:$PORT",
  "project_id": "$PROJECT_ID",
  "project_token": "$PROJECT_TOKEN",
  "nodes": {
    "a": {"name": "rt-net-a", "role": "compute",   "id": "$(node_id rt-net-a)", "ip": "$(node_ip rt-net-a)"},
    "b": {"name": "rt-net-b", "role": "compute",   "id": "$(node_id rt-net-b)", "ip": "$(node_ip rt-net-b)"},
    "c": {"name": "rt-net-c", "role": "inference", "id": "$(node_id rt-net-c)", "ip": "$(node_ip rt-net-c)"}
  }
}
EOF
echo "### env.json:"
cat "$OUT/env.json"
