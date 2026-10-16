#!/usr/bin/env bash
# warm_cluster.sh — assert the pre-established demo RUNNING state.
#
# Owner: kanban task t_ef5ef65c - *Warm the LXD cluster and agent VMs
# (registered + telemetry streaming)*. Parent workstream: t_5630daf9 (W6).
#
# The live demo must never cold-provision: the LXD cluster members are online,
# the compute "agent VMs" on Nodes A/B are RUNNING, each is registered with the
# controller (fresh last_seen_at), and each streams telemetry at the demo
# cadence. This is the focused, card-scoped assertion for that running state.
#
# Relationship to the other warm commands:
#   demo-env.sh up     creates/starts the cluster (the *action* that warms it)
#   warm_check.sh       the broad whole-layer assertion (image / model / scratch /
#                       inference / offline-bake as well as nodes)
#   warm_cluster.sh     THIS: the cluster running state only, and it quantifies
#                       telemetry cadence (median inter-sample gap) plus proves the
#                       agent-VM image is placed on the compute nodes A/B.
#
# Usage: infra/warm/warm_cluster.sh [--json] [--quiet] [--hold SECS] [--cadence-ms MS]
#   --hold SECS       observation window for the cadence sample (default 8)
#   --cadence-ms MS   expected telemetry interval (default 1000; demo cadence)
#   exit 0 = cluster warm; exit 1 = at least one cold/broken path, with reason.
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

JSON=0; QUIET=0
HOLD="${TERMINI_CLUSTER_HOLD_S:-8}"
CADENCE_MS="${TERMINI_TELEMETRY_INTERVAL_MS:-1000}"
AGENT_IMAGE="${TERMINI_AGENT_IMAGE_ALIAS:-agent-os/w5}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --quiet|-q) QUIET=1 ;;
    --hold) HOLD="${2:?--hold needs seconds}"; shift ;;
    --cadence-ms) CADENCE_MS="${2:?--cadence-ms needs ms}"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[ "$JSON" = 1 ] && QUIET=1

need lxc; need curl; need jq; need python3

declare -a R_NAME R_OK R_WHY
fails=0
ok()  { R_NAME+=("$1"); R_OK+=("true");  R_WHY+=("$2"); [ "$QUIET" = 1 ] || printf 'PASS  %-28s %s\n' "$1" "$2"; }
bad() { R_NAME+=("$1"); R_OK+=("false"); R_WHY+=("$2"); fails=$((fails+1)); printf 'FAIL  %-28s %s\n' "$1" "$2" >&2; }

auth=(-H "Authorization: Bearer $PROJECT_TOKEN")

# node_node_id_fast NAME -> the node_id the agent persisted (empty if unknown)
node_node_id_fast() {
  lxc exec "$1" -- /usr/local/bin/termini-bootstrap status </dev/null 2>/dev/null \
  | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("node_id") or "")
except Exception: print("")' 2>/dev/null || true
}

# controller_fresh NAME NID -> "role:age_s" when the controller has NID with
# last_seen_at inside 15 s; prints "absent" / "noreach" / "badts" otherwise.
controller_fresh() {
  printf '%s' "$1" | python3 -c '
import json,sys,datetime
nid=sys.argv[1]
try: data=json.load(sys.stdin)
except Exception: print("noreach"); sys.exit(0)
now=datetime.datetime.now(datetime.timezone.utc)
for n in data.get("nodes",[]):
    if n.get("node_id")==nid:
        try:
            t=datetime.datetime.strptime(n.get("last_seen_at") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            print("badts"); sys.exit(0)
        age=(now-t).total_seconds()
        if age < 15.0:
            print("ok:" + str(n.get("role")) + ":" + str(int(age)))
        else:
            print("stale:" + str(int(age)))
        sys.exit(0)
print("absent")
' "$2"
}

# telemetry_cadence NID CADENCE_MS -> "count median_gap rate_hz" over the sample
# window (0 count when there is nothing), so callers can assert the demo cadence.
telemetry_cadence() {
  printf '%s' "$1" | python3 -c '
import json,sys,datetime,statistics
cadence=float(sys.argv[1])
try: data=json.load(sys.stdin)
except Exception: print("0 0 0"); sys.exit(0)
now=datetime.datetime.now(datetime.timezone.utc)
ts=[]
for s in data.get("samples",[]):
    try:
        t=datetime.datetime.strptime(s.get("ts",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except Exception:
        continue
    if (now-t).total_seconds() <= 30.0:
        ts.append(t)
ts.sort()
if len(ts) < 2:
    print(str(len(ts)) + " 0 0"); sys.exit(0)
span=(ts[-1]-ts[0]).total_seconds()
gaps=[(ts[i+1]-ts[i]).total_seconds() for i in range(len(ts)-1)]
med=statistics.median(gaps)
rate=(len(ts)-1)/span if span > 0 else 0
print(f"{len(ts)} {med:.3f} {rate:.3f}")
' "$2"
}

# --------------------------------------------------------------------------- controller
if controller_up; then
  ok controller_reachable "$CONTROLLER_URL_HOST answering"
else
  bad controller_reachable "controller not answering on $CONTROLLER_URL_HOST"
fi

PID="$(project_id_from_db)"
if [ -n "$PID" ]; then ok controller_project "project $PID"; else bad controller_project "no project row in $CONTROLLER_DB"; fi

controller_nodes=""
if [ -n "$PID" ] && controller_up; then
  controller_nodes="$(curl -fsS -m 5 "$CONTROLLER_URL_HOST/v1/projects/$PID/nodes" "${auth[@]}" 2>/dev/null || true)"
fi

# cadence tolerance: median gap within +/-50% of the demo cadence
cad_lo="$(awk "BEGIN{printf \"%.3f\", $CADENCE_MS/1000*0.5}")"
cad_hi="$(awk "BEGIN{printf \"%.3f\", $CADENCE_MS/1000*2.0}")"
# require at least half the samples the cadence implies over HOLD seconds
min_samples="$(awk "BEGIN{n=int($HOLD/($CADENCE_MS/1000)*0.5); print (n<2?2:n)}")"

# --------------------------------------------------------------------------- per node
for entry in "${DEMO_NODES[@]}"; do
  name="${entry%%:*}"; role="$(node_role "$name")"

  if lxc_exists "$name" && lxc_running "$name"; then
    ok "$name.running" "agent VM (LXD instance) RUNNING"
  else
    bad "$name.running" "agent VM missing or not RUNNING"
    continue
  fi

  nid="$(node_node_id_fast "$name")"
  reg="$(controller_fresh "$controller_nodes" "$nid")"
  case "$reg" in
    ok:*) ok "$name.registered" "controller sees node_id=${nid:0:12} role=${reg#ok:*} (fresh)" ;;
    *)    bad "$name.registered" "controller registration: $reg" ;;
  esac

  if [ -n "$nid" ] && controller_up; then
    recent="$(curl -fsS -m 5 "$CONTROLLER_URL_HOST/v1/telemetry/recent?node=$nid&limit=60" "${auth[@]}" 2>/dev/null || true)"
    read -r cnt med rate < <(telemetry_cadence "$recent" "$CADENCE_MS")
    cad_ok="$(awk "BEGIN{print ($cnt>=$min_samples && $med>=$cad_lo && $med<=$cad_hi) ? 1 : 0}")"
    if [ "$cad_ok" = 1 ]; then
      ok "$name.telemetry_cadence" "${cnt} samples, median gap ${med}s (rate ${rate}Hz) at ${CADENCE_MS}ms cadence"
    else
      bad "$name.telemetry_cadence" "${cnt} samples, median gap ${med}s (rate ${rate}Hz) not on the ${CADENCE_MS}ms cadence (need >=${min_samples} samples, gap ${cad_lo}-${cad_hi}s)"
    fi
  fi

  # the compute nodes host the agent VMs: prove the guest image is placed locally
  # so an agent VM launches with no registry pull (D3 / t_7c40f9a3 placement).
  if [ "$role" = "compute" ]; then
    placed="$(lxc exec "$name" -- lxc image list "$AGENT_IMAGE" --format csv -c l </dev/null 2>/dev/null | tr -d '\r' | grep -qx "$AGENT_IMAGE" && echo yes || echo no)"
    if [ "$placed" = "yes" ]; then
      ok "$name.agent_vm_image" "guest image '$AGENT_IMAGE' placed locally (offline launch)"
    else
      bad "$name.agent_vm_image" "guest image '$AGENT_IMAGE' not in $name's local store (run infra/agent-vm/place-agent-image.sh)"
    fi
  fi
done

# --------------------------------------------------------------------------- report
if [ "$JSON" = 1 ]; then
  n=${#R_NAME[@]}
  printf '{"ok": %s, "hold_s": %s, "cadence_ms": %s, "checks": [' \
    "$([ "$fails" -eq 0 ] && echo true || echo false)" "$HOLD" "$CADENCE_MS"
  for ((i=0; i<n; i++)); do
    [ "$i" -gt 0 ] && printf ', '
    jq -cn --arg n "${R_NAME[$i]}" --argjson ok "${R_OK[$i]}" --arg why "${R_WHY[$i]}" \
      '{name:$n, ok:$ok, detail:$why}'
  done
  printf '], "failed": %d}\n' "$fails"
fi

if [ "$fails" -eq 0 ]; then
  [ "$QUIET" = 1 ] || printf '\nWARM CLUSTER: all checks green (%d checks)\n' "${#R_NAME[@]}"
  exit 0
fi
printf '\nCOLD CLUSTER: %d check(s) failed - the demo running state is not established.\n' "$fails" >&2
exit 1
