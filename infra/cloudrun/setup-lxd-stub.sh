#!/usr/bin/env bash
# Give the deployed operator a real LXD endpoint to remediate against.
#
# Without this the operator decides correctly and then fails at the last step:
#   remediation_audits.result = 'error'
#   detail = 'URLError: <urlopen error [Errno 111] Connection refused>'
# so the entire decision pipeline works and nothing ever lands. On a real cluster
# the nodes run LXD on 10.10.0.11-13:8443; the repo's own stub records the calls
# so the loop can be demonstrated completing end to end.
#
# NOTE: ship the whole `chaos/` PACKAGE, not just lxd_stub.py. The stub imports
# `from chaos import common`, so a lone copy dies with
#   ModuleNotFoundError: No module named 'chaos'
# and the unit restarts forever without ever binding its port.
set -euo pipefail

# Expects ./chaos/ to have been scp'd to the VM (see runbook).
sudo rm -rf /opt/termini/chaos
sudo cp -r /tmp/chaos /opt/termini/chaos
sudo chown -R ubuntu:ubuntu /opt/termini/chaos
sudo find /opt/termini/chaos -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

cd /opt/termini
PYTHONPATH=/opt/termini python3 -c 'from chaos import lxd_stub' \
  || { echo "chaos package not importable — aborting"; exit 1; }

sudo tee /etc/systemd/system/termini-lxd-stub.service >/dev/null <<'UNIT'
[Unit]
Description=Termini LXD stub (records remediation calls, demo target)
After=network-online.target

[Service]
User=ubuntu
Environment=PYTHONPATH=/opt/termini
WorkingDirectory=/opt/termini
# --log records every remediation call as JSONL, so the demo has evidence of
# what the operator actually asked LXD to do.
ExecStart=/usr/bin/python3 /opt/termini/chaos/lxd_stub.py \
  --host 127.0.0.1 --port 18443 --log /data/lxd-calls.jsonl
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now termini-lxd-stub
sleep 4
echo "stub: $(sudo systemctl is-active termini-lxd-stub)"
curl -sS -o /dev/null -w '  stub reachable: http=%{http_code}\n' \
  http://127.0.0.1:18443/1.0/instances

echo "==> pointing the operator at the stub"
sudo tee /etc/systemd/system/termini-controller.service.d/lxd.conf >/dev/null <<'DROPIN'
[Service]
Environment=TERMINI_LXD_BASE_URL=http://127.0.0.1:18443
DROPIN
sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 6
echo "controller: $(sudo systemctl is-active termini-controller)"
