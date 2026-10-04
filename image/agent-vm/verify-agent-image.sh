#!/usr/bin/env bash
# verify-agent-image.sh — end-to-end acceptance for the agent VM image.
#
#   1. recipe check (no LXD needed),
#   2. build + publish the `agent-vm/<version>` image via LXD,
#   3. boot it with a profile that has NO network interface,
#   4. run tools/offline-acceptance.sh inside and assert the JSON verdict,
#   5. copy the evidence out and tear the container down.
#
# Exit 0 only if every check passes. This is what the card's acceptance asks for:
# "image boots offline in an LXD container with all agent deps importable; no
# network fetch at boot."
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${TERMINI_VERSION:-2026.10.0}"
ALIAS="agent-vm/${VERSION}"
PROFILE="termini-agent-offline"
POOL="${TERMINI_POOL:-default}"
NAME="termini-agent-offline-$$"
EVIDENCE_DIR="${TERMINI_EVIDENCE_DIR:-$SELF_DIR/evidence}"
DO_BUILD=1
LXD="lxc"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-build) DO_BUILD=0; shift ;;
    --version) VERSION="$2"; ALIAS="agent-vm/${VERSION}"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --evidence) EVIDENCE_DIR="$2"; shift 2 ;;
    -h|--help) echo "usage: verify-agent-image.sh [--no-build] [--version X] [--name C] [--evidence DIR]"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

cleanup() { "$LXD" delete "$NAME" --force >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== stage 1/4: recipe check"
"$SELF_DIR/build-agent-image.sh" --check

echo "== stage 2/4: build + publish $ALIAS"
if [ "$DO_BUILD" = "1" ]; then
  "$SELF_DIR/build-agent-image.sh" --build --version "$VERSION"
else
  "$LXD" image info "$ALIAS" >/dev/null 2>&1 || { echo "FATAL: alias $ALIAS not present and --no-build given" >&2; exit 3; }
fi

echo "== stage 3/4: boot with NO network interface"
if ! "$LXD" profile show "$PROFILE" >/dev/null 2>&1; then
  "$LXD" profile create "$PROFILE"
  "$LXD" profile device add "$PROFILE" root disk path=/ pool="$POOL"
fi

"$LXD" launch "$ALIAS" "$NAME" -p "$PROFILE"
echo "[verify] waiting for boot + termini-agent"
ready=0
for _ in $(seq 1 90); do
  if "$LXD" exec "$NAME" -- systemctl is-active --quiet termini-agent 2>/dev/null; then ready=1; break; fi
  sleep 1
done
[ "$ready" = 1 ] || echo "[verify] WARN: termini-agent not active yet; running acceptance anyway"

echo "== stage 4/4: offline acceptance inside the VM"
tar -C "$SELF_DIR" -cf - tools/offline-acceptance.sh tools/net-accounting.sh | "$LXD" exec "$NAME" -- tar -C /root -xf -
set +e
"$LXD" exec "$NAME" -- bash /root/tools/offline-acceptance.sh
accept_rc=$?
set -e

install -d -m 0755 "$EVIDENCE_DIR"
for f in offline-acceptance.json first-boot.json selfcheck.json status.json; do
  "$LXD" file pull "$NAME/tmp/$f" "$EVIDENCE_DIR/$f" >/dev/null 2>&1 || true
done
"$LXD" exec "$NAME" -- bash -lc 'journalctl -b -u termini-agent --no-pager 2>/dev/null | tail -40' > "$EVIDENCE_DIR/boot-journal.txt" 2>/dev/null || true

verdict="$(jq -r '.verdict' "$EVIDENCE_DIR/offline-acceptance.json" 2>/dev/null || echo missing)"
echo "----------------------------------------------------------------"
echo "[verify] offline acceptance verdict: $verdict (rc=$accept_rc)"
jq -r '.checks[] | "  \(if .ok then "PASS" else "FAIL" end)  \(.name)  — \(.detail)"' \
  "$EVIDENCE_DIR/offline-acceptance.json" 2>/dev/null || true
echo "----------------------------------------------------------------"
[ "$verdict" = pass ] && [ "$accept_rc" -eq 0 ] && { echo "AGENT IMAGE VERIFIED OFFLINE"; exit 0; }
echo "AGENT IMAGE VERIFICATION FAILED" >&2
exit 1
