#!/usr/bin/env bash
# verify-bootstrap.sh — acceptance harness for the cloud-init + systemd node
# bootstrap (task t_066b6e3d, W3). Reproduces a FRESH node from the pre-baked
# image alias and checks the card's acceptance criteria:
#
#   1. the node reaches "registered" with the controller, timed from boot
#      (`/proc/uptime` at the controller acknowledgement) and from the bootstrap
#      service start (contract §12.5);
#   2. an SSH login using the cloud-init-injected team key succeeds WITHOUT a
#      password (and password auth is refused);
#   3. the flow is idempotent — a re-run of the key installer does not duplicate
#      the key, and a reboot reconnects without re-enrolling (stable node_id).
#
# It starts its OWN controller instance (fresh DB) so it never disturbs the demo.
# Prints a transcript; the caller redirects it to infra/bootstrap/evidence/.
#
# env: PORT (18099) IMAGE_ALIAS (agentic-os/w3) IMAGE_VERSION (2026.10.0)
#      NODE (bsp-verify) KEEP=1 to leave the node+controller behind for inspection
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

PORT="${PORT:-18099}"
IMAGE_ALIAS="${IMAGE_ALIAS:-agentic-os/w3}"
IMAGE_VERSION="${IMAGE_VERSION:-2026.10.0}"
NODE="${NODE:-bsp-verify}"
ROLE="${ROLE:-compute}"
GW=10.14.100.1
CONTROLLER_URL="http://${GW}:${PORT}"
PROJECT_ID="01J0B00TSTRAP00000000000AA"
PROJECT_TOKEN="bootstrap-verify-project-token"
TOKEN="bsp-verify-$(date +%s)-$RANDOM"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/bsp-verify.XXXXXX")"
KEY="$WORK/team_ed25519"
PIDFILE="$WORK/controller.pid"
DB="$WORK/controller.db"
CLOG="$WORK/controller.log"
FAIL=0

say()  { printf '\n=== %s ===\n' "$*"; }
ok()   { printf 'PASS  %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; FAIL=1; }
raw()  { printf '$ %s\n' "$*"; "$@" 2>&1; }

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "[verify] KEEP=1: node=$NODE controller pid=$(cat "$PIDFILE" 2>/dev/null) work=$WORK"
    return
  fi
  kill "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null || true
}
trap cleanup EXIT

command -v lxc >/dev/null || { echo "lxc not found"; exit 2; }
command -v ssh-keygen >/dev/null || { echo "ssh-keygen not found"; exit 2; }

lxc image list "$IMAGE_ALIAS" --format csv -c f 2>/dev/null | grep -q . \
  || { echo "image alias '$IMAGE_ALIAS' not present"; exit 2; }

# ------------------------------------------------------------------ 0. inputs
say "team SSH key (generated locally for this run; public half is injected)"
raw ssh-keygen -t ed25519 -N "" -C "termini-team-key" -f "$KEY"
TEAM_PUB="$KEY.pub"
printf '%s\n' "public key: $(cat "$TEAM_PUB")"

# ------------------------------------------------------------------ 1. controller
say "start scratch controller on :$PORT (fresh DB)"
( cd "$REPO/controller" && TERMINI_HOST=0.0.0.0 TERMINI_PORT="$PORT" TERMINI_DB_PATH="$DB" \
    TERMINI_BOOTSTRAP_TOKEN="$TOKEN" TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" \
    TERMINI_PROJECT_ID="$PROJECT_ID" TERMINI_MAX_INFERENCE_NODES=1 \
    TERMINI_MIN_REDACTOR_VERSION=3 TERMINI_TELEMETRY_INTERVAL_MS=1000 \
    nohup python3 -m app.main >"$CLOG" 2>&1 & echo $! > "$PIDFILE" )
for _ in $(seq 1 100); do
  curl -fsS -m 1 "http://127.0.0.1:$PORT/v1/telemetry/recent" -H "Authorization: Bearer $PROJECT_TOKEN" >/dev/null 2>&1 && break
  sleep 0.1
done
if curl -fsS -m 1 "http://127.0.0.1:$PORT/v1/telemetry/recent" -H "Authorization: Bearer $PROJECT_TOKEN" >/dev/null 2>&1; then
  ok "controller up"
else
  bad "controller did not come up"; sed -n '1,20p' "$CLOG"; exit 1
fi

count_nodes() {
  curl -fsS -m 2 "http://127.0.0.1:$PORT/v1/projects/$PROJECT_ID/nodes" \
    -H "Authorization: Bearer $PROJECT_TOKEN" 2>/dev/null | grep -o '"node_id"' | wc -l
}
node_ip() { lxc list "$1" --format csv -c 4 2>/dev/null | awk -F'[ ,]' '{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\./) {print $i; exit}}'; }
node_state() { lxc exec "$1" -- /usr/local/bin/termini-bootstrap status </dev/null 2>/dev/null; }
state_field() { node_state "$1" | grep -o "\"$2\": *[^,}]*" | head -1 | sed "s/\"$2\": *//; s/\"//g; s/ *$//"; }

# ------------------------------------------------------------------ 2. provision a FRESH node
say "provision FRESH node '$NODE' from pre-baked alias $IMAGE_ALIAS"
raw lxc delete --force "$NODE"
CONTROLLER_URL="$CONTROLLER_URL" TERMINI_IMAGE_ALIAS="$IMAGE_ALIAS" \
  TERMINI_IMAGE_VERSION="$IMAGE_VERSION" TEAM_SSH_PUBKEY_FILE="$TEAM_PUB" \
  raw bash "$HERE/provision-node.sh" "$NODE" "$ROLE" "$TOKEN"

say "rendered cloud-init user-data (first 40 lines)"
controller_url="$CONTROLLER_URL" node_name="$NODE" enrollment_token="$TOKEN" \
  team_ssh_pubkey="$(cat "$TEAM_PUB")" role_hint="$ROLE" \
  image_alias="$IMAGE_ALIAS" image_version="$IMAGE_VERSION" \
  bash "$HERE/render-cloud-init.sh" | sed -n '1,40p'

# ------------------------------------------------------------------ 3. boot + time it
say "boot node and time boot -> controller acknowledgement"
t0="$(date +%s.%N)"
raw lxc start "$NODE"
for _ in $(seq 1 3000); do          # up to ~60 s, 20 ms poll
  [ "$(count_nodes)" -gt 0 ] && break
  sleep 0.02
done
t1="$(date +%s.%N)"
HOST_BOOT_TO_ACK="$(awk "BEGIN{printf \"%.3f\", $t1-$t0}")"
UP_AT_ACK="$(lxc exec "$NODE" -- cat /proc/uptime </dev/null 2>/dev/null | awk '{print $1}')"

# wait for the daemon to be fully ONLINE (state.json)
for _ in $(seq 1 600); do
  [ "$(state_field "$NODE" state)" = "ONLINE" ] && break
  sleep 0.05
done
REG_MS="$(state_field "$NODE" register_ms)"
BOOT_MS="$(state_field "$NODE" boot_to_registered_ms)"
NODE_ID="$(state_field "$NODE" node_id)"

say "TIMING"
printf 'host clock (lxc start -> controller has node) : %ss\n' "$HOST_BOOT_TO_ACK"
printf 'node /proc/uptime when controller saw node   : %ss\n' "${UP_AT_ACK:-?}"
printf 'boot_to_registered_ms (state.json, node clock): %s ms\n' "${BOOT_MS:-?}"
printf 'register_ms (service start -> 2xx ack)        : %s ms\n' "${REG_MS:-?}"
[ -n "$NODE_ID" ] && ok "registered node_id=$NODE_ID" || bad "no node_id persisted"
if [ -n "$REG_MS" ] && [ "$REG_MS" -lt 5000 ]; then
  ok "registration (service start -> controller ack) ${REG_MS} ms < 5000 ms  [contract §12.5]"
else
  bad "registration ${REG_MS:-?} ms not < 5000 ms"
fi

say "systemd unit state after registration"
raw lxc exec "$NODE" -- systemctl is-active termini-bootstrap.service
raw lxc exec "$NODE" -- systemctl show termini-bootstrap.service -p Type -p SubState
raw lxc exec "$NODE" -- journalctl -u termini-bootstrap.service -b --no-pager -n 6

say "controller's own record of the node"
curl -fsS "http://127.0.0.1:$PORT/v1/projects/$PROJECT_ID/nodes" \
  -H "Authorization: Bearer $PROJECT_TOKEN" | python3 -m json.tool 2>/dev/null | sed -n '1,40p'

# ------------------------------------------------------------------ 4. SSH with the injected key
say "SSH login with the cloud-init-injected team key (no password)"
IP="$(node_ip "$NODE")"
printf 'node ip: %s\n' "$IP"
SSH_COMMON=(-i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8)
for _ in $(seq 1 60); do
  ssh "${SSH_COMMON[@]}" root@"$IP" true >/dev/null 2>&1 && break
  sleep 0.5
done
if out="$(ssh "${SSH_COMMON[@]}" root@"$IP" 'id -un; hostname' 2>&1)"; then
  ok "passwordless SSH as root succeeded: $(printf '%s' "$out" | tr '\n' ' ')"
else
  bad "passwordless SSH as root failed: $out"
fi
raw ssh "${SSH_COMMON[@]}" root@"$IP" 'grep -c . /root/.ssh/authorized_keys; stat -c "%a %U:%G %n" /root/.ssh /root/.ssh/authorized_keys'

say "password auth must be refused"
if ssh -o IdentitiesOnly=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no \
       -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
       -o ConnectTimeout=8 root@"$IP" true >/dev/null 2>&1; then
  bad "password auth unexpectedly succeeded"
else
  ok "password auth refused (PasswordAuthentication no)"
fi
raw ssh "${SSH_COMMON[@]}" root@"$IP" 'sshd -T 2>/dev/null | grep -E "^passwordauthentication|^permitrootlogin|^trustedusercakeys"'

# ------------------------------------------------------------------ 5. idempotency
say "idempotency: re-run the key installer (must not duplicate the key)"
raw lxc exec "$NODE" -- /usr/local/sbin/termini-install-ssh-key
raw lxc exec "$NODE" -- /usr/local/sbin/termini-install-ssh-key
keyline="$(cat "$TEAM_PUB")"
cnt="$(lxc exec "$NODE" -- grep -cxF "$keyline" /root/.ssh/authorized_keys </dev/null 2>/dev/null || echo 0)"
if [ "$cnt" = "1" ]; then ok "exactly one copy of the team key in authorized_keys"; else bad "team key appears $cnt times"; fi

say "idempotency: reboot reconnects with the same node_id (no re-enrollment)"
raw timeout 40 lxc stop "$NODE" --timeout 20
lxc list "$NODE" --format csv -c s 2>/dev/null | grep -q RUNNING && lxc stop "$NODE" --force >/dev/null 2>&1
for _ in $(seq 1 60); do lxc list "$NODE" --format csv -c s 2>/dev/null | grep -q STOPPED && break; sleep 0.5; done
raw lxc start "$NODE"
for _ in $(seq 1 1200); do
  [ "$(state_field "$NODE" state)" = "ONLINE" ] && break
  sleep 0.1
done
NODE_ID2="$(state_field "$NODE" node_id)"
N2="$(count_nodes)"
printf 'node_id before=%s after=%s  node_rows=%s\n' "$NODE_ID" "$NODE_ID2" "$N2"
[ -n "$NODE_ID2" ] && [ "$NODE_ID2" = "$NODE_ID" ] && ok "node_id stable across reboot" || bad "node_id changed after reboot"
[ "$N2" = "1" ] && ok "controller still has exactly one node (no duplicate)" || bad "controller has $N2 node rows"
raw lxc exec "$NODE" -- cat /var/lib/termini/state.json
cnt2="$(lxc exec "$NODE" -- grep -cxF "$keyline" /root/.ssh/authorized_keys </dev/null 2>/dev/null || echo 0)"
[ "$cnt2" = "1" ] && ok "team key still present exactly once after reboot" || bad "team key count after reboot: $cnt2"

# ------------------------------------------------------------------ verdict
say "VERDICT"
if [ "$FAIL" = "0" ]; then
  echo "RESULT: PASS — fresh node registered with the controller, passwordless SSH with the injected key works, flow is idempotent."
else
  echo "RESULT: FAIL — see the FAIL lines above."
fi
exit "$FAIL"
