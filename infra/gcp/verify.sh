#!/usr/bin/env bash
# Termini GCP footprint — acceptance verifier.
#
# Reads the inventory written by Terraform/provision.sh and checks the task's
# acceptance criteria against the live nodes:
#   1. inventory is valid and describes exactly 3 nodes with the right roles
#   2. all three nodes are SSH-reachable
#   3. nvidia-smi reports the accelerator on Node C
#   4. (optional) llama.cpp on Node C answers from Node A over the underlay
#
# Usage:
#   ./verify.sh [--inventory PATH] [--no-ssh] [--no-inference]
set -euo pipefail

INVENTORY="${INVENTORY:-$HOME/.termini/inventory.json}"
DO_SSH=1
DO_INFERENCE=1
SSH_USER_OVERRIDE="${SSH_USER:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --no-ssh) DO_SSH=0; shift ;;
    --no-inference) DO_INFERENCE=0; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail=0
ok()   { printf '  PASS  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fail=1; }
info() { printf '  ..    %s\n' "$*"; }

[[ -f "$INVENTORY" ]] || { echo "inventory not found: $INVENTORY" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

echo "== inventory: $INVENTORY"
jq -e . "$INVENTORY" >/dev/null && ok "valid JSON" || bad "invalid JSON"

count="$(jq '.nodes | length' "$INVENTORY")"
[[ "$count" == "3" ]] && ok "3 nodes present" || bad "expected 3 nodes, found $count"

USER_FROM_INV="$(jq -r '.ssh.user' "$INVENTORY")"
SSH_USER="${SSH_USER_OVERRIDE:-$USER_FROM_INV}"

echo "== declared footprint"
jq -r '.nodes[] | "  \(.name)  role=\(.role)  type=\(.machine_type)  private=\(.private_ip)  public=\(.public_ip // "-")  gpu=\(if .accelerator then .accelerator.type else "none" end)"' "$INVENTORY"

# Node C must declare a real accelerator.
if jq -e '.nodes[] | select(.name=="node-c") | .accelerator != null' "$INVENTORY" >/dev/null; then
  ok "node-c declares an accelerator"
else
  bad "node-c has no accelerator in inventory"
fi

if [[ "$DO_SSH" == "1" ]]; then
  echo "== SSH reachability"
  while IFS=$'\t' read -r name _host pub; do
    if [[ "$pub" == "null" || -z "$pub" ]]; then bad "$name has no public IP"; continue; fi
    out="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${pub}" 'hostname' 2>&1)" || { bad "$name ssh failed: $out"; continue; }
    ok "$name ssh ok (${SSH_USER}@${pub} -> $out)"
  done < <(jq -r '.nodes[] | [.name, .hostname, (.public_ip // "null")] | @tsv' "$INVENTORY")

  echo "== Node C accelerator (nvidia-smi)"
  cpub="$(jq -r '.nodes[] | select(.name=="node-c") | .public_ip // empty' "$INVENTORY")"
  if [[ -n "$cpub" ]]; then
    if smi="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${cpub}" 'nvidia-smi -L' 2>&1)"; then
      ok "nvidia-smi on node-c: ${smi}"
    else
      bad "nvidia-smi failed on node-c: $smi"
    fi
  else
    bad "node-c public IP missing"
  fi

  if [[ "$DO_INFERENCE" == "1" ]]; then
    echo "== inference reachability (D2: Node A -> Node C over underlay)"
    apub="$(jq -r '.nodes[] | select(.name=="node-a") | .public_ip // empty' "$INVENTORY")"
    cpriv="$(jq -r '.nodes[] | select(.name=="node-c") | .private_ip' "$INVENTORY")"
    if [[ -n "$apub" ]]; then
      if res="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${apub}" \
          "curl -sS -m 5 -o /dev/null -w 'http_%{http_code} in %{time_total}s' http://${cpriv}:8080/health" 2>&1)"; then
        ok "node-a -> node-c:8080 ${res}"
      else
        bad "llama.cpp not reachable from node-a yet: $res"
      fi
    fi
  fi
fi

echo
if [[ "$fail" == "0" ]]; then
  echo "RESULT: PASS"
else
  echo "RESULT: FAIL"
fi
exit "$fail"
