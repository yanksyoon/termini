#!/usr/bin/env bash
# Evidence harness for the client view (task t_5649d117).
#
# Runs the client path for real, with NO GCP and NO stub:
#
#   1. starts a REAL controller process (the same way the shipped client-install
#      harness does) with its own state dir and project credential;
#   2. mints a single-use enrollment credential through the provider helper and
#      installs a node with the DOCUMENTED customer one-liner
#      (`curl -fsSL <controller>/install/termini-node.sh | sh -s --`);
#   3. starts the real node daemon so the controller ingests real telemetry;
#   4. serves the dashboard bundle on a DIFFERENT origin from the controller;
#   5. parks, printing the dashboard URL, the project id and the node's own
#      credentials (into `$WORK/env.json`, 0600) so a browser can be pointed at
#      it: the "Your node" panel then reads the node with its own credential.
#
# It prints NO credential itself, and the client-facing URL it prints carries no
# credential either — that is the property being evidenced.
#
# Usage: dashboard/evidence/run-client-view-evidence.sh [workdir]
# Stop it with Ctrl-C / `kill <pid>`; the trap removes both processes.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${1:-$(mktemp -d "${TMPDIR:-/tmp}/termini-client-view.XXXXXX")}"
CLIENT="$REPO/infra/bootstrap/client"
DAEMON="$REPO/infra/bootstrap/node/termini-bootstrap.py"
PROJECT_ID="01J0DEM0WARM000000000000AB"
PROJECT_TOKEN="evidence-project-token"
mkdir -p "$WORK"

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

CPORT="$(free_port)"
DPORT="$(free_port)"
BASE="http://127.0.0.1:$CPORT"
DASH="http://127.0.0.1:$DPORT"

printf '%s\n' "$PROJECT_TOKEN" > "$WORK/project-token.txt"
chmod 0600 "$WORK/project-token.txt"

CONTROLLER_PID=""
DAEMON_PID=""
DASH_PID=""
cleanup() {
  [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null || true
  [ -n "$DASH_PID" ] && kill "$DASH_PID" 2>/dev/null || true
  [ -n "$CONTROLLER_PID" ] && kill "$CONTROLLER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

say() { printf '\n=== %s ===\n' "$*"; }

# ------------------------------------------------------------------- controller
say "controller: real process on $BASE"
( cd "$REPO/controller" && \
  TERMINI_HOST=127.0.0.1 TERMINI_PORT="$CPORT" \
  TERMINI_DB_PATH="$WORK/controller.db" \
  TERMINI_BOOTSTRAP_TOKEN="ev-boot-1" \
  TERMINI_PROJECT_ID="$PROJECT_ID" TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" \
  TERMINI_OPERATOR_ENABLED=false TERMINI_LXD_AUTO_PROVISION=false \
  python3 -m app.main >"$WORK/controller.log" 2>&1 & echo $! >"$WORK/controller.pid" )
CONTROLLER_PID="$(cat "$WORK/controller.pid")"

code=""
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PROJECT_TOKEN" \
          "$BASE/v1/projects/$PROJECT_ID/nodes" || true)"
  [ "$code" = "200" ] && break
  sleep 0.5
done
[ "${code:-}" = "200" ] || { echo "controller not ready (last $code)"; sed 's/^/  | /' "$WORK/controller.log"; exit 1; }
echo "controller ready"

# ------------------------------------------------------------------- 1. handover
say "1. provider handover (loopback mint, single-use credential)"
python3 "$CLIENT/issue-enrollment.py" --public-url "$BASE" --controller "$BASE" \
  --project-token-file "$WORK/project-token.txt" --name client-view-node \
  --out "$WORK/handover.txt" >"$WORK/handover.out" 2>&1
CRED="$(grep -o -- '--token [A-Za-z0-9_-]\{8,\}' "$WORK/handover.txt" | head -1 | cut -d' ' -f2)"
[ -n "$CRED" ] || { echo "no credential minted"; sed 's/^/  | /' "$WORK/handover.out"; exit 1; }
echo "credential minted (not printed)"

# ---------------------------------------------------------------- 2. one-liner
say "2. the documented customer one-liner"
ROOT="$WORK/root"
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "$CRED" --name client-view-node --root "$ROOT" --no-start \
  >"$WORK/install.out" 2>&1
grep -q "enrollment credential stored" "$WORK/install.out" || { echo "install failed"; sed 's/^/  | /' "$WORK/install.out"; exit 1; }
echo "installer wrote the node config + its own credential (0600)"

# ---------------------------------------------------------------- 3. real daemon
say "3. real node daemon (telemetry the controller really ingests)"
node_env() {
  printf 'TERMINI_CONFIG=%s TERMINI_TOKEN_FILE=%s TERMINI_IDENTITY_DIR=%s TERMINI_STATE_PATH=%s TERMINI_SPOOL_DIR=%s' \
    "$ROOT/etc/termini/config.yaml" "$ROOT/var/lib/termini/enrollment.token" \
    "$ROOT/var/lib/termini/identity" "$ROOT/var/lib/termini/state.json" \
    "$ROOT/var/lib/termini/telemetry"
}
# shellcheck disable=SC2046 # word-splitting the KEY=VALUE list is intended
env $(node_env) python3 "$DAEMON" >>"$ROOT/daemon.log" 2>&1 &
DAEMON_PID=$!

# The customer's own confirmation path, run the way the installer runs it.
STATUS_OUT="$WORK/status.out"
for _ in $(seq 1 3); do
  # shellcheck disable=SC2046
  env $(node_env) python3 "$ROOT/usr/local/bin/termini-node-status" --wait --timeout 60 \
    >"$STATUS_OUT" 2>&1 || true
  grep -q "YOUR NODE IS REGISTERED" "$STATUS_OUT" && break
done
grep -q "YOUR NODE IS REGISTERED" "$STATUS_OUT" || { echo "the node never registered"; sed 's/^/  | /' "$STATUS_OUT"; tail -20 "$ROOT/daemon.log"; exit 1; }
echo "the node registered (customer path: termini-node-status --wait)"

IDENT="$ROOT/var/lib/termini/identity/node.json"
NODE_ID=""
NODE_TOKEN=""
for _ in $(seq 1 120); do
  if [ -s "$IDENT" ]; then
    NODE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["node_id"])' "$IDENT" 2>/dev/null || true)"
    NODE_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["node_token"])' "$IDENT" 2>/dev/null || true)"
    [ -n "$NODE_ID" ] && [ -n "$NODE_TOKEN" ] && break
  fi
  sleep 0.5
done
[ -n "$NODE_ID" ] || { echo "no node identity"; tail -20 "$ROOT/daemon.log"; exit 1; }

# wait for the controller to actually hold a telemetry sample for this node
SAMPLES="0"
for _ in $(seq 1 60); do
  SAMPLES="$(curl -s -H "Authorization: Bearer $NODE_TOKEN" \
             "$BASE/v1/nodes/$NODE_ID/telemetry/recent?limit=5" \
             | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("samples") or []))' 2>/dev/null || echo 0)"
  [ "${SAMPLES:-0}" -ge 3 ] && break
  sleep 1
done
echo "telemetry samples stored for the node: $SAMPLES"

# -------------------------------------------------------------------- 4. dashboard
say "4. dashboard served on a second origin: $DASH"
( cd "$REPO" && python3 -m http.server "$DPORT" --bind 127.0.0.1 --directory dashboard \
  >"$WORK/dashboard.log" 2>&1 & echo $! >"$WORK/dash.pid" )
DASH_PID="$(cat "$WORK/dash.pid")"
for _ in $(seq 1 40); do
  curl -s -o /dev/null "$DASH/index.html" && break
  sleep 0.25
done

# no credential in the URL the client is given
DASH_URL="$DASH/?apiBase=$BASE/v1&projectId=$PROJECT_ID"
case "$DASH_URL" in
  *token=*) echo "REFUSING: the dashboard URL carries a credential"; exit 1;;
esac

python3 - "$WORK/env.json" "$DASH_URL" "$BASE" "$PROJECT_ID" "$NODE_ID" "$NODE_TOKEN" <<'PY'
import json, os, stat, sys
path, dash, base, pid, nid, nt = sys.argv[1:7]
with open(path, "w") as fh:
    json.dump({"dashboard_url": dash, "controller": base, "project_id": pid,
               "node_id": nid, "node_token": nt}, fh)
os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
print("env written:", path, "(0600)")
PY

say "ready — point a browser at: $DASH_URL"
echo "node id      : $NODE_ID"
echo "state file   : $WORK/state.json   (node daemon state)"
echo "controller   : $BASE"
echo "work dir     : $WORK"
echo
echo "Ctrl-C to stop (controller + daemon + dashboard all stop with this script)."

# park until killed
while kill -0 "$CONTROLLER_PID" 2>/dev/null; do sleep 2; done
