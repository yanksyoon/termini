#!/usr/bin/env bash
# Termini GCP footprint — clean teardown.
#
# Deletes every resource created by infra/gcp/*.tf or provision.sh, in
# dependency order, then optionally removes the local inventory.
#
# Usage:
#   PROJECT=my-project ./teardown.sh
#   ./teardown.sh --project my-project --remove-inventory
set -euo pipefail

PROJECT="${PROJECT:-}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
PREFIX="${PREFIX:-termini}"
INVENTORY_PATH="${INVENTORY_PATH:-$HOME/.termini/inventory.json}"
REMOVE_INVENTORY=0

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --inventory) INVENTORY_PATH="$2"; shift 2 ;;
    --remove-inventory) REMOVE_INVENTORY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$PROJECT" ]] || die "set PROJECT or pass --project"
command -v gcloud >/dev/null || die "gcloud not found on PATH"

VPC="${PREFIX}-vpc"
SUBNET="${PREFIX}-underlay"

log "deleting instances"
for node in node-a node-b node-c; do
  name="${PREFIX}-${node}"
  if gcloud compute instances describe "$name" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud compute instances delete "$name" --zone "$ZONE" --project "$PROJECT" --quiet
  else
    log "instance $name absent"
  fi
done

log "deleting static addresses"
for node in node-a node-b node-c; do
  name="${PREFIX}-${node}-ip"
  if gcloud compute addresses describe "$name" --region "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud compute addresses delete "$name" --region "$REGION" --project "$PROJECT" --quiet
  else
    log "address $name absent"
  fi
done

log "deleting firewall rules"
for rule in allow-ssh allow-controller-api allow-lxd-cluster allow-vxlan allow-inference; do
  name="${PREFIX}-${rule}"
  if gcloud compute firewall-rules describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud compute firewall-rules delete "$name" --project "$PROJECT" --quiet
  else
    log "firewall $name absent"
  fi
done

log "deleting subnet"
if gcloud compute networks subnets describe "$SUBNET" --region "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute networks subnets delete "$SUBNET" --region "$REGION" --project "$PROJECT" --quiet
else
  log "subnet $SUBNET absent"
fi

log "deleting VPC"
if gcloud compute networks describe "$VPC" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute networks delete "$VPC" --project "$PROJECT" --quiet
else
  log "vpc $VPC absent"
fi

if [[ "$REMOVE_INVENTORY" == "1" && -f "$INVENTORY_PATH" ]]; then
  log "removing inventory $INVENTORY_PATH"
  rm -f "$INVENTORY_PATH"
fi

log "teardown complete"
