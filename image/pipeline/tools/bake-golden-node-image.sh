#!/usr/bin/env bash
# bake-golden-node-image.sh — reproducible GOLDEN-VM bake of the Termini node OS
# image, and the publisher of a new member of the `termini-node-os` family.
#
# WHY THIS EXISTS
# ---------------
# `build-image.sh` needs `packer` (preferred) or `distrobuilder` on the build
# host. Neither is installed here, so the route that actually produced the
# deployed family members is a golden VM driven by `gcloud`. That route used to
# exist only in a previous session's scratch work — hand-assembled, uncommitted.
# This script is that route, committed and runnable, so the next bake is one
# command instead of archaeology.
#
# It creates a throwaway VM, uploads the CURRENT tree's provision scripts + the
# workload-image tarball, runs `../golden-vm/bake-node-os.sh` as a SINGLE pass
# (verify before de-instancing), stops the VM and captures the image into the
# family. The golden VM is deleted afterwards; the image is the artifact.
#
# Usage:
#   image/pipeline/tools/bake-golden-node-image.sh \
#     --workload-tarball ~/.cache/termini/images/agent-vm-2026.10.0-c3d039642a61.tar.gz
#
#   --workload-tarball PATH  the workload image tarball (`lxc image export agent-vm/<v>`)
#   --image-name NAME        image to create (default termini-node-os-4)
#   --family NAME            image family (default termini-node-os)
#   --version X.Y.Z          contract version (default 2026.10.0)
#   --project / --zone / --region / --network / --subnet / --tags
#   --machine-type MT        golden VM shape (default n2-standard-2)
#   --boot-disk-size GB      golden VM boot disk (default 20GB)
#   --golden-name NAME       golden VM name (default termini-node-bake-golden)
#   --no-external-ip         create the VM without an external IP (needs IAP/NAT)
#   --keep-golden            do NOT delete the golden VM after publishing
#   --dry-run                print the plan and exit (no gcloud calls)
#   -h|--help
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$SELF_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PIPELINE_DIR/../.." && pwd)"

PROJECT="${TERMINI_GCP_PROJECT:-termini-510609}"
ZONE="${TERMINI_GCP_ZONE:-asia-south1-a}"
REGION="${TERMINI_GCP_REGION:-asia-south1}"
NETWORK="${TERMINI_GCP_NETWORK:-termini-vpc}"
SUBNET="${TERMINI_GCP_SUBNET:-termini-underlay}"
TAGS="${TERMINI_GCP_TAGS:-termini-node}"
MACHINE_TYPE="${TERMINI_GOLDEN_MACHINE_TYPE:-n2-standard-2}"
BOOT_DISK_SIZE="${TERMINI_GOLDEN_BOOT_DISK_SIZE:-20GB}"
BASE_IMAGE_FAMILY="${TERMINI_GOLDEN_BASE_FAMILY:-ubuntu-2404-lts-amd64}"
BASE_IMAGE_PROJECT="${TERMINI_GOLDEN_BASE_PROJECT:-ubuntu-os-cloud}"
GOLDEN_NAME="${TERMINI_GOLDEN_NAME:-termini-node-bake-golden}"
IMAGE_NAME="${TERMINI_IMAGE_NAME:-termini-node-os-4}"
FAMILY="${TERMINI_IMAGE_FAMILY:-termini-node-os}"
VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"
WORKLOAD_TARBALL=""
EXTERNAL_IP=1
KEEP_GOLDEN=0
DRY_RUN=0

usage() { sed -n '2,40p' "$0"; }
log()  { printf '[bake-golden] %s\n' "$*" >&2; }
die()  { printf '[bake-golden] FATAL: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workload-tarball) WORKLOAD_TARBALL="$2"; shift 2 ;;
    --image-name) IMAGE_NAME="$2"; shift 2 ;;
    --family) FAMILY="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --subnet) SUBNET="$2"; shift 2 ;;
    --tags) TAGS="$2"; shift 2 ;;
    --machine-type) MACHINE_TYPE="$2"; shift 2 ;;
    --boot-disk-size) BOOT_DISK_SIZE="$2"; shift 2 ;;
    --golden-name) GOLDEN_NAME="$2"; shift 2 ;;
    --no-external-ip) EXTERNAL_IP=0; shift ;;
    --keep-golden) KEEP_GOLDEN=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$WORKLOAD_TARBALL" ] || die "--workload-tarball is required (the agent-vm/<version> tarball)"
[ -s "$WORKLOAD_TARBALL" ] || die "workload tarball not found or empty: $WORKLOAD_TARBALL"

# Verify the tarball's digest against its sidecar when one exists, so a truncated
# or wrong export cannot be baked into the fleet image.
SIDE="$WORKLOAD_TARBALL.sha256"
if [ -s "$SIDE" ]; then
  want="$(awk '{print $1}' "$SIDE")"
  got="$(sha256sum "$WORKLOAD_TARBALL" | awk '{print $1}')"
  [ "$want" = "$got" ] || die "workload tarball sha256 $got != sidecar $want"
  log "workload tarball digest OK ($(printf '%s' "$got" | cut -c1-12))"
else
  log "no .sha256 sidecar for $WORKLOAD_TARBALL; recording the digest in the image description"
fi

# A family resolves to its NEWEST image, so the caller must not be surprised about
# which image a later `--image-family` boots.
if command -v gcloud >/dev/null 2>&1; then
  existing="$(gcloud compute images list --project "$PROJECT" --filter="family=$FAMILY" \
    --format='value(name)' 2>/dev/null | tr '\n' ' ' || true)"
  log "family $FAMILY currently holds: ${existing:-<none>}"
  case " $existing " in *" $IMAGE_NAME "*) die "image $IMAGE_NAME already exists";; esac
fi

PLAN=$(cat <<PLAN
  project=$PROJECT zone=$ZONE region=$REGION
  network=$NETWORK subnet=$SUBNET tags=$TAGS
  golden VM=$GOLDEN_NAME ($MACHINE_TYPE, $BOOT_DISK_SIZE, external_ip=$EXTERNAL_IP)
  base=$BASE_IMAGE_FAMILY ($BASE_IMAGE_PROJECT)
  workload tarball=$WORKLOAD_TARBALL
  -> image=$IMAGE_NAME family=$FAMILY version=$VERSION
  -> in-VM bake: image/pipeline/golden-vm/bake-node-os.sh (verify then de-instance-ify)
PLAN
)
log "plan:"; printf '%s\n' "$PLAN" >&2
[ "$DRY_RUN" = "1" ] && { log "--dry-run: nothing done"; exit 0; }

command -v gcloud >/dev/null 2>&1 || die "gcloud not on PATH (export PATH=\"\$HOME/google-cloud-sdk/bin:\$PATH\")"
gcloud compute images list --project "$PROJECT" --limit=1 >/dev/null 2>&1 \
  || die "gcloud cannot reach project $PROJECT (auth/billing?)"

# ---------------------------------------------------------------------------
# Bundle the bake inputs. Only these paths are uploaded — the golden VM runs the
# CURRENT tree's provisioner + daemon, which is the point of the bake.
# ---------------------------------------------------------------------------
WORK="$(mktemp -d)"
BUNDLE="$WORK/termini-bake-bundle.tar.gz"
cleanup_work() { rm -rf "$WORK"; }

make_bundle() {
  tar -czf "$BUNDLE" -C "$REPO_ROOT" \
    image/pipeline/provision \
    image/pipeline/tools \
    image/pipeline/golden-vm \
    infra/bootstrap/node
  log "bundle $(du -h "$BUNDLE" | cut -f1)"
}
make_bundle

golden_exists() {
  gcloud compute instances describe "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" \
    >/dev/null 2>&1
}
destroy_golden() {
  if golden_exists; then
    log "deleting golden VM $GOLDEN_NAME"
    gcloud compute instances delete "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" \
      --quiet >/dev/null 2>&1 || log "WARNING: could not delete $GOLDEN_NAME"
  fi
}
# Never leave a golden VM (or temp files) behind — on success OR failure. An ERR
# trap would miss `die`'s plain `exit`, so teardown hangs off EXIT instead.
teardown() {
  rc=$?
  [ "$rc" = 0 ] || printf '[bake-golden] FAILED (rc=%s); tearing down golden VM\n' "$rc" >&2
  if [ "$KEEP_GOLDEN" = "1" ]; then
    golden_exists && log "--keep-golden: leaving $GOLDEN_NAME in place"
  else
    destroy_golden
  fi
  cleanup_work
}
trap teardown EXIT

# SSH transport: prefer a direct connection (the golden VM gets an ephemeral
# external IP by default; `termini-allow-ssh` admits tcp/22 to tag `termini-node`),
# and fall back to an IAP tunnel when the VM has no external IP. Uploading the
# workload tarball through the tunnel is several times slower, so this matters.
TUNNEL=()
pick_ssh_transport() {
  if gcloud compute ssh "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" \
       --command 'echo T_OK' --quiet 2>/dev/null | grep -q T_OK; then
    TUNNEL=(); log "SSH transport: direct"; return 0
  fi
  if gcloud compute ssh "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" \
       --tunnel-through-iap --command 'echo T_OK' --quiet 2>/dev/null | grep -q T_OK; then
    TUNNEL=(--tunnel-through-iap); log "SSH transport: IAP tunnel"; return 0
  fi
  return 1
}
gssh() { gcloud compute ssh "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" \
           "${TUNNEL[@]}" --command="$1" --quiet; }
gscp() { gcloud compute scp --project "$PROJECT" --zone "$ZONE" "${TUNNEL[@]}" \
           --quiet "$@"; }

golden_exists && die "golden VM $GOLDEN_NAME already exists — delete it or pick --golden-name"

log "creating golden VM $GOLDEN_NAME"
ADDR_ARG=(); [ "$EXTERNAL_IP" = "0" ] && ADDR_ARG=(--no-address)
gcloud compute instances create "$GOLDEN_NAME" \
  --project "$PROJECT" --zone "$ZONE" \
  --machine-type "$MACHINE_TYPE" \
  --image-family "$BASE_IMAGE_FAMILY" --image-project "$BASE_IMAGE_PROJECT" \
  --boot-disk-size "$BOOT_DISK_SIZE" --boot-disk-type pd-balanced \
  --network "$NETWORK" --subnet "$SUBNET" --tags "$TAGS" \
  --scopes=cloud-platform \
  "${ADDR_ARG[@]}" --quiet >/dev/null

log "waiting for the golden VM to boot and become SSH-reachable"
SSH_OK=0
for _ in $(seq 1 60); do
  if gcloud compute ssh "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" \
       --command 'cloud-init status --wait >/dev/null 2>&1 || true; echo SSH_READY' \
       --quiet 2>/dev/null | grep -q SSH_READY \
     || gcloud compute ssh "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
       --command 'cloud-init status --wait >/dev/null 2>&1 || true; echo SSH_READY' \
       --quiet 2>/dev/null | grep -q SSH_READY; then
    SSH_OK=1; break
  fi
  sleep 10
done
[ "$SSH_OK" = "1" ] || die "golden VM never became SSH-reachable"
pick_ssh_transport || die "no working SSH transport to $GOLDEN_NAME"

log "uploading the bake bundle"
gscp "$BUNDLE" "$GOLDEN_NAME:/tmp/termini-bake-bundle.tar.gz" >/dev/null
gssh 'sudo rm -rf /opt/termini/bake && sudo mkdir -p /opt/termini/bake && sudo tar -xzf /tmp/termini-bake-bundle.tar.gz -C /opt/termini/bake && ls /opt/termini/bake/image/pipeline/provision/10-node-os.sh'

log "uploading the workload image tarball ($(du -h "$WORKLOAD_TARBALL" | cut -f1))"
gscp "$WORKLOAD_TARBALL" "$GOLDEN_NAME:/tmp/termini-workload-image.tar.gz" >/dev/null

log "running the in-VM bake (single pass; this is the long step)"
BAKE_LOG="$WORK/bake.log"
# shellcheck disable=SC2016  # the remote shell expands the $-vars, not this one
gssh "sudo BAKE_SRC=/opt/termini/bake WORKLOAD_TARBALL=/tmp/termini-workload-image.tar.gz \
      TERMINI_IMAGE_VERSION='$VERSION' \
      bash /opt/termini/bake/image/pipeline/golden-vm/bake-node-os.sh" \
  2>&1 | tee "$BAKE_LOG" >&2

grep -q '^BAKE_OK ' "$BAKE_LOG" || die "the in-VM bake did not print BAKE_OK"

log "bake OK — capturing $IMAGE_NAME into family $FAMILY"
gcloud compute instances stop "$GOLDEN_NAME" --project "$PROJECT" --zone "$ZONE" --quiet >/dev/null
gcloud compute images create "$IMAGE_NAME" \
  --project "$PROJECT" \
  --source-disk "$GOLDEN_NAME" --source-disk-zone "$ZONE" \
  --family "$FAMILY" \
  --description "Termini node OS: LXD initialised serving :8443, workload image under agent-vm/${VERSION} + agent-os/w4, current termini-bootstrap reporting its own LXD endpoint (golden-VM bake, see image/pipeline/golden-vm)" \
  --quiet >/dev/null

for _ in $(seq 1 60); do
  st="$(gcloud compute images describe "$IMAGE_NAME" --project "$PROJECT" --format='value(status)' 2>/dev/null || true)"
  [ "$st" = "READY" ] && break
  sleep 5
done
[ "${st:-}" = "READY" ] || die "image $IMAGE_NAME did not reach READY (status=${st:-unknown})"

newest="$(gcloud compute images list --project "$PROJECT" --filter="family=$FAMILY" \
  --sort-by=~creationTimestamp --format='value(name)' | head -1)"
log "image READY: $IMAGE_NAME; family $FAMILY now resolves to: $newest"
[ "$newest" = "$IMAGE_NAME" ] || log "WARNING: $IMAGE_NAME is not the newest family member ($newest) — --image-family will boot $newest"

log "done. image=$IMAGE_NAME family=$FAMILY version=$VERSION"
log "verify with: image/pipeline/tools/verify-baked-node.sh --image-name $IMAGE_NAME"
