# CMD: curl -sS -H "Authorization: Bearer <project-token>" https://34-93-190-64.sslip.io/v1/projects/<pid>/nodes
# Step 03 — the controller's own view of the fleet (the API the dashboard consumes).
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 03 — the controller's fleet view   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

note "GET /v1/projects/$PROJ/nodes — both nodes, as the controller sees them"
api GET "/projects/$PROJ/nodes" > /tmp/termini-e2e-backend/nodes.json
jq -M '{nodes: [.nodes[] | {node_id, role, synthetic, status, last_seen_at, uptime_sec,
        hardware: {cpu_model: .hardware.cpu.model, cores: .hardware.cpu.cores, gpus: .hardware.gpus},
        capacity: {cpu_free: .capacity.cpu_free, ram_free_gb: .capacity.ram_free_gb},
        services: [.health.services[].name],
        degraded_reasons: .health.degraded_reasons}]}' /tmp/termini-e2e-backend/nodes.json

note "role as REPORTED (the honest state of the pivot, not the card's expectation)"
jq -Mr '.nodes[] | "\(.node_id)  role=\(.role)  synthetic=\(.synthetic)  status=\(.status)  last_seen=\(.last_seen_at)"' /tmp/termini-e2e-backend/nodes.json
echo
echo "Both standing nodes report role=compute: the frozen role rule assigns"
echo "role=inference only to a node reporting a GPU with >= 12 GiB VRAM, and this"
echo "fleet is CPU-only after the managed-model pivot. Node-c still SERVES inference —"
echo "as the host_service that fronts 10.10.0.13:8080 (step 05), not as an"
echo "inference-role node. Recorded as a finding against this card's step 03 wording."
