#!/usr/bin/env bash
# Evidence harness for the dashboard incident card (task t_ee3f18df).
#
# WHAT IS REAL HERE
#   * a real `python3 -m app.main` controller process (its own temp DB, migrated);
#   * a real node registration + a real telemetry sample in the store;
#   * faults injected through the controller's own operator surface
#     (`POST /v1/projects/{pid}/demo/faults`, controller-api.md §4.7), which writes
#     samples through the same store path a node's telemetry uses;
#   * the real OperatorLoop poll->detect->choose->execute->audit pass, the real
#     guardrail (G1-G9), the real SQLite persistence (migration 0016) and the real
#     `GET /v1/operator/decisions` response the dashboard reads;
#   * a real LXD REST call to `chaos/lxd_stub.py`, whose JSONL log records it.
#
# WHAT IS A STUB (stated plainly, and in the run's INDEX.md)
#   * the MODEL ENDPOINT only: `tools/stub_model.py` answers
#     POST /v1/chat/completions because this host has no fleet, no GPU and no
#     managed-model credential in this window. The answer it returns is DERIVED
#     FROM THE REQUEST the controller sent (see its docstring), and every request
#     is logged, so the prompt/answer pair is part of the evidence.
#
# THREE PHASES, one controller run each, all against the same DB:
#   1. model      - stub model answers -> row with incident_generated="model"
#   2. fallback   - the stub model is killed, the next fault falls back -> the
#                   §8.1 controller-generated record, labelled "fallback"
#   3. deterministic - the controller restarts in TERMINI_OPERATOR_DECISION_MODE=
#                   deterministic -> a row with NO incident columns at all
#
# It then serves the dashboard bundle on a second origin and parks, printing the
# URL to point a browser at. No credential is printed (the project credential is
# in $WORK/project-token.txt, 0600) and none is placed in a URL.
#
# Usage: run-live-incident.sh <out-dir> [work-dir]
set -euo pipefail

OUT="${1:?usage: run-live-incident.sh <out-dir> [work-dir]}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)"
WORK="${2:-$(mktemp -d "${TMPDIR:-/tmp}/termini-incident.XXXXXX")}"
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$OUT" "$WORK"
# A controller DB left in the work dir would BE the project: `ensure_project`
# reuses the first row it finds, so a re-run against a stale DB silently answers
# 403 to this run's project id. Move it aside and start clean.
if [ -f "$WORK/controller.db" ]; then
  mv "$WORK/controller.db" "$WORK/controller.db.stale-$(date +%s)"
fi

PROJECT_ID="01J0DEM0WARM000000000000AB"
PROJECT_TOKEN="evidence-project-token"
BOOTSTRAP_TOKEN="ev-boot-1"
VM_ID="$(python3 -c 'import sys; sys.path.insert(0, "'"$REPO"'"); from chaos import common; print(common.new_ulid())')"

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

CPORT="$(free_port)"; STUBPORT="$(free_port)"; LXDPORT="$(free_port)"; DPORT="$(free_port)"
# Seconds each phase after the first settles before injecting again, so the rule the
# previous phase fired can re-arm (see settle_for_rearm).
REARM_SETTLE_S="${REARM_SETTLE_S:-115}"
BASE="http://127.0.0.1:$CPORT"
DASH="http://127.0.0.1:$DPORT"

printf '%s' "$PROJECT_TOKEN" > "$WORK/project-token.txt"; chmod 0600 "$WORK/project-token.txt"

CONTROLLER_PID=""; STUB_PID=""; LXD_PID=""; DASH_PID=""
cleanup() {
  [ -n "$DASH_PID" ] && kill "$DASH_PID" 2>/dev/null || true
  [ -n "$CONTROLLER_PID" ] && kill "$CONTROLLER_PID" 2>/dev/null || true
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null || true
  [ -n "$LXD_PID" ] && kill "$LXD_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
# The trap tears the stack down whenever the script does NOT reach the parking
# step (any failure, or Ctrl-C). Reaching the park with KEEP=1 disarms it, so a
# healthy stack stays up for the capture step in a second shell.
trap cleanup EXIT INT TERM

say() { printf '\n=== %s ===\n' "$*"; }
# request <method> <path> [body]  (project token)
req() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -m 20 -X "$method" -H "Authorization: Bearer $PROJECT_TOKEN" \
      -H "Content-Type: application/json" -H "X-Termini-Version: 1" \
      -d "$body" "$BASE$path"
  else
    curl -sS -m 20 -X "$method" -H "Authorization: Bearer $PROJECT_TOKEN" \
      -H "X-Termini-Version: 1" "$BASE$path"
  fi
}

controller_env() { # controller_env <decision-mode>
  printf '%s\n' \
    "TERMINI_HOST=127.0.0.1" "TERMINI_PORT=$CPORT" "TERMINI_DB_PATH=$WORK/controller.db" \
    "TERMINI_BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN" "TERMINI_PROJECT_ID=$PROJECT_ID" \
    "TERMINI_PROJECT_TOKEN=$PROJECT_TOKEN" "TERMINI_MIN_REDACTOR_VERSION=3" \
    "TERMINI_OPERATOR_ENABLED=true" "TERMINI_OPERATOR_POLL_INTERVAL_S=0.5" \
    "TERMINI_OPERATOR_DECISION_MODE=$1" \
    "TERMINI_INFERENCE_BASE_URL=http://127.0.0.1:$STUBPORT" \
    "TERMINI_OPERATOR_DECISION_TIMEOUT_S=8" \
    "TERMINI_LXD_BASE_URL=http://127.0.0.1:$LXDPORT" \
    "TERMINI_LXD_DEFAULT_INSTANCE_NAME=agent-c1" \
    "TERMINI_LXD_INSTANCE_NAMES={\"$VM_ID\":\"agent-c1\"}" \
    "TERMINI_OPERATOR_DRY_RUN=false" "TERMINI_LXD_AUTO_PROVISION=false" \
    "PYTHONUNBUFFERED=1"
}

start_controller() { # start_controller <decision-mode> <log>
  # `exec` inside the backgrounded subshell matters: the subshell is replaced by
  # `env`, which is replaced by python3, so `$!` IS the controller's PID. Without
  # it (`( cd … && env … python3 … & echo $! )`) `&` binds the whole `cd && …`
  # chain, `$!` is a wrapper that has already exited, and `stop_controller` kills
  # nothing — the old controller keeps the port, the replacement dies of
  # EADDRINUSE, and the phase silently continues against the OLD controller (that
  # is how phase 4 produced a `fallback` row while claiming to be deterministic).
  ( cd "$REPO/controller" && exec env $(controller_env "$1") python3 -m app.main ) >"$2" 2>&1 &
  CONTROLLER_PID=$!
  echo "$CONTROLLER_PID" >"$WORK/controller.pid"
  # Readiness probe: a project-token GET. It must NOT consume the single-use
  # bootstrap token (a POST /v1/nodes/register here would burn it — that is the
  # exact failure this probe exists to avoid), so it only asserts HTTP 200.
  for _ in $(seq 1 100); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 \
      -H "Authorization: Bearer $PROJECT_TOKEN" \
      "$BASE/v1/projects/$PROJECT_ID/nodes" || true)"
    # A 200 alone is NOT proof: if the previous controller is still bound to the
    # port, its socket answers this probe while the replacement dies of EADDRINUSE
    # (observed: phase 4's deterministic controller crashed, the probe saw the OLD
    # model-mode process answering, and the "deterministic" phase silently produced
    # a FALLBACK row instead — a wrong record with a green check). So the probe
    # also requires the process we started to still be alive.
    if ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then
      echo "controller process exited during startup"; sed 's/^/  | /' "$2" | tail -20; return 1
    fi
    [ "$code" = "200" ] && return 0
    sleep 0.3
  done
  echo "controller did not come up"; sed 's/^/  | /' "$2" | tail -20; return 1
}

stop_controller() {
  if [ -n "$CONTROLLER_PID" ]; then
    kill "$CONTROLLER_PID" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$CONTROLLER_PID" 2>/dev/null || break; sleep 0.25; done
    if kill -0 "$CONTROLLER_PID" 2>/dev/null; then
      echo "  controller ignored SIGTERM; SIGKILL"
      kill -9 "$CONTROLLER_PID" 2>/dev/null || true
      sleep 0.5
    fi
  fi
  # The PORT must actually be free before the next controller starts, or the
  # replacement dies of EADDRINUSE. Bounded wait; '000' is curl's "no answer".
  for _ in $(seq 1 80); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 1 "$BASE/v1/projects/$PROJECT_ID/nodes" || true)"
    [ "$code" = "000" ] && break
    sleep 0.25
  done
  CONTROLLER_PID=""
}

start_stub_model() {
  python3 "$TOOLS/stub_model.py" --port "$STUBPORT" --log "$OUT/live/stub-model-requests.jsonl" \
    >"$WORK/stub.log" 2>&1 &
  STUB_PID=$!
  for _ in $(seq 1 40); do
    # A GET on any path is answered (501) by the stub's handler — enough to prove
    # the port is open WITHOUT putting a probe request into the request log.
    curl -s -o /dev/null -m 1 "http://127.0.0.1:$STUBPORT/" && return 0
    sleep 0.25
  done
  echo "stub model did not come up"; return 1
}

inject() { # inject <scenario> <duration_s>
  req POST "/v1/projects/$PROJECT_ID/demo/faults" \
    "{\"node_id\":\"$NODE_ID\",\"scenario\":\"$1\",\"duration_s\":$2,\"target\":\"real\"}"
}

# Phases 2-4 share ONE node, and the injector writes the node's telemetry while an
# injection is open. A second injection started on top of a live one makes the
# stored series alternate between two scenarios, so a rule that needs N
# consecutive samples never sees N of a kind and no decision is ever produced.
# This is not hypothetical: it is exactly how the first attempt's `latency` phase
# failed to fire while `oom` was still open. Every phase therefore waits for the
# previous injection to clear first.
wait_fault_clear() { # wait_fault_clear <label>
  local label="$1" out
  echo -n "  waiting for open injections to clear before $label ... "
  out="$(python3 "$TOOLS/wait_fault_clear.py" --base "$BASE" --token "$PROJECT_TOKEN" \
    --project-id "$PROJECT_ID" --timeout 120)"
  echo "$out"
}

# Clearing the injection is NOT the same as the rule being able to fire again: the
# detector re-arms only after the fault's recovery samples put the metric back
# under its threshold for the required consecutive samples, and the injector paces
# those recovery samples ~30s apart (measured: injection TTL 19:34:08, rule
# re-armed 19:35:45). Every phase after the first therefore settles for the rule to
# re-arm, so the next phase's fault produces a NEW decision instead of being
# absorbed by a rule that is still clearing.
settle_for_rearm() { # settle_for_rearm <label>
  local label="$1"
  echo "  settling ${REARM_SETTLE_S}s so the fired rule can re-arm before $label"
  sleep "$REARM_SETTLE_S"
}



############ 0. stub model + stub LXD + controller (model mode) ###############
say "0. stack: stub model :$STUBPORT, stub LXD :$LXDPORT, controller :$CPORT"
mkdir -p "$OUT/live"
start_stub_model
python3 "$REPO/chaos/lxd_stub.py" --port "$LXDPORT" --log "$OUT/live/lxd-commands.jsonl" \
  >"$WORK/lxd.log" 2>&1 &
LXD_PID=$!
start_controller model "$WORK/controller-model.log"
echo "controller up (in-process operator loop, decision mode=model)"

############ 1. a real node with a real sample ################################
say "1. node registration + first telemetry sample"
NODE_JSON="$(python3 "$TOOLS/node_setup.py" --base "$BASE" --bootstrap-token "$BOOTSTRAP_TOKEN" \
  --project-id "$PROJECT_ID" --vm-id "$VM_ID" --out "$WORK/node.json")"
echo "  $NODE_JSON"
NODE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["node_id"])' "$NODE_JSON")"

############ 2. model incident ################################################
say "2. inject oom (real fault injection) -> expect a MODEL incident record"
inject oom 25 | tee "$OUT/live/injection-01-oom.json"
echo
python3 "$TOOLS/wait_decision.py" --base "$BASE" --token "$PROJECT_TOKEN" \
  --out "$OUT/live/01-decisions-model-mode.json" \
  --match incident_generated=model --timeout 120 > "$OUT/live/01-row.json"
python3 - "$OUT/live/01-row.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  found={d['found']} decisions={d['decisions']}")
row = d["row"] or {}
print(f"  incident_generated={row.get('incident_generated')!r} source={row.get('decision_source')!r}")
print(f"  title={row.get('incident_title')!r}")
print(f"  evidence={len(row.get('incident_evidence') or [])} actions={len(row.get('incident_actions') or [])}")
PY

############ 3. fallback incident (the model endpoint disappears) #############
# The SCENARIO is `oom` again on purpose: `oom-guard-01` is the rule this harness
# has proven fires on this one-node setup, and the fallback is about the MODEL
# being gone, not about which rule fired. (The first attempt used `latency` here
# and produced no decision at all — twice — so the phase's own rule is not load
# bearing. What the phase asserts is the RECORD: `incident_generated="fallback"`.)
say "3. kill the model endpoint, inject oom -> expect a labelled FALLBACK record"
wait_fault_clear "phase 3"
settle_for_rearm "phase 3"
kill "$STUB_PID" 2>/dev/null || true; wait "$STUB_PID" 2>/dev/null || true; STUB_PID=""
inject oom 25 | tee "$OUT/live/injection-02-oom.json"
echo
python3 "$TOOLS/wait_decision.py" --base "$BASE" --token "$PROJECT_TOKEN" \
  --out "$OUT/live/02-decisions-fallback.json" \
  --match incident_generated=fallback --timeout 120 > "$OUT/live/02-row.json"
python3 - "$OUT/live/02-row.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
row = d["row"] or {}
print(f"  found={d['found']} decisions={d['decisions']}")
print(f"  incident_generated={row.get('incident_generated')!r} fallback_reason={row.get('fallback_reason')!r}")
print(f"  explanation={row.get('incident_explanation')!r}")
PY

############ 4. deterministic mode: no record at all #########################
say "4. restart the controller in deterministic mode -> expect NO incident columns"
wait_fault_clear "phase 4"
settle_for_rearm "phase 4"
stop_controller
start_controller deterministic "$WORK/controller-deterministic.log"
echo "controller up (decision mode=deterministic)"
inject oom 25 | tee "$OUT/live/injection-03-oom.json"
echo
python3 "$TOOLS/wait_decision.py" --base "$BASE" --token "$PROJECT_TOKEN" \
  --out "$OUT/live/03-decisions-deterministic.json" \
  --match decision_source=deterministic --null incident_generated --timeout 120 > "$OUT/live/03-row.json"
python3 - "$OUT/live/03-row.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
row = d["row"] or {}
print(f"  found={d['found']} decisions={d['decisions']}")
print(f"  decision_source={row.get('decision_source')!r} incident_generated={row.get('incident_generated')!r}")
PY

############ 5. all three states in one API response #########################
say "5. the whole decision list (all three states) + audits + events"
req GET "/v1/operator/decisions?limit=50" > "$OUT/live/decisions-all.json"
req GET "/v1/operator/audits?limit=50"   > "$OUT/live/audits-all.json"
req GET "/v1/operator/events?limit=50"   > "$OUT/live/events-all.json"
python3 - "$OUT/live/decisions-all.json" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))["decisions"]
for r in rows:
    print(f"  {r['decision_source']:<14} incident_generated={str(r.get('incident_generated')):<9} "
          f"rule={r['rule_id']:<17} action={r['action_kind']}")
PY

############ 6. dashboard on a second origin, and park #######################
say "6. dashboard served on $DASH"
# Same `exec` rule as the controller: `$!` must be the server's own PID, or the
# teardown cannot stop it.
( cd "$REPO" && exec python3 -m http.server "$DPORT" --bind 127.0.0.1 --directory dashboard ) \
  >"$WORK/dashboard.log" 2>&1 &
DASH_PID=$!
echo "$DASH_PID" >"$WORK/dash.pid"
for _ in $(seq 1 40); do curl -s -o /dev/null "$DASH/index.html" && break; sleep 0.25; done

DASH_URL="$DASH/?apiBase=$BASE/v1&projectId=$PROJECT_ID"
case "$DASH_URL" in *token=*) echo "REFUSING: the dashboard URL carries a credential"; exit 1;; esac
python3 - "$WORK/env.json" "$DASH_URL" "$BASE" "$PROJECT_ID" "$NODE_ID" <<'PY'
import json, os, stat, sys
path, dash, base, pid, nid = sys.argv[1:6]
with open(path, "w") as fh:
    json.dump({"dashboard_url": dash, "controller": base, "project_id": pid,
               "node_id": nid, "token_file": "project-token.txt"}, fh)
os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
PY

say "ready"
echo "dashboard URL : $DASH_URL        (carries no credential)"
echo "controller    : $BASE"
echo "project token : $WORK/project-token.txt (0600, not printed)"
echo "work dir      : $WORK"
echo "evidence      : $OUT/live"
if [ "${KEEP:-0}" = "1" ]; then
  # Disarm the teardown: the capture step runs against this live stack.
  trap - EXIT INT TERM
  echo "KEEP=1: parking (kill $CONTROLLER_PID / $DASH_PID / $LXD_PID to stop)"
  while kill -0 "$CONTROLLER_PID" 2>/dev/null; do sleep 2; done
fi
