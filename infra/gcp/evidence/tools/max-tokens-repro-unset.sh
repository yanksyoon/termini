#!/usr/bin/env bash
# max-tokens-repro-unset.sh — remove the t_77f26781 truncation-reproduction drop-in
# (see max-tokens-repro-set.sh), restore the deployed budget (2048) and restart.
set -euo pipefail

DROPIN=/etc/systemd/system/termini-controller.service.d/zz-max-tokens-repro.conf

sudo rm -f "$DROPIN"
sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 8
echo "service: $(systemctl is-active termini-controller) MainPID=$(systemctl show -p MainPID --value termini-controller)"
echo "budget override: $(systemctl show termini-controller -p Environment --value | tr ' ' '\n' | grep MAX_TOKENS || echo 'none (DEFAULT_MAX_TOKENS=2048)')"
