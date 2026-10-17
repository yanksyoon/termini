#!/usr/bin/env bash
# CMD: gcloud compute ssh termini-node-c --command="systemctl status termini-inference-proxy; systemctl show -p Environment; curl :8080/health; curl :8080/v1/models; ls /dev/nvidia*"
# Step 05 — the inference service on node C: it is up, it advertises the alias `gemma`,
# and it says in-band which backend answered (forwarded managed model — not local
# weights, not a GPU).
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 05 — the inference service on node C   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

note "the unit that serves :8080"
on_node termini-node-c "sudo systemctl status termini-inference-proxy --no-pager -l | head -14"

note "what it is configured as (Environment=) and that it is listening"
on_node termini-node-c "systemctl show -p ActiveState -p SubState -p MainPID -p Environment termini-inference-proxy; echo; sudo ss -ltnp | grep -E ':8080|:8081'"

note "/health — the backend it reports"
on_node termini-node-c "curl -sS -i http://127.0.0.1:8080/health | head -20"

note "/v1/models — the alias the dashboard and the fleet's peers use"
on_node termini-node-c "curl -sS http://127.0.0.1:8080/v1/models"

note "there is no GPU on this node — the forwarded path is the whole point of the pivot"
on_node termini-node-c "ls -l /dev/nvidia* 2>&1 | head -3; command -v nvidia-smi || echo 'nvidia-smi absent: no local GPU'"
