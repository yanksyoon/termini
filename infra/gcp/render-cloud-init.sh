#!/usr/bin/env bash
# render-cloud-init.sh — render a node's cloud-init user-data for the GCP
# footprint from the SHARED bootstrap template.
#
# cloud-init/node.yaml.tftpl is a symlink to
# infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl, and this script renders
# it with the SAME documented variable set as infra/bootstrap/render-cloud-init.sh
# (envsubst). Terraform renders that identical file via templatefile() in main.tf,
# so the Terraform path and the gcloud fallback (provision.sh) produce the same
# user-data — the single-template unification of task t_5e9c9d96.
#
# Usage (env):
#   node_name (required) team_ssh_pubkey (required)
#   optional: enrollment_token role_hint controller_url underlay_ip underlay_gw
#             seed_ip telemetry_interval_ms controller_ca_pubkey
#             image_project image_family
#   SERVE_INFERENCE=1 with node_name=node-c appends the host-exposed CPU
#   llama.cpp block (cloud-init/llama-serve.sh) as extra write_files/runcmd
#   fragments; the shared template hardcodes none.
#
# Prints the rendered user-data on stdout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDERER="$HERE/../bootstrap/render-cloud-init.sh"
TPL="$HERE/cloud-init/node.yaml.tftpl"

: "${node_name:?node_name is required}"
: "${team_ssh_pubkey:?team_ssh_pubkey is required}"
: "${enrollment_token:=}"
: "${role_hint:=auto}"
: "${controller_url:=https://controller.termini.internal}"
: "${underlay_ip:=10.10.0.11/24}"
: "${underlay_gw:=10.10.0.1}"
: "${seed_ip:=}"
: "${telemetry_interval_ms:=1000}"
: "${controller_ca_pubkey:=}"
: "${image_project:=ubuntu-os-cloud}"
: "${image_family:=ubuntu-2404-lts-amd64}"
: "${SERVE_INFERENCE:=0}"

image_alias="$image_project/$image_family"
image_version="$image_family"
extra_write_files=""
extra_runcmd=""

if [ "$node_name" = "node-c" ] && [ "$SERVE_INFERENCE" = "1" ]; then
  llama="$HERE/cloud-init/llama-serve.sh"
  [ -f "$llama" ] || { echo "render-cloud-init.sh: missing $llama" >&2; exit 1; }
  script="$(cat "$llama")"
  script="${script//__LLAMACPP_COMMIT__/${LLAMACPP_COMMIT:-7fe450e19305b828c199d602c23a8337aaa1f03b}}"
  script="${script//__MODEL_URL__/${INFERENCE_MODEL_URL:-https://huggingface.co/bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF/resolve/main/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf}}"
  script="${script//__MODEL_FILE__/${INFERENCE_MODEL_FILE:-DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf}}"
  script="${script//__MODEL_SHA256__/${INFERENCE_MODEL_SHA256:-87bcba20b4846d8dadf753d3ff48f9285d131fc95e3e0e7e934d4f20bc896f5d}}"
  script="${script//__MODEL_ALIAS__/${INFERENCE_MODEL_ALIAS:-deepseek-flash}}"
  script="${script//__PORT__/${INFERENCE_PORT:-8080}}"
  extra_write_files="  - path: /usr/local/sbin/termini-llama-serve.sh
    permissions: '0755'
    owner: root:root
    content: |
"
  while IFS= read -r line; do
    extra_write_files+="      ${line}"$'\n'
  done <<< "$script"
  extra_write_files="${extra_write_files%$'\n'}"
  extra_runcmd="  - [ /usr/local/sbin/termini-llama-serve.sh ]"
fi

controller_url="$controller_url" node_name="$node_name" enrollment_token="$enrollment_token" \
  team_ssh_pubkey="$team_ssh_pubkey" role_hint="$role_hint" \
  underlay_ip="$underlay_ip" underlay_gw="$underlay_gw" seed_ip="$seed_ip" \
  telemetry_interval_ms="$telemetry_interval_ms" controller_ca_pubkey="$controller_ca_pubkey" \
  image_alias="$image_alias" image_version="$image_version" \
  extra_write_files="$extra_write_files" extra_runcmd="$extra_runcmd" \
  bash "$RENDERER" "$TPL"
