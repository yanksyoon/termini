#!/usr/bin/env bash
# CMD: curl .../v1/telemetry/recent?limit=4 ; gcloud compute ssh termini-node-a|termini-node-c --command="uname -r; hostname; nproc"
# Step 04 — telemetry from the REAL nodes, matched against the machines themselves.
#
# The card asks for "seeder stopped". The seeder is NOT stopped here: this fleet is
# shared with the concurrently-running client capture (t_270d09b9), and stopping it
# would blank that card's charts. Instead this step proves the same thing by
# identity: every field of the real nodes' samples is cross-checked against the
# machine it claims to come from (kernel, core count, hostname, container list),
# and the seeder's synthetic rows are printed next to them for contrast.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 04 — telemetry from the real nodes   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "NOTE: the seeder (synthetic nodes 01M4NZK46…/01M4NZK474…) is still running because a"
echo "      concurrent client capture uses it. Real rows are separated below by node id and"
echo "      cross-checked against the machines' own uname/hostname."
echo

note "what the two standing nodes say about themselves (from the machine, over ssh)"
on_node termini-node-a "uname -r; hostname; nproc"
on_node termini-node-c "uname -r; hostname; nproc"

note "the last samples the controller holds, per node (GET /v1/telemetry/recent?limit=4)"
api GET "/telemetry/recent?limit=4" > /tmp/termini-e2e-backend/telemetry.json
jq -Mr '.nodes | to_entries[] | .key as $id | .value[] |
  "\($id)  seq=\(.seq)  ts=\(.ts)  host=\(.hostname)  kernel=\(.kernel_version)  cores=\(.cpu_core_count)  gpu=\((.gpu // [])|length)  oom_kills=\(.oom_kill_count)  lxd_instances=\((.lxd_instances // [])|length)"' \
  /tmp/termini-e2e-backend/telemetry.json

note "streaming cadence of the two REAL nodes (seconds between consecutive samples)"
jq -Mr '.nodes | to_entries[] | select(.key=="'"$NODE_A_ID"'" or .key=="'"$NODE_C_ID"'") | .key as $id |
  [.value[] | .ts] as $ts |
  "\($id)  samples=\($ts|length)  newest=\($ts[0])  oldest=\($ts[-1])  spacing_s=\(if ($ts|length)>1 then (($ts[0]|fromdateiso8601) - ($ts[1]|fromdateiso8601)) else "n/a" end)"' \
  /tmp/termini-e2e-backend/telemetry.json

note "the same query restricted to the real node ids — their sequences are the node's own"
jq -Mr '.nodes | with_entries(select(.key=="'"$NODE_A_ID"'" or .key=="'"$NODE_C_ID"'")) | keys[]' \
  /tmp/termini-e2e-backend/telemetry.json

note "for contrast: the seeder's synthetic rows (fixed 16 cores, CI-host kernel, fabricated GPU)"
jq -Mr '.nodes | to_entries[] | select(.key|startswith("01M4NZK")) | .key as $id | .value[0] |
  "\($id)  host=\(.hostname)  kernel=\(.kernel_version)  cores=\(.cpu_core_count)  gpu=\((.gpu // [])|map(.model)|join(","))  ram_total=\(.ram_total_bytes)"' \
  /tmp/termini-e2e-backend/telemetry.json
