#!/usr/bin/env bash
# render-cloud-init.sh — render infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl.
#
# Substitutes ONLY the documented template variables (via envsubst with an
# explicit list), so shell `$lower` references inside the embedded helper scripts
# are left untouched. Print the rendered user-data on stdout.
#
# Inputs (env): controller_url, node_name, enrollment_token, team_ssh_pubkey
#   optional: role_hint (auto) underlay_ip underlay_gw seed_ip
#             telemetry_interval_ms (1000) controller_ca_pubkey image_alias
#             (agentic-os/w3) image_version (2026.10.0)
set -euo pipefail

TPL="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cloud-init/node-bootstrap.yaml.tftpl}"

: "${controller_url:?controller_url is required}"
: "${node_name:?node_name is required}"
: "${enrollment_token:?enrollment_token is required}"
: "${team_ssh_pubkey:?team_ssh_pubkey is required}"
: "${role_hint:=auto}"
: "${underlay_ip:=10.10.0.0/24}"
: "${underlay_gw:=10.10.0.1}"
: "${seed_ip:=}"
: "${telemetry_interval_ms:=1000}"
: "${controller_ca_pubkey:=# Termini controller SSH CA not configured for this node}"
: "${image_alias:=agentic-os/w3}"
: "${image_version:=2026.10.0}"
export controller_url node_name enrollment_token team_ssh_pubkey role_hint \
       underlay_ip underlay_gw seed_ip telemetry_interval_ms controller_ca_pubkey \
       image_alias image_version

VARS='$controller_url $node_name $enrollment_token $team_ssh_pubkey $role_hint \
$underlay_ip $underlay_gw $seed_ip $telemetry_interval_ms $controller_ca_pubkey \
$image_alias $image_version'

envsubst "$VARS" < "$TPL"
