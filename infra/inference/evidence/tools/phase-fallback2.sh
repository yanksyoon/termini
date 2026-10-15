#!/usr/bin/env bash
# phase-fallback2.sh — the (d) controller-side capture, second attempt: a declared,
# reversible cooldown override so a decision lands inside the broken window.
set -uo pipefail
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
cd /home/ubuntu || exit 1
PROJECT=termini-510609; ZONE=asia-south1-a
OUT=/home/ubuntu/.hermes/kanban/boards/termini/workspaces/t_f466824c
say() { printf '\n########## %s ##########\n' "$*"; }

SSHC() {
  local inst="$1" cmd="$2" i
  for i in 1 2 3; do
    if gcloud compute ssh "$inst" --zone="$ZONE" --project="$PROJECT" --quiet \
         --ssh-flag="-o StrictHostKeyChecking=accept-new" --ssh-flag="-o ConnectTimeout=20" \
         --ssh-flag="-o ServerAliveInterval=15" --command "$cmd" 2>&1; then
      return 0
    fi
    echo "(ssh $inst failed, retry $i)"; sleep 15
  done
  return 1
}

say "1. copy the second-attempt collectors"
gcloud compute scp "$OUT/remote-fallback2.sh" termini-controller:/tmp/remote-fallback2.sh \
  --zone="$ZONE" --project="$PROJECT" --quiet 2>&1 | tail -1
gcloud compute scp "$OUT/collect-model-decision.py" termini-controller:/tmp/collect-model-decision.py \
  --zone="$ZONE" --project="$PROJECT" --quiet 2>&1 | tail -1

say "2. BREAK the forwarded backend (bogus model id) — the failure under test"
SSHC termini-node-c "set -x
  sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash-does-not-exist|' /etc/termini/inference-proxy.env
  sudo systemctl restart termini-inference-proxy; sleep 3
  curl -s -m 30 -w '\nHTTP=%{http_code}\n' http://127.0.0.1:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"gemma\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}],\"max_tokens\":8}'
  echo '--- the loud failure line in the proxy log'
  sudo journalctl -u termini-inference-proxy --no-pager -n 6 | tail -3" \
  | tee "$OUT/live-forced-failure-2.txt"

say "3. (d) the controller's deterministic fallback while it is broken"
SSHC termini-controller "bash /tmp/remote-fallback2.sh" | tee "$OUT/live-fallback.txt"

say "4. remove the capture-only override and restore the shipped configuration"
SSHC termini-controller "set -x
  sudo rm -f /etc/systemd/system/termini-controller.service.d/zzzzz-fallback-capture.conf
  sudo systemctl daemon-reload && sudo systemctl restart termini-controller
  sleep 6
  sudo ls /etc/systemd/system/termini-controller.service.d/
  systemctl show termini-controller -p Environment | tr ' ' '\n' | grep -E 'COOLDOWN|DECISION_MODE'
  sudo systemctl is-active termini-controller" | tee "$OUT/live-fallback-restore.txt"
SSHC termini-node-c "set -x
  sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash|' /etc/termini/inference-proxy.env
  sudo /usr/local/sbin/termini-inference-switch gemini-proxy
  sudo cat /run/termini/inference-backend.json
  curl -s -m 15 http://127.0.0.1:8080/health; echo" | tee "$OUT/live-restore.txt"
echo "PHASE-FALLBACK2 DONE"
