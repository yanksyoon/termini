#!/usr/bin/env bash
# max-tokens-repro-set.sh — force the operator-decision completion budget down so a
# live fire TRUNCATES, for the t_77f26781 raw-capture evidence.
#
# Why a smaller value than the pre-fix 512: the reply only truncates when it needs
# more tokens than the cap, and a live reply is usually ~460–500 completion tokens,
# so 512 reproduces the failure only on the longer incidents (the card saw it on
# "roughly half" of fires). 200 makes it deterministic for the capture. The shape
# the parser then sees — a valid action_id, cut mid-JSON — is the same either way.
#
#     bash max-tokens-repro-set.sh [tokens]     # default 200
#     bash max-tokens-repro-unset.sh            # remove it again
#
# It writes ONE drop-in, reloads systemd and restarts termini-controller (the
# script does the sudo itself, so run it as the ssh user, not under sudo).
set -euo pipefail

TOKENS="${1:-200}"
DROPIN=/etc/systemd/system/termini-controller.service.d/zz-max-tokens-repro.conf

sudo mkdir -p "$(dirname "$DROPIN")"
sudo tee "$DROPIN" >/dev/null <<EOF
[Service]
# t_77f26781 reproduction only: force a completion budget small enough that the
# operator-decision v2 reply (decision + incident record) is cut mid-JSON.
# Remove with max-tokens-repro-unset.sh.
Environment=TERMINI_INFERENCE_MAX_TOKENS=$TOKENS
EOF
sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 8
echo "service: $(systemctl is-active termini-controller) MainPID=$(systemctl show -p MainPID --value termini-controller)"
echo "budget:  $(systemctl show termini-controller -p Environment --value | tr ' ' '\n' | grep MAX_TOKENS)"
