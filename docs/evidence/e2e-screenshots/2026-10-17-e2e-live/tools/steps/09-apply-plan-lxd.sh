#!/usr/bin/env bash
# CMD: POST /intents {"prompt":"deploy 1 worker agent for coding"} ; POST /vms/apply {"plan_id":...} ; gcloud compute ssh termini-node-a --command="lxc list; lxc info <vm>"
# Step 09 — apply a plan and watch LXD do real work: the controller creates an LXD
# instance on node-a and the node's own `lxc list` shows it RUNNING with the same
# name the controller returned as the vm_id.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 09 — apply a plan: LXD materialises a real instance   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

note "accept a plan from plain English (the intent extractor runs on node-c's managed model)"
PLAN="$(api POST "/projects/$PROJ/intents" '{"prompt":"deploy 1 worker agent for coding"}')"
echo "--- raw response body:"
echo "$PLAN"
echo "--- projection:"
echo "$PLAN" | jq -M '{plan_id: (.plan_id // .id), state, allocations: [.allocations[]? | {index, kind, target_node_id, cpu_cores, ram_gb, realized_as}], guardrails: (.guardrails // [])}' 2>/dev/null || true
PLAN_ID="$(printf '%s' "$PLAN" | jq -r '.plan_id // .id // empty' 2>/dev/null)"
echo "plan_id=$PLAN_ID"
printf 'PLAN_ID=%s\n' "$PLAN_ID" >>"$STATE"
[ -n "$PLAN_ID" ] || { echo "NO PLAN ACCEPTED — the response above is the controller's own refusal; step 09 has nothing to apply"; exit 1; }

note "apply it — the controller drives node-a's LXD REST API and returns a TERMINAL state"
APPLY="$(api POST "/projects/$PROJ/vms/apply" "{\"plan_id\":\"$PLAN_ID\"}")"
echo "$APPLY" | jq -M . 2>/dev/null || echo "$APPLY"
VM_IDS="$(printf '%s' "$APPLY" | jq -r '[.allocations[]?.vm_ids[]?] | join(" ")' 2>/dev/null)"
echo "vm_ids=$VM_IDS"
printf 'VM_IDS=%s\n' "$VM_IDS" >>"$STATE"

note "the node's OWN lxc list — the same instance names, RUNNING"
FIRST_VM="$(printf '%s' "$VM_IDS" | awk '{print $1}')"
# Match the instance name the controller returned (the ULID is truncated to 24
# chars in the name, so grepping the full plan id would never match).
on_node termini-node-a "sudo lxc list --format csv -c ns4 | grep -i '${FIRST_VM}'; echo '--- other instances on node-a (untouched by this apply):'; sudo lxc list --format csv -c ns4 | grep -iv '${FIRST_VM}' | head -10"

note "the instance's own limits and image (lxc info on node-a)"
on_node termini-node-a "sudo lxc info '${FIRST_VM}' | head -30"

note "the controller's capacity accounting moved with the create"
api GET "/projects/$PROJ/nodes/$NODE_A_ID" | jq -M '{node_id, capacity}'
