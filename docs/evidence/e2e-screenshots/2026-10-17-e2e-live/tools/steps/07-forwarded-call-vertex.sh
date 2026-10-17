#!/usr/bin/env bash
# CMD: (node-c) journalctl -u termini-inference-proxy | grep inference_request ; ss -tnp dport 443 ; getent hosts aiplatform.googleapis.com ; metadata-server token probe
# Step 07 — the forwarded call itself: where the request actually goes, where the
# credential comes from (the metadata server, not a key file), and what it costs in
# wall clock against the CPU baseline.
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
echo "step 07 — the forwarded call to Vertex AI   ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

note "the proxy's own record of the last forwarded requests (destination, latency, answered_by)"
on_node termini-node-c "sudo journalctl -u termini-inference-proxy --since '-15 min' --no-pager | grep inference_request | tail -3"

note "the destination resolved, and the outbound TLS connection on the wire"
on_node termini-node-c "getent hosts aiplatform.googleapis.com; echo; sudo ss -tnp state established '( dport = :443 )' | head -8"

note "credential source: the metadata server, not a key file"
on_node termini-node-c "echo '--- metadata token endpoint (token itself NOT printed):'; curl -sS -o /tmp/mt.json -w 'http=%{http_code} bytes=%{size_download}\\n' -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'; grep -o '\"expires_in\":[0-9]*' /tmp/mt.json; echo '--- key files on this node:'; sudo ls -l /etc/termini/*.json 2>&1; echo '--- GOOGLE_APPLICATION_CREDENTIALS in the unit:'; grep -c GOOGLE_APPLICATION_CREDENTIALS /etc/systemd/system/termini-inference-proxy.service 2>/dev/null || true"

note "the baseline this is measured against (CPU Gemma, same decision prompt)"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../../" && pwd)"
echo "$REPO/infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md:"
grep -n '402' "$REPO/infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md" | head -5
echo
echo "The 402.1 s figure is the CPU Gemma path answering the REAL 20-sample decision"
echo "prompt (12,748 prompt tokens). The forwarded calls above are the same decision"
echo "prompt shape answered by gemini-2.5-flash at aiplatform.googleapis.com; step 12"
echo "puts a same-prompt measurement of both paths side by side."
