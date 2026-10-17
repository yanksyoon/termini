# CMD: gcloud compute instances list; gcloud compute disks list --format=table(name,sizeGb,status,sourceImage); gcloud compute instances describe termini-node-a|termini-node-c
# Step 01 — the raise: node-a and node-c are real GCE instances booted from the baked termini-node-os image.
#
# NOT RAISED BY THIS RUN, stated up front: the cost guard on this card forbids
# `gcloud compute instances create|start`. These two instances were raised by the
# approved window (t_ece9d14f) and the fleet has been up since; this step captures
# the fleet listing and the boot-disk provenance that ties each instance to the
# termini-node-os image family. At raise time the command is
# `infra/gcp/tools/raise-standing-fleet.sh --raise` (approved windows only).
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 01 — raise / fleet listing   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "NOTE: this run did NOT create or start any GCP compute. The instances below were"
echo "      raised by the approved window and must stay up for the sibling client capture."

note "fleet listing"
run "$GCLOUD" compute instances list

note "boot-disk provenance — each node's disk comes from the termini-node-os family"
run "$GCLOUD" compute disks list --format='table(name,sizeGb,status,sourceImage)'

note "node-a: status, creationTimestamp, machine type, internal IP"
cmdx "gcloud compute instances describe termini-node-a --format=json | jq '{name,status,creationTimestamp,machineType,internal_ip}'"
"$GCLOUD" compute instances describe termini-node-a --zone="$ZONE" --format=json \
  | jq -M '{name,status,creationTimestamp,machineType:(.machineType|split("/")|last),internal_ip:.networkInterfaces[0].networkIP}'

note "node-c: status, creationTimestamp, machine type, internal IP"
cmdx "gcloud compute instances describe termini-node-c --format=json | jq '{name,status,creationTimestamp,machineType,internal_ip}'"
"$GCLOUD" compute instances describe termini-node-c --zone="$ZONE" --format=json \
  | jq -M '{name,status,creationTimestamp,machineType:(.machineType|split("/")|last),internal_ip:.networkInterfaces[0].networkIP}'

note "the image family both disks were created from"
cmdx "gcloud compute images describe termini-node-os-5 --format=json | jq '{name,family,status,creationTimestamp,diskSizeGb}'"
"$GCLOUD" compute images describe termini-node-os-5 --format=json \
  | jq -M '{name,family,status,creationTimestamp,diskSizeGb}'
