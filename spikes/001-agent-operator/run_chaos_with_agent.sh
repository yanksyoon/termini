#!/usr/bin/env bash
# Drive a REAL chaos scenario with the agent brain instead of a playbook.
#
#   ./run_chaos_with_agent.sh [oom|disk-degrade|latency|healthy]
#
# It runs the production operator CLI (app.operator.cli) through the chaos
# harness's TERMINI_CHAOS_OPERATOR_CMD hook, with app.operator.playbook.choose
# replaced by the agent. Two deliberate choices prove the point:
#
#   --playbooks-dir /nonexistent   the CLI loads ZERO playbooks and says so,
#                                  and the operator still acts correctly
#   --audit-out  <file>            the model's own rationale lands in the
#                                  audit trail alongside the LXD call
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SCENARIO="${1:-oom}"
AUDIT="${TERMINI_AGENT_AUDIT:-/tmp/agent-chaos-audit.jsonl}"
LOG="${TERMINI_AGENT_OPLOG:-/tmp/agent-chaos-operator.log}"

# CPU inference is the binding constraint: the local model needs ~25s of prompt
# processing plus ~4 tokens/s of generation per decision, so a stock 8s scenario
# window (operator gets ~15s) cannot fit one. Widen the scenario to match the
# measured latency instead of pretending it is fast.
SCEN_DURATION="${TERMINI_AGENT_SCENARIO_DURATION:-90}"
OP_DURATION=$((SCEN_DURATION + 8))
SUFFIX="$(printf '%s' "$SCENARIO" | tr '[:lower:]-' '[:upper:]_')"
export "TERMINI_CHAOS_DURATION_${SUFFIX}=${SCEN_DURATION}"

# REQUIRED: chaos boots its controller with TERMINI_OPERATOR_ENABLED unset, which
# defaults to TRUE. That in-process operator grabs the single-owner lease (and
# points at :8443, not the stub), so an external operator is a permanent standby
# and the scenario fails with "remediation_command_issued count=0". chaos builds
# the controller env from os.environ, so exporting this here reaches it.
export TERMINI_OPERATOR_ENABLED=false

rm -f "$AUDIT" "$LOG"

export TERMINI_AGENT_URL="${TERMINI_AGENT_URL:-http://127.0.0.1:8099}"
export TERMINI_AGENT_MODEL="${TERMINI_AGENT_MODEL:-local}"

OP="python3 $HERE/agent_operator.py \
--controller-url {controller_url} \
--lxd-url {lxd_url} \
--node {node_id} \
--project-token {project_token} \
--thresholds '{thresholds_json}' \
--playbooks-dir /nonexistent-playbooks \
--poll-interval 1 --duration $OP_DURATION --audit-out $AUDIT"

echo "==> scenario: $SCENARIO  (widened to ${SCEN_DURATION}s so one CPU decision fits)"
echo "==> brain   : $TERMINI_AGENT_URL (model $TERMINI_AGENT_MODEL)"
echo "==> audit   : $AUDIT"
echo

cd "$REPO"
TERMINI_CHAOS_OPERATOR_CMD="$OP" python3 chaos/run.py --scenario "$SCENARIO"
rc=$?

echo
echo "==> operator stderr/stdout ================================"
sed -n '1,40p' "$LOG" 2>/dev/null || echo "(no operator log)"
echo
echo "==> audit trail (the model's decisions) ==================="
if [ -s "$AUDIT" ]; then
  python3 - "$AUDIT" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
print(f"{len(rows)} decision(s)")
for r in rows:
    print(f"  rule={r.get('rule')}  -> action={r.get('action')}  params={r.get('action_params')}")
    print(f"     result={r.get('result')} {r.get('lxd_method','')} {r.get('lxd_path','')}")
    print(f"     model rationale: {str(r.get('reason',''))[:150]}")
PY
else
  echo "(no audit rows written)"
fi
exit $rc
