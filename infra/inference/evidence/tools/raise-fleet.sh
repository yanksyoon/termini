#!/usr/bin/env bash
# raise-fleet.sh — bring the three standing Termini instances back UP (start, do
# NOT recreate: the front-door install and the controller tree live on the disks),
# with the asia-south1-a n2 capacity workaround this project has used twice.
set -uo pipefail
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
PROJECT=termini-510609; ZONE=asia-south1-a
OUT=/home/ubuntu/.hermes/kanban/boards/termini/workspaces/t_f466824c
say() { printf '\n########## %s ##########\n' "$*"; }
FMT="table(name,status,machineType.basename(),networkInterfaces[0].networkIP,networkInterfaces[0].accessConfigs[0].natIP)"

say "0. before"
gcloud compute instances list --project "$PROJECT" --format="$FMT" | tee "$OUT/live-raise-before.txt"

say "1. start (fall back to the e2 equivalent if the zone is out of n2)"
for spec in termini-controller:n2-standard-2:e2-standard-2 \
            termini-node-a:n2-standard-2:e2-standard-2 \
            termini-node-c:n2-standard-8:e2-standard-8; do
  inst="${spec%%:*}"; rest="${spec#*:}"; want="${rest%%:*}"; alt="${rest##*:}"
  if gcloud compute instances start "$inst" --zone="$ZONE" --project "$PROJECT" 2>&1 | tail -2; then
    echo "$inst: start OK (machine type $want)"
    continue
  fi
  echo "$inst: start FAILED on $want -> temporarily $alt (same vCPU/RAM), restored at teardown"
  gcloud compute instances stop "$inst" --zone="$ZONE" --project "$PROJECT" 2>&1 | tail -1
  gcloud compute instances set-machine-type "$inst" --zone="$ZONE" --project "$PROJECT" \
    --machine-type="$alt" 2>&1 | tail -1
  gcloud compute instances start "$inst" --zone="$ZONE" --project "$PROJECT" 2>&1 | tail -2
done

say "2. temporary external IPs for SSH on node-a/node-c (SSH only; removed at teardown)"
for inst in termini-node-a termini-node-c; do
  gcloud compute instances add-access-config "$inst" --zone="$ZONE" --project "$PROJECT" 2>&1 | tail -1
done

say "3. after"
gcloud compute instances list --project "$PROJECT" --format="$FMT" | tee "$OUT/live-raise-after.txt"

say "4. ssh readiness"
for inst in termini-controller termini-node-a termini-node-c; do
  for _ in $(seq 1 20); do
    if gcloud compute ssh "$inst" --zone="$ZONE" --project="$PROJECT" --quiet \
         --ssh-flag="-o StrictHostKeyChecking=accept-new" --ssh-flag="-o ConnectTimeout=10" \
         --command "hostname; systemctl is-system-running || true" 2>&1 | tail -2; then
      break
    fi
    sleep 10
  done
done
echo "RAISE-FLEET DONE"
