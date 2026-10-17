# CMD: GET /operator/audits?node=node-a ; gcloud compute ssh termini-node-a --command="lxc info <target>" ; DELETE /demo/faults/<iid>
# Step 10 — the remediation against real LXD: the PUT the controller issued
# (/1.0/instances/<id>/state), the instance's real state change on the node, and the
# audit row that records all of it with result=ok and the endpoint it dialled.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
# shellcheck disable=SC1090
if [ -f "$STATE.decision" ]; then . "$STATE.decision"; elif [ -f "$STATE" ]; then . "$STATE"; fi
echo "step 10 — remediation against real LXD + the audit row   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "decision under test: ${DECISION_ID:-<none>}  rule=${DECISION_RULE:-?}  action=${DECISION_ACTION:-?}  target=${DECISION_TARGET:-?}"

note "the audit row the controller wrote for that decision"
api GET "/operator/audits?node=$NODE_A_ID&limit=10" > /tmp/termini-e2e-backend/audits.json
jq -M --arg d "${DECISION_ID:-}" '.audits[] | select(.decision_id==$d) | {id, decision_id, rule_id, action_kind, result, detail, lxd_method, lxd_path, lxd_body, lxd_operation, lxd_endpoint, executed_at, idempotency_key}' /tmp/termini-e2e-backend/audits.json

note "the same PUT, seen from the LXD side: the instance's own state on node-a"
on_node termini-node-a "sudo lxc list '${DECISION_TARGET:-demo-workload}' --format csv -c ns4; echo '--- lxc info:'; sudo lxc info '${DECISION_TARGET:-demo-workload}' | head -22"

note "the controller's log lines for the execution window"
SINCE="$(date -u -d '-8 min' +%Y-%m-%dT%H:%M:%SZ)"
on_node termini-controller "sudo journalctl -u termini-controller --since '$SINCE' --no-pager | grep -iE 'remediat|instance\.(restart|stop|start)|lxd' | tail -8"

note "restore the node's own telemetry series (clear the injected fault)"
if [ -n "${IID:-}" ]; then
  api DELETE "/projects/$PROJ/demo/faults/$IID" | jq -M . 2>/dev/null || true
else
  api POST "/projects/$PROJ/demo/faults/clear" '{}' | jq -M . 2>/dev/null || true
fi
echo
echo "note: clearing writes the healthy baseline back through the same ingest path until"
echo "the rule re-arms, which is what makes the next reader see node-a recovered."
