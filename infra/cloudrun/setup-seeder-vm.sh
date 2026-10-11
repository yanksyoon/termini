#!/usr/bin/env bash
# Run the demo seeder on the controller VM as a systemd service.
#
# Why: the seeder was previously started from the operator's own machine, so the
# demo died whenever that process was reaped — the nodes went OFFLINE on the
# dashboard. Hosting it beside the controller makes the deployed stack
# self-contained: nothing outside GCP is needed for the demo to stay live.
#
# The seeder talks to the controller over loopback, so no TLS and no public
# round-trip is involved for its own telemetry.
set -euo pipefail

TOKENS="demo-boot-a3,demo-boot-c3"   # fresh: bootstrap tokens are single-use

sudo mkdir -p /opt/termini/console
sudo cp /tmp/seed_demo.py /opt/termini/console/seed_demo.py
sudo chown -R ubuntu:ubuntu /opt/termini

# Fresh bootstrap tokens + a clean DB, so registration cannot collide with the
# already-consumed tokens from an earlier run.
sudo sed -i "s|^Environment=TERMINI_BOOTSTRAP_TOKEN=.*|Environment=TERMINI_BOOTSTRAP_TOKEN=${TOKENS}|" \
  /etc/systemd/system/termini-controller.service
sudo systemctl daemon-reload
sudo systemctl stop termini-controller
sudo rm -f /data/controller.db /data/controller.db-wal /data/controller.db-shm
sudo systemctl start termini-controller
sleep 6
echo "controller: $(sudo systemctl is-active termini-controller)"

sudo tee /etc/systemd/system/termini-seeder.service >/dev/null <<UNIT
[Unit]
Description=Termini demo seeder (synthetic nodes + telemetry heartbeat)
After=network-online.target termini-controller.service
Wants=network-online.target

[Service]
User=ubuntu
ExecStart=/usr/bin/python3 /opt/termini/console/seed_demo.py \\
  --controller http://127.0.0.1:8080 \\
  --token demo-project-token \\
  --bootstrap ${TOKENS} \\
  --duration 0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now termini-seeder
sleep 20
echo "seeder: $(sudo systemctl is-active termini-seeder)"
sudo journalctl -u termini-seeder --no-pager -n 8 | tail -8
echo "--- nodes seen by the controller ---"
curl -sS -H "Authorization: Bearer demo-project-token" \
  "http://127.0.0.1:8080/v1/projects/01J0DEM0WARM000000000000AB/nodes" | head -c 300
echo
