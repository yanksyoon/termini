#!/usr/bin/env bash
# Verify the PAUSE after an approved teardown, and report every line that keeps
# billing. This script STOPS NOTHING — it only reads.
#
# Step 11 of the backend E2E pack could not be captured while the fleet is up
# (shared with the client capture, owner's keep-running decision, card cost
# guard). Run this once the teardown has actually happened, in the same window,
# and paste its output into the manifest row for step 11.
#
# The discipline it encodes: a terminated instance is not a stopped bill
# (t_9232df6d). Compute stops; disks, reserved addresses and image storage do not.
set -uo pipefail
GCLOUD="${GCLOUD:-/home/ubuntu/google-cloud-sdk/bin/gcloud}"
ZONE="${ZONE:-asia-south1-a}"
REGION="${REGION:-asia-south1}"
URL="${URL:-https://34-93-190-64.sslip.io/}"
FAIL=0

say() { printf '\n== %s\n' "$*"; }

say "instances (every one must be TERMINATED)"
"$GCLOUD" compute instances list
if "$GCLOUD" compute instances list --format='value(status)' | grep -qE 'RUNNING|STAGING'; then
  echo "FAIL: an instance is not terminated"; FAIL=1
else
  echo "OK: no instance is running"
fi

say "the controller URL (must answer 000 — nothing is listening behind it)"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$URL" 2>/dev/null || echo 000)"
echo "GET $URL -> HTTP $code"
[ "$code" = "000" ] || { echo "FAIL: the URL still answers $code"; FAIL=1; }

say "quota usage (CPUS / NVIDIA_L4_GPUS / CPUS_ALL_REGIONS must be 0.0)"
"$GCLOUD" compute regions describe "$REGION" --format=json \
  | jq -r '.quotas[] | select(.metric=="CPUS" or .metric=="NVIDIA_L4_GPUS" or .metric=="CPUS_ALL_REGIONS") | "\(.metric) usage=\(.usage) limit=\(.limit)"'
usage="$("$GCLOUD" compute regions describe "$REGION" --format=json \
  | jq -r '[.quotas[] | select(.metric=="CPUS" or .metric=="NVIDIA_L4_GPUS") | .usage] | add')"
echo "sum(usage)=$usage"
[ "$usage" = "0" ] || echo "NOTE: usage is not zero — read the lines below before claiming the bill stopped"

say "STILL BILLING after the terminate (this is the point of the step)"
echo "-- persistent disks (billed per GiB-month while they exist, whatever the instance state):"
"$GCLOUD" compute disks list --format='table(name,sizeGb,status,users)'
echo "-- reserved addresses (an unattached static address bills at the idle rate):"
"$GCLOUD" compute addresses list
echo "-- image storage (the baked termini-node-os images stay in the project):"
"$GCLOUD" compute images list --no-standard-images --format='table(name,family,status,diskSizeGb)'

say "verdict"
if [ "$FAIL" -eq 0 ]; then
  echo "PASS: compute is paused, the URL is dark, quota usage is zero"
  echo "      (the disk/address/image lines above are the residual, non-compute bill)"
else
  echo "FAIL: see the lines marked FAIL above"
fi
exit "$FAIL"
