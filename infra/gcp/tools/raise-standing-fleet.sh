#!/usr/bin/env bash
# raise-standing-fleet.sh — raise node-a (compute) + node-c (inference) of the
# STANDING Termini demo from the pre-baked node image FAMILY, with the SHARED
# bootstrap cloud-init (the same template the Terraform path and the LXD flow use).
#
# Why this exists (and is not just `terraform apply`):
#   * `infra/gcp/*.tf` renders node-a/b/c together, so adopting the baked image
#     there would also try to create node-b — which CANNOT be up: the project's
#     CPUS_ALL_REGIONS quota is 12 and is consumed exactly by
#     controller(2) + node-a(2) + node-c(8). node-b must stay TERMINATED.
#   * the standing nodes must keep their names/underlay IPs/tags/machine types,
#     not the values of a generic apply.
#   * each node needs its OWN single-use enrollment token (a token is consumed by
#     the first registration), which the single-token `provision.sh` cannot express.
#
# It reuses `infra/bootstrap/render-cloud-init.sh` verbatim, so the rendered
# user-data is the SAME document the Terraform path produces (including Node C's
# llama.cpp serve block, injected as the optional extra_write_files/extra_runcmd).
#
# Usage:
#   TOKEN_A=... TOKEN_C=... TEAM_SSH_PUBKEY_FILE=~/.ssh/team.pub \
#     ./raise-standing-fleet.sh --raise                     # recreate a and c
#   ... --raise --only c --with-external-ip                 # from-scratch node-c
#   TOKEN_A=x TOKEN_C=y TEAM_SSH_PUBKEY_FILE=... \
#     ./raise-standing-fleet.sh --render-only --render-out /tmp/ci
#   ... --only a | --only c
#
# --with-external-ip: give the node an EPHEMERAL public IP for its lifetime.
# Required for a FROM-SCRATCH node-c (t_359ed606): Node C's first boot installs
# build-essential/cmake with apt and clones llama.cpp, and NEITHER the Ubuntu
# mirror nor github.com is reachable without a public path (Private Google
# Access covers Google APIs — storage.googleapis.com — not archive.ubuntu.com).
# Without the flag a recreated node-c's cloud-init ends in
# `status: error ... Runparts: 1 failures (runcmd)` with
# "Cannot initiate the connection to asia-south1.gce.archive.ubuntu.com" and NO
# inference endpoint comes up at all; the standing node-c only works because its
# boot disk predates the external-IP removal and already carries the binary and
# weights. Delete the access config once the node has booted (the default
# posture for the standing fleet stays --no-address).
#
# Recreating an instance REPLACES its boot disk (image_family is a boot-disk
# property). That is intended: the standing nodes carried the stock Ubuntu image,
# on which the activation-only cloud-init finds no termini-bootstrap.service and
# the node never registers.
set -euo pipefail

PROJECT="${PROJECT:-termini-510609}"
REGION="${REGION:-asia-south1}"
ZONE="${ZONE:-asia-south1-a}"
NETWORK="${NETWORK:-termini-vpc}"
SUBNET="${SUBNET:-termini-underlay}"
IMAGE_PROJECT="${IMAGE_PROJECT:-termini-510609}"
IMAGE_FAMILY="${IMAGE_FAMILY:-termini-node-os}"
COMPUTE_MACHINE_TYPE="${COMPUTE_MACHINE_TYPE:-n2-standard-2}"
INFERENCE_MACHINE_TYPE="${INFERENCE_MACHINE_TYPE:-n2-standard-8}"
BOOT_DISK_GB="${BOOT_DISK_GB:-60}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-pd-balanced}"
CONTROLLER_URL="${CONTROLLER_URL:-http://10.10.0.2:8080}"
TELEMETRY_INTERVAL_MS="${TELEMETRY_INTERVAL_MS:-1000}"
NODE_IMAGE_VERSION="${NODE_IMAGE_VERSION:-2026.10.0}"
INFERENCE_PORT="${INFERENCE_PORT:-8080}"
# Which backend a FRESH node serves: gemini-proxy (the shipped demo
# configuration — the forwarded managed model emulating the GPU node) or local
# (the node's own CPU weights). On a running node the same choice is one command:
# `termini-inference-switch local|gemini-proxy` (task t_359ed606).
INFERENCE_BACKEND="${INFERENCE_BACKEND:-gemini-proxy}"
INFERENCE_MODEL_URL="${INFERENCE_MODEL_URL:-https://storage.googleapis.com/termini-510609-models/gemma-3-4b-it-gguf/gemma-3-4b-it-Q4_K_M.gguf}"
INFERENCE_MODEL_FILE="${INFERENCE_MODEL_FILE:-gemma-3-4b-it-Q4_K_M.gguf}"
INFERENCE_MODEL_SHA256="${INFERENCE_MODEL_SHA256:-04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19}"
INFERENCE_MODEL_ALIAS="${INFERENCE_MODEL_ALIAS:-gemma}"
LLAMACPP_COMMIT="${LLAMACPP_COMMIT:-7fe450e19305b828c199d602c23a8337aaa1f03b}"

# Least-privilege identity for the inference node (task t_5a4a0f73).
# node-c must NOT run as the project's default compute SA (roles/editor): it only
# needs to call Vertex AI and write logs/metrics. The role bindings
# (roles/aiplatform.user, roles/logging.logWriter, roles/monitoring.metricWriter)
# are applied out-of-band by infra/gcp/tools/verify-node-c-egress.sh's companion
# setup; this variable is what makes a RECREATE keep the identity. Leave empty to
# fall back to the legacy default-SA behaviour.
INFERENCE_SERVICE_ACCOUNT="${INFERENCE_SERVICE_ACCOUNT:-termini-node-inference@${PROJECT}.iam.gserviceaccount.com}"
INFERENCE_SCOPES="${INFERENCE_SCOPES:-cloud-platform}"

TOKEN_A="${TOKEN_A:-}"
TOKEN_C="${TOKEN_C:-}"
TEAM_SSH_PUBKEY_FILE="${TEAM_SSH_PUBKEY_FILE:-}"
RENDER_ONLY=0
RENDER_OUT=""
ONLY=""
# 1 = an ephemeral public IP on the created node (see the header: a from-scratch
# node-c cannot install its build tools without a public path).
EXTERNAL_IP="${EXTERNAL_IP:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GCP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RENDER_CLOUD_INIT="$GCP_DIR/../bootstrap/render-cloud-init.sh"
LLAMA_SERVE_SCRIPT="$GCP_DIR/cloud-init/llama-serve.sh"
# ONE renderer for Node C's serve block (llama-serve.sh + the front-door payload),
# shared with infra/gcp/provision.sh so the two gcloud raise paths cannot drift
# (task t_359ed606). The Terraform path renders the same block natively.
NODE_C_SERVE_BLOCK="$GCP_DIR/cloud-init/node-c-serve-block.sh"

log() { printf '[raise-fleet] %s\n' "$*" >&2; }
die() { printf '[raise-fleet] FATAL: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --raise) : ;;
    --render-only) RENDER_ONLY=1 ;;
    --render-out) RENDER_OUT="$2"; shift ;;
    --only) ONLY="$2"; shift ;;
    --with-external-ip) EXTERNAL_IP=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[ -f "$RENDER_CLOUD_INIT" ] || die "missing shared renderer: $RENDER_CLOUD_INIT"
[ -f "$LLAMA_SERVE_SCRIPT" ] || die "missing $LLAMA_SERVE_SCRIPT"
[ -x "$NODE_C_SERVE_BLOCK" ] || die "missing/not executable: $NODE_C_SERVE_BLOCK"

# Node C's serve block — the shared renderer (llama-serve.sh + the front-door
# payload), identical to infra/gcp/provision.sh's (the Terraform path does the
# same through extra_write_files).
llama_extra_write_files() {
  LLAMACPP_COMMIT="$LLAMACPP_COMMIT" \
  MODEL_URL="$INFERENCE_MODEL_URL" \
  MODEL_FILE="$INFERENCE_MODEL_FILE" \
  MODEL_SHA256="$INFERENCE_MODEL_SHA256" \
  MODEL_ALIAS="$INFERENCE_MODEL_ALIAS" \
  PORT="$INFERENCE_PORT" \
  INFERENCE_BACKEND="$INFERENCE_BACKEND" \
    "$NODE_C_SERVE_BLOCK" --write-files
}

userdata() { # $1=node $2=role $3=underlay_ip $4=enrollment_token
  local node="$1" role="$2" ip="$3" token="$4"
  local extra_write="" extra_runcmd=""
  if [ "$node" = "node-c" ]; then
    extra_write="$(llama_extra_write_files)"
    extra_runcmd="$("$NODE_C_SERVE_BLOCK" --runcmd)"
  fi
  controller_url="$CONTROLLER_URL" node_name="$node" \
    enrollment_token="$token" team_ssh_pubkey="$(cat "$TEAM_SSH_PUBKEY_FILE")" \
    role_hint="$role" underlay_ip="$ip/24" underlay_gw="10.10.0.1" seed_ip="10.10.0.11" \
    telemetry_interval_ms="$TELEMETRY_INTERVAL_MS" controller_ca_pubkey="" \
    image_alias="${IMAGE_PROJECT}/${IMAGE_FAMILY}" image_version="$NODE_IMAGE_VERSION" \
    extra_write_files="$extra_write" extra_runcmd="$extra_runcmd" \
    "$RENDER_CLOUD_INIT"
}

render_node() { # $1=node  -> stdout user-data
  case "$1" in
    node-a) userdata node-a compute 10.10.0.11 "$TOKEN_A" ;;
    node-c) userdata node-c inference 10.10.0.13 "$TOKEN_C" ;;
    *) die "unknown node $1" ;;
  esac
}

if [ "$RENDER_ONLY" = "1" ]; then
  [ -n "$TEAM_SSH_PUBKEY_FILE" ] || die "TEAM_SSH_PUBKEY_FILE is required"
  for n in node-a node-c; do
    [ -n "$ONLY" ] && [ "$ONLY" != "${n#node-}" ] && continue
    if [ -n "$RENDER_OUT" ]; then
      mkdir -p "$RENDER_OUT"; render_node "$n" > "$RENDER_OUT/$n.yaml"
      log "rendered $RENDER_OUT/$n.yaml"
    else
      printf -- '--- %s ---\n' "$n"; render_node "$n"
    fi
  done
  exit 0
fi

[ -n "$TEAM_SSH_PUBKEY_FILE" ] || die "TEAM_SSH_PUBKEY_FILE is required"
[ -s "$TEAM_SSH_PUBKEY_FILE" ] || die "team key file empty/missing: $TEAM_SSH_PUBKEY_FILE"
command -v gcloud >/dev/null || die "gcloud not on PATH"

WORKDIR="$(mktemp -d)"; trap 'rm -rf "$WORKDIR"' EXIT

raise_node() { # $1=node
  local node="$1" ip mtype name token
  case "$node" in
    node-a) ip=10.10.0.11; mtype="$COMPUTE_MACHINE_TYPE"; token="$TOKEN_A" ;;
    node-c) ip=10.10.0.13; mtype="$INFERENCE_MACHINE_TYPE"; token="$TOKEN_C" ;;
    *) die "unknown node $node" ;;
  esac
  [ -n "$token" ] || die "$node needs its single-use enrollment token (TOKEN_A / TOKEN_C)"
  name="termini-${node}"
  local ud="$WORKDIR/${node}.yaml"
  local identity=()
  if [ "$node" = "node-c" ] && [ -n "$INFERENCE_SERVICE_ACCOUNT" ]; then
    identity=(--service-account "$INFERENCE_SERVICE_ACCOUNT" --scopes "$INFERENCE_SCOPES")
  fi
  render_node "$node" > "$ud"
  log "$node: user-data $(wc -c <"$ud") bytes -> $ud"
  if gcloud compute instances describe "$name" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
    log "$node: deleting existing $name (replaces the boot disk)"
    gcloud compute instances delete "$name" --zone "$ZONE" --project "$PROJECT" --quiet >/dev/null
  fi
  local addr=()
  if [ "$EXTERNAL_IP" != "1" ]; then
    addr=(--no-address)
  else
    log "$node: --with-external-ip — an ephemeral public IP (first boot needs the Ubuntu mirror)"
  fi
  log "$node: creating $name ($mtype, $ip) from family $IMAGE_FAMILY"
  gcloud compute instances create "$name" \
    --project "$PROJECT" --zone "$ZONE" \
    --machine-type "$mtype" \
    --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
    --boot-disk-size "${BOOT_DISK_GB}GB" --boot-disk-type "$BOOT_DISK_TYPE" \
    --network "$NETWORK" --subnet "$SUBNET" \
    --private-network-ip "$ip" \
    --tags "termini-node,termini-${node}" \
    --labels "project=termini,managed-by=gcloud,workstream=infra,node=${node},role=$([ "$node" = node-c ] && echo inference || echo compute)" \
    --no-shielded-secure-boot "${addr[@]}" \
    "${identity[@]}" \
    --metadata-from-file "user-data=$ud" --quiet
}

for n in node-a node-c; do
  [ -n "$ONLY" ] && [ "$ONLY" != "${n#node-}" ] && continue
  raise_node "$n"
done

log "done — nodes created; waiting for first boot (node-c rebuilds llama.cpp + fetches the GGUF)"
