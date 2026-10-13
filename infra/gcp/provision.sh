#!/usr/bin/env bash
# Termini GCP footprint — one-shot gcloud provisioner (Terraform fallback).
#
# Creates exactly the same three-node footprint as infra/gcp/*.tf:
#   - one VPC + one underlay subnet (default 10.10.0.0/24)
#   - static internal IPs 10.10.0.11 (node-a), .12 (node-b), .13 (node-c)
#   - firewall rules for SSH(22), controller API(443), LXD cluster(8443),
#     VXLAN/UDP(4789) and llama.cpp HTTP(8080, node C only)
#   - node-a/b = compute machine type; node-c = CPU-heavy inference host
#     (CPU-only by default — free-tier billing rejects every non-TPU accelerator)
#   - node-c additionally serves host-exposed CPU llama.cpp on :8080 (pinned
#     gemma GGUF (Gemma 3 4B IT), OpenAI-compatible, alias `gemma`)
#   - an inventory JSON at $INVENTORY_PATH for downstream tasks
#
# Requires an authenticated gcloud (GOOGLE_APPLICATION_CREDENTIALS or
# `gcloud auth login`) with compute permission on $PROJECT.
#
# Usage:
#   PROJECT=my-project ./provision.sh
#   ./provision.sh --project my-project --zone us-central1-a
#   TEAM_SSH_PUBKEY_FILE=~/.ssh/team.pub ENROLLMENT_TOKEN=... --render-only
set -euo pipefail

PROJECT="${PROJECT:-}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
PREFIX="${PREFIX:-termini}"
NETWORK_CIDR="${NETWORK_CIDR:-10.10.0.0/24}"
COMPUTE_MACHINE_TYPE="${COMPUTE_MACHINE_TYPE:-n2-standard-4}"
INFERENCE_MACHINE_TYPE="${INFERENCE_MACHINE_TYPE:-n2-standard-8}"
GPU_ACCELERATOR_TYPE="${GPU_ACCELERATOR_TYPE:-nvidia-l4}"
GPU_ACCELERATOR_COUNT="${GPU_ACCELERATOR_COUNT:-0}"
SERVE_INFERENCE="${SERVE_INFERENCE:-1}"
INFERENCE_PORT="${INFERENCE_PORT:-8080}"
INFERENCE_MODEL_URL="${INFERENCE_MODEL_URL:-https://storage.googleapis.com/termini-510609-models/gemma-3-4b-it-gguf/gemma-3-4b-it-Q4_K_M.gguf}"
INFERENCE_MODEL_FILE="${INFERENCE_MODEL_FILE:-gemma-3-4b-it-Q4_K_M.gguf}"
INFERENCE_MODEL_SHA256="${INFERENCE_MODEL_SHA256:-04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19}"
INFERENCE_MODEL_ALIAS="${INFERENCE_MODEL_ALIAS:-gemma}"
LLAMACPP_COMMIT="${LLAMACPP_COMMIT:-7fe450e19305b828c199d602c23a8337aaa1f03b}"
IMAGE_PROJECT="${IMAGE_PROJECT:-ubuntu-os-cloud}"
IMAGE_FAMILY="${IMAGE_FAMILY:-ubuntu-2404-lts-amd64}"
BOOT_DISK_GB="${BOOT_DISK_GB:-60}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-pd-balanced}"
SSH_SOURCE_RANGES="${SSH_SOURCE_RANGES:-0.0.0.0/0}"
SSH_USER="${SSH_USER:-ubuntu}"
INVENTORY_PATH="${INVENTORY_PATH:-$HOME/.termini/inventory.json}"

# --- node cloud-init / SSH custody (task t_5e9c9d96) -------------------------
# Both GCP paths render the SAME cloud-init as the LXD flow. The team SSH key is
# injected into authorized_keys (+ sshd hardening); the controller CA anchors
# TrustedUserCAKeys; the enrollment token is written for registration.
CONTROLLER_URL="${CONTROLLER_URL:-https://controller.termini.internal}"
CONTROLLER_CA_PUBKEY="${CONTROLLER_CA_PUBKEY:-}"
TEAM_SSH_PUBKEY_FILE="${TEAM_SSH_PUBKEY_FILE:-}"
ENROLLMENT_TOKEN="${ENROLLMENT_TOKEN:-}"
NODE_IMAGE_VERSION="${NODE_IMAGE_VERSION:-$IMAGE_FAMILY}"
# --render-only renders each node's user-data (no gcloud) for offline checks.
RENDER_ONLY="${RENDER_ONLY:-0}"
RENDER_OUT_DIR="${RENDER_OUT_DIR:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_SERVE_SCRIPT="$SCRIPT_DIR/cloud-init/llama-serve.sh"
RENDER_CLOUD_INIT="$SCRIPT_DIR/../bootstrap/render-cloud-init.sh"

# GPU only when the accelerator count is non-zero (single switch, mirrors the
# Terraform gpu_accelerator_count variable).
use_accel=false
if [[ "$GPU_ACCELERATOR_COUNT" != "0" ]]; then use_accel=true; fi

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --inventory) INVENTORY_PATH="$2"; shift 2 ;;
    --render-only) RENDER_ONLY=1; shift ;;
    --render-out) RENDER_OUT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Only /24 underlays are supported so the .11/.12/.13 host offsets are exact.
if [[ ! "$NETWORK_CIDR" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.0/24$ ]]; then
  die "NETWORK_CIDR must look like x.y.z.0/24 (got: $NETWORK_CIDR)"
fi
NETBASE="${BASH_REMATCH[1]}"
GW_IP="${NETBASE}.1"
IP_A="${NETBASE}.11"
IP_B="${NETBASE}.12"
IP_C="${NETBASE}.13"

VPC="${PREFIX}-vpc"
SUBNET="${PREFIX}-underlay"
COMMON_TAGS="termini-node"
LABELS="project=termini,managed-by=gcloud,workstream=infra,task=t-f2173236"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- cloud-init user-data: render the SHARED bootstrap template --------------
# Same template as the LXD flow (task t_5e9c9d96): team SSH key injection, sshd
# hardening, controller CA, config + enrollment token. Node C's llama.cpp serve
# block is passed in as the optional extra_write_files/extra_runcmd blocks.
: "${ENROLLMENT_TOKEN:?ENROLLMENT_TOKEN is required (single-use controller token)}"
[ -n "$TEAM_SSH_PUBKEY_FILE" ] || die "TEAM_SSH_PUBKEY_FILE is required (team SSH public key file)"
[ -s "$TEAM_SSH_PUBKEY_FILE" ] || die "team key file empty/missing: $TEAM_SSH_PUBKEY_FILE"
[ -f "$RENDER_CLOUD_INIT" ] || die "missing shared cloud-init renderer: $RENDER_CLOUD_INIT"

llama_extra_write_files() {
  local script
  script="$(cat "$LLAMA_SERVE_SCRIPT")"
  script="${script//__LLAMACPP_COMMIT__/$LLAMACPP_COMMIT}"
  script="${script//__MODEL_URL__/$INFERENCE_MODEL_URL}"
  script="${script//__MODEL_FILE__/$INFERENCE_MODEL_FILE}"
  script="${script//__MODEL_SHA256__/$INFERENCE_MODEL_SHA256}"
  script="${script//__MODEL_ALIAS__/$INFERENCE_MODEL_ALIAS}"
  script="${script//__PORT__/$INFERENCE_PORT}"
  printf '  - path: /usr/local/sbin/termini-llama-serve.sh\n'
  printf '    permissions: %s\n' "'0755'"
  printf '    owner: root:root\n'
  printf '    content: |\n'
  while IFS= read -r line; do printf '      %s\n' "$line"; done <<< "$script"
}

userdata() { # $1=node $2=role $3=underlay_ip $4=seed_ip
  local node="$1" role="$2" ip="$3" seed="$4"
  local extra_write="" extra_runcmd=""
  if [[ "$node" == "node-c" && "$SERVE_INFERENCE" == "1" ]]; then
    [[ -f "$LLAMA_SERVE_SCRIPT" ]] || die "missing $LLAMA_SERVE_SCRIPT"
    extra_write="$(llama_extra_write_files)"
    extra_runcmd="  - [ /usr/local/sbin/termini-llama-serve.sh ]"
  fi
  controller_url="$CONTROLLER_URL" node_name="$node" \
    enrollment_token="$ENROLLMENT_TOKEN" team_ssh_pubkey="$(cat "$TEAM_SSH_PUBKEY_FILE")" \
    role_hint="$role" underlay_ip="$ip" underlay_gw="$GW_IP" seed_ip="$seed" \
    telemetry_interval_ms="1000" controller_ca_pubkey="$CONTROLLER_CA_PUBKEY" \
    image_alias="${IMAGE_PROJECT}/${IMAGE_FAMILY}" image_version="$NODE_IMAGE_VERSION" \
    extra_write_files="$extra_write" extra_runcmd="$extra_runcmd" \
    "$RENDER_CLOUD_INIT"
}

# --render-only: emit each node's cloud-init (no gcloud needed) for offline
# inspection / CI, then exit.
if [[ "$RENDER_ONLY" == "1" ]]; then
  for pair in "node-a:compute:$IP_A" "node-b:compute:$IP_B" "node-c:inference:$IP_C"; do
    n="${pair%%:*}"; rest="${pair#*:}"; r="${rest%%:*}"; nip="${rest#*:}"
    if [[ -n "$RENDER_OUT_DIR" ]]; then
      mkdir -p "$RENDER_OUT_DIR"
      userdata "$n" "$r" "${nip}/24" "$IP_A" > "$RENDER_OUT_DIR/$n.yaml"
    else
      printf -- '--- %s ---\n' "$n"
      userdata "$n" "$r" "${nip}/24" "$IP_A"
    fi
  done
  log "rendered cloud-init for node-a/b/c (no gcloud calls)"
  exit 0
fi

[[ -n "$PROJECT" ]] || die "set PROJECT or pass --project"
command -v gcloud >/dev/null || die "gcloud not found on PATH"
command -v jq >/dev/null || die "jq not found on PATH"
command -v envsubst >/dev/null || die "envsubst not found on PATH"

log "project=$PROJECT region=$REGION zone=$ZONE prefix=$PREFIX cidr=$NETWORK_CIDR accel=$use_accel"

# --- Network ----------------------------------------------------------------
if ! gcloud compute networks describe "$VPC" --project "$PROJECT" >/dev/null 2>&1; then
  log "create VPC $VPC"
  gcloud compute networks create "$VPC" --project "$PROJECT" \
    --subnet-mode=custom \
    --description="Termini demo VPC (infra-decisions.md D2)"
fi

if ! gcloud compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  log "create subnet $SUBNET ($NETWORK_CIDR)"
  gcloud compute networks subnets create "$SUBNET" --project "$PROJECT" \
    --network="$VPC" --region="$REGION" --range="$NETWORK_CIDR"
fi

# --- Static internal addresses ----------------------------------------------
for pair in "node-a:$IP_A" "node-b:$IP_B" "node-c:$IP_C"; do
  name="${pair%%:*}"; ip="${pair##*:}"
  if ! gcloud compute addresses describe "${PREFIX}-${name}-ip" --region "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
    log "reserve internal IP $ip for $name"
    gcloud compute addresses create "${PREFIX}-${name}-ip" --project "$PROJECT" \
      --region="$REGION" --subnet="$SUBNET" --addresses="$ip"
  fi
done

# --- Firewall rules ----------------------------------------------------------
fw() { # $1=name $2=allow $3=source $4=tags
  local name="$1" allow="$2" src="$3" tags="$4"
  if ! gcloud compute firewall-rules describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
    log "create firewall $name ($allow from $src)"
    gcloud compute firewall-rules create "$name" --project "$PROJECT" \
      --network="$VPC" --direction=INGRESS --priority=1000 \
      --action=ALLOW --rules="$allow" --source-ranges="$src" --target-tags="$tags"
  fi
}
fw "${PREFIX}-allow-ssh"            "tcp:22"     "$SSH_SOURCE_RANGES" "$COMMON_TAGS"
fw "${PREFIX}-allow-controller-api" "tcp:443"    "$NETWORK_CIDR"      "$COMMON_TAGS"
fw "${PREFIX}-allow-lxd-cluster"    "tcp:8443"   "$NETWORK_CIDR"      "$COMMON_TAGS"
fw "${PREFIX}-allow-vxlan"          "udp:4789"   "$NETWORK_CIDR"      "$COMMON_TAGS"
fw "${PREFIX}-allow-inference"      "tcp:${INFERENCE_PORT}" "$NETWORK_CIDR" "${PREFIX}-node-c"

# --- Instances -----------------------------------------------------------------
create_node() { # $1=node $2=role $3=ip $4=machine_type $5=gpu(true/false)
  local node="$1" role="$2" ip="$3" mtype="$4" gpu="$5"
  local name="${PREFIX}-${node}"
  if gcloud compute instances describe "$name" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
    log "instance $name already exists"
    return 0
  fi
  local ud="$WORKDIR/${node}.yaml"
  userdata "$node" "$role" "${ip}/24" "$IP_A" > "$ud"
  local args=(
    --project "$PROJECT" --zone "$ZONE"
    --machine-type "$mtype"
    --subnet "$SUBNET" --private-network-ip "$ip"
    --tags "$COMMON_TAGS,${PREFIX}-${node}"
    --labels "$LABELS,node=${node},role=${role}"
    --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT"
    --boot-disk-size "${BOOT_DISK_GB}GB" --boot-disk-type "$BOOT_DISK_TYPE"
    --metadata-from-file "user-data=${ud}"
    --no-shielded-secure-boot
  )
  if [[ "$gpu" == "true" ]]; then
    args+=( --accelerator "type=${GPU_ACCELERATOR_TYPE},count=${GPU_ACCELERATOR_COUNT}"
            --maintenance-policy TERMINATE --restart-on-failure )
    log "create instance $name ($mtype, accel=${GPU_ACCELERATOR_TYPE}x${GPU_ACCELERATOR_COUNT})"
  else
    log "create instance $name ($mtype, CPU-only)"
  fi
  gcloud compute instances create "$name" "${args[@]}"
}
create_node "node-a" "compute"   "$IP_A" "$COMPUTE_MACHINE_TYPE"   false
create_node "node-b" "compute"   "$IP_B" "$COMPUTE_MACHINE_TYPE"   false
create_node "node-c" "inference" "$IP_C" "$INFERENCE_MACHINE_TYPE" "$use_accel"

# --- Inventory -----------------------------------------------------------------
mkdir -p "$(dirname "$INVENTORY_PATH")"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

node_json() { # $1=node $2=role $3=ip $4=mtype $5=accel_json
  local node="$1" role="$2" ip="$3" mtype="$4" accel="$5"
  local name="${PREFIX}-${node}" pub
  pub="$(gcloud compute instances describe "$name" --zone "$ZONE" --project "$PROJECT" \
        --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
  jq -n \
    --arg name "$node" --arg host "$name" --arg role "$role" --arg mtype "$mtype" \
    --arg ip "$ip" --arg pub "$pub" --arg zone "$ZONE" --argjson accel "$accel" \
    --arg prefix "$PREFIX" \
    '{name:$name, hostname:$host, role:$role, machine_type:$mtype,
      private_ip:$ip, public_ip:(if $pub=="" then null else $pub end), zone:$zone,
      accelerator:$accel,
      tags:["termini-node", ($prefix + "-" + $name)],
      labels:{"managed-by":"gcloud","node":$name,"role":$role}}'
}

if [[ "$use_accel" == "true" ]]; then
  ACCEL_JSON="$(jq -n --arg t "$GPU_ACCELERATOR_TYPE" --argjson c "$GPU_ACCELERATOR_COUNT" '{type:$t,count:$c}')"
else
  ACCEL_JSON="null"
fi

NODES_JSON="$(jq -s '.' \
  <(node_json node-a compute   "$IP_A" "$COMPUTE_MACHINE_TYPE" null) \
  <(node_json node-b compute   "$IP_B" "$COMPUTE_MACHINE_TYPE" null) \
  <(node_json node-c inference "$IP_C" "$INFERENCE_MACHINE_TYPE" "$ACCEL_JSON"))"

jq -n \
  --arg ts "$now" --arg project "$PROJECT" --arg region "$REGION" --arg zone "$ZONE" \
  --arg vpc "$VPC" --arg subnet "$SUBNET" --arg cidr "$NETWORK_CIDR" --arg gw "$GW_IP" \
  --arg user "$SSH_USER" --argjson nodes "$NODES_JSON" \
  '{schema_version:"1.0", generated_by:"provision.sh", generated_at:$ts,
    project:$project, region:$region, zone:$zone,
    network:{vpc:$vpc, subnetwork:$subnet, cidr:$cidr, gateway:$gw},
    ssh:{user:$user, port:22},
    nodes:$nodes}' > "$INVENTORY_PATH"

log "inventory written to $INVENTORY_PATH"
jq -r '.nodes[] | "  \(.name)\t\(.role)\t\(.machine_type)\t\(.private_ip)\t\(.public_ip // "-")"' "$INVENTORY_PATH"

log "inference endpoint: http://${IP_C}:${INFERENCE_PORT}   lxd seed: ${IP_A}:8443"
