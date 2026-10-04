#!/usr/bin/env bash
# demo-env.sh — lifecycle + pre-warm for the Termini demo environment.
#
#   infra/warm/demo-env.sh bake      build + publish the pre-baked node image
#   infra/warm/demo-env.sh up        start controller + demo nodes, warm, snapshot
#   infra/warm/demo-env.sh warm      (re-)warm slow paths on a running env
#   infra/warm/demo-env.sh down      full stop (nodes + controller)
#   infra/warm/demo-env.sh restart   down then up, printing the timing
#   infra/warm/demo-env.sh fresh     snapshot-restore a fresh node and time it
#   infra/warm/demo-env.sh status    one-line-per-node status
#
# The environment is deliberately small: one controller, three demo nodes
# (A/B compute, C inference) created from the pinned image alias, each running
# the real termini-bootstrap agent against the real controller core.
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

CMD="${1:-}"; shift || true

need lxc; need jq; need curl; need python3

mkdir -p "$STATE_DIR" "$BUILD_DIR" "$MODELS_DIR" "$PUBLISH_DIR"

# --------------------------------------------------------------------------- helpers
controller_log() { tail -n "${1:-20}" "$CONTROLLER_LOG" 2>/dev/null || true; }

# Bootstrap tokens are single-use. The three demo nodes consume the fixed ones
# once; any extra token (e.g. one minted for each `fresh` run) is appended here
# and the controller is restarted so it re-seeds them.
TOKENS_EXTRA_FILE="$STATE_DIR/bootstrap-tokens.extra"
effective_tokens() {
  local extra=""
  [ -f "$TOKENS_EXTRA_FILE" ] && extra="$(tr '\n' ',' < "$TOKENS_EXTRA_FILE")"
  printf '%s,%s' "$BOOTSTRAP_TOKENS" "${extra%,}"
}

start_controller() {
  if controller_up; then log "controller already up on $CONTROLLER_URL_HOST"; return 0; fi
  log "starting controller on :$CONTROLLER_PORT (db=$CONTROLLER_DB)"
  sudo systemctl stop termini-controller.service >/dev/null 2>&1 || true
  mkdir -p "$STATE_DIR"
  sudo systemd-run --unit=termini-controller --collect --quiet \
    --working-directory="$REPO_ROOT/controller" \
    --setenv=TERMINI_HOST="$CONTROLLER_BIND" \
    --setenv=TERMINI_PORT="$CONTROLLER_PORT" \
    --setenv=TERMINI_DB_PATH="$CONTROLLER_DB" \
    --setenv=TERMINI_BOOTSTRAP_TOKEN="$(effective_tokens)" \
    --setenv=TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" \
    --setenv=TERMINI_PROJECT_ID="$PROJECT_ID" \
    --setenv=TERMINI_MAX_INFERENCE_NODES=1 \
    --setenv=TERMINI_MIN_REDACTOR_VERSION=3 \
    --setenv=TERMINI_TELEMETRY_INTERVAL_MS=1000 \
    /usr/bin/env python3 -m app.main
  wait_until 30 "controller answering on $CONTROLLER_URL_HOST" controller_up \
    || { controller_log 40; die "controller did not come up"; }
  log "controller up"
}

mint_token() { # PREFIX -> new single-use token, registered with the controller
  local tok
  tok="${1}-$(date +%s)-$RANDOM"
  echo "$tok" >> "$TOKENS_EXTRA_FILE"
  start_controller_restart
  printf '%s' "$tok"
}

start_controller_restart() {
  sudo systemctl stop termini-controller.service >/dev/null 2>&1 || true
  start_controller
}

stop_controller() {
  sudo systemctl stop termini-controller.service >/dev/null 2>&1 || true
  log "controller stopped"
}

# render_cloud_init NAME ROLE TOKEN -> stdout
render_cloud_init() {
  local name="$1" role="$2" token="$3"
  local gpu_bytes=0
  [ "$role" = "inference" ] && gpu_bytes=25769803776   # 24 GiB, Node C (L4) per D5
  cat <<EOF
#cloud-config
write_files:
  - path: /etc/termini/config.yaml
    permissions: '0600'
    owner: root:root
    content: |
      controller_url: ${CONTROLLER_URL_NODE}
      node_name: ${name}
      role_hint: ${role}
      telemetry_interval_ms: 1000
      inference_port: ${INFERENCE_PORT}
      model_alias: ${MODEL_ALIAS}
      image_version: ${IMAGE_VERSION}
      project_id: ${PROJECT_ID}
      gpu_vram_bytes: ${gpu_bytes}
  - path: /var/lib/termini/enrollment.token
    permissions: '0600'
    owner: root:root
    content: |
      ${token}
runcmd:
  - [ systemctl, restart, termini-bootstrap.service ]
  - [ systemctl, restart, llama-cpp.service ]
EOF
}

ensure_scratch_volume() { # NAME
  if lxc storage volume show default "$1" >/dev/null 2>&1; then
    log "scratch volume $1 present"
  else
    log "creating scratch volume $1 ($SCRATCH_SIZE)"
    lxc storage volume create default "$1" --type=filesystem size="$SCRATCH_SIZE" >/dev/null
  fi
}

configure_node() { # NAME ROLE TOKEN
  local name="$1" role="$2" token="$3"
  lxc config set "$name" cloud-init.user-data "$(render_cloud_init "$name" "$role" "$token")"
  if [ "$role" = "inference" ]; then
    lxc config set "$name" limits.cpu 4
    lxc config set "$name" limits.memory 8GiB
    if ! lxc config device show "$name" | grep -q '^models:'; then
      lxc config device add "$name" models disk source="$MODELS_DIR" path="$MODELS_DIR" readonly=true
    fi
  else
    lxc config set "$name" limits.cpu 2
    lxc config set "$name" limits.memory 1GiB
  fi
  # Scenario-B scratch disk (one pre-created volume per node)
  local vol="$SCRATCH_VOLUME" cur
  case "$name" in
    *-b) vol="$SCRATCH_VOLUME_B" ;;
    *-c|*fresh) vol="$SCRATCH_VOLUME_C" ;;
  esac
  cur="$(lxc config device get "$name" scratch source 2>/dev/null || true)"
  if [ "$cur" != "$vol" ]; then
    [ -n "$cur" ] && lxc config device remove "$name" scratch >/dev/null 2>&1 || true
    lxc config device add "$name" scratch disk pool=default source="$vol" path="$SCRATCH_MOUNT" </dev/null
  fi
}

create_node() { # NAME ROLE TOKEN
  local name="$1"
  if lxc_exists "$name"; then
    log "node $name exists"
  else
    log "creating node $name from image alias $IMAGE_ALIAS"
    lxc init "$IMAGE_ALIAS" "$name" </dev/null
  fi
  configure_node "$@"
}

snapshot_node() { # NAME SNAPSHOT
  local name="$1" snap="$2"
  lxc delete "$name/$snap" >/dev/null 2>&1 || true
  lxc snapshot "$name" "$snap"
}

node_ip() { # NAME
  local name="$1"
  lxc list "$name" --format csv -c 4 2>/dev/null | awk -F'[ ,]' '{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\./) {print $i; exit}}'
}

node_state() { # NAME -> state.json string
  local name="$1"
  lxc exec "$name" -- /usr/local/bin/termini-bootstrap status </dev/null 2>/dev/null || true
}

wait_registered() { # NAME BUDGET_S
  local name="$1" budget="$2" start now
  start="$(date +%s)"
  while :; do
    if node_state "$name" | grep -q '"state": "ONLINE"'; then
      # and the controller must have seen a recent sample
      if controller_knows_node "$name"; then
        now="$(date +%s)"; printf '%s' "$((now - start))"; return 0
      fi
    fi
    now="$(date +%s)"
    [ $((now - start)) -ge "$budget" ] && return 1
    sleep 0.5
  done
}

# controller_knows_node NAME -> 0 if the controller has this node (matched by the
# node_id the agent persisted) with a last_seen_at inside the last 15s.
controller_knows_node() {
  local name="$1" pid nid
  nid="$(node_node_id "$name")"; [ -n "$nid" ] || return 1
  pid="$(project_id_from_db)"; [ -n "$pid" ] || return 1
  curl -fsS -m 3 "$CONTROLLER_URL_HOST/v1/projects/$pid/nodes" \
    -H "Authorization: Bearer $PROJECT_TOKEN" 2>/dev/null \
  | python3 -c '
import json,sys,datetime
nid=sys.argv[1]
try: data=json.load(sys.stdin)
except Exception: sys.exit(1)
now=datetime.datetime.now(datetime.timezone.utc)
for n in data.get("nodes",[]):
    if n.get("node_id")==nid:
        ls=n.get("last_seen_at") or ""
        try:
            t=datetime.datetime.strptime(ls,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            sys.exit(1)
        sys.exit(0 if (now-t).total_seconds()<15 else 1)
sys.exit(1)
' "$nid"
}

wait_inference() { # NAME BUDGET_S -> prints seconds, 0 ok
  local name="$1" budget="$2" start now ip
  start="$(date +%s)"
  while :; do
    ip="$(node_ip "$name")"
    if [ -n "$ip" ] && curl -fsS -m 3 "http://$ip:$INFERENCE_PORT/health" >/dev/null 2>&1; then
      now="$(date +%s)"; printf '%s' "$((now - start))"; return 0
    fi
    now="$(date +%s)"
    [ $((now - start)) -ge "$budget" ] && return 1
    sleep 0.5
  done
}

# --------------------------------------------------------------------------- commands
cmd_bake() { exec "$WARM_DIR/bake-image.sh" "$@"; }

cmd_up() {
  require_pinned_image
  ensure_model
  start_controller
  ensure_scratch_volume "$SCRATCH_VOLUME"
  ensure_scratch_volume "$SCRATCH_VOLUME_B"
  ensure_scratch_volume "$SCRATCH_VOLUME_C"

  local entry name role token secs
  for entry in "${DEMO_NODES[@]}"; do
    name="${entry%%:*}"; role="$(node_role "$name")"; token="$(node_token "$name")"
    create_node "$name" "$role" "$token"
    # the pre-enrollment restore point must be taken BEFORE the node ever boots
    # and must never be overwritten with an enrolled state; create it once.
    if snapshot_exists "$name" "$SNAPSHOT_UNREG"; then
      log "restore point '$SNAPSHOT_UNREG' already recorded for $name"
    else
      snapshot_node "$name" "$SNAPSHOT_UNREG"
    fi
    # bring the node up; tolerate transient LXD states (an interrupted snapshot
    # can leave an instance FROZEN). lxc start on a running instance is benign.
    local state
    state="$(lxc list "$name" --format csv -c s 2>/dev/null || echo absent)"
    case "$state" in
      RUNNING) : ;;
      FROZEN)  log "unfreezing node $name"
               lxc stop "$name" --force >/dev/null 2>&1 || true
               lxc start "$name" </dev/null 2>/dev/null || true ;;
      *)       log "starting node $name"
               lxc start "$name" </dev/null 2>/dev/null || true ;;
    esac
    if secs="$(wait_registered "$name" 60)"; then
      log "node $name registered in ${secs}s"
    else
      warn "node $name did not register within 60s"
    fi
  done

  cmd_warm
}

ensure_model() {
  local dest="$MODELS_DIR/$MODEL_FILE"
  if [ -f "$dest" ] && [ "$(stat -c %s "$dest")" = "$MODEL_SIZE_BYTES" ]; then
    log "model present: $dest ($(stat -c %s "$dest") bytes)"
    [ -f "$dest.sha256" ] || sha256sum "$dest" | awk '{print $1"  '"$MODEL_FILE"'"}' > "$dest.sha256"
    return 0
  fi
  die "pinned model missing or wrong size: $dest (expected $MODEL_SIZE_BYTES bytes).
      Fetch it once with: image/pipeline/provision/40-models.sh, or
        curl -fL --retry 3 -o '$dest.part' \\
          https://huggingface.co/bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF/resolve/main/$MODEL_FILE \\
        && mv '$dest.part' '$dest' && sha256sum '$dest' | awk '{print \$1\"  $MODEL_FILE\"}' > '$dest.sha256'"
}

cmd_warm() {
  log "warming slow paths"
  # 1. resident model on the inference node: restart the unit and wait for RSS
  local inf_name="" entry name role
  for entry in "${DEMO_NODES[@]}"; do
    name="${entry%%:*}"; role="$(node_role "$name")"
    [ "$role" = "inference" ] && inf_name="$name"
  done
  [ -n "$inf_name" ] || die "no inference node defined"

  lxc exec "$inf_name" -- systemctl restart llama-cpp.service
  if wait_until 120 "model resident on $inf_name" model_resident "$inf_name"; then
    log "model resident on $inf_name ($(model_rss "$inf_name") bytes RSS)"
  else
    warn "model not confirmed resident on $inf_name within 120s"
  fi
  # host page cache is warmed by the same read (device is a host bind-mount)
  # 2. guest image store sentinel: the pinned image is already local (bake)
  # 3. snapshots: the restore points the demo starts from (recorded once, at
  #    first warm — re-snapshotting a running node on every `up` is slow and can
  #    freeze it under load, and refreshing it later would capture a degraded state)
  for entry in "${DEMO_NODES[@]}"; do
    name="${entry%%:*}"
    if snapshot_exists "$name" "$SNAPSHOT_WARM"; then
      log "warm restore point '$SNAPSHOT_WARM' already recorded for $name"
    else
      snapshot_node "$name" "$SNAPSHOT_WARM"
      log "snapshot '$SNAPSHOT_WARM' recorded for $name"
    fi
  done
}

model_rss() { # NAME -> bytes
  lxc exec "$1" -- bash -c 'p=$(pidof llama-server | awk "{print \$1}"); [ -n "$p" ] && awk "/VmRSS/{print \$2*1024}" /proc/$p/status' </dev/null 2>/dev/null | tr -d '\r'
}

model_resident() { # NAME
  local rss
  rss="$(model_rss "$1")"
  [ -n "$rss" ] && [ "$rss" -ge "$RESIDENT_RSS_MIN_BYTES" ]
}

cmd_down() {
  local entry name
  for entry in "${DEMO_NODES[@]}"; do
    name="${entry%%:*}"
    if lxc_running "$name"; then
      log "stopping node $name"
      lxc stop "$name" --timeout 30 >/dev/null 2>&1 || lxc stop "$name" --force >/dev/null 2>&1 || true
    fi
  done
  # a stop can lag behind the request (guest shutdown of llama-server takes a
  # moment); make sure the environment is really down before returning
  for entry in "${DEMO_NODES[@]}"; do
    name="${entry%%:*}"
    wait_until 60 "node $name stopped" node_stopped "$name" >/dev/null 2>&1 \
      || { warn "node $name did not stop in time; forcing"; lxc stop "$name" --force >/dev/null 2>&1 || true; }
  done
  stop_controller
  log "demo environment stopped"
}

cmd_restart() {
  cmd_down
  local t0 t1
  t0="$(date +%s.%N)"
  cmd_up
  t1="$(date +%s.%N)"
  log "restart: full stop->warm completed in $(awk "BEGIN{printf \"%.1f\", $t1-$t0}")s"
}

# fresh_node_registered NAME -> 0 once the agent is ONLINE and the controller
# has a fresh last_seen for it.
fresh_node_registered() {
  node_state "$1" | grep -q '"state": "ONLINE"' && controller_knows_node "$1"
}

# fresh_node_inference_ok NAME -> 0 once /health answers on the node.
fresh_node_inference_ok() {
  local ip; ip="$(node_ip "$1")"
  [ -n "$ip" ] && curl -fsS -m 3 "http://$ip:$INFERENCE_PORT/health" >/dev/null 2>&1
}

cmd_fresh() {
  require_pinned_image
  local inf="${1:-}"; [ -n "$inf" ] || inf="demo-node-c"
  lxc delete --force "$FRESH_NAME" >/dev/null 2>&1 || true
  log "snapshot-restoring $FRESH_NAME from $inf/$SNAPSHOT_UNREG"
  local ftok t_r0 t_r1 restore_secs t_start el
  local reg_at="" inf_at="" ready_secs
  ftok="$(mint_token term-demo-fresh)"

  # --- restore (copy the snapshot to a brand-new node) ---------------------
  t_r0="$(date +%s.%N)"
  lxc copy "$inf/$SNAPSHOT_UNREG" "$FRESH_NAME" </dev/null
  configure_node "$FRESH_NAME" inference "$ftok"
  # preload the model into the host page cache so the node's llama-server
  # initializes from RAM, not a cold disk read
  warm_model_cache
  t_r1="$(date +%s.%N)"
  restore_secs="$(awk "BEGIN{printf \"%.1f\", $t_r1-$t_r0}")"

  # --- the timed window: from `lxc start` to registered AND inference up ---
  lxc start "$FRESH_NAME" </dev/null
  t_start="$(date +%s.%N)"
  while :; do
    el="$(awk "BEGIN{printf \"%.1f\", $(date +%s.%N)-$t_start}")"
    [ -z "$reg_at" ] && fresh_node_registered "$FRESH_NAME" && reg_at="$el"
    [ -z "$inf_at" ] && fresh_node_inference_ok "$FRESH_NAME" && inf_at="$el"
    if [ -n "$reg_at" ] && [ -n "$inf_at" ]; then break; fi
    if awk "BEGIN{exit !($el >= $REGISTER_BUDGET_S)}"; then break; fi
    sleep 0.5
  done
  ready_secs="$el"
  log "fresh node: restore=${restore_secs}s (copy+configure) registered=${reg_at:-TIMEOUT}s inference=${inf_at:-TIMEOUT}s ready=${ready_secs}s (budget ${REGISTER_BUDGET_S}s)"
  [ -n "$reg_at" ] && [ -n "$inf_at" ] || { warn "fresh node not ready within ${REGISTER_BUDGET_S}s"; return 1; }
  awk "BEGIN{exit !($ready_secs < $REGISTER_BUDGET_S)}"
}

cmd_status() {
  local entry name role
  for entry in "${DEMO_NODES[@]}"; do
    name="${entry%%:*}"; role="$(node_role "$name")"
    printf '%-16s role=%-10s state=%s ip=%s\n' "$name" "$role" \
      "$(lxc list "$name" --format csv -c s 2>/dev/null || echo absent)" "$(node_ip "$name" 2>/dev/null)"
  done
}

cmd_reset() {
  cmd_down
  local entry
  for entry in "${DEMO_NODES[@]}"; do
    lxc delete --force "${entry%%:*}" >/dev/null 2>&1 || true
  done
  lxc delete --force "$FRESH_NAME" >/dev/null 2>&1 || true
  sudo systemctl stop termini-controller.service >/dev/null 2>&1 || true
  rm -f "$CONTROLLER_DB" "$TOKENS_EXTRA_FILE"
  log "demo environment reset (containers + controller state removed)"
}

case "$CMD" in
  bake) cmd_bake "$@" ;;
  up) cmd_up ;;
  warm) cmd_warm ;;
  down) cmd_down ;;
  restart) cmd_restart ;;
  fresh) cmd_fresh "$@" ;;
  reset) cmd_reset ;;
  status) cmd_status ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
