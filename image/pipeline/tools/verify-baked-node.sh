#!/usr/bin/env bash
# verify-baked-node.sh — boot a node from the baked image family and prove, ON
# THE NODE, that the image carries what the fleet needs. Then delete the node.
#
# This is the behaviour test the recipe check cannot be: `build-image.sh --check`
# proves syntax/JSON, not that a booted node serves LXD on :8443, holds the
# workload image under the controller's alias, and runs a daemon that reports its
# own LXD endpoint. Only a real boot answers those.
#
# Checks (all on the node, raw output preserved):
#   1. curl -sk https://127.0.0.1:8443/1.0            -> 200
#   2. lxc list -> exit 0, AND a USABLE hypervisor: a `default` storage pool,
#      the lxdbr0 bridge, and a `default` profile with a root disk
#   3. lxc image list shows agent-vm/<version> AND agent-os/w4 (same fingerprint)
#   4. the baked daemon reports its LXD endpoint at registration — a recording
#      stub captures the exact `machine.lxd.endpoint` the node sends
#   5. the offline sentinel + image-version are present
#   6. a container LAUNCHES from the workload alias (the exact call the
#      controller's instance.create makes: alias + profile default, no devices)
#
# Usage:
#   image/pipeline/tools/verify-baked-node.sh                 # newest termini-node-os
#   image/pipeline/tools/verify-baked-node.sh --image-name termini-node-os-4
#   --image-family F / --image-name N   which image to boot (default family=termini-node-os)
#   --underlay-ip IP                    free underlay IP for the test node (default 10.10.0.20)
#   --node-name N                       test instance name (default termini-node-verify)
#   --zone / --project / --network / --subnet / --tags
#   --keep-node                         do NOT delete the test node afterwards
#   --dry-run
#   -h|--help
set -euo pipefail

PROJECT="${TERMINI_GCP_PROJECT:-termini-510609}"
ZONE="${TERMINI_GCP_ZONE:-asia-south1-a}"
NETWORK="${TERMINI_GCP_NETWORK:-termini-vpc}"
SUBNET="${TERMINI_GCP_SUBNET:-termini-underlay}"
TAGS="${TERMINI_GCP_TAGS:-termini-node}"
IMAGE_FAMILY="${TERMINI_IMAGE_FAMILY:-termini-node-os}"
IMAGE_NAME=""
UNDERLAY_IP="${TERMINI_VERIFY_IP:-10.10.0.20}"
NODE_NAME="${TERMINI_VERIFY_NAME:-termini-node-verify}"
VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"
MACHINE_TYPE="${TERMINI_VERIFY_MACHINE_TYPE:-n2-standard-2}"
KEEP_NODE=0
DRY_RUN=0

IMAGE_ARG=()
log() { printf '[verify-node] %s\n' "$*" >&2; }
die() { printf '[verify-node] FATAL: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,30p' "$0"; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --image-family) IMAGE_FAMILY="$2"; shift 2 ;;
    --image-name) IMAGE_NAME="$2"; shift 2 ;;
    --underlay-ip) UNDERLAY_IP="$2"; shift 2 ;;
    --node-name) NODE_NAME="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --subnet) SUBNET="$2"; shift 2 ;;
    --tags) TAGS="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --keep-node) KEEP_NODE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [ -n "$IMAGE_NAME" ]; then IMAGE_ARG=(--image "$IMAGE_NAME"); else IMAGE_ARG=(--image-family "$IMAGE_FAMILY"); fi

command -v gcloud >/dev/null 2>&1 || die "gcloud not on PATH"

if [ "$DRY_RUN" = "1" ]; then
  log "--dry-run: would create $NODE_NAME @ $UNDERLAY_IP from ${IMAGE_ARG[*]} in $PROJECT/$ZONE"
  exit 0
fi

if [ -n "$IMAGE_NAME" ]; then
  src="$(gcloud compute images describe "$IMAGE_NAME" --project "$PROJECT" --format='value(name,family,status)' 2>/dev/null || true)"
else
  src="$(gcloud compute images describe-from-family "$IMAGE_FAMILY" --project "$PROJECT" --format='value(name,family,status)' 2>/dev/null || true)"
fi
[ -n "$src" ] || die "no image resolves for ${IMAGE_NAME:-family=$IMAGE_FAMILY} in $PROJECT"
log "booting from: $src"

node_exists() { gcloud compute instances describe "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" >/dev/null 2>&1; }
delete_node() {
  if node_exists; then
    log "deleting test node $NODE_NAME"
    gcloud compute instances delete "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" --quiet >/dev/null 2>&1 \
      || log "WARNING: could not delete $NODE_NAME"
  fi
}
# Teardown hangs off EXIT, not ERR: `die` exits directly and an ERR trap would
# miss it, leaving the test node behind. EXIT fires on success, `die` and `set -e`.
teardown() {
  rc=$?
  [ "$rc" = 0 ] || printf '[verify-node] FAILED (rc=%s); deleting the test node\n' "$rc" >&2
  if [ "$KEEP_NODE" = "1" ]; then
    node_exists && log "--keep-node: leaving $NODE_NAME up"
  else
    delete_node
  fi
}
trap teardown EXIT
node_exists && die "test node $NODE_NAME already exists — delete it or use --node-name"

log "creating test node $NODE_NAME from ${IMAGE_ARG[*]}"
gcloud compute instances create "$NODE_NAME" \
  --project "$PROJECT" --zone "$ZONE" \
  --machine-type "$MACHINE_TYPE" \
  "${IMAGE_ARG[@]}" --image-project "$PROJECT" \
  --boot-disk-size 20GB --boot-disk-type pd-balanced \
  --network "$NETWORK" --subnet "$SUBNET" \
  --private-network-ip "$UNDERLAY_IP" --tags "$TAGS" \
  --scopes=cloud-platform --quiet >/dev/null

log "waiting for the node to boot"
# Prefer a direct SSH connection (the node gets an ephemeral external IP);
# fall back to an IAP tunnel when it does not.
TUNNEL=()
pick_ssh_transport() {
  if gcloud compute ssh "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" \
       --command 'echo T_OK' --quiet 2>/dev/null | grep -q T_OK; then
    TUNNEL=(); log "SSH transport: direct"; return 0
  fi
  if gcloud compute ssh "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" \
       --tunnel-through-iap --command 'echo T_OK' --quiet 2>/dev/null | grep -q T_OK; then
    TUNNEL=(--tunnel-through-iap); log "SSH transport: IAP tunnel"; return 0
  fi
  return 1
}
for _ in $(seq 1 60); do
  if gcloud compute ssh "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" \
       --command 'echo SSH_READY' --quiet 2>/dev/null | grep -q SSH_READY \
     || gcloud compute ssh "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
       --command 'echo SSH_READY' --quiet 2>/dev/null | grep -q SSH_READY; then
    break
  fi
  sleep 10
done
pick_ssh_transport || die "no working SSH transport to $NODE_NAME"

gssh() { gcloud compute ssh "$NODE_NAME" --project "$PROJECT" --zone "$ZONE" "${TUNNEL[@]}" \
           --command="$1" --quiet; }

# ---------------------------------------------------------------------------
# The on-node check script.
#
# It is DELIVERED AS A FILE and run with `bash <file>`, never piped into `bash -s`.
# Reason: `bash -s` reads its script from stdin incrementally, so a child that
# reads stdin (LXD's `lxc launch` does) steals the rest of the script and feeds it
# to the daemon — which surfaces as a nonsense API parse error
# ("cannot unmarshal !!str `printf ...` into api.InstancePut") instead of the real
# launch result. A file removes the contention completely.
# ---------------------------------------------------------------------------
REMOTE_SCRIPT="$(cat <<'REMOTE'
set -uo pipefail
fail=0
say() { printf '%s\n' "$*"; }
chk() { # NAME EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then say "  CHECK $1: PASS ($3)"; else say "  CHECK $1: FAIL expected=[$2] actual=[$3]"; fail=1; fi
}
say "== host =="; hostname; uname -r; cat /etc/termini/image-version 2>/dev/null || say "no image-version"

say "== 1. LXD API on :8443 =="
code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 https://127.0.0.1:8443/1.0 || echo 000)"
say "  curl -sk https://127.0.0.1:8443/1.0 -> $code"
chk api_8443_http_code 200 "$code"
chk core_https_address ":8443" "$(lxc config get core.https_address 2>/dev/null || echo NONE)"

say "== 2. lxc list + a USABLE hypervisor =="
lxc list --format csv; chk lxc_list_rc 0 "$?"
# Listening is not usable. The controller's instance.create sends
# profiles: ["default"] with no devices, so a missing pool / device-less default
# profile means no instance can ever be created on this node.
lxc storage show default >/dev/null 2>&1; chk storage_pool_default 0 "$?"
if lxc network show lxdbr0 >/dev/null 2>&1; then chk guest_bridge_lxdbr0 1 1; else chk guest_bridge_lxdbr0 1 0; fi
chk default_profile_root_disk 1 "$(lxc profile device show default 2>/dev/null | grep -c '^root:')"

say "== 3. workload image in the local store (both aliases) =="
lxc image list
wf="$(lxc image info "agent-vm/${TERMINI_IMAGE_VERSION}" 2>/dev/null | awk '/^Fingerprint:/{print $2}')"
nf="$(lxc image info agent-os/w4 2>/dev/null | awk '/^Fingerprint:/{print $2}')"
say "  agent-vm/${TERMINI_IMAGE_VERSION} fingerprint=${wf:-<missing>}"
say "  agent-os/w4 fingerprint=${nf:-<missing>}"
[ -n "$wf" ] && chk alias_agent_vm "$wf" "$nf" || { say "  CHECK alias_agent_vm: FAIL (agent-vm/${TERMINI_IMAGE_VERSION} missing)"; fail=1; }

say "== 4. the baked daemon reports its own LXD endpoint =="
if grep -q '"endpoint"' /usr/local/bin/termini-bootstrap; then say "  daemon carries the endpoint field: yes"; else say "  CHECK bootstrap_reports_endpoint: FAIL (field absent)"; fail=1; fi
# Recording stub: record ONLY the registration body, so the daemon's later
# telemetry POSTs cannot overwrite it.
cat > /tmp/termini-regstub.py <<'PY'
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def _read(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n)
    def do_POST(self):
        body = self._read()
        kind = 'register' if self.path.endswith('/register') else 'telemetry'
        with open('/tmp/termini-%s-body.json' % kind, 'wb') as fh:
            fh.write(body)
        resp = json.dumps({"node_id": "01NODEFROMSTUB000000000000", "node_token": "stub-token", "role": "compute"}).encode()
        self.send_response(201 if kind == 'register' else 202)
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(resp)))
        self.end_headers(); self.wfile.write(resp)
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 9099), H).serve_forever()
PY
pkill -f termini-regstub.py 2>/dev/null || true; sleep 1
rm -f /tmp/termini-register-body.json /tmp/termini-telemetry-body.json
nohup python3 /tmp/termini-regstub.py >/tmp/termini-regstub.log 2>&1 &
sleep 2
iface="$(ip -o -4 route show default | awk '{print $5; exit}')"; iface="${iface:-ens4}"
mkdir -p /etc/termini /var/lib/termini/identity
cat > /etc/termini/config.yaml <<CFG
controller_url: http://127.0.0.1:9099
node_name: $(hostname)
iface: $iface
image_version: ${TERMINI_IMAGE_VERSION}
role_hint: compute
CFG
say "  on-node config.yaml:"; sed 's/^/    /' /etc/termini/config.yaml
echo "verify-enroll-token" > /var/lib/termini/enrollment.token
systemctl stop termini-bootstrap.service 2>/dev/null || true
pkill -f '/usr/local/bin/termini-bootstrap' 2>/dev/null || true
sleep 1
# Run the daemon against the stub exactly as the unit would.
timeout 25 /usr/local/bin/termini-bootstrap >/tmp/termini-bootstrap-verify.log 2>&1 || true
say "  daemon log:"; sed 's/^/    /' /tmp/termini-bootstrap-verify.log | head -20
if [ -s /tmp/termini-register-body.json ]; then
  say "  registration body captured by the stub:"
  if python3 -c "import json;d=json.load(open('/tmp/termini-register-body.json'));print('    machine.lxd.endpoint  =',d['machine']['lxd'].get('endpoint'));print('    machine.hostname      =',d['machine']['hostname']);print('    machine.image_version =',d['machine'].get('image_version'))" 2>/tmp/termini-ep.err; then
    ep="$(python3 -c "import json;print(json.load(open('/tmp/termini-register-body.json'))['machine']['lxd'].get('endpoint',''))")"
    chk machine_lxd_endpoint "${UNDERLAY_IP}:8443" "$ep"
  else
    say "  CHECK machine_lxd_endpoint: FAIL (could not parse the captured body)"; sed 's/^/    /' /tmp/termini-ep.err; fail=1
  fi
else
  say "  CHECK machine_lxd_endpoint: FAIL (no registration body captured)"; fail=1
fi

say "== 5. offline sentinel + version =="
chk baked_offline_sentinel 0 "$(test -f /var/lib/termini/.baked-offline; echo $?)"
chk image_version_present 0 "$(test -s /etc/termini/image-version; echo $?)"

# Launch a container from the workload alias. This is not decoration: it is the
# exact call the controller's instance.create makes (image alias + profile
# `default`, no explicit devices), so a node on which it fails cannot be used.
say "== 6. launch a container from the workload alias =="
lxc delete verify-container --force 2>/dev/null || true
if timeout 180 lxc launch agent-os/w4 verify-container </dev/null >/tmp/termini-launch.log 2>&1; then
  if lxc list --format csv -c ns | grep -q '^verify-container,'; then
    say "  CHECK workload_launchable: PASS (container running)"; say "  $(lxc list verify-container --format csv -c ns)"
  else
    say "  CHECK workload_launchable: FAIL (launched but not listed)"; fail=1
  fi
  lxc delete verify-container --force 2>/dev/null || true
else
  say "  CHECK workload_launchable: FAIL (launch failed)"; fail=1
  sed 's/^/    /' /tmp/termini-launch.log | head -8
fi

printf 'VERIFY_RESULT=%s\n' "$([ "$fail" = 0 ] && echo PASS || echo FAIL)"
exit "$fail"
REMOTE
)"

B64="$(printf '%s' "$REMOTE_SCRIPT" | base64 -w0)"
log "running on-node checks"
OUT="$(gssh "echo $B64 | base64 -d > /tmp/termini-verify-on-node.sh && sudo env TERMINI_IMAGE_VERSION='$VERSION' UNDERLAY_IP='$UNDERLAY_IP' bash /tmp/termini-verify-on-node.sh")" || true
printf '%s\n' "$OUT" >&2

printf '%s\n' "$OUT" | grep -q 'VERIFY_RESULT=PASS' || die "on-node verification FAILED (see output above)"

log "PASS — the baked image serves LXD on :8443, holds the workload image under both aliases, and its daemon reports its own LXD endpoint"
