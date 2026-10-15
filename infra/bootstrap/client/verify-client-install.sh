#!/usr/bin/env bash
# verify-client-install.sh — hermetic acceptance harness for the client install flow
# (task t_70bd0f2b). Runs the whole documented flow against a REAL controller
# process and the REAL node daemon, on loopback, with no root, no systemd and no
# cloud spend:
#
#   1. provider handover  — issue-enrollment.py mints a single-use credential and
#      prints the customer command (asserting the project token leaks nowhere);
#   2. the customer one-liner — `curl -fsSL <controller>/install/termini-node.sh |
#      sh -s -- --controller … --token …` installs the node;
#   3. registration + the client's own view — termini-node-status shows the
#      registered node and its first telemetry;
#   4. re-run — a second credential is NOT consumed;
#   5. the three failure modes — rejected / already-used credential, unreachable
#      controller — each with its own message and exit code.
#
# What it deliberately does NOT cover (it needs a real machine, and is run in the
# approved live window): the systemd start and the installer's own wait loop
# (`systemctl restart termini-bootstrap.service`), which is what `--root` +
# `--no-start` stands in for here. The harness prints that boundary in its summary.
#
# Usage: infra/bootstrap/client/verify-client-install.sh [workdir]
# Exit: 0 all checks passed, 1 otherwise.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
CLIENT="$REPO/infra/bootstrap/client"
DAEMON="$REPO/infra/bootstrap/node/termini-bootstrap.py"

WORK="${1:-$(mktemp -d "${TMPDIR:-/tmp}/termini-client-verify.XXXXXX")}"
mkdir -p "$WORK"
PROJECT_ID="01J0DEM0WARM000000000000AB"
PROJECT_TOKEN="harness-project-token"
PASS=0
FAIL=0

say()  { printf '\n=== %s ===\n' "$*"; }
ok()   { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
need() { # need <condition-description> <output-file> <pattern>
  if grep -qF -- "$3" "$2"; then ok "$1"; else bad "$1 (missing: $3)"; sed 's/^/      | /' "$2"; fi
}

CONTROLLER_PID=""
NODE_PID=""

stop_node() {
  if [ -n "$NODE_PID" ]; then kill "$NODE_PID" 2>/dev/null || true; fi
  NODE_PID=""
}

stop_controller() {
  if [ -n "$CONTROLLER_PID" ]; then
    kill "$CONTROLLER_PID" 2>/dev/null || true
    wait "$CONTROLLER_PID" 2>/dev/null || true
  fi
  CONTROLLER_PID=""
}

cleanup() {
  stop_node
  stop_controller
}
trap cleanup EXIT

# --------------------------------------------------------------------------- controller
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
BASE="http://127.0.0.1:$PORT"
printf '%s\n' "$PROJECT_TOKEN" > "$WORK/project-token.txt"
chmod 0600 "$WORK/project-token.txt"

say "controller: starting a real controller process on $BASE"
# `exec` matters: without it the PID captured by `$!` is the wrapper subshell, and
# the controller survives the harness holding its listening port — the leaked
# controller this script must never leave behind (kill it by explicit PID, never
# `pkill -f`).
( cd "$REPO/controller" && exec env \
    TERMINI_HOST=127.0.0.1 TERMINI_PORT="$PORT" \
    TERMINI_DB_PATH="$WORK/controller.db" \
    TERMINI_BOOTSTRAP_TOKEN="harness-boot-1,harness-boot-2" \
    TERMINI_PROJECT_ID="$PROJECT_ID" TERMINI_PROJECT_TOKEN="$PROJECT_TOKEN" \
    TERMINI_OPERATOR_ENABLED=false TERMINI_LXD_AUTO_PROVISION=false \
    python3 -m app.main ) > "$WORK/controller.log" 2>&1 &
CONTROLLER_PID=$!
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PROJECT_TOKEN" \
          "$BASE/v1/projects/$PROJECT_ID/nodes" || true)"
  [ "$code" = "200" ] && break
  sleep 0.5
done
if [ "${code:-}" != "200" ]; then
  bad "controller did not become ready (last HTTP ${code:-none})"
  sed 's/^/      | /' "$WORK/controller.log"
  exit 1
fi
ok "controller ready (HTTP 200 on an authenticated route)"

# --------------------------------------------------------------------------- 1. handover
say "1. provider handover: mint a single-use credential and print the customer command"
python3 "$CLIENT/issue-enrollment.py" --public-url "$BASE" --controller "$BASE" \
  --project-token-file "$WORK/project-token.txt" --name client-node \
  --out "$WORK/handover.txt" > "$WORK/handover.out" 2>&1 || true
cat "$WORK/handover.out"
need "handover preflighted the public install surface" "$WORK/handover.out" "install surface OK"
need "handover printed the customer one-liner" "$WORK/handover.out" "install/termini-node.sh | sudo sh -s --"
if grep -qF "$PROJECT_TOKEN" "$WORK/handover.txt" || grep -qF "$PROJECT_TOKEN" "$WORK/handover.out"; then
  bad "the project token leaked into the handover"
else
  ok "the project token appears in neither the handover file nor its output"
fi
CRED="$(grep -o -- '--token [A-Za-z0-9_-]\{8,\}' "$WORK/handover.txt" | head -1 | cut -d' ' -f2)"
[ -n "$CRED" ] && ok "handover carries a single-use credential" || { bad "no credential in handover"; exit 1; }

# --------------------------------------------------------------------------- 2. the one-liner
say "2. the customer one-liner: curl | sh -s -- (installs this machine, no provider shell)"
ROOT="$WORK/node1"
# shellcheck disable=SC2016 # the pipe is the point of the check
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "$CRED" --name client-node --root "$ROOT" --no-start \
  > "$WORK/install1.out" 2>&1 || true
cat "$WORK/install1.out"
need "the one-liner fetched the installer over HTTP and ran it" "$WORK/install1.out" "termini-node-install"
need "it installed the node runtime from the controller" "$WORK/install1.out" "node runtime: not present"
need "it installed the client status helper" "$WORK/install1.out" "client status helper: installed"
need "it stored the credential 0600" "$WORK/install1.out" "enrollment credential stored (0600)"
if grep -qF "$CRED" "$WORK/install1.out"; then
  bad "the installer echoed the credential back"
else
  ok "the installer never echoed the credential"
fi
HELPER="$ROOT/usr/local/bin/termini-node-status"
[ -x "$HELPER" ] && [ -x "$ROOT/usr/local/bin/termini-bootstrap" ] \
  && ok "runtime + status helper installed under the node root" \
  || bad "runtime or status helper missing under $ROOT"

node_env() { # node_env <root>  -> prints the env overrides the unit itself uses
  printf 'TERMINI_CONFIG=%s TERMINI_TOKEN_FILE=%s TERMINI_IDENTITY_DIR=%s TERMINI_STATE_PATH=%s TERMINI_SPOOL_DIR=%s' \
    "$1/etc/termini/config.yaml" "$1/var/lib/termini/enrollment.token" \
    "$1/var/lib/termini/identity" "$1/var/lib/termini/state.json" \
    "$1/var/lib/termini/telemetry"
}

start_node() { # start_node <root> ; sets NODE_PID
  # What `systemctl restart termini-bootstrap.service` does on a real machine:
  # start the baked/pre-installed daemon with its config and credential in place.
  # shellcheck disable=SC2046 # word-splitting the KEY=VALUE list is intended
  env $(node_env "$1") python3 "$DAEMON" >> "$1/daemon.log" 2>&1 &
  NODE_PID=$!
}

stop_node() {
  if [ -n "$NODE_PID" ]; then kill "$NODE_PID" 2>/dev/null || true; fi
  NODE_PID=""
}

status_of() { # status_of <root> <outfile> -> exit code of the client's own view
  local rc=0
  # shellcheck disable=SC2046
  env $(node_env "$1") "$HELPER" --wait --timeout 60 > "$2" 2>&1 || rc=$?
  return "$rc"
}

# --------------------------------------------------------------------------- 3. registration
say "3. registration and the client's own view of the node"
start_node "$ROOT"
rc=0
status_of "$ROOT" "$WORK/status1.out" || rc=$?
cat "$WORK/status1.out"
[ "$rc" = "0" ] && ok "termini-node-status confirms the registration" \
                || bad "termini-node-status exited $rc"
need "the client sees the node id" "$WORK/status1.out" "node_id"
need "the client sees the assigned role" "$WORK/status1.out" "role"
need "the client sees its first telemetry" "$WORK/status1.out" "HEALTH"
need "the node acknowledged the registration" "$WORK/status1.out" "REGISTERED"
NODE_ID="$(sed -n 's/.*"node_id": "\([^"]*\)".*/\1/p' "$ROOT/var/lib/termini/identity/node.json" | head -1)"
fleet="$(curl -s -H "Authorization: Bearer $PROJECT_TOKEN" "$BASE/v1/projects/$PROJECT_ID/nodes")"
case "$fleet" in *"$NODE_ID"*) ok "the controller lists the node ($NODE_ID)";;
                *) bad "the controller does not list $NODE_ID"; printf '%s\n' "$fleet";; esac
stop_node

# --------------------------------------------------------------------------- 4. re-run
say "4. re-run on the registered node: identity reused, no second credential burned"
python3 "$CLIENT/issue-enrollment.py" --public-url "$BASE" --controller "$BASE" \
  --project-token-file "$WORK/project-token.txt" --name client-node-2 \
  --out "$WORK/handover2.txt" > "$WORK/handover2.out" 2>&1 || true
CRED2="$(grep -o -- '--token [A-Za-z0-9_-]\{8,\}' "$WORK/handover2.txt" | head -1 | cut -d' ' -f2)"
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "$CRED2" --name client-node --root "$ROOT" --no-start \
  > "$WORK/install2.out" 2>&1 || true
cat "$WORK/install2.out"
need "the re-run recognised the persisted identity" "$WORK/install2.out" "already registered"
need "the re-run did not consume the new credential" "$WORK/install2.out" "NOT used and is NOT consumed"
# The proof that it survived: the very same credential registers a DIFFERENT machine.
ROOT2="$WORK/node2"
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "$CRED2" --name client-node-2 --root "$ROOT2" --no-start \
  > "$WORK/install3.out" 2>&1 || true
start_node "$ROOT2"
rc=0
status_of "$ROOT2" "$WORK/status2.out" || rc=$?
if grep -q "YOUR NODE IS REGISTERED" "$WORK/status2.out"; then
  ok "the unburned credential registered a second node"
else
  bad "the credential was burned by the re-run"; sed 's/^/      | /' "$WORK/status2.out"
fi
stop_node

# --------------------------------------------------------------------------- 5. failure modes
say "5a. a rejected credential"
ROOT3="$WORK/node3"
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "harness-not-a-real-credential" --name client-node-3 \
  --root "$ROOT3" --no-start > "$WORK/install4.out" 2>&1 || true
start_node "$ROOT3"
rc=0
status_of "$ROOT3" "$WORK/fail-cred.out" || rc=$?
cat "$WORK/fail-cred.out"
[ "$rc" = "3" ] && ok "rejected credential exits 3" || bad "rejected credential exited $rc, expected 3"
need "rejected credential message is actionable" "$WORK/fail-cred.out" "enrollment credential was rejected"
stop_node

say "5b. an already-used credential (the same one that registered node1)"
ROOT4="$WORK/node4"
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "$CRED" --name client-node-4 --root "$ROOT4" --no-start \
  > "$WORK/install5.out" 2>&1 || true
start_node "$ROOT4"
rc=0
status_of "$ROOT4" "$WORK/fail-used.out" || rc=$?
cat "$WORK/fail-used.out"
[ "$rc" = "4" ] && ok "used credential exits 4" || bad "used credential exited $rc, expected 4"
need "used credential message is actionable" "$WORK/fail-used.out" "already been used"
stop_node

say "5c. an unreachable controller (config points at a black hole)"
ROOT5="$WORK/node5"
curl -fsSL "$BASE/install/termini-node.sh" | sh -s -- \
  --controller "$BASE" --token "harness-not-a-real-credential" --name client-node-5 \
  --root "$ROOT5" --no-start > "$WORK/install6.out" 2>&1 || true
# Simulate the controller going away / a wrong URL in the node's config.
python3 - "$ROOT5/etc/termini/config.yaml" <<'PY'
import sys
path = sys.argv[1]
lines = []
with open(path, encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("controller_url:"):
            line = "controller_url: http://127.0.0.1:9\n"
        lines.append(line)
with open(path, "w", encoding="utf-8") as fh:
    fh.writelines(lines)
PY
start_node "$ROOT5"
rc=0
status_of "$ROOT5" "$WORK/fail-net.out" || rc=$?
cat "$WORK/fail-net.out"
[ "$rc" = "5" ] && ok "unreachable controller exits 5" || bad "unreachable controller exited $rc, expected 5"
need "unreachable message is actionable" "$WORK/fail-net.out" "controller could not be reached"
distinct="$(cat "$WORK/fail-cred.out" "$WORK/fail-used.out" "$WORK/fail-net.out" | sort -u | wc -l)"
[ "$distinct" -ge 3 ] && ok "the three failure messages are distinct" \
                       || bad "the three failure messages are not distinct ($distinct distinct lines)"
stop_node

# --------------------------------------------------------------------------- teardown
stop_node
stop_controller
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    bad "the harness leaked its controller (port $PORT still listening)"
  else
    ok "no leaked controller (port $PORT released)"
  fi
fi

# --------------------------------------------------------------------------- summary
say "summary"
printf '  checks passed: %d\n  checks failed: %d\n  workdir:       %s\n' "$PASS" "$FAIL" "$WORK"
cat <<'EOF'
  covered here:     handover + one-liner + registration + client view + re-run +
                    the three failure modes, against a real controller process
                    and the real node daemon on loopback.
  covered live:     the systemd start path and the installer's own wait loop
                    (`systemctl restart termini-bootstrap.service`) — run this
                    flow on a machine booted from the termini-node-os image in
                    the approved live window.
EOF
[ "$FAIL" = "0" ] || exit 1
exit 0
