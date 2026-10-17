#!/usr/bin/env bash
# CMD: gcloud compute ssh termini-node-a|termini-node-c --command="journalctl -u termini-bootstrap | grep REGISTERED; systemctl show; uptime -s"
# Step 02 — boot to registration, straight from the bootstrap daemon's own journal.
#
# The REGISTERED line carries register_ms (time inside the register call) and
# boot_to_registered_ms (from boot to a registered identity). Both nodes emit it
# ONCE, on the boot that first registered them; a rebooted node reuses its
# persisted identity and does not re-emit the line. That is captured as it is —
# see the "REGISTERED lines in THIS boot" count of 0 — rather than papered over.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 02 — boot to registration   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

REMOTE="echo '--- every REGISTERED line the journal holds (all boots)'; sudo journalctl -u termini-bootstrap --no-pager | grep REGISTERED | tail -4; echo; echo '--- REGISTERED lines in THIS boot (0 = persisted identity reused)'; sudo journalctl -u termini-bootstrap -b --no-pager | grep -c REGISTERED; echo; echo '--- the daemon that produced it'; systemctl show -p ActiveState -p SubState -p ExecMainStartTimestamp termini-bootstrap; echo; echo '--- this machine booted'; uptime -s; uptime"

note "node-a"
on_node termini-node-a "$REMOTE"

note "node-c"
on_node termini-node-c "$REMOTE"

note "the controller's view of liveness (last_seen_at heartbeat, not a cached row)"
api GET "/projects/$PROJ/nodes" | jq -M '.nodes[] | {node_id,role,synthetic,status,last_seen_at,uptime_sec}'
