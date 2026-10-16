#!/bin/sh
# termini-node-install.sh — join a machine to a Termini deployment.
#
# THIS IS THE CUSTOMER-FACING INSTALLER (task t_70bd0f2b). It is served by the
# controller at /install/termini-node.sh and is meant to be piped straight into a
# shell by the customer, on the machine that will become a node:
#
#   curl -fsSL https://<your-controller>/install/termini-node.sh | sudo sh -s -- \
#     --controller https://<your-controller> --token <YOUR ENROLLMENT CREDENTIAL>
#
# It takes the customer's OWN single-use enrollment credential (issued by their
# provider — see infra/bootstrap/client/README.md) and:
#
#   1. installs the node runtime if the machine does not already carry it — Termini
#      node images ship it pre-baked; a stock machine fetches it from the same
#      public install surface this script came from;
#   2. installs `termini-node-status`, the customer's own view of the node;
#   3. writes /etc/termini/config.yaml (controller URL, node name, role, cadence)
#      and the credential to /var/lib/termini/enrollment.token (0600);
#   4. starts termini-bootstrap.service and waits for the controller to
#      ACKNOWLEDGE the registration;
#   5. prints "YOUR NODE IS REGISTERED" plus the node's first telemetry, read back
#      with the node's own credential.
#
# WHAT IT NEVER TOUCHES
#   The controller's project token. The customer holds only their own single-use
#   enrollment credential, and after registration the node's own node token, which
#   lives 0600 on the node and reads back only this node's health. No step of this
#   flow can be completed with (or requires) the project token.
#
# IDEMPOTENT / RE-RUNNABLE
#   A machine that is already registered keeps its persisted identity
#   (/var/lib/termini/identity/node.json). Re-running this installer — with or
#   without a credential — reuses that identity, reconnects, and NEVER consumes a
#   second enrollment credential. That is the property behind the card's
#   acceptance (b).
#
# FAILURE OUTPUT
#   The three distinguishable failures — a rejected credential, an already-used
#   credential, an unreachable controller — each print their own actionable
#   message and exit with their own code (3/4/5; see termini-node-status --help).
#   The classification lives in `termini-node-status`, the same code that renders
#   the client's normal view, so the two can never disagree.
#
# Written in POSIX sh on purpose: the one-liner pipes it into `sh`, which is dash
# on Debian/Ubuntu.
set -eu

PROG=termini-node-install
DEFAULT_WAIT_S=90
VERSION=1

die() { printf '[%s] ERROR: %s\n' "$PROG" "$*" >&2; exit 1; }
note() { printf '[%s] %s\n' "$PROG" "$*"; }

usage() {
  cat <<'EOF'
termini-node-install — join this machine to a Termini deployment

Usage:
  curl -fsSL <controller>/install/termini-node.sh | sudo sh -s -- \
      --controller <controller-url> --token <enrollment-credential> [options]

Options:
  --controller URL        controller base URL (or TERMINI_CONTROLLER_URL).
                          Required: it decides which deployment this node joins.
  --token CREDENTIAL      your single-use enrollment credential, as issued by your
                          provider (or TERMINI_ENROLLMENT_TOKEN). Not needed when
                          this machine is already registered.
  --token-file FILE       read the credential from FILE instead of the command
                          line (keeps it out of the shell history).
  --name NAME             node name the controller will show (default: hostname).
  --role auto|compute|inference
                          requested role; `auto` lets the controller decide from
                          the machine's hardware (default: auto).
  --telemetry-interval-ms N
                          health/telemetry cadence in milliseconds (default 1000).
  --wait-seconds N        how long to wait for the controller to acknowledge the
                          registration before giving up (default 90).
  --status-helper FILE    install the client status helper from a local file
                          instead of fetching it (air-gapped / offline installs).
  --no-start              write the configuration and stop; do not touch systemd.
  --root DIR              operate inside DIR instead of / (image baking, tests).
  -h, --help              this text.
EOF
}

ROOT=/
CONTROLLER_URL=${TERMINI_CONTROLLER_URL:-}
TOKEN=${TERMINI_ENROLLMENT_TOKEN:-}
TOKEN_FILE=
NODE_NAME=
ROLE=auto
TELEMETRY_INTERVAL_MS=1000
WAIT_SECONDS=$DEFAULT_WAIT_S
STATUS_HELPER_SRC=
DO_START=1

while [ $# -gt 0 ]; do
  case "$1" in
    --controller)
      [ $# -ge 2 ] || die "--controller needs a value"
      CONTROLLER_URL=$2; shift 2 ;;
    --token)
      [ $# -ge 2 ] || die "--token needs a value"
      TOKEN=$2; shift 2 ;;
    --token-file)
      [ $# -ge 2 ] || die "--token-file needs a value"
      TOKEN_FILE=$2; shift 2 ;;
    --name)
      [ $# -ge 2 ] || die "--name needs a value"
      NODE_NAME=$2; shift 2 ;;
    --role)
      [ $# -ge 2 ] || die "--role needs a value"
      ROLE=$2; shift 2 ;;
    --telemetry-interval-ms)
      [ $# -ge 2 ] || die "--telemetry-interval-ms needs a value"
      TELEMETRY_INTERVAL_MS=$2; shift 2 ;;
    --wait-seconds)
      [ $# -ge 2 ] || die "--wait-seconds needs a value"
      WAIT_SECONDS=$2; shift 2 ;;
    --status-helper)
      [ $# -ge 2 ] || die "--status-helper needs a value"
      STATUS_HELPER_SRC=$2; shift 2 ;;
    --root)
      [ $# -ge 2 ] || die "--root needs a value"
      ROOT=${2%/}; [ -n "$ROOT" ] || ROOT=/
      shift 2 ;;
    --no-start)
      DO_START=0; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --version)
      printf '%s %s\n' "$PROG" "$VERSION"; exit 0 ;;
    *)
      printf '[%s] ERROR: unknown argument: %s\n' "$PROG" "$1" >&2
      usage >&2
      exit 2 ;;
  esac
done

# Everything below operates on an absolute path under $ROOT.
CONFIG_PATH="$ROOT/etc/termini/config.yaml"
TOKEN_PATH="$ROOT/var/lib/termini/enrollment.token"
IDENTITY_DIR="$ROOT/var/lib/termini/identity"
IDENTITY_PATH="$IDENTITY_DIR/node.json"
STATE_PATH="$ROOT/var/lib/termini/state.json"
STATUS_HELPER="$ROOT/usr/local/bin/termini-node-status"
DAEMON="$ROOT/usr/local/bin/termini-bootstrap"
UNIT="$ROOT/etc/systemd/system/termini-bootstrap.service"

if [ "$ROOT" != "/" ] && [ "$DO_START" = 1 ]; then
  note "root override $ROOT: systemd is not touched (implied --no-start)"
  DO_START=0
fi

# Validate what the caller asked for BEFORE demanding privileges: "you forgot the
# controller URL" is a more useful message than "you are not root".
[ -n "$CONTROLLER_URL" ] || die "no controller URL.
  Pass --controller <URL> (or set TERMINI_CONTROLLER_URL). This decides which
  Termini deployment this node joins, and it is also where the installer and the
  node runtime are fetched from."
CONTROLLER_URL=${CONTROLLER_URL%/}

if [ "$ROOT" = "/" ] && [ "$(id -u)" != "0" ]; then
  die "this installer must run as root (it writes /etc and starts a service).
  Re-run it with sudo, e.g.:
    curl -fsSL $CONTROLLER_URL/install/termini-node.sh | sudo sh -s -- --controller $CONTROLLER_URL --token <CREDENTIAL>"
fi

command -v python3 >/dev/null 2>&1 || die "python3 is required (it is the node runtime's own language) and is not installed on this machine."

[ -n "$NODE_NAME" ] || NODE_NAME=$(hostname)

json_field() { # $1 = json file, $2 = top-level key; prints "" when absent
  python3 - "$1" "$2" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
value = data.get(sys.argv[2])
sys.stdout.write("" if value is None else str(value))
PY
}

if command -v curl >/dev/null 2>&1; then
  DOWNLOADER=curl
elif command -v wget >/dev/null 2>&1; then
  DOWNLOADER=wget
else
  die "neither curl nor wget is installed, and this installer needs one to fetch the node runtime from the controller."
fi

download() { # $1 = url, $2 = destination
  if [ "$DOWNLOADER" = curl ]; then
    curl -fsSL --connect-timeout 10 --max-time 120 -o "$2" "$1"
  else
    wget -q --timeout=120 -O "$2" "$1"
  fi
}

fetch_artifact() { # $1 = artifact name, $2 = destination, $3 = mode
  url="$CONTROLLER_URL/install/$1"
  tmp="$2.tmp.$$"
  mkdir -p "${2%/*}"
  if ! download "$url" "$tmp" || [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    die "cannot download $1 from $url
  * check the controller URL is exact (scheme, host, port, no trailing path);
  * check this machine has outbound network access to it, and that no proxy or
    firewall is stripping the request;
  * check the controller answers:      curl -fsS $CONTROLLER_URL/healthz
  Nothing on this machine was changed."
  fi
  install -m "$3" "$tmp" "$2"
  rm -f "$tmp"
}

note "controller:  $CONTROLLER_URL"
note "node name:   $NODE_NAME   role: $ROLE"

# ------------------------------------------------------------------ 1. identity
NODE_ID=$(json_field "$IDENTITY_PATH" node_id)
REGISTERED_TOKEN=$(json_field "$IDENTITY_PATH" node_token)
ALREADY=0
if [ -n "$NODE_ID" ] && [ -n "$REGISTERED_TOKEN" ]; then
  ALREADY=1
fi

if [ "$ALREADY" = 1 ]; then
  note "this machine is already registered as node_id=$NODE_ID — reusing its persisted identity"
  if [ -n "$TOKEN" ] || [ -n "$TOKEN_FILE" ]; then
    note "the enrollment credential you supplied is NOT used and is NOT consumed"
    note "  (enrollment credentials are single-use; this node never needs another)"
  fi
else
  if [ -n "$TOKEN_FILE" ]; then
    [ -s "$TOKEN_FILE" ] || die "the credential file is empty or missing: $TOKEN_FILE"
    TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")
  fi
  [ -n "$TOKEN" ] || die "no enrollment credential.
  Pass the single-use credential your provider issued for this node:
    --token <CREDENTIAL>        or        --token-file <FILE>
  (A machine that is ALREADY registered needs no credential at all — just re-run
  this installer without one.)"
fi

# ------------------------------------------------------------------ 2. runtime
if [ -x "$DAEMON" ] && [ -f "$UNIT" ]; then
  note "node runtime: already present on this machine (nothing to install)"
else
  note "node runtime: not present — installing it from the controller"
  fetch_artifact termini-bootstrap.py "$DAEMON" 0755
  fetch_artifact termini-bootstrap.service "$UNIT" 0644
  note "node runtime: termini-bootstrap installed"
fi

# ------------------------------------------------------- 3. client status helper
if [ -n "$STATUS_HELPER_SRC" ]; then
  [ -f "$STATUS_HELPER_SRC" ] || die "--status-helper $STATUS_HELPER_SRC does not exist"
  mkdir -p "${STATUS_HELPER%/*}"
  install -m 0755 "$STATUS_HELPER_SRC" "$STATUS_HELPER"
elif [ -x "$STATUS_HELPER" ] && [ "$ALREADY" = 1 ]; then
  note "client status helper: already installed ($STATUS_HELPER)"
else
  fetch_artifact termini-node-status.py "$STATUS_HELPER" 0755
  note "client status helper: installed ($STATUS_HELPER)"
fi

# ------------------------------------------------------------------ 4. config
mkdir -p "${CONFIG_PATH%/*}"
(
  umask 077
  cat > "$CONFIG_PATH" <<EOF
# Termini node configuration — written by $PROG.
# controller_url is the Termini deployment this node belongs to.
controller_url: $CONTROLLER_URL
node_name: $NODE_NAME
role_hint: $ROLE
telemetry_interval_ms: $TELEMETRY_INTERVAL_MS
EOF
)
chmod 0600 "$CONFIG_PATH"

if [ "$ALREADY" = 0 ]; then
  mkdir -p "${TOKEN_PATH%/*}"
  (
    umask 077
    printf '%s\n' "$TOKEN" > "$TOKEN_PATH"
  )
  chmod 0600 "$TOKEN_PATH"
  note "enrollment credential stored (0600) at $TOKEN_PATH"
fi

if [ "$DO_START" = 0 ]; then
  note "--no-start: wrote $CONFIG_PATH; start termini-bootstrap.service to register."
  exit 0
fi

# ------------------------------------------------------------------ 5. start
systemctl daemon-reload
systemctl enable termini-bootstrap.service >/dev/null 2>&1 \
  || note "could not enable termini-bootstrap.service (continuing)"
systemctl restart termini-bootstrap.service
note "termini-bootstrap.service started; waiting up to ${WAIT_SECONDS}s for the controller to acknowledge the registration"

# ------------------------------------------------------------------ 6. confirm
rc=0
TERMINI_CONFIG="$CONFIG_PATH" \
TERMINI_IDENTITY_DIR="$IDENTITY_DIR" \
TERMINI_STATE_PATH="$STATE_PATH" \
  python3 "$STATUS_HELPER" --wait --timeout "$WAIT_SECONDS" || rc=$?

if [ "$rc" != 0 ]; then
  note "this node did NOT register (exit $rc). Nothing else on this machine was changed."
  note "fix what the message above names and re-run this installer; the node that is"
  note "already registered here keeps its identity and needs no new credential."
  exit "$rc"
fi

note "done — this node is part of your Termini deployment."
note "your own view of it, using only this node's credential:"
note "  termini-node-status           # registration + latest health"
note "  termini-node-status --watch   # stream its health"
exit 0
