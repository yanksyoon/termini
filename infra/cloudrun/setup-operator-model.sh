#!/usr/bin/env bash
# Put the Termini operator into model mode, deciding via Gemma on Cloud Run GPU.
#
# Two services:
#   termini-inference-proxy  - loopback proxy that mints a fresh OIDC identity
#                              token per request, so the authenticated Cloud Run
#                              service works without a static (expiring) secret.
#   termini-controller       - now with TERMINI_OPERATOR_ENABLED=true and
#                              TERMINI_OPERATOR_DECISION_MODE=model, pointed at
#                              the proxy.
set -euo pipefail

sudo cp /tmp/inference-proxy.py /opt/termini/inference-proxy.py
sudo chown ubuntu:ubuntu /opt/termini/inference-proxy.py

sudo tee /etc/systemd/system/termini-inference-proxy.service >/dev/null <<'UNIT'
[Unit]
Description=Termini inference proxy (fresh OIDC token for Cloud Run Gemma)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
ExecStart=/usr/bin/python3 /opt/termini/inference-proxy.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now termini-inference-proxy
sleep 3
echo "proxy state: $(sudo systemctl is-active termini-inference-proxy)"

# Prove the proxy works before touching the controller.
echo "--- proxy round-trip ---"
curl -sS -m 60 -o /tmp/models.json -w "  /v1/models http=%{http_code}\n" http://127.0.0.1:8090/v1/models || true
head -c 160 /tmp/models.json 2>/dev/null; echo

# Flip the operator to model mode.
echo "--- enabling model-driven decisions on the controller ---"
sudo mkdir -p /etc/systemd/system/termini-controller.service.d
sudo tee /etc/systemd/system/termini-controller.service.d/model-mode.conf >/dev/null <<'DROPIN'
[Service]
Environment=TERMINI_OPERATOR_ENABLED=true
Environment=TERMINI_OPERATOR_DECISION_MODE=model
Environment=TERMINI_INFERENCE_BASE_URL=http://127.0.0.1:8090
Environment=TERMINI_INFERENCE_MODEL=gemma
Environment=TERMINI_INFERENCE_TIMEOUT_S=120
Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=120
Environment=TERMINI_OPERATOR_POLL_INTERVAL_S=2
DROPIN

sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 5
echo "controller state: $(sudo systemctl is-active termini-controller)"
sudo journalctl -u termini-controller --no-pager -n 8 | tail -8
