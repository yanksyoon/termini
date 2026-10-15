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
# The REAL LXD instance the synthetic nodes advertise in `lxd_instances[]`.
#
# The seeder's guardrail-resolvable target used to be an INVENTED id
# (`01M4N827979BD9D2762EFE24A0`), so the model's remediation was resolved to a
# vm_id that exists nowhere and the executor's real LXD call came back
# `HTTP 404 ... Instance not found` — on camera, indistinguishable from the AI
# failing (measured live 2026-10-15T15:29:48Z). It now advertises the
# controller-issued vm_id of an instance that EXISTS on the node the executor
# dials (`--lxd-instance`), so the action LANDS. The default keeps the seeder in
# step with the controller's own fallback instance (`TERMINI_LXD_DEFAULT_INSTANCE_NAME`,
# the same instance the operator names when the model does not), so the demo is
# solvable by construction rather than by a per-take flag.
SEEDER_LXD_INSTANCE="${SEEDER_LXD_INSTANCE:-${TERMINI_LXD_DEFAULT_INSTANCE_NAME:-demo-workload}}"

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
  --lxd-instance ${SEEDER_LXD_INSTANCE} \\
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
# …then RESTART, always. `enable --now` is a no-op on a unit that is already
# active, so re-running this script after changing anything above (a new
# `--lxd-instance`, a new controller URL) left the OLD argv running and the change
# silently not in effect — the seeder kept advertising the previous target while
# the unit file on disk said otherwise (measured on termini-controller,
# 2026-10-15: `--lxd-instance demo-workload` was written and `systemctl is-active`
# stayed `active` with the pre-change command line). A restart is what makes the
# unit the single source of truth it claims to be.
sudo systemctl restart termini-seeder
sleep 10
echo "seeder: $(systemctl is-active termini-seeder)"
echo "seeder argv: $(ps -eo args | grep '[s]eed_demo.py')"
sudo journalctl -u termini-seeder --no-pager -n 12 | tail -12
echo "--- nodes seen by the controller ---"
curl -sS -H "Authorization: Bearer ${PROJECT_TOKEN}" \
  "${CONTROLLER_URL}/v1/projects/01J0DEM0WARM000000000000AB/nodes?include=synthetic" \
  | head -c 400
echo
