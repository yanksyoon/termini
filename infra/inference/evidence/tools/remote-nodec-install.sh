#!/usr/bin/env bash
# remote-nodec-install.sh — runs ON termini-node-c: install the CURRENT branch's
# front door (proxy.py + unit + switch), release :8080 to the proxy, and report
# the active backend with raw output.
set -uo pipefail
echo "host: $(hostname)  ts: $(date -u +%FT%TZ)"
echo "--- the log-sink fix is in the installed file (grep proof)"
grep -n "self.stream if self.stream is not None" /tmp/proxy.py || echo "MISSING FIX IN /tmp/proxy.py"
sudo install -m 0755 /tmp/proxy.py /opt/termini/inference/proxy.py
grep -n "self.stream if self.stream is not None" /opt/termini/inference/proxy.py
sudo bash /tmp/install-inference-proxy.sh --backend gemini-proxy
sudo install -m 0755 /tmp/termini-inference-switch.sh /usr/local/sbin/termini-inference-switch
sudo install -m 0755 /tmp/verify-proxy.sh /usr/local/bin/termini-verify-proxy
echo "--- waiting for the local worker on loopback :8081 (llama.cpp returns 503 while loading)"
for i in $(seq 1 90); do
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/health || true)
  if [ "$code" = "200" ]; then echo "worker ready after ${i}s"; break; fi
  sleep 1
done
echo "--- units"
sudo systemctl is-active termini-inference-proxy.service termini-llama-server.service termini-llama-intent.service || true
echo "--- listeners"
sudo ss -ltnp | grep -E ':8080|:8081' || true
echo "--- active backend: /run/termini/inference-backend.json"
sudo cat /run/termini/inference-backend.json
echo "--- GET /health (what nodes A/B see)"
curl -s -m 10 http://127.0.0.1:8080/health; echo
echo "--- GET /v1/models"
curl -s -m 10 http://127.0.0.1:8080/v1/models; echo
