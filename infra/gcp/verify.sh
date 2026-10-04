#!/usr/bin/env bash
# Termini GCP footprint — acceptance verifier (CPU-only Node C).
#
# Reads the inventory written by Terraform/provision.sh and checks the task's
# acceptance criteria against the live nodes:
#   1. inventory is valid and describes exactly 3 nodes with the right roles
#   2. no node declares an accelerator (CPU-only footprint)
#   3. all three nodes are SSH-reachable
#   4. no accelerator device is present on Node C
#   5. Node C answers llama.cpp /health on :8080 from Node A over the underlay
#
# Usage:
#   ./verify.sh [--inventory PATH] [--no-ssh] [--no-inference] [--wait SECONDS]
#
# --wait is how long to wait for Node C's first-boot CPU llama.cpp build to come
# up (default 1800s; llama.cpp is built and the GGUF model downloaded at boot).
set -euo pipefail

INVENTORY="${INVENTORY:-$HOME/.termini/inventory.json}"
DO_SSH=1
DO_INFERENCE=1
INFER_WAIT="${INFER_WAIT:-1800}"
SSH_USER_OVERRIDE="${SSH_USER:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --wait) INFER_WAIT="$2"; shift 2 ;;
    --no-ssh) DO_SSH=0; shift ;;
    --no-inference) DO_INFERENCE=0; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
jq -r '.nodes[] | "  \(.name)  role=\(.role)  type=\(.machine_type)  private=\(.private_ip)  public=\(.public_ip // "-")  accel=\(if .accelerator then .accelerator.type else "none" end)"' "$INVENTORY"

# Acceptance: no node carries an accelerator (CPU-only Node C).
if jq -e '.nodes[] | select(.accelerator != null)' "$INVENTORY" >/dev/null; then
  bad "a node declares an accelerator (CPU-only footprint requires none)"
else
  ok "no node declares an accelerator (CPU-only)"
fi

if [[ "$DO_SSH" == "1" ]]; then
  echo "== SSH reachability"
  while IFS=$'\t' read -r name _host pub; do
    if [[ "$pub" == "null" || -z "$pub" ]]; then bad "$name has no public IP"; continue; fi
    out="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${pub}" 'hostname' 2>&1)" || { bad "$name ssh failed: $out"; continue; }
    ok "$name ssh ok (${SSH_USER}@${pub} -> $out)"
  done < <(jq -r '.nodes[] | [.name, .hostname, (.public_ip // "null")] | @tsv' "$INVENTORY")

  echo "== Node C has no accelerator device"
  cpub="$(jq -r '.nodes[] | select(.name=="node-c") | .public_ip // empty' "$INVENTORY")"
  if [[ -n "$cpub" ]]; then
    accel="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${cpub}" \
      'n=$(lspci 2>/dev/null | grep -icE "nvidia|3d controller"); cpus=$(nproc); echo "${n}|${cpus}"' 2>&1)"
    n="${accel%%|*}"; cpus="${accel##*|}"
    if [[ "$n" == "0" ]]; then
      ok "node-c has no accelerator device (lspci); nproc=$cpus"
    else
      bad "node-c reports $n accelerator device(s) via lspci"
    fi
  else
    bad "node-c public IP missing"
  fi

  if [[ "$DO_INFERENCE" == "1" ]]; then
    echo "== inference reachability (D2: Node A -> Node C over underlay)"
    apub="$(jq -r '.nodes[] | select(.name=="node-a") | .public_ip // empty' "$INVENTORY")"
    cpriv="$(jq -r '.nodes[] | select(.name=="node-c") | .private_ip' "$INVENTORY")"
    cport=8080
    if [[ -n "$apub" && -n "$cpriv" ]]; then
      info "waiting up to ${INFER_WAIT}s for http://${cpriv}:${cport}/health from node-a"
      deadline=$(( SECONDS + INFER_WAIT ))
      res=""
      while :; do
        res="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${apub}" \
          "curl -sS -m 5 -o /dev/null -w 'http_%{http_code} in %{time_total}s' http://${cpriv}:${cport}/health" 2>&1 || true)"
        if [[ "$res" == http_200* ]]; then
          ok "node-a -> node-c:${cport} ${res}"
          break
        fi
        if (( SECONDS >= deadline )); then
          bad "llama.cpp not reachable from node-a within ${INFER_WAIT}s: $res"
          ssh "${SSH_OPTS[@]}" "${SSH_USER}@${cpub}" \
            'sudo tail -n 20 /var/log/termini-llama-serve.log 2>/dev/null; systemctl status termini-llama-server --no-pager -n 5 2>&1 | tail -n 10' || true
          break
        fi
        info "not up yet ($res); retrying in 20s"
        sleep 20
      done
    else
      bad "node-a public IP or node-c private IP missing"
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
