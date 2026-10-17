# CMD: POST /demo/faults (oom, node-a) ; poll GET /operator/decisions ; journalctl -u termini-controller | grep externalModelCall
# Step 08 — the operator deciding: a REAL node breaches a frozen threshold, and the
# decision that comes back is decision_source=model. The transcript shows the breach,
# the decision (with its model-call latency), and the destination the call went to.
#
# The fault is injected through the controller's operator-only demo surface (§4.7).
# It is not a simulation of the decision: the telemetry series is the node's, the
# threshold rule is the frozen one, and the decision is the controller's own record.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 08 — the operator deciding (real node-a, injected OOM)   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
SINCE="$(date -u -d '-5 min' +%Y-%m-%dT%H:%M:%SZ)"

note "inject: scenario=oom on the REAL node $NODE_A_ID (node-a), 180 s"
INJ="$(api POST "/projects/$PROJ/demo/faults" "{\"scenario\":\"oom\",\"node_id\":\"$NODE_A_ID\",\"target\":\"real\",\"duration_s\":180}")"
echo "$INJ" | jq -M . 2>/dev/null || echo "$INJ"
IID="$(printf '%s' "$INJ" | jq -r '.injection_id // .id // empty' 2>/dev/null)"
echo "injection_id=$IID"
printf 'IID=%s\n' "$IID" >"$STATE.decision"

note "wait for the operator's decision on node-a (polling GET /operator/decisions)"
# Only a decision NEWER than this injection counts: a re-run must not re-present the
# previous episode's decision as if the fresh fault had produced it.
SINCE_INJ="$(printf '%s' "$INJ" | jq -r '.started_at // empty' 2>/dev/null)"
echo "counting only decisions with decided_at >= $SINCE_INJ"
DEC=""
for i in $(seq 1 45); do
  RAW="$(curl -sS -H "Authorization: Bearer $TOKEN" "$API/operator/decisions?limit=15")"
  DEC="$(printf '%s' "$RAW" | jq -c --arg n "$NODE_A_ID" --arg s "$SINCE_INJ" '[.decisions[] | select(.node_id==$n and .decided_at >= $s)][0] // empty' 2>/dev/null)"
  if [ -n "$DEC" ]; then
    echo "poll $i  $(date -u +%H:%M:%SZ): decision on node-a -> $(printf '%s' "$DEC" | jq -r '.id') ($(printf '%s' "$DEC" | jq -r '.rule_id'))"
    break
  fi
  echo "poll $i  $(date -u +%H:%M:%SZ): no decision on node-a yet"
  sleep 3
done

note "the decision, as the controller records it"
printf '%s' "$DEC" | jq -M '{
  id, rule_id, action_kind, action_params, decision_source, confidence, fallback_reason,
  decided_at, node_id, reason,
  model_call: ([.decision_trace.steps[] | select(.step=="model_call")][0] | {ts, detail, state}),
  executed: ([.decision_trace.steps[] | select(.step=="executed")][0] | {ts, detail, state}),
  audit_recorded: ([.decision_trace.steps[] | select(.step=="audit_recorded")][0] | {ts, detail, state})}'

note "the controller's own log lines for that call — including the destination"
on_node termini-controller "sudo journalctl -u termini-controller --since '$SINCE' --no-pager | grep -iE 'externalModelCall|destination|answered_by' | tail -6"

note "state recorded for steps 10 (audit + LXD) "
DID="$(printf '%s' "$DEC" | jq -r '.id // empty')"
TARGET="$(printf '%s' "$DEC" | jq -r '.action_params.instance_name // empty')"
printf 'DECISION_ID=%s\nDECISION_RULE=%s\nDECISION_ACTION=%s\nDECISION_TARGET=%s\nNODE_ID=%s\n' \
  "$DID" "$(printf '%s' "$DEC" | jq -r '.rule_id')" "$(printf '%s' "$DEC" | jq -r '.action_kind')" "$TARGET" "$NODE_A_ID" >>"$STATE.decision"
echo "DECISION_ID=$DID"; echo "DECISION_TARGET=$TARGET"
