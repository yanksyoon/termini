#!/usr/bin/env bash
# Termini GCP footprint — one-shot gcloud provisioner (Terraform fallback).
#
# Creates exactly the same three-node footprint as infra/gcp/*.tf:
#   - one VPC + one underlay subnet (default 10.10.0.0/24)
#   - static internal IPs 10.10.0.11 (node-a), .12 (node-b), .13 (node-c)
#   - firewall rules for SSH(22), controller API(443), LXD cluster(8443),
#     VXLAN/UDP(4789) and llama.cpp HTTP(8080, node C only)
#   - node-a/b = compute machine type, node-c = real GPU + attached accelerator
#   - an inventory JSON at $INVENTORY_PATH for downstream tasks
#
# Requires an authenticated gcloud (GOOGLE_APPLICATION_CREDENTIALS or
# `gcloud auth login`) with compute permission on $PROJECT.
#
# Usage:
#   PROJECT=my-project ./provision.sh
#   ./provision.sh --project my-project --zone us-central1-a
set -euo pipefail

PROJECT="${PROJECT:-}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
PREFIX="${PREFIX:-termini}"
NETWORK_CIDR="${NETWORK_CIDR:-10.10.0.0/24}"
COMPUTE_MACHINE_TYPE="${COMPUTE_MACHINE_TYPE:-n2-standard-4}"
GPU_MACHINE_TYPE="${GPU_MACHINE_TYPE:-g2-standard-4}"
GPU_ACCELERATOR_TYPE="${GPU_ACCELERATOR_TYPE:-nvidia-l4}"
GPU_ACCELERATOR_COUNT="${GPU_ACCELERATOR_COUNT:-1}"
IMAGE_PROJECT="${IMAGE_PROJECT:-ubuntu-os-cloud}"
IMAGE_FAMILY="${IMAGE_FAMILY:-ubuntu-2404-lts-amd64}"
BOOT_DISK_GB="${BOOT_DISK_GB:-60}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-pd-balanced}"
SSH_SOURCE_RANGES="${SSH_SOURCE_RANGES:-0.0.0.0/0}"
SSH_USER="${SSH_USER:-ubuntu}"
INVENTORY_PATH="${INVENTORY_PATH:-$HOME/.termini/inventory.json}"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --inventory) INVENTORY_PATH="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$PROJECT" ]] || die "set PROJECT or pass --project"
command -v gcloud >/dev/null || die "gcloud not found on PATH"
command -v jq >/dev/null || die "jq not found on PATH"

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

userdata() { # $1=node $2=role $3=underlay_ip $4=seed_ip
  local node="$1" role="$2" ip="$3" seed="$4"
  cat <<EOF
#cloud-config
write_files:
  - path: /etc/termini/config.yaml
    permissions: '0600'
    owner: root:root
    content: |
      controller_url: https://controller.termini.internal
      node_name: ${node}
      role_hint: ${role}
      underlay_ip: ${ip}
      underlay_gw: ${GW_IP}
      bootstrap_seed_ip: ${seed}
      telemetry_interval_ms: 1000
runcmd:
  - [ systemctl, enable, --now, termini-bootstrap.service ]
EOF
}

log "project=$PROJECT region=$REGION zone=$ZONE prefix=$PREFIX cidr=$NETWORK_CIDR"

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
fw "${PREFIX}-allow-inference"      "tcp:8080"   "$NETWORK_CIDR"      "${PREFIX}-node-c"

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
    log "create instance $name ($mtype, GPU=${GPU_ACCELERATOR_TYPE}x${GPU_ACCELERATOR_COUNT})"
  else
    log "create instance $name ($mtype)"
  fi
  gcloud compute instances create "$name" "${args[@]}"
}
create_node "node-a" "compute"   "$IP_A" "$COMPUTE_MACHINE_TYPE" false
create_node "node-b" "compute"   "$IP_B" "$COMPUTE_MACHINE_TYPE" false
create_node "node-c" "inference" "$IP_C" "$GPU_MACHINE_TYPE"     true

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

NODES_JSON="$(jq -s '.' \
  <(node_json node-a compute   "$IP_A" "$COMPUTE_MACHINE_TYPE" null) \
  <(node_json node-b compute   "$IP_B" "$COMPUTE_MACHINE_TYPE" null) \
  <(node_json node-c inference "$IP_C" "$GPU_MACHINE_TYPE" \
      "$(jq -n --arg t "$GPU_ACCELERATOR_TYPE" --argjson c "$GPU_ACCELERATOR_COUNT" '{type:$t,count:$c}')"))"

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

log "inference endpoint: http://${IP_C}:8080   lxd seed: ${IP_A}:8443"
