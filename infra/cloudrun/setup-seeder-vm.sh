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
#
# SELF-PROVISIONING (no hand-minted tokens). The seeder registers two synthetic
# nodes through `POST /v1/nodes/register`, which BURNS a single-use bootstrap
# token. This script used to hard-code "freshly minted" tokens into the unit — and
# even then a drop-in could override them (drop-ins load after the unit and the
# last one setting a key wins), so a restart died `401 bootstrap token already
# used` / `401 unknown bootstrap token`. That is a recording landmine: a take could
# fail mid-way unless someone re-minted tokens by hand.
#
# The seeder now mints its own tokens at startup with `--bootstrap-mint 2` from
# `POST /v1/bootstrap-tokens` (project-token auth, loopback-only). A bare
# `sudo systemctl start termini-seeder` therefore just works, and the single-use
# property is untouched for real nodes (registration still burns the token; a
# replay is still `401 bootstrap token already used`).
set -euo pipefail

CONTROLLER_URL="${CONTROLLER_URL:-http://127.0.0.1:8080}"
PROJECT_TOKEN="${PROJECT_TOKEN:-demo-project-token}"
SEEDER_TOKENS="${SEEDER_TOKENS:-2}"

sudo mkdir -p /opt/termini/console
if [ -f /tmp/seed_demo.py ]; then
  sudo cp /tmp/seed_demo.py /opt/termini/console/seed_demo.py
fi
sudo chown -R ubuntu:ubuntu /opt/termini

# The seeder's unit is the single source of truth: remove the whole drop-in
# directory. Drop-ins load after the unit and the last one setting a key wins, so
# a leftover drop-in (a hand-minted `--bootstrap`, or `Restart=no`) silently
# overrides what this script installs.
if [ -d /etc/systemd/system/termini-seeder.service.d ]; then
  sudo cp -r /etc/systemd/system/termini-seeder.service.d \
    "/home/ubuntu/seeder-dropins-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  sudo rm -rf /etc/systemd/system/termini-seeder.service.d
fi

sudo tee /etc/systemd/system/termini-seeder.service >/dev/null <<UNIT
[Unit]
Description=Termini demo seeder (synthetic nodes + telemetry heartbeat)
After=network-online.target termini-controller.service
Wants=network-online.target

[Service]
User=ubuntu
ExecStart=/usr/bin/python3 /opt/termini/console/seed_demo.py \\
  --controller ${CONTROLLER_URL} \\
  --token ${PROJECT_TOKEN} \\
  --bootstrap-mint ${SEEDER_TOKENS} \\
  --duration 0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# NOTE: do NOT add a drop-in that sets ExecStart for this unit. Drop-ins load
# after the unit and the last one setting a key wins — that is how the old
# hand-minted token silently became the one in effect and broke restarts.
sudo systemctl daemon-reload
sudo systemctl enable --now termini-seeder
sleep 10
echo "seeder: $(systemctl is-active termini-seeder)"
sudo journalctl -u termini-seeder --no-pager -n 12 | tail -12
echo "--- nodes seen by the controller ---"
curl -sS -H "Authorization: Bearer ${PROJECT_TOKEN}" \
  "${CONTROLLER_URL}/v1/projects/01J0DEM0WARM000000000000AB/nodes?include=synthetic" \
  | head -c 400
echo
