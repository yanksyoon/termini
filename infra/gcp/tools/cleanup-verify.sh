#!/usr/bin/env bash
#
# cleanup-verify.sh — the billable-resource enumerator for the Termini GCP
# footprint (project `termini-510609`). It exists because "compute stopped" is
# NOT "clean": a TERMINATED GCE instance keeps billing its disks, its images and
# any static external IP still associated with it. This script answers the only
# question that matters before and after a cleanup: *what is still here, in which
# region, how big is it, and does it bill while idle?*
#
# It is deliberately dual-use:
#   * PRE-CHECK  — run it before `teardown.sh` to see the whole billable surface.
#   * POST-CHECK — run it after `teardown.sh --full`; it exits non-zero if any
#                  resource in a class `--full` claims to remove survives.
#
# Usage:
#   cleanup-verify.sh --project P --region R --zone Z [--name-filter ERE] [--json] [--quiet]
#
#   --project P        GCP project id (required, never defaulted)
#   --region R         primary region of the footprint (required)
#   --zone Z           primary zone of the footprint (required)
#   --name-filter ERE  only report resources whose NAME matches this ERE
#                      (default: ^termini — the whole project footprint)
#   --json             emit the rows as JSON lines instead of a table
#   --quiet            suppress the progress notes on stderr
#
# Exit codes:
#   0  no billable resource found in the classes `--full` removes (in filter scope)
#   1  at least one such resource is still present  -> the cleanup is NOT complete
#   2  an instrument failed (API error / auth / quota): the answer is UNKNOWN,
#      which is never reported as "clean"
#   3  no in-remit resource, but a billable resource OUTSIDE --full's remit was
#      found (Cloud SQL, GKE, Filestore, Redis, Dataproc, Vertex endpoints)
#
# Sizes: `unknown` is printed where no API exposes a size (Cloud Logging buckets
# have no size instrument) — that is an honest "could not verify", never a 0.
set -euo pipefail

PROJECT=""
REGION=""
ZONE=""
NAME_FILTER="^termini"
JSON=0
QUIET=0

log()  { [ "$QUIET" = 1 ] || printf '[cleanup-verify] %s\n' "$*" >&2; }
err()  { printf '[cleanup-verify] ERROR: %s\n' "$*" >&2; }
die()  { err "$*"; exit 2; }

usage() { sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project)     PROJECT="${2:-}"; shift ;;
    --region)      REGION="${2:-}";  shift ;;
    --zone)        ZONE="${2:-}";    shift ;;
    --name-filter) NAME_FILTER="${2:-}"; shift ;;
    --json)        JSON=1 ;;
    --quiet)       QUIET=1 ;;
    -h|--help)     usage; exit 0 ;;
    *)             err "unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

[ -n "$PROJECT" ] || die "--project is required"
[ -n "$REGION" ]  || die "--region is required"
[ -n "$ZONE" ]    || die "--zone is required"
case "$ZONE" in
  "$REGION"-*) : ;;
  *) die "--zone $ZONE does not belong to --region $REGION" ;;
esac
command -v gcloud >/dev/null || die "gcloud not found on PATH"
command -v jq >/dev/null || die "jq not found on PATH"

ROWS="$(mktemp)"
trap 'rm -f "$ROWS"' EXIT

# ---------------------------------------------------------------------------
# row CLASS NAME LOCATION SIZE BILLS_WHILE_IDLE REMIT
#   REMIT: removable -> a class `teardown.sh --full` deletes (drives exit 1)
#          outside   -> billable but outside --full's remit (drives exit 3)
#          optin     -> removed only with an explicit opt-in flag (no billing)
#          info      -> inventory only, not billable
# ---------------------------------------------------------------------------
row() { printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$6" >> "$ROWS"; }

matches() { # $1 = resource name
  [[ "$1" =~ $NAME_FILTER ]]
}

human_bytes() { # $1 = bytes (integer or empty)
  awk -v b="${1:-0}" 'BEGIN{
    if (b == "" ) { print "unknown"; exit }
    split("B KiB MiB GiB TiB", u, " ");
    i = 1
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    printf (i == 1 ? "%.0f %s\n" : "%.2f %s\n"), b, u[i]
  }'
}

# gcloud_json LABEL ARGS... -> JSON array on stdout.
# The label is a free-form instrument name; ARGS are the `gcloud` arguments
# (a leading `gcloud` token may be included or omitted — both are accepted).
# A disabled API is a legitimate "no resources of this class" (not a failure).
# Any other failure aborts with exit 2 — never "absent", never a silent 0.
gcloud_json() {
  local label="$1"; shift
  if [ "${1:-}" = "gcloud" ]; then shift; fi
  local out errfile
  errfile="$(mktemp)"
  # stderr is kept SEPARATE from stdout: gcloud writes human status lines (e.g.
  # artifacts' "Listing items under project …") to stderr, and merging them into
  # the JSON stream made jq fail — a silent-instrument bug in its own right.
  if out="$(gcloud "$@" 2>"$errfile")"; then
    rm -f "$errfile"
    printf '%s' "$out"
    return 0
  fi
  out="$(cat "$errfile")"
  rm -f "$errfile"
  case "$out" in
    *SERVICE_DISABLED*|*"'is not enabled'"*|*"API has not been enabled"*|*"has not been used in project"*|*"is not enabled"*|*"not enabled"*)
      log "skip $label: service not enabled (API disabled) — zero resources of this class"
      printf '%s' '[]'
      return 0 ;;
  esac
  err "instrument FAILED for $label — the inventory is incomplete and cannot be trusted:"
  printf '%s\n' "$out" >&2
  exit 2
}

# ---------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------
enumerate_instances() {
  local json n
  json="$(gcloud_json compute.instances compute instances list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.instances: $n in project"
  jq -r '.[] | [(.name), (.zone|sub(".*/";"")), (.machineType|sub(".*/";"")), (.status)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name zone mtype status; do
    matches "$name" || continue
    local idle
    case "$status" in
      RUNNING) idle="yes (vCPU+RAM per hour)" ;;
      TERMINATED|STOPPED) idle="no (the stopped VM itself is not billed — its disks and any reserved external IP still are)" ;;
      SUSPENDED) idle="no (vCPU+RAM stopped; disks still bill)" ;;
      *) idle="unknown" ;;
    esac
    row "compute.instance" "$name" "$zone" "$mtype ($status)" "$idle" "removable"
  done
}

enumerate_disks() {
  local json n
  json="$(gcloud_json compute.disks compute disks list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.disks: $n in project"
  jq -r '.[] | [(.name), (.zone|sub(".*/";"")), (.sizeGb|tostring), (.type|sub(".*/";"")), ((.users // [])|length|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name zone size dtype users; do
    matches "$name" || continue
    row "compute.disk" "$name" "$zone" "${size} GiB ${dtype} (${users} user(s))" "yes (attached or not)" "removable"
  done
}

enumerate_images() {
  local json n
  json="$(gcloud_json compute.images compute images list --project="$PROJECT" --no-standard-images --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.images: $n in project"
  jq -r '.[] | [(.name), (.family // "-"), (.diskSizeGb|tostring), ((.archiveSizeBytes // 0)|tostring), .status] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name family disk_gb arch status; do
    matches "$name" || continue
    row "compute.image" "$name" "global" "${disk_gb} GiB image (archive $(human_bytes "$arch")), family=${family}, ${status}" "yes (image storage)" "removable"
  done
  # The family pointer is a separate, silent failure mode: the image can be gone
  # while `--image-family termini-node-os` still resolves.
  local fam
  if fam="$(gcloud compute images list --project="$PROJECT" --no-standard-images --format="value(family)" 2>&1)"; then
    log "image families present: $(printf '%s' "$fam" | sort -u | tr '\n' ' ')"
  else
    err "instrument FAILED for image families — inventory incomplete"
    printf '%s\n' "$fam" >&2
    exit 2
  fi
}

enumerate_snapshots() {
  local json n
  json="$(gcloud_json compute.snapshots compute snapshots list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.snapshots: $n in project"
  jq -r '.[] | [(.name), (.diskSizeGb|tostring), (.status // "-"), ((.storageBytes // 0)|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name size status sbytes; do
    matches "$name" || continue
    row "compute.snapshot" "$name" "global" "${size} GiB source disk (stored $(human_bytes "$sbytes")), ${status}" "yes (snapshot storage)" "removable"
  done
}

address_row() { # $1=name $2=scope $3=addressType $4=address $5=status $6=users-json
  local name="$1" scope="$2" atype="$3" addr="$4" status="$5" users="$6"
  matches "$name" || return 0
  local idle
  if [ "$atype" = "EXTERNAL" ]; then
    idle="yes (static external IP: billed whether or not the instance runs)"
  else
    idle="no (internal RFC1918 address)"
  fi
  row "compute.address" "$name" "$scope" "$atype $addr (${status}, ${users} user(s))" "$idle" "removable"
}

enumerate_addresses() {
  local json n
  json="$(gcloud_json compute.addresses.regional compute addresses list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.addresses (regional): $n in project"
  jq -r '.[] | [(.name), (.region|sub(".*/";"")), (.addressType // "EXTERNAL"), (.address // "-"), (.status // "-"), ((.users // [])|length|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name region atype addr status users; do
    address_row "$name" "$region" "$atype" "$addr" "$status" "$users"
  done

  json="$(gcloud_json compute.addresses.global compute addresses list --project="$PROJECT" --global --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.addresses (global): $n in project"
  jq -r '.[] | [(.name), (.addressType // "EXTERNAL"), (.address // "-"), (.status // "-"), ((.users // [])|length|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name atype addr status users; do
    address_row "$name" "global" "$atype" "$addr" "$status" "$users"
  done
}

enumerate_forwarding_rules() {
  local json n
  json="$(gcloud_json compute.forwarding-rules.regional compute forwarding-rules list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.forwarding-rules (regional): $n in project"
  jq -r '.[] | [(.name), (.region|sub(".*/";"")), (.IPAddress // "-"), (.target // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name region ip target; do
    matches "$name" || continue
    row "compute.forwarding-rule" "$name" "$region" "$ip -> ${target##*/}" "yes (in-use load balancer)" "removable"
  done

  json="$(gcloud_json compute.forwarding-rules.global compute forwarding-rules list --project="$PROJECT" --global --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "compute.forwarding-rules (global): $n in project"
  jq -r '.[] | [(.name), (.IPAddress // "-"), (.target // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name ip target; do
    matches "$name" || continue
    row "compute.forwarding-rule" "$name" "global" "$ip -> ${target##*/}" "yes (in-use load balancer)" "removable"
  done
}

enumerate_machine_images_templates_reservations() {
  local json n
  json="$(gcloud_json compute.machine-images compute machine-images list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "compute.machine-images: $n in project"
  jq -r '.[] | [(.name), (.status // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name status; do
    matches "$name" || continue
    row "compute.machine-image" "$name" "global" "$status (stored copies of all disks)" "yes (storage per disk copy)" "removable"
  done

  json="$(gcloud_json compute.instance-templates compute instance-templates list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "compute.instance-templates: $n in project"
  jq -r '.[] | [(.name), (.properties.machineType // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name mtype; do
    matches "$name" || continue
    row "compute.instance-template" "$name" "global" "${mtype##*/}" "no (metadata only)" "removable"
  done

  json="$(gcloud_json compute.reservations compute reservations list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "compute.reservations: $n in project"
  jq -r '.[] | [(.name), (.zone|sub(".*/";"")), (.specificReservation.count|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name zone count; do
    matches "$name" || continue
    row "compute.reservation" "$name" "$zone" "$count reserved instance(s)" "yes (reservation is billed like the instances)" "removable"
  done
}

# ---------------------------------------------------------------------------
# storage / serverless / registry
# ---------------------------------------------------------------------------
enumerate_buckets() {
  local json n
  json="$(gcloud_json storage.buckets gcloud storage buckets list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "storage.buckets: $n in project"
  local name location bytes objects size ls_out
  while IFS=$'\t' read -r name location; do
    matches "$name" || continue
    if ! size="$(gcloud storage du -s "gs://$name" 2>&1)"; then
      err "instrument FAILED for gs://$name (size) — inventory incomplete"
      printf '%s\n' "$size" >&2
      exit 2
    fi
    bytes="$(printf '%s' "$size" | awk '{print $1}')"
    if ! ls_out="$(gcloud storage ls --recursive "gs://$name" 2>&1)"; then
      case "$ls_out" in
        *"matched no objects"*) objects=0 ;;
        *) err "instrument FAILED for gs://$name (object list) — inventory incomplete"
           printf '%s\n' "$ls_out" >&2
           exit 2 ;;
      esac
    else
      objects="$(printf '%s\n' "$ls_out" | awk 'NF && $0 !~ /:$/ {c++} END{print c+0}')"
    fi
    row "storage.bucket" "gs://$name" "$location" "$(human_bytes "$bytes") in $objects object(s)" "yes (object storage)" "removable"
  done < <(jq -r '.[] | [(.name), (.location // "-")] | @tsv' <<<"$json")
}

enumerate_run() {
  local json n
  json="$(gcloud_json run.services gcloud run services list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "run.services: $n in project"
  local name region minscale
  while IFS=$'\t' read -r name region minscale; do
    matches "$name" || continue
    row "run.service" "$name" "$region" "min-instances=${minscale} (scales to zero)" "no (per-request only when min-instances=0)" "removable"
    local rjson rn
    rjson="$(gcloud_json "run.revisions:$name" gcloud run revisions list --project="$PROJECT" --region="$region" --service="$name" --format=json)"
    rn="$(jq -r 'length' <<<"$rjson")"
    log "  run.revisions[$name]: $rn"
    jq -r '.[] | (.metadata.name)' <<<"$rjson" |
    while read -r rev; do
      row "run.revision" "$rev" "$region" "revision of $name (container in Artifact Registry)" "no (revisions are free; the container image is not)" "removable"
    done
  done < <(jq -r '.[] | [(.metadata.name), (.metadata.labels["cloud.googleapis.com/location"] // "-"), (.spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // .metadata.annotations["run.googleapis.com/minScale"] // "0")] | @tsv' <<<"$json")
}

enumerate_artifacts() {
  local json n
  json="$(gcloud_json artifacts.repositories gcloud artifacts repositories list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "artifacts.repositories: $n in project"
  local name location
  while IFS=$'\t' read -r name location; do
    matches "$name" || continue
    local desc size
    desc="$(gcloud artifacts repositories describe "$name" --location="$location" --project="$PROJECT" 2>&1)" || {
      err "instrument FAILED for artifact repo $name — inventory incomplete"; printf '%s\n' "$desc" >&2; exit 2
    }
    size="$(printf '%s\n' "$desc" | sed -n 's/^Repository Size: *//p' | head -1)"
    [ -n "$size" ] || size="unknown"
    row "artifacts.repository" "$name" "$location" "Docker repo, size ${size:-unknown}" "yes if it holds layers (>0 B)" "removable"
  done < <(jq -r '.[] | [(.name|split("/")|last), (.name|split("/")|.[3])] | @tsv' <<<"$json")
}

enumerate_logging_buckets() {
  local json n
  json="$(gcloud_json logging.buckets gcloud logging buckets list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"
  log "logging.buckets: $n in project"
  jq -r '.[] | [(.name|split("/")|last), (.location // "-"), ((.retentionDays // 0)|tostring), ((.locked // false)|tostring), (.lifecycleState // "ACTIVE")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name location retention locked lstate; do
    matches "$name" || continue
    case "$name" in
      _Default|_Required)
        row "logging.bucket" "$name" "$location" "${retention}d retention, locked=$locked, size=unknown" "yes beyond the 50 GiB free tier; NO size instrument exists" "info"
        ;;
      *)
        if [ "$lstate" = "DELETE_REQUESTED" ]; then
          # The Logging API pins a bucket with retained logs: DELETE moves it to
          # DELETE_REQUESTED and it disappears when the retention window closes.
          # Reported, not counted as a survivor — no API lever removes it sooner.
          row "logging.bucket" "$name" "$location" "${retention}d retention, size=unknown, lifecycle=DELETE_REQUESTED" "no further logs are accepted; the API pins it until the retention window closes" "pending"
        else
          row "logging.bucket" "$name" "$location" "${retention}d retention, locked=$locked, size=unknown" "yes (log storage beyond the free tier)" "removable"
        fi
        ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# billable classes OUTSIDE teardown --full's remit — reported so they can never
# hide, but they do not make --full "incomplete" (exit 3, not 1).
# ---------------------------------------------------------------------------
enumerate_out_of_remit() {
  local json n
  json="$(gcloud_json container.clusters gcloud container clusters list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "container.clusters: $n in project"
  jq -r '.[] | [(.name), (.location // "-"), (.currentNodeCount|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name location nodes; do
    matches "$name" || continue
    row "container.cluster" "$name" "$location" "$nodes node(s)" "yes (nodes bill like GCE VMs)" "outside"
  done

  json="$(gcloud_json sql.instances gcloud sql instances list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "sql.instances: $n in project"
  jq -r '.[] | [(.name), (.region // "-"), (.tier // "-"), (.state // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name region tier state; do
    matches "$name" || continue
    row "sql.instance" "$name" "$region" "$tier ($state)" "yes (instance + storage)" "outside"
  done

  json="$(gcloud_json filestore.instances gcloud filestore instances list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "filestore.instances: $n in project"
  jq -r '.[] | [(.name|split("/")|last), (.fileShares[0].capacityGb|tostring), (.tier // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name size tier; do
    matches "$name" || continue
    row "filestore.instance" "$name" "$REGION" "${size} GiB ($tier)" "yes (provisioned capacity)" "outside"
  done

  json="$(gcloud_json redis.instances gcloud redis instances list --project="$PROJECT" --region="$REGION" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "redis.instances: $n in $REGION"
  jq -r '.[] | [(.name|split("/")|last), (.memorySizeGb|tostring), (.tier // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name size tier; do
    matches "$name" || continue
    row "redis.instance" "$name" "$REGION" "${size} GiB ($tier)" "yes (provisioned memory)" "outside"
  done

  json="$(gcloud_json dataproc.clusters gcloud dataproc clusters list --project="$PROJECT" --region="$REGION" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "dataproc.clusters: $n in $REGION"
  jq -r '.[] | [(.clusterName), (.status.state // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name state; do
    matches "$name" || continue
    row "dataproc.cluster" "$name" "$REGION" "$state" "yes (cluster VMs)" "outside"
  done

  json="$(gcloud_json aiplatform.endpoints gcloud ai endpoints list --project="$PROJECT" --region="$REGION" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "aiplatform.endpoints: $n in $REGION"
  jq -r '.[] | [(.displayName // "-"), (.name|split("/")|last)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r display id; do
    matches "$display" || continue
    row "aiplatform.endpoint" "$display" "$REGION" "endpoint $id (deployed model)" "yes (deployed model replicas)" "outside"
  done
}

# ---------------------------------------------------------------------------
# non-billable inventory — printed so "we forgot about it" is impossible.
# ---------------------------------------------------------------------------
enumerate_inventory_only() {
  local json n name
  json="$(gcloud_json quotas.preferences gcloud beta quotas preferences list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "quotas.preferences: $n in project"
  jq -r '.[] | [(.name|split("/")|last), (.quotaId // "-"), (.quotaConfig.grantedValue // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name qid granted; do
    matches "$name" || continue
    row "quotas.preference" "$name" "global" "$qid granted=$granted (request record)" "no (not a billed resource; removed only with teardown --delete-quota-preferences)" "optin"
  done

  json="$(gcloud_json iam.service-accounts gcloud iam service-accounts list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "iam.service-accounts: $n in project"
  jq -r '.[] | [(.email), (.disabled|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r email disabled; do
    matches "$email" || continue
    row "iam.service-account" "$email" "global" "disabled=$disabled" "no" "info"
  done

  json="$(gcloud_json logging.sinks gcloud logging sinks list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "logging.sinks: $n in project"
  jq -r '.[] | [(.name), (.destination // "-")] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name dest; do
    matches "$name" || continue
    row "logging.sink" "$name" "global" "-> $dest" "no" "info"
  done

  json="$(gcloud_json compute.networks compute networks list --project="$PROJECT" --format=json)"
  n="$(jq -r 'length' <<<"$json")"; log "compute.networks: $n in project"
  jq -r '.[] | [(.name), (.autoCreateSubnetworks|tostring)] | @tsv' <<<"$json" |
  while IFS=$'\t' read -r name auto; do
    matches "$name" || continue
    row "compute.network" "$name" "global" "auto-subnets=$auto" "no (VPCs and firewall rules are free)" "info"
  done
}

# ---------------------------------------------------------------------------
# run it
# ---------------------------------------------------------------------------
log "project=$PROJECT region=$REGION zone=$ZONE filter=$NAME_FILTER"

enumerate_instances
enumerate_disks
enumerate_images
enumerate_snapshots
enumerate_addresses
enumerate_forwarding_rules
enumerate_machine_images_templates_reservations
enumerate_buckets
enumerate_run
enumerate_artifacts
enumerate_logging_buckets
enumerate_out_of_remit
enumerate_inventory_only

in_remit="$(awk -F'\t' '$6=="removable"{c++} END{print c+0}' "$ROWS")"
outside="$(awk -F'\t' '$6=="outside"{c++} END{print c+0}' "$ROWS")"
optin="$(awk -F'\t' '$6=="optin"{c++} END{print c+0}' "$ROWS")"
info="$(awk -F'\t' '$6=="info"{c++} END{print c+0}' "$ROWS")"
pending="$(awk -F'\t' '$6=="pending"{c++} END{print c+0}' "$ROWS")"

if [ "$JSON" = 1 ]; then
  awk -F'\t' 'NF{printf "{\"class\":\"%s\",\"name\":\"%s\",\"location\":\"%s\",\"size\":\"%s\",\"idle_billing\":\"%s\",\"remit\":\"%s\"}\n",$1,$2,$3,$4,$5,$6}' "$ROWS"
else
  printf '\n'
  printf '%-24s %-30s %-16s %-58s %-58s %s\n' CLASS NAME LOCATION SIZE BILLS_WHILE_IDLE REMIT
  printf '%-24s %-30s %-16s %-58s %-58s %s\n' "------------------------" "------------------------------" "----------------" "----------------------------------------------------------" "----------------------------------------------------------" "---------"
  if [ "$in_remit" -eq 0 ] && [ "$outside" -eq 0 ] && [ "$optin" -eq 0 ] && [ "$info" -eq 0 ] && [ "$pending" -eq 0 ]; then
    printf '(no resources of any enumerated class match filter %s)\n' "$NAME_FILTER"
  else
    awk -F'\t' 'NF{printf "%-24s %-30s %-16s %-58s %-58s %s\n",$1,$2,$3,$4,$5,$6}' "$ROWS"
  fi
  printf '\n'
  printf 'in-remit billable resources (classes teardown --full removes) : %s\n' "$in_remit"
  printf 'billable resources OUTSIDE --full remit                       : %s\n' "$outside"
  printf 'pending deletion (API pins until the retention window closes)  : %s\n' "$pending"
  printf 'opt-in only (not billable, needs an explicit flag)            : %s\n' "$optin"
  printf 'non-billable inventory                                        : %s\n' "$info"
  if [ "$in_remit" -ne 0 ]; then
    printf 'VERDICT: FAIL — %s billable resource(s) `teardown.sh --full` must remove are still present\n' "$in_remit"
  elif [ "$outside" -ne 0 ]; then
    printf 'VERDICT: FAIL-PARTIAL — 0 in-remit resources, but %s billable resource(s) outside --full remit are present\n' "$outside"
  else
    printf 'VERDICT: PASS — no removable billable resource of any enumerated class is present in filter scope\n'
    if [ "$pending" -ne 0 ]; then
      printf '         (%s deletion(s) pending API-side finalization — see the table above)\n' "$pending"
    fi
  fi
fi

if [ "$in_remit" -ne 0 ]; then
  exit 1
fi
if [ "$outside" -ne 0 ]; then
  exit 3
fi
exit 0
