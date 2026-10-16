#!/usr/bin/env bash
# setup-inference-surfaces.sh — put the PROVIDER-ONLY inference surface on the
# CONTROLLER and point the operator's diagnosis at it (infra-decisions.md D9).
#
# WHY THIS EXISTS
# ---------------
# The footprint has ONE model address before this script, and two surfaces want
# it for opposite reasons:
#
#   * the client's AGENTS want their own address on Node C
#     (`http://10.10.0.13:8080`) — the agent-facing surface. Since the pivot
#     (kanban `t_957956e5` / `t_3fcee90b`) that surface SHIPS
#     `gemini-proxy` too, with `local` (this node's own CPU weights) as the
#     one-variable fallback, so "the agent port cannot forward" is no longer true;
#   * the OPERATOR'S DIAGNOSIS wants Google's Gemini on
#     `aiplatform.googleapis.com` (the user's Q1/Q1b decision of 2026-10-15: the
#     diagnosis prompt — C0 telemetry, no workload payloads — may leave the
#     premises as part of the managed service), and it wants it on a surface that
#     is never handed to an agent.
#
# This script materialises the second surface ON THE CONTROLLER, on LOOPBACK, as
# a separate listener with its own env file, its own unit and its own address:
#
#   http://127.0.0.1:8085   backend=gemini-proxy   destination=aiplatform.googleapis.com
#
# and then repoints the controller's diagnosis call at it. The fleet has no route
# to 127.0.0.1 on the controller, so the agent surface and the provider surface
# cannot be confused with each other — the two are one command apart and
# provably independent (flipping either backend leaves the other's `/health`
# unchanged).
#
# IDENTITY: the controller's OWN metadata identity mints the token per request
# (`roles/aiplatform.user` + `cloud-platform`). **No service-account key file is
# written here and none may be** — the same rule the node's egress card
# (`t_5a4a0f73`) established for Node C. Setting that identity is a separate,
# announced stop/start of the instance (see the header of
# `../cloudrun/setup-operator-node-c.sh` for the pre-split wiring, and
# `docs/contracts/infra-decisions.md` D9).
#
# USAGE (on the controller VM, as root; the payload is staged beside this file —
# the same shape Node C's first boot uses for `install-inference-proxy.sh`):
#
#   sudo ./setup-inference-surfaces.sh
#   sudo ./setup-inference-surfaces.sh --node-c 10.10.0.13 --provider-port 8085
#   ./setup-inference-surfaces.sh --render-dir /tmp/out     # render only, no root
#
# Staged payload (three files, staged by the operator or by cloud-init):
#
#   <stage>/proxy.py                          <- ../inference/proxy.py
#   <stage>/termini-inference-provider.service <- ../inference/termini-inference-provider.service
#   <stage>/setup-inference-surfaces.sh        <- this file
#
# `--render-dir DIR` writes every artifact this script would install under DIR
# (mirroring the absolute paths) and touches NOTHING else: no useradd, no
# systemctl. That is how the rendering is verified without a live controller.
set -euo pipefail

NODE_C_HOST="${NODE_C_HOST:-10.10.0.13}"
PROVIDER_BIND="${PROVIDER_BIND:-127.0.0.1}"
PROVIDER_PORT="${PROVIDER_PORT:-8085}"
ALIAS="${ALIAS:-gemma}"
GEMINI_HOST="${GEMINI_HOST:-aiplatform.googleapis.com}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
GEMINI_TIMEOUT_S="${GEMINI_TIMEOUT_S:-30}"
DECISION_TIMEOUT_S="${DECISION_TIMEOUT_S:-120}"
INTENT_BASE_URL="${INTENT_BASE_URL:-}"
ROUNDTRIP_TIMEOUT_S="${ROUNDTRIP_TIMEOUT_S:-60}"

BIN_DIR="${BIN_DIR:-/opt/termini/inference}"
ENV_DIR="${ENV_DIR:-/etc/termini}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
TMPFILES_DIR="${TMPFILES_DIR:-/usr/lib/tmpfiles.d}"
SERVICE_USER="${SERVICE_USER:-termini-infer}"
PROVIDER_UNIT="termini-inference-provider.service"
CONTROLLER_UNIT="termini-controller.service"
DROPIN_NAME="zz-inference-surfaces.conf"
# The pre-split drop-in wrote the DIAGNOSIS address as Node C. It sorts AFTER this
# script's drop-in (`zz-n…` > `zz-i…`), so leaving it in place would silently win
# and put the diagnosis back on the agent surface. It is disabled, not deleted.
LEGACY_DROPIN="zz-node-c-model.conf"
STATE_FILE="${STATE_FILE:-/run/termini/provider-backend.json}"
LOG_FILE="${LOG_FILE:-/var/log/termini-inference-provider.jsonl}"

RENDER_DIR=""
DO_RESTART=1
DO_VERIFY=1

log() { printf '[setup-inference-surfaces] %s\n' "$*" >&2; }
die() { printf '[setup-inference-surfaces] FATAL: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --node-c) NODE_C_HOST="${2:-}"; shift ;;
    --provider-port) PROVIDER_PORT="${2:-}"; shift ;;
    --provider-bind) PROVIDER_BIND="${2:-}"; shift ;;
    --decision-timeout-s) DECISION_TIMEOUT_S="${2:-}"; shift ;;
    --intent-base-url) INTENT_BASE_URL="${2:-}"; shift ;;
    --render-dir) RENDER_DIR="${2:-}"; shift ;;
    --no-restart) DO_RESTART=0 ;;
    --no-verify) DO_VERIFY=0 ;;
    -h|--help) sed -n '2,52p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

case "$PROVIDER_PORT" in
  ''|*[!0-9]*) die "--provider-port must be a number (got '$PROVIDER_PORT')" ;;
esac
[ -n "$NODE_C_HOST" ] || die "--node-c must be a host"
[ -n "$INTENT_BASE_URL" ] || INTENT_BASE_URL="http://${NODE_C_HOST}:8090"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SRC="${PROXY_SRC:-$HERE/proxy.py}"
UNIT_SRC="${UNIT_SRC:-$HERE/$PROVIDER_UNIT}"

[ -f "$PROXY_SRC" ] || die "proxy source not found: $PROXY_SRC"
[ -f "$UNIT_SRC" ] || die "unit source not found: $UNIT_SRC"

# Everything is written under $ROOT: "" for a real install, the render dir for
# --render-dir. Same code path either way, so the rendering that is tested is the
# rendering that is installed.
ROOT=""
if [ -n "$RENDER_DIR" ]; then
  ROOT="$(cd "$(dirname "$RENDER_DIR")" && pwd)/$(basename "$RENDER_DIR")"
  mkdir -p "$ROOT"
  log "RENDER ONLY — artifacts go under $ROOT, the system is not touched"
else
  [ "$(id -u)" = "0" ] || die "run as root (it writes systemd units and env files)"
fi

write_text() { # $1 = absolute target path, $2 = mode; content on stdin
  local path="$1" mode="$2"
  mkdir -p "$(dirname "$ROOT$path")"
  cat > "$ROOT$path"
  chmod "$mode" "$ROOT$path"
}

# --- 1. the proxy itself ----------------------------------------------------- #
install -D -m 0755 "$PROXY_SRC" "$ROOT$BIN_DIR/proxy.py"
if [ -z "$ROOT" ]; then
  /usr/bin/python3 "$BIN_DIR/proxy.py" --check >/dev/null \
    || die "the proxy does not start cleanly (--check failed)"
fi
log "installed $BIN_DIR/proxy.py"

# --- 2. the provider surface's backend choice (configuration, not code) ------ #
write_text "$ENV_DIR/inference-proxy.env" 0644 <<EOF
# The PROVIDER-ONLY surface (D9). THIS is the diagnosis listener's backend, and
# the only listener in the footprint that is meant to forward.
#   gemini-proxy : translate to Vertex AI $GEMINI_MODEL and label the substitution
#   local        : would be a passthrough to a llama.cpp worker — there is none on
#                  the controller, which is why the provider surface is always
#                  gemini-proxy and the agent surface is always local.
TERMINI_INFERENCE_BACKEND=gemini-proxy
# LOOPBACK ONLY. The agent fleet has no route here; a caller on the controller is
# the only possible client.
TERMINI_INFERENCE_HOST=$PROVIDER_BIND
TERMINI_INFERENCE_PORT=$PROVIDER_PORT
TERMINI_INFERENCE_ALIAS=$ALIAS
# No worker exists on this host. The value is kept for shape only, and /health
# reports the truth (local_path.available=false) by MEASURING it, not asserting it.
TERMINI_INFERENCE_UPSTREAM=http://127.0.0.1:8081
TERMINI_INFERENCE_UPSTREAM_TIMEOUT_S=900
# Vertex AI. The project id is read from the instance metadata server when empty
# (no project constant baked in), and the access token is minted per request from
# the same metadata server — there is no key file on the controller.
TERMINI_GEMINI_HOST=$GEMINI_HOST
TERMINI_GEMINI_LOCATION=$GEMINI_LOCATION
TERMINI_GEMINI_MODEL=$GEMINI_MODEL
TERMINI_GEMINI_PROJECT=
TERMINI_GEMINI_TIMEOUT_S=$GEMINI_TIMEOUT_S
TERMINI_GEMINI_THINKING_BUDGET=0
TERMINI_INFERENCE_STATE_FILE=$STATE_FILE
TERMINI_INFERENCE_LOG_FILE=$LOG_FILE
EOF
log "wrote $ENV_DIR/inference-proxy.env (backend=gemini-proxy, bind=$PROVIDER_BIND:$PROVIDER_PORT)"

# --- 3. the unit ------------------------------------------------------------- #
install -D -m 0644 "$UNIT_SRC" "$ROOT$UNIT_DIR/$PROVIDER_UNIT"
log "installed $UNIT_DIR/$PROVIDER_UNIT"

# --- 4. the controller's own wiring: diagnosis -> loopback provider ---------- #
write_text "$UNIT_DIR/$CONTROLLER_UNIT.d/$DROPIN_NAME" 0644 <<EOF
[Service]
# --- the PROVIDER surface (D9): the diagnosis leaves from THIS host ----------
# Loopback. Node C's :8080 is the AGENT surface and no longer answers this call.
Environment=TERMINI_OPERATOR_ENABLED=true
Environment=TERMINI_OPERATOR_DECISION_MODE=model
Environment=TERMINI_INFERENCE_BASE_URL=http://$PROVIDER_BIND:$PROVIDER_PORT
Environment=TERMINI_INFERENCE_MODEL=$ALIAS
# A provider round-trip is seconds; a hung upstream must not hold the operator
# loop for the CPU-era 900 s budget it used to need.
Environment=TERMINI_INFERENCE_TIMEOUT_S=$DECISION_TIMEOUT_S
Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=$DECISION_TIMEOUT_S
# --- the AGENT surface is still Node C --------------------------------------
# User-facing intent extraction keeps the dedicated small-context llama.cpp
# instance on Node C (:8090) — the same node, served by its own local weights
# (this instance is deliberately NOT fronted by the proxy).
Environment=TERMINI_INTENT_INFERENCE_BASE_URL=$INTENT_BASE_URL
Environment=TERMINI_INTENT_MODEL_TIMEOUT_S=$DECISION_TIMEOUT_S
# 'TERMINI_INFERENCE_BASE_URL' now names loopback, so it can no longer identify
# the node that serves the model. Node C is named EXPLICITLY, otherwise it would
# silently become a candidate for auto agent placement (controller-api.md §5.2
# step 5) — a compute node running llama.cpp for the whole fleet must not take
# agent workloads.
Environment=TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS=$NODE_C_HOST
EOF
log "wrote $UNIT_DIR/$CONTROLLER_UNIT.d/$DROPIN_NAME"

# --- 5. /run survives a reboot ---------------------------------------------- #
write_text "$TMPFILES_DIR/termini-inference.conf" 0644 <<EOF
# Termini inference provider surface (D9): the active-backend state file lives in
# /run, which is a tmpfs. Recreate it, owned by the service user.
d /run/termini 0755 $SERVICE_USER $SERVICE_USER -
EOF

if [ -n "$ROOT" ]; then
  log "rendered artifacts:"
  find "$ROOT" -type f -printf '  %P\n' | sort >&2
  log "render done — nothing was installed"
  exit 0
fi

# --------------------------------------------------------------------------- #
# A real install from here on.
# --------------------------------------------------------------------------- #
id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

install -d -m 0755 "$BIN_DIR" "$ENV_DIR" /run/termini /var/log
chown "$SERVICE_USER:$SERVICE_USER" /run/termini
install -m 0644 /dev/null "$LOG_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_FILE"
systemd-tmpfiles --create "$TMPFILES_DIR/termini-inference.conf" >/dev/null 2>&1 || true

# Retire the pre-split drop-in BEFORE daemon-reload: it sorts after this script's
# drop-in and would otherwise decide the diagnosis address.
if [ -f "$UNIT_DIR/$CONTROLLER_UNIT.d/$LEGACY_DROPIN" ]; then
  mv "$UNIT_DIR/$CONTROLLER_UNIT.d/$LEGACY_DROPIN" \
     "$UNIT_DIR/$CONTROLLER_UNIT.d/$LEGACY_DROPIN.disabled"
  log "disabled the pre-split drop-in $LEGACY_DROPIN (renamed .disabled); it pointed the diagnosis at the agent surface"
fi

systemctl daemon-reload
systemctl enable "$PROVIDER_UNIT" >/dev/null 2>&1 || true
systemctl restart "$PROVIDER_UNIT"

log "waiting for the provider listener on $PROVIDER_BIND:$PROVIDER_PORT"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://127.0.0.1:$PROVIDER_PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" = "1" ] || die "the provider listener did not answer on 127.0.0.1:$PROVIDER_PORT"

if [ "$DO_RESTART" = "1" ]; then
  systemctl restart "$CONTROLLER_UNIT"
  log "restarted $CONTROLLER_UNIT (diagnosis now dials http://$PROVIDER_BIND:$PROVIDER_PORT)"
fi

if [ "$DO_VERIFY" = "1" ]; then
  echo "== provider listener: $(systemctl is-active "$PROVIDER_UNIT")"
  health="$(curl -fsS -m 5 "http://127.0.0.1:$PROVIDER_PORT/health")"
  echo "== http://127.0.0.1:$PROVIDER_PORT/health"
  echo "$health"
  backend="$(printf '%s' "$health" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("backend",""))')"
  [ "$backend" = "gemini-proxy" ] \
    || die "the provider listener reports backend='$backend', expected gemini-proxy"

  echo "== a REAL provider round-trip (this is the diagnosis path)"
  body="$(mktemp -t termini-provider-XXXXXX.json)"
  code="$(curl -sS -m "$ROUNDTRIP_TIMEOUT_S" -o "$body" -w '%{http_code}' \
    -X POST "http://127.0.0.1:$PROVIDER_PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$ALIAS\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ok\"}],\"max_tokens\":16,\"temperature\":0}")"
  echo "POST /v1/chat/completions -> HTTP $code"
  head -c 400 "$body"; echo
  [ "$code" = "200" ] || die "the provider surface answered HTTP $code (see the body above)"
  egress="$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("termini_backend") or {}).get("egress",""))' "$body")"
  rm -f "$body"
  [ "$egress" = "external" ] \
    || die "the provider answer is not labelled egress=external (got '$egress')"

  echo "== controller effective env"
  systemctl show "$CONTROLLER_UNIT" -p Environment | tr ' ' '\n' \
    | grep -E '^TERMINI_(INFERENCE|INTENT_INFERENCE|AGENT_PLACEMENT|OPERATOR_DECISION_(MODE|TIMEOUT))' || true
fi

log "done — diagnosis: http://$PROVIDER_BIND:$PROVIDER_PORT (provider-only, never handed to an agent); agents: http://$NODE_C_HOST:8080 (front door — gemini-proxy as shipped, local as the fallback)"
