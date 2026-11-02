#!/usr/bin/env bash
# provision-fleet.sh — turn three booted LXD VMs into a working Termini fleet.
#
# What it does, in order (every step is idempotent — re-run it after a reboot):
#
#   1. base packages in all three guests (openssh-server, so `sshd: running` is
#      true in the node's own health sample);
#   2. CONTROLLER: the repo tree at /opt/termini, a SQLite dir at /data, and the
#      termini-controller systemd unit — the same unit shape the GCE controller
#      runs, with TERMINI_INFERENCE_BASE_URL pointing at the local node-c;
#   3. NODES: LXD inside each guest (each node advertises its OWN :8443 API), then
#      the DOCUMENTED customer installer (`/install/termini-node.sh`) with a
#      single-use bootstrap token minted by the controller — the same path a
#      customer machine takes, not a private side door;
#   4. NODE C: the inference front door in `gemini-proxy` mode plus the
#      token-rotation timer that replaces the GCE metadata identity on a machine
#      that is not a GCE instance;
#   5. HOST: the dashboard gateway on :8099 (static dashboard + /v1 proxy), so the
#      fleet is reachable from the tailnet without a route into the LXD bridge.
#
# Usage:
#   ./provision-fleet.sh --sa-key ~/.config/gcloud/application_default_credentials.json
#   ./provision-fleet.sh --ref origin/main --no-gateway
set -euo pipefail

CONTROLLER=tvm-controller
NODE_A=tvm-node-a
NODE_C=tvm-node-c
CTL_IP=10.14.100.10
NODE_C_IP=10.14.100.30
PROJECT_ID="${TERMINI_PROJECT_ID:-01J0DEM0WARM000000000000AB}"
PROJECT_TOKEN="${TERMINI_PROJECT_TOKEN:-term-demo-project-token-000000000001}"
GEMINI_PROJECT="${TERMINI_GEMINI_PROJECT:-termini-510609}"
REF="${TERMINI_REF:-HEAD}"
SA_KEY="${TERMINI_VERTEX_SA_KEY:-}"
DO_GATEWAY=1
GATEWAY_PORT="${TERMINI_GATEWAY_PORT:-8099}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift ;;
    --sa-key) SA_KEY="$2"; shift ;;
    --no-gateway) DO_GATEWAY=0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[provision] %s\n' "$*" >&2; }
in_ctl() { lxc exec "$CONTROLLER" -- "$@"; }

api() { # PATH [METHOD] [BODY]
  local path=$1 method=${2:-GET} body=${3:-}
  if [ -n "$body" ]; then
    in_ctl curl -sS -X "$method" "http://127.0.0.1:8080$path" \
      -H "Authorization: Bearer $PROJECT_TOKEN" -H "Content-Type: application/json" -d "$body"
  else
    in_ctl curl -sS -X "$method" "http://127.0.0.1:8080$path" -H "Authorization: Bearer $PROJECT_TOKEN"
  fi
}

# --------------------------------------------------------------------------- 1
base_packages() {
  for vm in "$CONTROLLER" "$NODE_A" "$NODE_C"; do
    log "$vm: base packages"
    lxc exec "$vm" -- sh -c 'export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null 2>&1
      apt-get install -y -qq openssh-server >/dev/null 2>&1
      systemctl enable --now ssh >/dev/null 2>&1' || log "$vm: package step failed"
  done
}

# --------------------------------------------------------------------------- 2
deploy_controller() {
  log "controller: staging $REF"
  local tarball; tarball=$(mktemp -t termini-tree-XXXXXX.tar)
  git -C "$REPO_ROOT" archive --format=tar -o "$tarball" "$REF" \
    controller termini console chaos infra image/models.lock.json
  lxc file push -q "$tarball" "$CONTROLLER/tmp/termini-tree.tar"
  rm -f "$tarball"

  lxc exec "$CONTROLLER" -- bash -c '
    set -e
    mkdir -p /opt/termini /data
    tar -xf /tmp/termini-tree.tar -C /opt/termini
    chown -R ubuntu:ubuntu /opt/termini
    chown ubuntu:ubuntu /data
    chmod 775 /data
  '

  lxc exec "$CONTROLLER" -- bash -c "cat > /etc/systemd/system/termini-controller.service <<'UNIT'
[Unit]
Description=Termini controller (control plane)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/opt/termini/controller
Environment=PYTHONPATH=/opt/termini/controller:/opt/termini
Environment=TERMINI_HOST=0.0.0.0
Environment=TERMINI_PORT=8080
Environment=TERMINI_DB_PATH=/data/controller.db
Environment=TERMINI_PROJECT_ID=$PROJECT_ID
Environment=TERMINI_PROJECT_TOKEN=$PROJECT_TOKEN
Environment=TERMINI_BOOTSTRAP_TOKEN=local-fleet-bootstrap
Environment=TERMINI_MIN_REDACTOR_VERSION=3
Environment=TERMINI_OPERATOR_ENABLED=true
Environment=TERMINI_OPERATOR_DECISION_MODE=model
Environment=TERMINI_INFERENCE_BASE_URL=http://$NODE_C_IP:8080
Environment=TERMINI_INFERENCE_MODEL=gemma
Environment=TERMINI_INFERENCE_TIMEOUT_S=900
Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=900
Environment=TERMINI_OPERATOR_POLL_INTERVAL_S=2
Environment=TERMINI_INTENT_INFERENCE_BASE_URL=http://$NODE_C_IP:8080
Environment=TERMINI_LXD_DEFAULT_INSTANCE_NAME=demo-workload
Environment=TERMINI_OPERATOR_WINDOW=20
Environment=TERMINI_OPERATOR_COOLDOWN_S=60
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m app.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now termini-controller >/dev/null
systemctl restart termini-controller"

  log "controller: waiting for the API"
  for _i in $(seq 1 40); do
    if api "/v1/projects/$PROJECT_ID/nodes" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  api "/v1/projects/$PROJECT_ID/nodes" | head -c 120 >/dev/null \
    || { log "controller API did not come up"; return 1; }
  log "controller: API up"
}

# --------------------------------------------------------------------------- 3
mint_token() {
  api "/v1/bootstrap-tokens" POST '{"count":1}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tokens"][0])'
}

join_node() { # vm name
  local vm=$1 name=$2
  log "$vm: LXD inside the guest"
  lxc exec "$vm" -- sh -c '
    if ! snap list lxd >/dev/null 2>&1; then snap install lxd >/dev/null 2>&1; fi
    if [ ! -d /var/snap/lxd/common/lxd/database ]; then lxd init --auto >/dev/null 2>&1; fi' || true

  if lxc exec "$vm" -- test -f /var/lib/termini/identity/node.json; then
    log "$vm: already enrolled — restarting the bootstrap unit"
    lxc exec "$vm" -- systemctl restart --no-block termini-bootstrap.service || true
    return 0
  fi

  local tok; tok=$(mint_token)
  log "$vm: installing the node runtime (documented customer installer)"
  lxc exec "$vm" -- bash -c "curl -fsSL http://$CTL_IP:8080/install/termini-node.sh | sh -s -- \
      --controller http://$CTL_IP:8080 --token $tok --name $name --role compute --wait-seconds 90" \
    | tail -8
}

# --------------------------------------------------------------------------- 4
node_c_front_door() {
  if [ -n "$SA_KEY" ] && [ -f "$SA_KEY" ]; then
    log "node-c: installing the Vertex service-account key (0600, never in the repo)"
    lxc file push -q --mode=600 "$SA_KEY" "$NODE_C/etc/termini/vertex-sa.json"
    lxc exec "$NODE_C" -- chown root:root /etc/termini/vertex-sa.json
  fi

  log "node-c: staging the repo tree for the proxy installer"
  local tarball; tarball=$(mktemp -t termini-infra-XXXXXX.tar)
  tar -C "$REPO_ROOT" -cf "$tarball" infra
  lxc file push -q "$tarball" "$NODE_C/tmp/termini-infra.tar"
  rm -f "$tarball"

  lxc exec "$NODE_C" -- bash -c "
    set -e
    mkdir -p /opt/termini
    tar -xf /tmp/termini-infra.tar -C /opt/termini
    bash /opt/termini/infra/local/fleet-vm/install-node-c-proxy.sh --project $GEMINI_PROJECT" \
    | tail -6
}

# --------------------------------------------------------------------------- 5
dashboard_gateway() {
  log "host: dashboard gateway on :$GATEWAY_PORT"
  sudo -n install -d -m 0755 /opt/termini-local
  sudo -n install -m 0755 "$HERE/dashboard-gateway.py" /opt/termini-local/dashboard-gateway.py
  sudo -n rm -rf /opt/termini-local/dashboard
  sudo -n cp -r "$REPO_ROOT/dashboard" /opt/termini-local/dashboard

  sudo -n tee /etc/systemd/system/termini-local-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Termini local fleet dashboard gateway (static dashboard + /v1 proxy)
After=network-online.target

[Service]
User=$USER
WorkingDirectory=/opt/termini-local
ExecStart=/usr/bin/python3 /opt/termini-local/dashboard-gateway.py --controller http://$CTL_IP:8080 --dashboard /opt/termini-local/dashboard --port $GATEWAY_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  sudo -n systemctl daemon-reload
  sudo -n systemctl enable --now termini-local-dashboard >/dev/null
  sudo -n systemctl restart termini-local-dashboard
  sleep 2
  curl -fsS -o /dev/null "http://127.0.0.1:$GATEWAY_PORT/" \
    && log "host: dashboard answers on :$GATEWAY_PORT" \
    || log "host: dashboard did NOT answer"
}

base_packages
deploy_controller
join_node "$NODE_A" node-a
join_node "$NODE_C" node-c
node_c_front_door
[ "$DO_GATEWAY" = 1 ] && dashboard_gateway

log "fleet provisioned: http://$CTL_IP:8080 (controller), dashboard on :$GATEWAY_PORT"
