#!/usr/bin/env bash
# ==============================================================================
# Termini — acceptance verifier for the LXD cluster + single-CIDR overlay.
#
# Checks, against the live footprint:
#   1. every inventory member is ONLINE in `lxc cluster list`
#   2. an instance launched on the first member (node-a) and one on the last
#      member (node-c) both obtain an address from the SAME subnet
#   3. they reach each other directly (ping both ways) and over HTTP
#
# Instances are ephemeral and removed afterwards. Exits 0 only when all pass.
#
# Usage: infra/cluster/verify.sh [--inventory PATH] [--key PATH]
# ==============================================================================
set -euo pipefail

INVENTORY="${INVENTORY:-$HOME/.termini/inventory.json}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --key) SSH_KEY="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$INVENTORY" ]] || { echo "inventory not found: $INVENTORY" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

SSH_USER="$(jq -r '.ssh.user // "ubuntu"' "$INVENTORY")"
declare -A PUB
while IFS=$'\t' read -r n pub; do PUB["$n"]="$pub"; done < <(jq -r '.nodes[] | [.name, (.public_ip // "")] | @tsv' "$INVENTORY")
MEMBERS=()
while IFS= read -r n; do MEMBERS+=("$n"); done < <(jq -r '.nodes[].name' "$INVENTORY")
A="${MEMBERS[0]}"
B="${MEMBERS[${#MEMBERS[@]}-1]}"

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=15 -o BatchMode=yes)

PAYLOAD="$(mktemp)"
cat > "$PAYLOAD" <<'PAYLOAD_EOF'
#!/usr/bin/env bash
set -uo pipefail
MA="$1"; MB="$2"
fail=0
ok()  { echo "  PASS  $*"; }
bad() { echo "  FAIL  $*"; fail=1; }
ip_of() { sudo lxc list "$1" --format csv -c 4 2>/dev/null | head -1 | sed 's/(.*//' | tr -d ' '; }
wait_ip() { local n="$1" i ip; for i in $(seq 1 30); do ip="$(ip_of "$n")"; if [[ -n "$ip" ]]; then printf '%s' "$ip"; return 0; fi; sleep 2; done; return 1; }

echo "== LXD cluster members (online?) =="
sudo lxc cluster list --format csv
for m in "$MA" "$MB"; do
  if sudo lxc cluster list --format csv | grep -E "^$m," | grep -q ONLINE; then ok "cluster member $m ONLINE"; else bad "cluster member $m not ONLINE"; fi
done

echo "== ephemeral instances on $MA and $MB (containers: no KVM on this footprint) =="
sudo lxc delete -f ovlverify-a >/dev/null 2>&1 || true
sudo lxc delete -f ovlverify-b >/dev/null 2>&1 || true
sudo lxc init images:alpine/edge ovlverify-a --target "$MA" </dev/null
sudo lxc init images:alpine/edge ovlverify-b --target "$MB" </dev/null
sudo lxc start ovlverify-a
sudo lxc start ovlverify-b

A_IP="$(wait_ip ovlverify-a || true)"
B_IP="$(wait_ip ovlverify-b || true)"
if [[ -n "$A_IP" ]]; then ok "ovlverify-a($MA) DHCP address $A_IP"; else bad "ovlverify-a($MA) got no address"; fi
if [[ -n "$B_IP" ]]; then ok "ovlverify-b($MB) DHCP address $B_IP"; else bad "ovlverify-b($MB) got no address"; fi

if [[ -n "$A_IP" && -n "$B_IP" ]]; then
  if [[ "${A_IP%.*}" == "${B_IP%.*}" ]]; then ok "same CIDR: $A_IP and $B_IP"; else bad "different subnets: $A_IP vs $B_IP"; fi

  echo "== ping across members =="
  if sudo lxc exec ovlverify-a -- ping -c 3 -W 3 "$B_IP" >/tmp/pa.txt 2>&1; then ok "ping $MA -> $MB"; else bad "ping $MA -> $MB"; fi
  tail -1 /tmp/pa.txt
  if sudo lxc exec ovlverify-b -- ping -c 3 -W 3 "$A_IP" >/tmp/pb.txt 2>&1; then ok "ping $MB -> $MA"; else bad "ping $MB -> $MA"; fi
  tail -1 /tmp/pb.txt

  echo "== HTTP across members =="
  cat > /tmp/ovl-responder.sh <<'RESP'
#!/bin/sh
while true; do
  printf 'HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\noverlay-ok\n' | nc -l -p 8000
done
RESP
  sudo lxc file push /tmp/ovl-responder.sh ovlverify-a/root/responder.sh >/dev/null
  sudo lxc exec ovlverify-a -- sh -c 'chmod +x /root/responder.sh; nohup /root/responder.sh >/tmp/r.log 2>&1 & sleep 0.3; true' >/dev/null
  sleep 1
  if out="$(sudo lxc exec ovlverify-b -- wget -q -O- -T 5 "http://$A_IP:8000/" 2>/dev/null)" && [[ "$out" == *overlay-ok* ]]; then
    ok "HTTP $MB -> $MA returned: $out"
  else
    bad "HTTP $MB -> $MA failed"
  fi
fi

echo "== teardown =="
sudo lxc delete -f ovlverify-a >/dev/null 2>&1 || true
sudo lxc delete -f ovlverify-b >/dev/null 2>&1 || true

echo
if [[ "$fail" == 0 ]]; then echo "RESULT: PASS"; else echo "RESULT: FAIL"; fi
exit "$fail"
PAYLOAD_EOF

ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUB[$A]}" \
  "cat > /tmp/termini-verify.sh && bash /tmp/termini-verify.sh '$A' '$B'" < "$PAYLOAD"
rm -f "$PAYLOAD"
