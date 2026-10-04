#!/usr/bin/env bash
# warm_check.sh — ONE command that asserts the demo environment is fully warm.
#
# It fails loudly (non-zero, with a specific reason per failed check) if any of
# the cold paths the demo must never hit are still cold:
#
#   image    the pinned alias + immutable tag resolve in the local LXD store
#   node     every demo node exists and is RUNNING
#   agent    every node's bootstrap agent reports ONLINE
#   registry the controller has every node with a fresh last_seen_at
#   baked    no node can fall back to kitchen-sink provisioning (.baked-offline)
#   model    the inference node has the pinned GGUF resident in RAM (not cold)
#   infer    the inference node answers /health and /v1/models
#   scratch  the Scenario-B scratch volumes exist, are attached, and writable
#   stream   telemetry is arriving at the configured demo cadence
#
# Usage: infra/warm/warm_check.sh [--json] [--quiet]
#   exit 0 = all green; exit 1 = at least one cold path
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

JSON=0; QUIET=0
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    --quiet|-q) QUIET=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $a" ;;
  esac
done

# machine-readable mode prints only the JSON document on stdout
if [ "$JSON" = 1 ]; then QUIET=1; fi

need lxc; need jq; need curl; need python3

declare -a R_NAME R_OK R_WHY
fails=0
ok()   { R_NAME+=("$1"); R_OK+=("true");  R_WHY+=("$2"); [ "$QUIET" = 1 ] || printf 'PASS  %-24s %s\n' "$1" "$2"; }
bad()  { R_NAME+=("$1"); R_OK+=("false"); R_WHY+=("$2"); fails=$((fails+1)); printf 'FAIL  %-24s %s\n' "$1" "$2" >&2; }

node_state_json() { lxc exec "$1" -- /usr/local/bin/termini-bootstrap status </dev/null 2>/dev/null || true; }
node_ip() { lxc list "$1" --format csv -c 4 2>/dev/null | awk -F'[ ,]' '{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\./) {print $i; exit}}'; }
model_rss() { lxc exec "$1" -- bash -c 'p=$(pidof llama-server | awk "{print \$1}"); [ -n "$p" ] && awk "/VmRSS/{print \$2*1024}" /proc/$p/status' </dev/null 2>/dev/null | tr -d '\r'; }

# --------------------------------------------------------------------------- image
if image_pinned; then
  ok image_alias_pinned "alias '$IMAGE_ALIAS' resolves locally"
else
  bad image_alias_pinned "alias '$IMAGE_ALIAS' missing from the local LXD store"
fi

PUBLISHED="$PUBLISH_DIR/published.json"
if [ -f "$PUBLISHED" ]; then
  tag="$(jq -r '.immutable_tag' "$PUBLISHED")"
  fp_pub="$(jq -r '.fingerprint' "$PUBLISHED")"
  fp_now="$(lxc image list "$IMAGE_ALIAS" --format csv -c f | head -n1)"
  if [ "$tag" != "null" ] && lxc image list "$tag" --format csv -c f 2>/dev/null | grep -q .; then
    ok image_immutable_tag "immutable tag '$tag' resolves"
  else
    bad image_immutable_tag "immutable tag '$tag' does not resolve"
  fi
  if [ "$fp_now" = "$fp_pub" ]; then
    ok image_fingerprint "alias points at baked fingerprint ${fp_now:0:12}"
  else
    bad image_fingerprint "alias fingerprint ${fp_now:0:12} != published ${fp_pub:0:12}"
  fi
else
  bad image_manifest "no published.json at $PUBLISHED (run bake-image.sh)"
fi

# --------------------------------------------------------------------------- controller
if controller_up; then
  ok controller_reachable "$CONTROLLER_URL_HOST answering"
else
  bad controller_reachable "controller not answering on $CONTROLLER_URL_HOST"
fi

PID="$(project_id_from_db)"
if [ -n "$PID" ]; then ok controller_project "project $PID"; else bad controller_project "no project row in $CONTROLLER_DB"; fi

# nodes list from the controller (once)
controller_nodes=""
if [ -n "$PID" ] && controller_up; then
  controller_nodes="$(curl -fsS -m 5 "$CONTROLLER_URL_HOST/v1/projects/$PID/nodes" \
      -H "Authorization: Bearer $PROJECT_TOKEN" 2>/dev/null || true)"
fi

# --------------------------------------------------------------------------- per node
for entry in "${DEMO_NODES[@]}"; do
  name="${entry%%:*}"; role="$(node_role "$name")"

  if lxc_exists "$name" && lxc_running "$name"; then
    ok "$name.running" "container RUNNING"
  else
    bad "$name.running" "container missing or not RUNNING"
    continue
  fi

  st="$(node_state_json "$name")"
  if printf '%s' "$st" | grep -q '"state": "ONLINE"'; then
    ok "$name.agent_online" "termini-bootstrap state=ONLINE"
  else
    bad "$name.agent_online" "agent state not ONLINE: $(printf '%s' "$st" | tr -d '\n' | cut -c1-120)"
  fi

  if printf '%s' "$st" | grep -q '"last_error": null'; then
    ok "$name.no_error" "no bootstrap error"
  else
    bad "$name.no_error" "agent reports last_error: $(printf '%s' "$st" | grep last_error | head -1)"
  fi

  # controller registration + fresh last_seen (matched by the agent's node_id)
  nid="$(printf '%s' "$st" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("node_id") or "")
except Exception: print("")' 2>/dev/null || true)"
  reg_ok="$(printf '%s' "$controller_nodes" | python3 -c '
import json,sys,datetime
nid=sys.argv[1]
if not nid:
    print("no-node-id"); sys.exit(0)
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
        print("ok:" + str(n.get("role")) + ":" + str(int(age)) + "s"); sys.exit(0)
print("absent")
' "$nid")"
  case "$reg_ok" in
    ok:*) ok "$name.registered" "controller node_id=${nid:0:12} role=${reg_ok#ok:}" ;;
    *)    bad "$name.registered" "controller registration check: $reg_ok" ;;
  esac

  if lxc exec "$name" -- test -f /var/lib/termini/.baked-offline </dev/null; then
    ok "$name.baked_offline" "bake sentinel present (no boot-time fetch)"
  else
    bad "$name.baked_offline" "missing /var/lib/termini/.baked-offline"
  fi

  # scratch volume attached + writable
  dev_vol="$(lxc config device get "$name" scratch source 2>/dev/null || true)"
  if [ -n "$dev_vol" ]; then
    if lxc exec "$name" -- bash -c "touch $SCRATCH_MOUNT/.warmcheck && rm -f $SCRATCH_MOUNT/.warmcheck" </dev/null; then
      ok "$name.scratch" "volume '$dev_vol' attached at $SCRATCH_MOUNT and writable"
    else
      bad "$name.scratch" "scratch volume '$dev_vol' attached but not writable"
    fi
  else
    bad "$name.scratch" "no scratch device attached (Scenario B would cold-create it)"
  fi

  # telemetry cadence: >=2 samples in the last 15s
  if [ -n "$PID" ] && [ -n "$nid" ]; then
    recent="$(curl -fsS -m 5 "$CONTROLLER_URL_HOST/v1/telemetry/recent?node=$nid&limit=50" -H "Authorization: Bearer $PROJECT_TOKEN" 2>/dev/null || true)"
    n_recent="$(printf '%s' "$recent" | python3 -c '
import json,sys,datetime
try: data=json.load(sys.stdin)
except Exception: print(0); sys.exit(0)
now=datetime.datetime.now(datetime.timezone.utc); c=0
for s in data.get("samples",[]):
    try:
        t=datetime.datetime.strptime(s.get("ts",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except Exception: continue
    if (now-t).total_seconds()<=15: c+=1
print(c)
' 2>/dev/null || echo 0)"
    if [ "${n_recent:-0}" -ge 2 ]; then
      ok "$name.telemetry" "${n_recent} samples in the last 15s"
    else
      bad "$name.telemetry" "only ${n_recent:-0} telemetry samples in the last 15s"
    fi
  fi

  # inference node specifics
  if [ "$role" = "inference" ]; then
    ip="$(node_ip "$name")"
    if [ -n "$ip" ] && curl -fsS -m 5 "http://$ip:$INFERENCE_PORT/health" >/dev/null 2>&1; then
      ok "$name.inference_health" "GET http://$ip:$INFERENCE_PORT/health OK"
    else
      bad "$name.inference_health" "no /health answer at $ip:$INFERENCE_PORT"
    fi
    if [ -n "$ip" ] && curl -fsS -m 10 "http://$ip:$INFERENCE_PORT/v1/models" 2>/dev/null | grep -q "$MODEL_ALIAS\|models\|gguf\|id"; then
      ok "$name.inference_models" "/v1/models answered"
    else
      bad "$name.inference_models" "/v1/models did not answer with a model listing"
    fi

    # model file: present, right size, sha sidecar, local filesystem, resident in RAM
    if lxc exec "$name" -- test -f "$MODELS_DIR/$MODEL_FILE" </dev/null; then
      got_size="$(lxc exec "$name" -- stat -c %s "$MODELS_DIR/$MODEL_FILE" </dev/null | tr -d '\r')"
      if [ "$got_size" = "$MODEL_SIZE_BYTES" ]; then
        ok "$name.model_file" "pinned GGUF present ($got_size bytes)"
      else
        bad "$name.model_file" "GGUF size $got_size != pinned $MODEL_SIZE_BYTES"
      fi
      if lxc exec "$name" -- test -f "$MODELS_DIR/$MODEL_FILE.sha256" </dev/null; then
        ok "$name.model_sha_sidecar" "sha256 sidecar present"
      else
        bad "$name.model_sha_sidecar" "no .sha256 sidecar next to the GGUF"
      fi
      fstype="$(lxc exec "$name" -- stat -f -c %T "$MODELS_DIR" </dev/null | tr -d '\r')"
      if printf '%s' "$fstype" | grep -Eq '^(nfs|nfs4|cifs|smb2|smbfs|9p)$'; then
        bad "$name.model_fs_local" "model root is a network filesystem: $fstype"
      else
        ok "$name.model_fs_local" "model root fs_type=$fstype"
      fi
      rss="$(model_rss "$name")"
      if [ -n "$rss" ] && [ "$rss" -ge "$RESIDENT_RSS_MIN_BYTES" ]; then
        ok "$name.model_resident" "llama-server RSS=${rss} bytes (>= ${RESIDENT_RSS_MIN_BYTES})"
      else
        bad "$name.model_resident" "model not resident (RSS=${rss:-none} < ${RESIDENT_RSS_MIN_BYTES})"
      fi
    else
      bad "$name.model_file" "pinned GGUF absent at $MODELS_DIR/$MODEL_FILE"
    fi
  fi
done

# --------------------------------------------------------------------------- report
if [ "$JSON" = 1 ]; then
  n=${#R_NAME[@]}
  printf '{"ok": %s, "checks": [' "$([ "$fails" -eq 0 ] && echo true || echo false)"
  for ((i=0; i<n; i++)); do
    [ "$i" -gt 0 ] && printf ', '
    jq -cn --arg n "${R_NAME[$i]}" --argjson ok "${R_OK[$i]}" --arg why "${R_WHY[$i]}" \
      '{name:$n, ok:$ok, detail:$why}'
  done
  printf '], "failed": %d}\n' "$fails"
fi

if [ "$fails" -eq 0 ]; then
  [ "$QUIET" = 1 ] || printf '\nWARM: all checks green (%d checks)\n' "${#R_NAME[@]}"
  exit 0
fi
printf '\nCOLD: %d check(s) failed — the demo would cold-provision.\n' "$fails" >&2
exit 1
