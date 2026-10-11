#!/usr/bin/env bash
# Install the Termini controller on a GCE VM as a systemd service.
# The controller has zero third-party runtime deps (pure stdlib), so no
# container and no pip install are needed — just the code and a persistent
# /data dir for the SQLite database.
set -euo pipefail

SRC=/tmp/termini-src
APP=/opt/termini

sudo mkdir -p "$APP" /data
sudo cp -r "$SRC/controller" "$SRC/termini" "$APP/"
sudo chown -R ubuntu:ubuntu "$APP"
# The controller's SQLite DB lives here — a real disk, not an ephemeral
# container filesystem. This is the whole reason the controller is not on
# Cloud Run (see issue #96).
sudo chmod 775 /data

sudo tee /etc/systemd/system/termini-controller.service >/dev/null <<'UNIT'
[Unit]
Description=Termini controller (control plane)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/opt/termini/controller
Environment=PYTHONPATH=/opt/termini/controller:/opt/termini
Environment=TERMINI_HOST=127.0.0.1
Environment=TERMINI_PORT=8080
Environment=TERMINI_DB_PATH=/data/controller.db
Environment=TERMINI_PROJECT_ID=01J0DEM0WARM000000000000AB
Environment=TERMINI_PROJECT_TOKEN=demo-project-token
Environment=TERMINI_BOOTSTRAP_TOKEN=demo-boot-a,demo-boot-c
Environment=TERMINI_MIN_REDACTOR_VERSION=3
Environment=TERMINI_OPERATOR_ENABLED=false
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m app.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now termini-controller
sleep 4
echo "service state: $(sudo systemctl is-active termini-controller)"
curl -sS -o /dev/null -w 'healthz http=%{http_code}\n' http://127.0.0.1:8080/healthz || true
echo "--- last log lines ---"
sudo journalctl -u termini-controller --no-pager -n 12 | tail -12
