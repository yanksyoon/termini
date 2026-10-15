#!/usr/bin/env bash
#
# teardown.sh — take the Termini GCP footprint down, in one of TWO explicit modes.
#
# WHY THIS EXISTS (measured 2026-10-15 on the live project termini-510609)
#   "Compute stopped" is NOT "clean". All four GCE instances (termini-controller,
#   termini-node-a/b/c) were TERMINATED and Cloud Run termini-gemma scaled to
#   zero — and the project still billed $37.77/mo of idle spend:
#
#     210 GiB pd-balanced, 4 disks (30+60+60+60)   $0.120/GiB.mo = $25.20/mo
#      60 GiB image storage  termini-node-os-1/3/5 $0.060/GiB.mo =  $3.60/mo
#      static IP termini-controller-ip 34.93.190.64 $0.012/h x730h =  $8.76/mo
#      gs://termini-510609-models 10.37 GiB (SG)   $0.020/GiB.mo =  $0.21/mo
#
#   A static external IP is billed as IN USE while it is associated with an
#   instance — whether that instance is running or stopped. A TERMINATED
#   instance keeps billing every disk and image attached to it.
#
# The previous version of this script could not delete ANY of that: it defaulted
# REGION/ZONE to us-central1/us-central1-a while the footprint lives in
# asia-south1/asia-south1-a, so every `describe` failed, every branch printed
# "<name> absent", and it ended with "teardown complete" having deleted nothing.
# It swallowed API errors as "absent", never touched the images, the bucket, the
# Cloud Run service, the registry or the reserved IP, and had no way to tell the
# operator that it had failed.
#
# MODES (exactly one is required — there is no default, on purpose)
#   --pause   Stop everything that bills by the hour (RUNNING instances) and
#             scale Cloud Run to zero. KEEPS the disks, the images, the GCS
#             bucket and the reserved IP, so the fleet can be raised again
#             quickly from the pre-baked node image. Deletes NOTHING.
#   --full    Delete every billable resource in scope: instances (including
#             termini-controller) and their disks, regional/global addresses and
#             forwarding rules, images (every version of the termini-node-os
#             family), snapshots, machine images, reservations, instance
#             templates, the Cloud Run service termini-gemma and its revisions,
#             gs://…-models and its objects, the Artifact Registry repository,
#             non-Google-managed log buckets, and anything else it enumerates.
#             Exits NON-ZERO if anything it intended to delete survives.
#
# Usage:
#   teardown.sh --pause --project P --region R --zone Z [--dry-run] [--name-filter ERE]
#   teardown.sh --full  --project P --region R --zone Z [--dry-run] [--name-filter ERE]
#
#   --project P          GCP project id                     (required, no default)
#   --region R           primary region of the footprint    (required, no default)
#   --zone Z             primary zone of the footprint      (required, no default)
#   --dry-run            print every WOULD DELETE / RETAIN decision and change nothing
#   --name-filter ERE    only act on resources whose NAME matches (default: ^termini)
#                        use it to exercise --full on throwaway termini-verify-*
#                        resources without touching a protected footprint
#   --services-region N  region of Cloud Run / Artifact Registry (default asia-southeast1)
#   --allow-empty-scope  permit a run whose declared zone/region holds none of the
#                        project's instances/disks (default: FAIL LOUDLY — this is
#                        the wrong-zone guard the old script lacked)
#   --allow-protected-demo
#                        override the retention freeze and allow --full to delete
#                        the protected demo set before 2026-11-06 (requires an
#                        explicit human approval recorded on the kanban card)
#   --delete-quota-preferences
#                        also delete the Cloud Quotas preference records created
#                        by us (OFF by default: they are not billable and one of
#                        them holds the granted CPUS-ALL-REGIONS=12 state the
#                        fleet depends on)
#   --include-network    also delete firewall rules, subnets and the VPC
#                        (OFF by default: a VPC, subnet and firewall rule are free)
#   --inventory PATH / --remove-inventory
#                        local inventory file handling
#
# Exit codes:
#   0  the requested mode completed and its post-condition was verified
#   1  refused / aborted: bad arguments, unreachable project, wrong zone/region,
#      a describe that failed for a reason other than NOT_FOUND, or a protected
#      resource in the deletion scope before the freeze expires
#   2  --full ran, but resources it intended to delete SURVIVE (listed at the end)
#
# Every mutating command is followed by a `describe` that must confirm the
# resource is gone. Nothing is reported as deleted on the strength of an exit code.
set -euo pipefail

MODE=""
PROJECT=""
REGION=""
ZONE=""
DRY_RUN=0
NAME_FILTER="^termini"
SERVICES_REGION="asia-southeast1"
ALLOW_EMPTY_SCOPE=0
ALLOW_PROTECTED=0
DELETE_QUOTA_PREFS=0
INCLUDE_NETWORK=0
REMOVE_INVENTORY=0
INVENTORY_PATH="${INVENTORY_PATH:-$HOME/.termini/inventory.json}"

# Retention freeze (user decision recorded on kanban t_59831eb3): until this date
# `--full` refuses to touch the demo footprint; termini-controller-ip is the
# published demo URL (https://34-93-190-64.sslip.io/...) and the fleet must stay
# re-raisable. After it, `--full` deletes everything, protection included.
PROTECT_UNTIL="2026-11-06"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFY_TOOL="$SCRIPT_DIR/tools/cleanup-verify.sh"

SURVIVORS=()      # resources that should be gone but are not
DELETED=()        # resources confirmed gone
RETAINED=()       # resources deliberately kept, with the reason
WOULD=()          # dry-run decisions

log()  { printf '[teardown] %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '[teardown] WARNING: %s\n' "$*" >&2; }
err()  { printf '[teardown] ERROR: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }
usage() { sed -n '2,72p' "$0" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --pause)             [ -z "$MODE" ] || die "give exactly one of --pause / --full"; MODE="pause" ;;
    --full)              [ -z "$MODE" ] || die "give exactly one of --pause / --full"; MODE="full" ;;
    --project)           PROJECT="${2:-}"; shift ;;
    --region)            REGION="${2:-}"; shift ;;
    --zone)              ZONE="${2:-}"; shift ;;
    --services-region)   SERVICES_REGION="${2:-}"; shift ;;
    --dry-run)           DRY_RUN=1 ;;
    --name-filter)       NAME_FILTER="${2:-}"; shift ;;
    --allow-empty-scope) ALLOW_EMPTY_SCOPE=1 ;;
    --allow-protected-demo) ALLOW_PROTECTED=1 ;;
    --delete-quota-preferences) DELETE_QUOTA_PREFS=1 ;;
    --include-network)   INCLUDE_NETWORK=1 ;;
    --inventory)         INVENTORY_PATH="${2:-}"; shift ;;
    --remove-inventory)  REMOVE_INVENTORY=1 ;;
    -h|--help)           usage; exit 0 ;;
    *)                   err "unknown argument: $1"; usage; exit 1 ;;
  esac
  shift
done

[ -n "$MODE" ] || die "a mode is required: --pause or --full (there is deliberately no default)"
[ -n "$PROJECT" ] || die "--project is required (there is deliberately no default)"
[ -n "$REGION" ]  || die "--region is required (there is deliberately no default)"
[ -n "$ZONE" ]    || die "--zone is required (there is deliberately no default)"
case "$ZONE" in
  "$REGION"-*) : ;;
  *) die "--zone $ZONE does not belong to --region $REGION" ;;
esac
# The old default (us-central1/us-central1-a) is unreachable on purpose: it is
# what made every describe fail and the script report a clean teardown of nothing.
case "$REGION" in
  us-central1|us-east1|us-west1|europe-west1|us)
    die "region $REGION is the legacy (wrong) default for this footprint — pass the real one (asia-south1)" ;;
esac
case "$ZONE" in
  us-central1-a|us-central1-b|us-central1-f|us-east1-b|us-west1-b|europe-west1-b)
    die "zone $ZONE is the legacy (wrong) default for this footprint — pass the real one (asia-south1-a)" ;;
esac
command -v gcloud >/dev/null || die "gcloud not found on PATH"
[ -x "$VERIFY_TOOL" ] || die "missing enumerator: $VERIFY_TOOL"
[ "$MODE" = "full" ] || [ "$DELETE_QUOTA_PREFS" = 0 ] || die "--delete-quota-preferences only applies to --full"

log "mode=$MODE project=$PROJECT region=$REGION zone=$ZONE filter=$NAME_FILTER dry_run=$DRY_RUN"

# ---------------------------------------------------------------------------
# fail-loud primitives
# ---------------------------------------------------------------------------
# gcloud_text LABEL GCLOUD-ARGS... -> prints stdout; a disabled API is an empty
# result (a project cannot hold resources of a service it has never enabled),
# ANY other failure aborts the run.
gcloud_text() {
  local label="$1"; shift
  # call sites may pass the full command with a leading `gcloud` token
  if [ "${1:-}" = "gcloud" ]; then shift; fi
  local out errfile
  errfile="$(mktemp)"
  if out="$(gcloud "$@" 2>"$errfile")"; then
    rm -f "$errfile"; printf '%s' "$out"; return 0
  fi
  out="$(cat "$errfile")"; rm -f "$errfile"
  case "$out" in
    *SERVICE_DISABLED*|*"API has not been enabled"*|*"has not been used in project"*|*"is not enabled"*)
      log "$label: API not enabled for this project — nothing of this class exists"
      printf ''; return 0 ;;
  esac
  err "$label FAILED — refusing to guess:"
  printf '%s\n' "$out" >&2
  exit 1
}

# state LABEL GCLOUD-DESCRIBE-ARGS... -> 0 = present, 1 = NOT_FOUND, exit 1 = anything else.
# This is the defect-4 fix: a 403, a disabled API, a quota error or a malformed
# zone must never be read as "absent".
state() {
  local label="$1"; shift
  # call sites may pass the full command with a leading `gcloud` token
  if [ "${1:-}" = "gcloud" ]; then shift; fi
  local out errfile rc
  errfile="$(mktemp)"
  set +e
  gcloud "$@" >/dev/null 2>"$errfile"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then rm -f "$errfile"; return 0; fi
  out="$(cat "$errfile")"; rm -f "$errfile"
  case "$out" in
    *"does not have permission"*|*PERMISSION_DENIED*|*"Required '"*|*SERVICE_DISABLED*|\
    *"API has not been used"*|*"has not been used in project"*|*"is not enabled"*|\
    *"Invalid value for field"*|*"Unknown zone"*|*RESOURCE_EXHAUSTED*|*"Quota exceeded"*|\
    *UNAVAILABLE*|*"Internal error"*|*"backendError"*)
      err "describe failed for $label for a reason other than NOT_FOUND — aborting (this is NOT 'absent'):"
      printf '%s\n' "$out" >&2
      exit 1 ;;
    *"was not found"*|*"not found: 404"*|*"Cannot find service"*|*NOT_FOUND*|*"does not exist"*)
      return 1 ;;
    *)
      err "describe failed for $label with an unrecognised error — aborting (this is NOT 'absent'):"
      printf '%s\n' "$out" >&2
      exit 1 ;;
  esac
}

in_filter() { [[ "$1" =~ $NAME_FILTER ]]; }

# Protected demo set: the published demo URL (the reserved IP), its 4 VMs, their
# disks (same names) and the node OS image family. Frozen until $PROTECT_UNTIL.
protected_name() {
  case "$1" in
    termini-controller|termini-controller-ip|\
    termini-node-a|termini-node-b|termini-node-c|\
    termini-node-a-ip|termini-node-b-ip|termini-node-c-ip|\
    termini-510609-models|termini-node-os|termini-node-os-*) return 0 ;;
  esac
  return 1
}

freeze_active() { [ "$(date -u +%Y%m%d)" -lt "${PROTECT_UNTIL//-/}" ]; }

# ---------------------------------------------------------------------------
# preflight — the project must be reachable and the declared scope must be real
# ---------------------------------------------------------------------------
preflight() {
  step "preflight"
  local out
  if ! out="$(gcloud projects describe "$PROJECT" --format="value(projectId,lifecycleState)" 2>&1)"; then
    err "project $PROJECT is not reachable (this is what the old script printed as '<name> absent'):"
    printf '%s\n' "$out" >&2
    exit 1
  fi
  case "$out" in
    *ACTIVE*) log "project $PROJECT reachable and ACTIVE" ;;
    *) die "project $PROJECT is not ACTIVE: $out" ;;
  esac

  local ij dj total_i zone_i total_d zone_d a_total a_region
  ij="$(gcloud_text instances compute instances list --project="$PROJECT" --format=json)"
  dj="$(gcloud_text disks compute disks list --project="$PROJECT" --format=json)"
  total_i="$(jq -r 'length' <<<"$ij")"
  zone_i="$(jq -r --arg z "$ZONE" '[.[] | select((.zone|sub(".*/";""))==$z)] | length' <<<"$ij")"
  total_d="$(jq -r 'length' <<<"$dj")"
  zone_d="$(jq -r --arg z "$ZONE" '[.[] | select((.zone|sub(".*/";""))==$z)] | length' <<<"$dj")"
  a_total="$(jq -r 'length' <<<"$(gcloud_text addresses compute addresses list --project="$PROJECT" --format=json)")"
  a_region="$(jq -r --arg r "$REGION" '[.[] | select((.region|sub(".*/";""))==$r)] | length' <<<"$(gcloud_text addresses compute addresses list --project="$PROJECT" --format=json)")"

  log "scope: instances ${zone_i}/${total_i} in $ZONE · disks ${zone_d}/${total_d} · addresses ${a_region}/${a_total} in $REGION"

  if [ "$ALLOW_EMPTY_SCOPE" = 0 ]; then
    if [ "$total_i" -gt 0 ] && [ "$zone_i" -eq 0 ]; then
      err "DECLARED ZONE DOES NOT HOLD THIS FOOTPRINT: the project has $total_i instance(s) but 0 in $ZONE."
      jq -r '.[] | "    instance \(.name) is in \((.zone|sub(".*/";"")))"' <<<"$ij" >&2
      err "the old script reported this exact situation as '<name> absent … teardown complete'."
      err "pass the real --zone, or --allow-empty-scope if you really mean to run against an empty zone."
      exit 1
    fi
    if [ "$total_d" -gt 0 ] && [ "$zone_d" -eq 0 ]; then
      err "DECLARED ZONE DOES NOT HOLD THIS FOOTPRINT: the project has $total_d disk(s) but 0 in $ZONE."
      jq -r '.[] | "    disk \(.name) is in \((.zone|sub(".*/";"")))"' <<<"$dj" >&2
      exit 1
    fi
    if [ "$a_total" -gt 0 ] && [ "$a_region" -eq 0 ]; then
      err "DECLARED REGION DOES NOT HOLD THIS FOOTPRINT: the project has $a_total address(es) but 0 in $REGION."
      exit 1
    fi
  fi

  if [ "$MODE" = "full" ] && freeze_active && [ "$ALLOW_PROTECTED" = 0 ]; then
    local -a hit_lines=()
    local n
    while IFS= read -r n; do
      in_filter "$n" && protected_name "$n" && hit_lines+=("compute.instance $n")
    done < <(jq -r --arg z "$ZONE" '.[] | select((.zone|sub(".*/";""))==$z) | .name' <<<"$ij")
    while IFS= read -r n; do
      in_filter "$n" && protected_name "$n" && hit_lines+=("compute.disk $n")
    done < <(jq -r --arg z "$ZONE" '.[] | select((.zone|sub(".*/";""))==$z) | .name' <<<"$dj")
    while IFS= read -r n; do
      in_filter "$n" && protected_name "$n" && hit_lines+=("compute.image $n")
    done < <(jq -r '.[] | .name' <<<"$(gcloud_text images compute images list --project="$PROJECT" --no-standard-images --format=json)")
    while IFS= read -r n; do
      in_filter "$n" && protected_name "$n" && hit_lines+=("compute.address $n")
    done < <(jq -r '.[] | .name' <<<"$(gcloud_text addresses2 compute addresses list --project="$PROJECT" --format=json)")
    while IFS= read -r n; do
      in_filter "$n" && protected_name "$n" && hit_lines+=("storage.bucket gs://$n")
    done < <(jq -r '.[] | .name' <<<"$(gcloud_text buckets gcloud storage buckets list --project="$PROJECT" --format=json)")
    if [ "${#hit_lines[@]}" -gt 0 ]; then
      if [ "$DRY_RUN" = 1 ]; then
        log "DRY RUN: these ${#hit_lines[@]} protected resources are in scope and would be RETAINED (freeze until $PROTECT_UNTIL):"
        printf '    %s\n' "${hit_lines[@]}"
      else
        err "--full would delete ${#hit_lines[@]} PROTECTED demo resource(s) and the retention freeze is active until $PROTECT_UNTIL:"
        printf '    %s\n' "${hit_lines[@]}" >&2
        err "termini-controller-ip is the published demo URL (https://34-93-190-64.sslip.io/...) and the fleet"
        err "must stay re-raisable. Re-run with --allow-protected-demo ONLY with the user's recorded approval,"
        err "or scope the run with --name-filter (e.g. '^termini-verify-')."
        exit 1
      fi
    fi
  fi
  log "preflight OK"
}

# ---------------------------------------------------------------------------
# one resource: exists? -> in scope? -> protected? -> delete -> confirm gone
# ---------------------------------------------------------------------------
resource() { # LABEL NAME LOCATION [gcloud] gcloud-describe-args...
  local label="$1" name="$2" location="$3"; shift 3
  if [ "${1:-}" = "gcloud" ]; then shift; fi
  local id="$label $name ($location)"
  if ! in_filter "$name"; then
    RETAINED+=("$id — outside --name-filter $NAME_FILTER"); return 0
  fi
  if state "$id" "$@"; then
    :
  else
    log "absent: $id"; return 0
  fi
  if protected_name "$name" && freeze_active && [ "$ALLOW_PROTECTED" = 0 ]; then
    RETAINED+=("$id — PROTECTED demo resource, retention freeze until $PROTECT_UNTIL"); return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    WOULD+=("$id — would delete")
    return 0
  fi
  local dcmd=("$@")
  dcmd=("${dcmd[@]/describe/delete}")
  if ! gcloud "${dcmd[@]}" --quiet; then
    err "delete failed for $id"
    SURVIVORS+=("$id — delete command failed")
    return 0
  fi
  if state "$id" "$@"; then
    SURVIVORS+=("$id — still present after delete")
  else
    DELETED+=("$id")
    log "deleted: $id"
  fi
}

retain() { RETAINED+=("$1"); }
would()  { WOULD+=("$1"); }

# ---------------------------------------------------------------------------
# PAUSE — stop what bills by the hour; delete nothing
# ---------------------------------------------------------------------------
do_pause() {
  step "pause: stopping instances (nothing is deleted)"
  local json
  json="$(gcloud_text instances compute instances list --project="$PROJECT" --format=json)"
  local name zone status
  while IFS=$'\t' read -r name zone status; do
    in_filter "$name" || { retain "compute.instance $name ($zone) — outside --name-filter $NAME_FILTER"; continue; }
    if [ "$zone" != "$ZONE" ]; then
      retain "compute.instance $name ($zone) — outside --zone $ZONE"; continue
    fi
    case "$status" in
      RUNNING|PROVISIONING|STAGING)
        if [ "$DRY_RUN" = 1 ]; then would "compute.instance $name ($zone) — would STOP (bills while running)"; continue; fi
        log "stopping $name ($zone, $status)"
        gcloud compute instances stop "$name" --zone "$zone" --project "$PROJECT" --quiet
        if [ "$(gcloud compute instances describe "$name" --zone "$zone" --project "$PROJECT" --format="value(status)")" = "RUNNING" ]; then
          SURVIVORS+=("compute.instance $name — still RUNNING after stop")
        else
          DELETED+=("compute.instance $name — stopped (instance billing ended; disks/IP still bill)")
        fi ;;
      TERMINATED|STOPPED|SUSPENDED)
        log "already stopped: $name ($zone, $status) — no mutation" ;;
      *)
        warn "instance $name is in unexpected state '$status' — left alone" ;;
    esac
  done < <(jq -r '.[] | [(.name), (.zone|sub(".*/";"")), .status] | @tsv' <<<"$json")

  step "pause: Cloud Run min-instances (scale to zero)"
  local rjson
  rjson="$(gcloud_text run.services gcloud run services list --project="$PROJECT" --format=json)"
  local sname sregion min
  while IFS=$'\t' read -r sname sregion min; do
    in_filter "$sname" || { retain "run.service $sname ($sregion) — outside --name-filter $NAME_FILTER"; continue; }
    if [ "$min" = "0" ] || [ -z "$min" ]; then
      log "already scales to zero: $sname ($sregion, min-instances=${min:-0})"
    elif [ "$DRY_RUN" = 1 ]; then
      would "run.service $sname ($sregion) — would set min-instances=0"
    else
      log "setting min-instances=0 on $sname ($sregion)"
      gcloud run services update "$sname" --region "$sregion" --project "$PROJECT" --min-instances 0 --quiet
    fi
  done < <(jq -r '.[] | [(.metadata.name), (.metadata.labels["cloud.googleapis.com/location"] // "-"), (.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0")] | @tsv' <<<"$rjson")

  step "pause: verifying the retained set is still raisable"
  local misses=0
  while IFS=$'\t' read -r kind name scope; do
    in_filter "$name" || continue
    local args=()
    case "$kind" in
      instance)    args=(compute instances describe "$name" --zone "$scope" --project "$PROJECT") ;;
      disk)        args=(compute disks describe "$name" --zone "$scope" --project "$PROJECT") ;;
      image)       args=(compute images describe "$name" --project "$PROJECT") ;;
      address)     args=(compute addresses describe "$name" --region "$scope" --project "$PROJECT") ;;
      bucket)      args=(gcloud storage buckets describe "gs://$name" --project "$PROJECT") ;;
    esac
    if ! state "$kind $name" "${args[@]}"; then
      warn "retained resource MISSING: $kind $name ($scope) — the fleet may not be raisable"
      misses=1
    fi
  done < <(pause_retention_inventory)
  [ "$misses" = 0 ] && log "retained set intact (disks, images, bucket, reserved IP)"

  step "pause: retained (nothing was deleted)"
  local kind scope
  while IFS=$'\t' read -r kind name scope; do
    in_filter "$name" || continue
    retain "$kind $name ($scope) — kept for a fast re-raise"
  done < <(pause_retention_inventory)
}

# the expensive-to-rebuild set: disks, images, bucket, reserved addresses
pause_retention_inventory() {
  jq -r '.[] | ["disk", .name, (.zone|sub(".*/";""))] | @tsv' \
     <<<"$(gcloud_text disks compute disks list --project="$PROJECT" --format=json)"
  jq -r '.[] | ["image", .name, "global"] | @tsv' \
     <<<"$(gcloud_text images compute images list --project="$PROJECT" --no-standard-images --format=json)"
  jq -r '.[] | ["address", .name, (.region|sub(".*/";""))] | @tsv' \
     <<<"$(gcloud_text addresses compute addresses list --project="$PROJECT" --format=json)"
  gcloud_text buckets gcloud storage buckets list --project="$PROJECT" --format=json |
    jq -r '.[] | ["bucket", .name, (.location // "-")] | @tsv'
}

# ---------------------------------------------------------------------------
# FULL — delete every billable resource in scope
# ---------------------------------------------------------------------------
do_full() {
  step "full: instances (incl. termini-controller) and their disks"
  local json name zone
  json="$(gcloud_text instances compute instances list --project="$PROJECT" --format=json)"
  while IFS=$'\t' read -r name zone; do
    resource "compute.instance" "$name" "$zone" compute instances describe "$name" --zone "$zone" --project "$PROJECT"
  done < <(jq -r '.[] | [(.name), (.zone|sub(".*/";""))] | @tsv' <<<"$json")

  json="$(gcloud_text disks compute disks list --project="$PROJECT" --format=json)"
  while IFS=$'\t' read -r name zone; do
    resource "compute.disk" "$name" "$zone" compute disks describe "$name" --zone "$zone" --project="$PROJECT"
  done < <(jq -r '.[] | [(.name), (.zone|sub(".*/";""))] | @tsv' <<<"$json")

  step "full: regional and global addresses, forwarding rules"
  json="$(gcloud_text addresses compute addresses list --project="$PROJECT" --format=json)"
  local region
  while IFS=$'\t' read -r name region; do
    resource "compute.address" "$name" "$region" compute addresses describe "$name" --region "$region" --project="$PROJECT"
  done < <(jq -r '.[] | [(.name), (.region|sub(".*/";""))] | @tsv' <<<"$json")
  json="$(gcloud_text addresses_global compute addresses list --project="$PROJECT" --global --format=json)"
  while IFS= read -r name; do
    resource "compute.address.global" "$name" "global" compute addresses describe "$name" --global --project="$PROJECT"
  done < <(jq -r '.[] | .name' <<<"$json")

  json="$(gcloud_text forwarding_rules compute forwarding-rules list --project="$PROJECT" --format=json)"
  while IFS=$'\t' read -r name region; do
    resource "compute.forwarding-rule" "$name" "$region" compute forwarding-rules describe "$name" --region "$region" --project="$PROJECT"
  done < <(jq -r '.[] | [(.name), (.region|sub(".*/";""))] | @tsv' <<<"$json")
  json="$(gcloud_text forwarding_rules_global compute forwarding-rules list --project="$PROJECT" --global --format=json)"
  while IFS= read -r name; do
    resource "compute.forwarding-rule.global" "$name" "global" compute forwarding-rules describe "$name" --global --project="$PROJECT"
  done < <(jq -r '.[] | .name' <<<"$json")

  step "full: images (every version of every family), snapshots, machine images, reservations, templates"
  json="$(gcloud_text images compute images list --project="$PROJECT" --no-standard-images --format=json)"
  while IFS= read -r name; do
    resource "compute.image" "$name" "global" compute images describe "$name" --project="$PROJECT"
  done < <(jq -r '.[] | .name' <<<"$json")

  json="$(gcloud_text snapshots compute snapshots list --project="$PROJECT" --format=json)"
  while IFS= read -r name; do
    resource "compute.snapshot" "$name" "global" compute snapshots describe "$name" --project="$PROJECT"
  done < <(jq -r '.[] | .name' <<<"$json")

  json="$(gcloud_text machine_images compute machine-images list --project="$PROJECT" --format=json)"
  while IFS= read -r name; do
    resource "compute.machine-image" "$name" "global" compute machine-images describe "$name" --project="$PROJECT"
  done < <(jq -r '.[] | .name' <<<"$json")

  json="$(gcloud_text reservations compute reservations list --project="$PROJECT" --format=json)"
  while IFS=$'\t' read -r name zone; do
    resource "compute.reservation" "$name" "$zone" compute reservations describe "$name" --zone "$zone" --project="$PROJECT"
  done < <(jq -r '.[] | [(.name), (.zone|sub(".*/";""))] | @tsv' <<<"$json")

  json="$(gcloud_text instance_templates compute instance-templates list --project="$PROJECT" --format=json)"
  while IFS= read -r name; do
    resource "compute.instance-template" "$name" "global" compute instance-templates describe "$name" --project="$PROJECT"
  done < <(jq -r '.[] | .name' <<<"$json")

  step "full: Cloud Run service and its revisions ($SERVICES_REGION)"
  json="$(gcloud_text run.services gcloud run services list --project="$PROJECT" --format=json)"
  local sname sregion
  while IFS=$'\t' read -r sname sregion; do
    resource "run.service" "$sname" "$sregion" gcloud run services describe "$sname" --region "$sregion" --project="$PROJECT"
    local rjson
    rjson="$(gcloud_text "run.revisions:$sname" gcloud run revisions list --project="$PROJECT" --region "$sregion" --service "$sname" --format=json)"
    while IFS= read -r rev; do
      if [ "$DRY_RUN" = 1 ]; then
        in_filter "$rev" && would "run.revision $rev ($sregion) — deleted with its service"
      elif in_filter "$rev"; then
        SURVIVORS+=("run.revision $rev ($sregion) — survived the deletion of service $sname")
      fi
    done < <(jq -r '.[] | .metadata.name' <<<"$rjson")
  done < <(jq -r '.[] | [(.metadata.name), (.metadata.labels["cloud.googleapis.com/location"] // "-")] | @tsv' <<<"$json")

  step "full: Artifact Registry repositories ($SERVICES_REGION)"
  json="$(gcloud_text artifacts.repositories gcloud artifacts repositories list --project="$PROJECT" --format=json)"
  local rname rloc
  while IFS=$'\t' read -r rname rloc; do
    if ! in_filter "$rname"; then retain "artifacts.repository $rname ($rloc) — outside --name-filter $NAME_FILTER"; continue; fi
    if ! state "artifacts.repository $rname" gcloud artifacts repositories describe "$rname" --location "$rloc" --project="$PROJECT"; then
      log "absent: artifacts.repository $rname ($rloc)"; continue
    fi
    if [ "$DRY_RUN" = 1 ]; then would "artifacts.repository $rname ($rloc) — would delete (with every image in it)"; continue; fi
    # A repository that still holds images cannot be deleted: drain it first,
    # then retry. Both attempts are reported: a repo that survives is a SURVIVOR.
    local repo_url dimgs
    repo_url="$rloc-docker.pkg.dev/$PROJECT/$rname"
    if ! gcloud artifacts repositories delete "$rname" --location "$rloc" --project="$PROJECT" --quiet; then
      log "repository $rname is not empty — draining its images first"
      dimgs="$(gcloud_text "artifacts.images:$rname" gcloud artifacts docker images list "$repo_url" --project="$PROJECT" --format=json)"
      while IFS= read -r img; do
        [ -n "$img" ] || continue
        log "removing image $img from $rname"
        gcloud artifacts docker images delete "$img" --delete-tags --quiet --project="$PROJECT"
      done < <(jq -r '.[] | (.package? // .name? // empty)' <<<"$dimgs")
      if ! gcloud artifacts repositories delete "$rname" --location "$rloc" --project="$PROJECT" --quiet; then
        SURVIVORS+=("artifacts.repository $rname ($rloc) — delete command failed"); continue
      fi
    fi
    if state "artifacts.repository $rname" gcloud artifacts repositories describe "$rname" --location "$rloc" --project="$PROJECT"; then
      SURVIVORS+=("artifacts.repository $rname ($rloc) — still present after delete")
    else
      DELETED+=("artifacts.repository $rname ($rloc)"); log "deleted: artifacts.repository $rname ($rloc)"
    fi
  done < <(jq -r '.[] | [(.name|split("/")|last), (.name|split("/")|.[3])] | @tsv' <<<"$json")

  step "full: GCS buckets and their objects"
  json="$(gcloud_text buckets gcloud storage buckets list --project="$PROJECT" --format=json)"
  local bname count ls_out
  while IFS= read -r bname; do
    if ! in_filter "$bname"; then retain "storage.bucket gs://$bname — outside --name-filter $NAME_FILTER"; continue; fi
    if ! state "storage.bucket gs://$bname" gcloud storage buckets describe "gs://$bname" --project="$PROJECT"; then
      log "absent: storage.bucket gs://$bname"; continue
    fi
    if protected_name "$bname" && freeze_active && [ "$ALLOW_PROTECTED" = 0 ]; then
      RETAINED+=("storage.bucket gs://$bname — PROTECTED demo resource, retention freeze until $PROTECT_UNTIL"); continue
    fi
    count=0
    if ls_out="$(gcloud storage ls --recursive "gs://$bname" 2>&1)"; then
      count="$(printf '%s\n' "$ls_out" | awk 'NF && $0 !~ /:$/ {c++} END{print c+0}')"
    else
      case "$ls_out" in
        *"matched no objects"*) count=0 ;;
        *) err "could not list objects of gs://$bname — refusing to delete blind:"; printf '%s\n' "$ls_out" >&2; exit 1 ;;
      esac
    fi
    if [ "$DRY_RUN" = 1 ]; then would "storage.bucket gs://$bname — would delete together with $count object(s)"; continue; fi
    if ! gcloud storage rm -r "gs://$bname" --quiet; then
      SURVIVORS+=("storage.bucket gs://$bname — delete command failed"); continue
    fi
    if state "storage.bucket gs://$bname" gcloud storage buckets describe "gs://$bname" --project="$PROJECT"; then
      SURVIVORS+=("storage.bucket gs://$bname — still present after delete")
    else
      DELETED+=("storage.bucket gs://$bname (+$count objects)"); log "deleted: storage.bucket gs://$bname"
    fi
  done < <(jq -r '.[] | .name' <<<"$json")

  step "full: log buckets (Google-managed buckets are retained — the API refuses them)"
  json="$(gcloud_text logging.buckets gcloud logging buckets list --project="$PROJECT" --format=json)"
  local lname lloc lstate
  while IFS=$'\t' read -r lname lloc; do
    case "$lname" in
      _Default|_Required)
        retain "logging.bucket $lname ($lloc) — Google-managed; not removable via the API (no billed line item once nothing is ingested)"; continue ;;
    esac
    if ! in_filter "$lname"; then retain "logging.bucket $lname ($lloc) — outside --name-filter $NAME_FILTER"; continue; fi
    if ! state "logging.bucket $lname" gcloud logging buckets describe "$lname" --location "$lloc" --project="$PROJECT"; then
      log "absent: logging.bucket $lname ($lloc)"; continue
    fi
    if [ "$DRY_RUN" = 1 ]; then would "logging.bucket $lname ($lloc) — would delete"; continue; fi
    if ! gcloud logging buckets delete "$lname" --location "$lloc" --project="$PROJECT" --quiet; then
      SURVIVORS+=("logging.bucket $lname ($lloc) — delete command failed"); continue
    fi
    if ! state "logging.bucket $lname" gcloud logging buckets describe "$lname" --location "$lloc" --project="$PROJECT"; then
      DELETED+=("logging.bucket $lname ($lloc)"); log "deleted: logging.bucket $lname ($lloc)"; continue
    fi
    # A log bucket with retained logs moves to DELETE_REQUESTED and is reclaimed
    # when its retention window closes; no API lever removes it sooner, and it no
    # longer accepts logs. Reported as retained-with-reason, NOT as a survivor.
    lstate="$(gcloud_text "logging.bucket:$lname" gcloud logging buckets describe "$lname" --location "$lloc" --project="$PROJECT" --format="value(lifecycleState)")"
    if [ "$lstate" = "DELETE_REQUESTED" ]; then
      RETAINED+=("logging.bucket $lname ($lloc) — DELETE_REQUESTED: delete issued, the Logging API pins it until its retention window closes (it accepts no further logs)")
    else
      SURVIVORS+=("logging.bucket $lname ($lloc) — still present after delete (lifecycleState=$lstate)")
    fi
  done < <(jq -r '.[] | [(.name|split("/")|last), (.location // "global")] | @tsv' <<<"$json")

  if [ "$DELETE_QUOTA_PREFS" = 1 ]; then
    step "full: quota preference records created by us (--delete-quota-preferences)"
    local token pjson pname
    token="$(gcloud auth print-access-token)"
    pjson="$(gcloud_text quotas.preferences gcloud beta quotas preferences list --project="$PROJECT" --format=json)"
    while IFS= read -r pname; do
      [ -n "$pname" ] || continue
      in_filter "${pname##*/}" || { retain "quotas.preference ${pname##*/} — outside --name-filter $NAME_FILTER"; continue; }
      if [ "$DRY_RUN" = 1 ]; then would "quotas.preference ${pname##*/} — would DELETE (not billable)"; continue; fi
      if ! curl -sS -X DELETE -H "Authorization: Bearer $token" \
           "https://cloudquotas.googleapis.com/v1/$pname" >/dev/null; then
        SURVIVORS+=("quotas.preference ${pname##*/} — DELETE request failed"); continue
      fi
      log "deleted: quotas.preference ${pname##*/}"
      DELETED+=("quotas.preference ${pname##*/}")
    done < <(jq -r '.[].name' <<<"$pjson")
  else
    retain "quotas.preference records (project-wide) — NOT billable, and CPUS-ALL-REGIONS holds the granted=12 state the fleet needs; pass --delete-quota-preferences to remove them"
  fi

  if [ "$INCLUDE_NETWORK" = 1 ]; then
    step "full: network (firewall rules, subnet, VPC)"
    local fjson sjson
    fjson="$(gcloud_text firewalls compute firewall-rules list --project="$PROJECT" --format=json)"
    while IFS= read -r fname; do
      resource "compute.firewall-rule" "$fname" "global" compute firewall-rules describe "$fname" --project="$PROJECT"
    done < <(jq -r '.[] | .name' <<<"$fjson")
    sjson="$(gcloud_text subnets compute networks subnets list --project="$PROJECT" --format=json)"
    while IFS=$'\t' read -r sname sregion; do
      resource "compute.subnet" "$sname" "$sregion" compute networks subnets describe "$sname" --region "$sregion" --project="$PROJECT"
    done < <(jq -r '.[] | [(.name), (.region|sub(".*/";""))] | @tsv' <<<"$sjson")
    while IFS= read -r nname; do
      resource "compute.network" "$nname" "global" compute networks describe "$nname" --project="$PROJECT"
    done < <(jq -r '.[] | .name' <<<"$(gcloud_text networks compute networks list --project="$PROJECT" --format=json)")
  else
    retain "VPC / subnet / firewall rules — kept by default (not billable); pass --include-network to remove them"
  fi
}

# ---------------------------------------------------------------------------
# report + post-condition
# ---------------------------------------------------------------------------
report() {
  step "summary"
  local x
  if [ "${#WOULD[@]}" -gt 0 ]; then
    log "DRY RUN — ${#WOULD[@]} action(s) that would be taken:"
    for x in "${WOULD[@]}"; do printf '  WOULD  %s\n' "$x"; done
  fi
  if [ "${#DELETED[@]}" -gt 0 ]; then
    log "${#DELETED[@]} resource(s) confirmed gone:"
    for x in "${DELETED[@]}"; do printf '  GONE   %s\n' "$x"; done
  fi
  if [ "${#RETAINED[@]}" -gt 0 ]; then
    log "${#RETAINED[@]} resource(s) RETAINED with reason:"
    for x in "${RETAINED[@]}"; do printf '  KEEP   %s\n' "$x"; done
  fi
  if [ "${#SURVIVORS[@]}" -gt 0 ]; then
    err "${#SURVIVORS[@]} resource(s) SURVIVED:"
    for x in "${SURVIVORS[@]}"; do printf '  ALIVE  %s\n' "$x" >&2; done
  fi
}

verify_after_full() {
  [ "$DRY_RUN" = 0 ] || { log "dry run: post-check skipped (nothing was changed)"; return 0; }
  step "post-check: enumerating every billable class again"
  local rc=0
  "$VERIFY_TOOL" --project "$PROJECT" --region "$REGION" --zone "$ZONE" --name-filter "$NAME_FILTER" || rc=$?
  case "$rc" in
    0) log "post-check PASS: no billable resource of any enumerated class is present in filter scope" ;;
    1) err "post-check FAIL: billable resources survives --full (see the table above)"; return 2 ;;
    3) warn "post-check: 0 in-remit resources, but billable resources OUTSIDE --full's remit exist (see the table above)"; return 0 ;;
    *) err "post-check could not complete (instrument failure, exit $rc) — the result is UNKNOWN, not clean"; return 1 ;;
  esac
}

main() {
  preflight
  local rc=0
  case "$MODE" in
    pause) do_pause ;;
    full)  do_full ;;
  esac

  if [ "$MODE" = "full" ]; then
    report
    verify_after_full || rc=$?
  else
    report
    if [ "${#SURVIVORS[@]}" -gt 0 ]; then
      err "pause did not reach its post-condition"; rc=2
    else
      log "pause complete — instances down, disks/images/bucket/IP retained for a fast re-raise"
    fi
  fi

  if [ "$REMOVE_INVENTORY" = 1 ] && [ -f "$INVENTORY_PATH" ]; then
    if [ "$DRY_RUN" = 1 ]; then would "local inventory $INVENTORY_PATH — would be removed"
    else log "removing local inventory $INVENTORY_PATH"; rm -f "$INVENTORY_PATH"; fi
  fi

  if [ "$DRY_RUN" = 1 ]; then
    log "dry run complete — nothing was changed"
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    err "$MODE did not complete cleanly (exit $rc)"; exit "$rc"
  fi
  log "$MODE complete"
}

main
