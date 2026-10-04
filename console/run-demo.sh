#!/usr/bin/env bash
# run-demo.sh — bring up the composed W5 customer operations console.
#
# One command starts the three pieces the customer sees together:
#   1. the controller (this repo's origin/main build) on $CONTROLLER_PORT;
#   2. two seeded demo nodes + a 1 s telemetry heartbeat (live health, live AI log);
#   3. the console — dashboard at / and web terminal at /terminal/ — on one origin.
#
#   console/run-demo.sh              # http://127.0.0.1:8090/
#   CONTROLLER_PORT=8085 CONSOLE_PORT=8090 console/run-demo.sh
#   TERMINI_TERMINAL_BACKEND=ssh console/run-demo.sh   # proxy shells to the real node host
#
# Ctrl-C tears everything down.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

CONTROLLER_PORT="${CONTROLLER_PORT:-8085}"
CONSOLE_PORT="${CONSOLE_PORT:-8090}"
PROJECT_ID="${PROJECT_ID:-01J0DEM0WARM000000000000AB}"
PROJECT_TOKEN="${PROJECT_TOKEN:-demo-project-token}"
BOOTSTRAP="${BOOTSTRAP:-demo-boot-a,demo-boot-c}"
TERMINAL_BACKEND="${TERMINI_TERMINAL_BACKEND:-local}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_PORT="${SSH_PORT:-22}"
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"

STATE="${TERMINI_DEMO_STATE:-$(mktemp -d "${TMPDIR:-/tmp}/termini-console.XXXXXX")}"
mkdir -p "$STATE"
INVENTORY="$STATE/inventory.json"
KEY="${TERMINI_SSH_KEY:-$STATE/breakglass}"

echo "[run-demo] state dir      : $STATE"
echo "[run-demo] controller     : http://127.0.0.1:$CONTROLLER_PORT"
echo "[run-demo] console        : http://127.0.0.1:$CONSOLE_PORT/  (terminal: /terminal/)"

# --- one-time demo material -------------------------------------------------
if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N '' -C 'termini-breakglass' -f "$KEY"
fi
cat > "$INVENTORY" <<JSON
{
  "schema_version": "1.0",
  "generated_by": "console/run-demo.sh",
  "project": "termini-demo",
  "ssh": {"user": "$SSH_USER", "port": $SSH_PORT},
  "nodes": [
    {"name": "demo-node-a", "hostname": "termini-node-a", "role": "compute",
     "public_ip": "$PUBLIC_IP", "private_ip": "10.14.100.11", "tags": [], "labels": {}},
    {"name": "demo-node-c", "hostname": "termini-node-c", "role": "inference",
     "public_ip": "$PUBLIC_IP", "private_ip": "10.14.100.13", "tags": [], "labels": {},
     "accelerator": {"type": "nvidia-l4", "count": 1}}
  ]
}
JSON

pids=()
cleanup() {
  echo "[run-demo] stopping…"
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. controller ----------------------------------------------------------
echo "[run-demo] starting controller…"
( cd "$REPO_ROOT/controller" && \
  TERMINI_HOST=127.0.0.1 TERMINI_PORT="$CONTROLLER_PORT" \
  TERMINI_DB_PATH="$STATE/controller.db" \
  TERMINI_BOOTSTRAP_TOKEN="$BOOTSTRAP" \
  TERMINI_PROJECT_ID="$PROJECT_ID" TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" \
  TERMINI_INVENTORY_PATH="$INVENTORY" \
  TERMINI_SSH_PUBLIC_KEY="$(cat "$KEY.pub")" \
  TERMINI_SSH_CLIENT_KEY_PATH="$KEY" \
  TERMINI_TERMINAL_BACKEND="$TERMINAL_BACKEND" \
  TERMINI_OPERATOR_ENABLED=true TERMINI_OPERATOR_DRY_RUN=true \
  TERMINI_OPERATOR_POLL_INTERVAL_S=1 TERMINI_OPERATOR_COOLDOWN_S=3 \
  TERMINI_OOM_CONSECUTIVE=2 TERMINI_OOM_CLEAR_CONSECUTIVE=2 \
  TERMINI_DISK_WRITE_LATENCY_MS=50 TERMINI_DISK_LATENCY_CONSECUTIVE=2 \
  python3 -m app.main > "$STATE/controller.log" 2>&1 ) &
pids+=("$!")

for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$CONTROLLER_PORT/healthz" >/dev/null 2>&1 \
     || curl -sS -o /dev/null "http://127.0.0.1:$CONTROLLER_PORT/v1/nodes/register" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
echo "[run-demo] controller up"

# --- 2. seed demo nodes + telemetry ----------------------------------------
echo "[run-demo] seeding demo nodes…"
python3 "$HERE/seed_demo.py" \
  --controller "http://127.0.0.1:$CONTROLLER_PORT" \
  --token "$PROJECT_TOKEN" --bootstrap "$BOOTSTRAP" > "$STATE/seed.log" 2>&1 &
pids+=("$!")
sleep 1

# --- 3. console (foreground) ------------------------------------------------
echo "[run-demo] serving console — open http://127.0.0.1:$CONSOLE_PORT/"
exec python3 "$HERE/serve.py" \
  --host 127.0.0.1 --port "$CONSOLE_PORT" \
  --controller "http://127.0.0.1:$CONTROLLER_PORT" \
  --project "$PROJECT_ID" --token "$PROJECT_TOKEN" --key "$KEY"
