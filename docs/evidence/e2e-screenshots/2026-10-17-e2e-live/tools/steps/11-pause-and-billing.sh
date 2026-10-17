# CMD: gcloud compute instances list ; gcloud compute disks list ; gcloud compute addresses list ; gcloud compute regions describe asia-south1 (quotas)
# Step 11 — pause and the money. NOT CAPTURED AS THE CARD ASKS, and here is why:
#
#   * the fleet is UP for the concurrently-running client capture (t_270d09b9);
#     terminating it here would destroy that card's run mid-flight;
#   * the owner decided on 2026-10-17 13:12Z to KEEP the demo running (t_f3e25715);
#   * this card's cost guard forbids starting/stopping GCP compute from a worker.
#
# What IS captured is the part that does not need a teardown, and the part the
# card is really after: the list of things that keep billing when the instances
# are terminated. A terminated instance is not a stopped bill (t_9232df6d).
# tools/verify-pause-and-billing.sh runs the same checks after an approved teardown.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 11 — pause and the money   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "STATUS: NOT CAPTURED — the termination, the 000 from the controller URL, and a"
echo "        zero-usage billing view are deferred to an approved teardown. Fleet is UP"
echo "        and shared; see the header of this snippet."

note "what is running right now (the compute being paid for)"
run "$GCLOUD" compute instances list

note "what keeps billing after a terminate — persistent disks (they do not stop billing)"
run "$GCLOUD" compute disks list --format='table(name,sizeGb,status,users)'

note "…and reserved static addresses (an address reserved but unattached bills at the idle rate)"
run "$GCLOUD" compute addresses list

note "…and the project images (storage), which no terminate touches"
run "$GCLOUD" compute images list --no-standard-images --format='table(name,family,status,diskSizeGb,creationTimestamp)'

note "quota view — the same CPUS / GPU-usage check the teardown verification uses"
cmdx "gcloud compute regions describe asia-south1 --format=json | jq '.quotas[] | select(.metric==\"CPUS\" or .metric==\"NVIDIA_L4_GPUS\" or .metric==\"CPUS_ALL_REGIONS\") | {metric,limit,usage}'"
"$GCLOUD" compute regions describe asia-south1 --format=json \
  | jq -M '.quotas[] | select(.metric=="CPUS" or .metric=="NVIDIA_L4_GPUS" or .metric=="CPUS_ALL_REGIONS") | {metric,limit,usage}'

note "the public URL today (it answers while the fleet is up; after teardown it must answer 000)"
cmdx "curl -sS -o /dev/null -w 'GET https://34-93-190-64.sslip.io/ -> HTTP %{http_code} (%{time_total}s)\n' https://34-93-190-64.sslip.io/"
curl -sS -o /dev/null -w 'GET https://34-93-190-64.sslip.io/ -> HTTP %{http_code} (%{time_total}s)\n' https://34-93-190-64.sslip.io/
echo
echo "NOT CAPTURED: instances TERMINATED + controller URL 000 + zero compute usage."
echo "To finish this step in an approved window:  bash tools/verify-pause-and-billing.sh"
echo "(it stops nothing; it verifies the pause and prints every line that still bills)."
