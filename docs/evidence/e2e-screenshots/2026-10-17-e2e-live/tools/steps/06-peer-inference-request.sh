# CMD: (node-a) curl -X POST http://10.10.0.13:8080/v1/chat/completions ; (node-c) journalctl -u termini-inference-proxy
# Step 06 — a real inference request as a PEER: node-a calls node-c's inference front
# door over the underlay, and node-c logs the request arriving and the answer leaving.
# Both sides in one transcript, correlated by the prompt's own marker.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
STAMP="$(date -u +%H%M%SZ)"
echo "step 06 — peer inference request node-a -> node-c   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "marker: e2e-backend-$STAMP"

PAYLOAD='{"model":"gemma","messages":[{"role":"user","content":"One sentence: why restart an instance that exceeds its memory limit? (e2e-backend-'"$STAMP"')"}],"max_tokens":64,"temperature":0}'

note "the request as node-a sends it (peer-to-peer over 10.10.0.0/24)"
q="curl -sS -X POST http://10.10.0.13:8080/v1/chat/completions -H \"Content-Type: application/json\" -d '$PAYLOAD' -w \"\\n--- http_code=%{http_code} total_s=%{time_total}\\n\""
echo "$q" | sed 's/^/$ /'
on_node termini-node-a "$q"

note "the SAME moment on node-c: the request arriving and the answer returning (proxy journal)"
on_node termini-node-c "sudo journalctl -u termini-inference-proxy --since '-3 min' --no-pager | grep inference_request | grep e2e-backend || sudo journalctl -u termini-inference-proxy --since '-3 min' --no-pager | tail -4"

note "node-c's own view of the caller and the connection"
on_node termini-node-c "sudo ss -tn state established '( sport = :8080 )' | head -5"
