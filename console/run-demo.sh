#!/usr/bin/env bash
# run-demo.sh — bring up the composed W5 customer operations console.
#
# One command starts the four pieces the customer sees together:
#   1. the SSH CA + a demo "node" sshd, so the web terminal opens a real shell
#      *on the node* (dashboard.md §3.1, D4) rather than on the controller host;
#   2. the controller (this checkout's build) on $CONTROLLER_PORT;
#   3. two seeded demo nodes + a 1 s telemetry heartbeat (live health, live AI log);
#   4. the console — dashboard at / and web terminal at /terminal/ — on one origin.
#
#   console/run-demo.sh                                             # :8090
#   CONTROLLER_PORT=8085 CONSOLE_PORT=8090 SSH_PORT=22222 console/run-demo.sh
#   TERMINI_NODE_SSHD=off console/run-demo.sh   # use an externally-run node sshd
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
# The terminal opens a shell *on the node* (dashboard.md §3.1); the demo bridge
# proxies over SSH with a short-lived, CA-signed operator certificate (D4). The
# `local` backend (bash on the controller host) is a hermetic-test fallback only
# and is never the demo default.
TERMINAL_BACKEND="${TERMINI_TERMINAL_BACKEND:-ssh}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_PORT="${SSH_PORT:-22}"
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"
NODE_SSHD="${TERMINI_NODE_SSHD:-auto}"
BREAKGLASS_KEY="${TERMINI_SSH_KEY:-$HOME/.ssh/termini_breakglass}"

STATE="${TERMINI_DEMO_STATE:-$(mktemp -d "${TMPDIR:-/tmp}/termini-console.XXXXXX")}"
mkdir -p "$STATE"
INVENTORY="$STATE/inventory.json"
CA_KEY="$STATE/ssh_ca"
OPERATOR_KEY="$STATE/operator_key"
NODE_SSHD_DIR="$STATE/node-sshd"
NODE_SSHD_PID="$NODE_SSHD_DIR/sshd.pid"

echo "[run-demo] state dir       : $STATE"
echo "[run-demo] controller      : http://127.0.0.1:$CONTROLLER_PORT"
echo "[run-demo] console         : http://127.0.0.1:$CONSOLE_PORT/  (terminal: /terminal/)"
echo "[run-demo] terminal backend: $TERMINAL_BACKEND (node sshd: $NODE_SSHD on port $SSH_PORT)"

# --- one-time demo material -------------------------------------------------
# SSH CA (D4): the controller signs a short-lived operator certificate for the
# terminal bridge. The CA private key stays in the state dir; its public half is
# the node's trust anchor (TrustedUserCAKeys) and the fingerprint the dashboard
# surfaces (dashboard.md §3.2).
if [ ! -f "$CA_KEY" ]; then
  ssh-keygen -q -t ed25519 -N '' -C 'termini-ssh-ca' -f "$CA_KEY"
fi
# Controller-local operator identity key, pre-signed so the SSH string the
# dashboard renders works before the first terminal connect.
if [ ! -f "$OPERATOR_KEY" ]; then
  ssh-keygen -q -t ed25519 -N '' -C 'termini-operator-cert' -f "$OPERATOR_KEY"
  ssh-keygen -q -s "$CA_KEY" -I termini-controller -n "$SSH_USER" -V +8h "$OPERATOR_KEY.pub"
fi
# One-time break-glass recovery key (dashboard.md §3.2); trusted by the node's
# authorized_keys so the copy-paste SSH string is usable.
if [ ! -f "$BREAKGLASS_KEY" ]; then
  mkdir -p "$(dirname "$BREAKGLASS_KEY")"
  ssh-keygen -q -t ed25519 -N '' -C 'termini-breakglass' -f "$BREAKGLASS_KEY"
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

# --- the demo node sshd (what the one-click terminal shells into) -----------
# sshd needs root (it switches users via PAM), so this best-effort step uses
# passwordless sudo. It trusts the CA (TrustedUserCAKeys) for the terminal
# bridge's operator certificate and the break-glass key for the copy-paste
# string. When it cannot start, say so loudly — the terminal would otherwise
# silently fall back to a controller-host shell, which is NOT the contract.
if [ "$NODE_SSHD" = "auto" ]; then
  mkdir -p "$NODE_SSHD_DIR"
  if [ ! -f "$NODE_SSHD_DIR/host_key" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$NODE_SSHD_DIR/host_key"
  fi
  cat "$BREAKGLASS_KEY.pub" > "$NODE_SSHD_DIR/authorized_keys"
  # Also trust the conventional break-glass path if it is a different key.
  if [ -f "$HOME/.ssh/termini_breakglass.pub" ] \
     && ! grep -qF "$(cat "$HOME/.ssh/termini_breakglass.pub")" "$NODE_SSHD_DIR/authorized_keys"; then
    cat "$HOME/.ssh/termini_breakglass.pub" >> "$NODE_SSHD_DIR/authorized_keys"
  fi
  cat > "$NODE_SSHD_DIR/sshd_config" <<EOF
Port $SSH_PORT
ListenAddress 127.0.0.1
HostKey $NODE_SSHD_DIR/host_key
PidFile $NODE_SSHD_PID
AuthorizedKeysFile $NODE_SSHD_DIR/authorized_keys
TrustedUserCAKeys $CA_KEY.pub
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM yes
StrictModes no
PermitRootLogin no
AllowUsers $SSH_USER
LogLevel VERBOSE
EOF
  if [ -f "$NODE_SSHD_PID" ]; then
    sudo -n kill "$(cat "$NODE_SSHD_PID")" 2>/dev/null || true
    sleep 0.3
  fi
  if sudo -n /usr/sbin/sshd -f "$NODE_SSHD_DIR/sshd_config"; then
    echo "[run-demo] node sshd up    : 127.0.0.1:$SSH_PORT (user $SSH_USER, CA + break-glass)"
  else
    echo "[run-demo] WARN: could not start the demo node sshd (needs root)." >&2
    echo "[run-demo]       The web terminal needs a reachable node — start one yourself" >&2
    echo "[run-demo]       (or re-run with privileges). Set TERMINI_NODE_SSHD=off to silence." >&2
  fi
fi

pids=()
cleanup() {
  echo "[run-demo] stopping…"
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  if [ -f "$NODE_SSHD_PID" ]; then
    sudo -n kill "$(cat "$NODE_SSHD_PID")" 2>/dev/null || true
  fi
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
  TERMINI_SSH_CA_KEY_PATH="$CA_KEY" \
  TERMINI_SSH_CERT_PRINCIPAL="$SSH_USER" \
  TERMINI_SSH_OPERATOR_KEY_PATH="$OPERATOR_KEY" \
  TERMINI_SSH_CLIENT_KEY_PATH="$OPERATOR_KEY" \
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
# The seeder self-provisions its bootstrap tokens (`--bootstrap-mint 2`): it asks
# the controller for two FRESH single-use tokens on every start instead of
# hand-minting a fixed pair here. A fixed pair would be burned by the first
# registration, so a second run against a persistent TERMINI_DEMO_STATE would die
# `401 bootstrap token already used` exactly like the deployed seeder once did.
python3 "$HERE/seed_demo.py" \
  --controller "http://127.0.0.1:$CONTROLLER_PORT" \
  --token "$PROJECT_TOKEN" --bootstrap-mint 2 > "$STATE/seed.log" 2>&1 &
pids+=("$!")
sleep 1

# --- 3. console (foreground) ------------------------------------------------
echo "[run-demo] serving console — open http://127.0.0.1:$CONSOLE_PORT/"
exec python3 "$HERE/serve.py" \
  --host 127.0.0.1 --port "$CONSOLE_PORT" \
  --controller "http://127.0.0.1:$CONTROLLER_PORT" \
  --project "$PROJECT_ID" --token "$PROJECT_TOKEN" --key "$OPERATOR_KEY"
