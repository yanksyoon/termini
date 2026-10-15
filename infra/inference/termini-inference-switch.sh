#!/usr/bin/env bash
# termini-inference-switch.sh — flip Node C's inference backend in ONE step.
#
# Run ON Node C as root:
#
#   termini-inference-switch gemini-proxy   # forwarded managed model (emulating a GPU node)
#   termini-inference-switch local          # CPU weights on this node (llama.cpp worker)
#   termini-inference-switch legacy         # the pre-proxy serve path, exactly as before
#   termini-inference-switch status         # what is active RIGHT NOW, with evidence
#
# Nothing here rebuilds or reinstalls anything: the backend is an environment
# file, and each target restarts exactly the units that target needs. The chosen
# backend is recorded in the node's own status file
# (`/run/termini/inference-backend.json`) and reported by `/health`.
#
# `legacy` is the one-step restore of the host-exposed llama.cpp server on :8080
# (the shape the node shipped with, before this proxy existed); `local` serves the
# same CPU path through the front door instead. Both leave the proxy installed,
# so switching back is again one step.
set -euo pipefail

TARGET="${1:-status}"
PROXY_PORT="${PROXY_PORT:-8080}"
WORKER_PORT="${WORKER_PORT:-8081}"
ENV_DIR="${ENV_DIR:-/etc/termini}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
PROXY_UNIT="termini-inference-proxy.service"
LLAMA_UNIT="termini-llama-server.service"
PROXY_ENV="$ENV_DIR/inference-proxy.env"
WORKER_ENV="$ENV_DIR/llama-worker.env"
STATE_FILE="${STATE_FILE:-/run/termini/inference-backend.json}"

log() { printf '[inference-switch] %s\n' "$*" >&2; }
die() { printf '[inference-switch] FATAL: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" = "0" ] || die "run as root (it rewrites units and restarts services)"; }

wait_health() { # $1 = port, $2 = seconds
  local port="$1" tries="${2:-60}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

set_env_backend() { # $1 = backend
  [ -f "$PROXY_ENV" ] || die "$PROXY_ENV missing — run install-inference-proxy.sh first"
  if grep -q '^TERMINI_INFERENCE_BACKEND=' "$PROXY_ENV"; then
    sed -i "s|^TERMINI_INFERENCE_BACKEND=.*|TERMINI_INFERENCE_BACKEND=$1|" "$PROXY_ENV"
  else
    printf 'TERMINI_INFERENCE_BACKEND=%s\n' "$1" >> "$PROXY_ENV"
  fi
}

write_state() { # $1 = backend, $2 = active unit, $3 = answered_by, $4 = destination
  install -d -m 0755 "$(dirname "$STATE_FILE")"
  cat > "$STATE_FILE" <<EOF
{
  "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "backend": "$1",
  "active_unit": "$2",
  "answered_by": "$3",
  "destination": "$4",
  "listen": "0.0.0.0:$PROXY_PORT",
  "worker": "127.0.0.1:$WORKER_PORT",
  "is_gpu_inference": false
}
EOF
}

show_status() {
  echo "== node status: /etc/termini =="
  echo "--- $PROXY_ENV"
  grep -v '^#' "$PROXY_ENV" 2>/dev/null | grep -v '^$' || echo "(missing)"
  echo "--- $WORKER_ENV"
  grep -v '^#' "$WORKER_ENV" 2>/dev/null | grep -v '^$' || echo "(missing)"
  echo
  echo "== units =="
  for unit in "$PROXY_UNIT" "$LLAMA_UNIT"; do
    printf '%s: enabled=%s active=%s\n' "$unit" \
      "$(systemctl is-enabled "$unit" 2>/dev/null || echo n/a)" \
      "$(systemctl is-active "$unit" 2>/dev/null || echo n/a)"
  done
  echo
  echo "== listeners =="
  ss -lntp 2>/dev/null | grep -E ":($PROXY_PORT|$WORKER_PORT)\b" || echo "(none)"
  echo
  echo "== recorded active backend =="
  cat "$STATE_FILE" 2>/dev/null || echo "(no state file)"
  echo
  echo "== :$PROXY_PORT/health (what nodes A/B see) =="
  curl -fsS -m 5 "http://127.0.0.1:$PROXY_PORT/health" || echo "(no answer on :$PROXY_PORT)"
  echo
  echo "== :$WORKER_PORT/health (the local worker, loopback only) =="
  curl -fsS -m 5 "http://127.0.0.1:$WORKER_PORT/health" || echo "(no answer on :$WORKER_PORT)"
  echo
}

case "$TARGET" in
  gemini-proxy|local)
    need_root
    set_env_backend "$TARGET"
    log "TERMINI_INFERENCE_BACKEND=$TARGET"
    systemctl start "$LLAMA_UNIT" 2>/dev/null || true   # local weights stay loadable
    systemctl enable "$PROXY_UNIT" >/dev/null 2>&1 || true
    systemctl restart "$PROXY_UNIT"
    wait_health "$PROXY_PORT" 60 || die "the front door did not answer on :$PROXY_PORT"
    active="$(curl -fsS -m 5 "http://127.0.0.1:$PROXY_PORT/health" \
              | python3 -c 'import json,sys;print(json.load(sys.stdin).get("backend",""))')"
    [ "$active" = "$TARGET" ] || die "asked for $TARGET but the endpoint reports '$active'"
    log "ACTIVE BACKEND: $active"
    show_status
    ;;

  legacy)
    need_root
    log "restoring the pre-proxy serve path: host-exposed llama.cpp on 0.0.0.0:$PROXY_PORT"
    systemctl disable --now "$PROXY_UNIT" >/dev/null 2>&1 || true
    install -d -m 0755 "$ENV_DIR"
    cat > "$WORKER_ENV" <<EOF
# Legacy mode: the host-exposed llama.cpp server answers nodes A/B directly on
# 0.0.0.0:$PROXY_PORT, exactly as it did before the inference proxy existed.
LLAMA_HOST=0.0.0.0
LLAMA_PORT=$PROXY_PORT
EOF
    systemctl restart "$LLAMA_UNIT"
    wait_health "$PROXY_PORT" 90 || die "the restored llama.cpp server did not answer on :$PROXY_PORT"
    write_state "legacy" "$LLAMA_UNIT" "local-weights" "http://127.0.0.1:$PROXY_PORT"
    log "ACTIVE BACKEND: legacy (llama.cpp on 0.0.0.0:$PROXY_PORT, no proxy in the path)"
    show_status
    ;;

  status)
    show_status
    ;;

  *)
    echo "usage: $0 {gemini-proxy|local|legacy|status}" >&2
    exit 2
    ;;
esac
