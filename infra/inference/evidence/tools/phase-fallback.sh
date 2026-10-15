#!/usr/bin/env bash
# phase-fallback.sh — the remaining acceptance (d) capture, and the reason step I
# of phase-live.sh came back empty: the remote script was never copied.
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

say "1. copy the fallback collector to the controller"
gcloud compute scp "$OUT/remote-controller-fallback.sh" termini-controller:/tmp/remote-controller-fallback.sh \
  --zone="$ZONE" --project="$PROJECT" --quiet 2>&1 | tail -1

say "2. BREAK the forwarded backend again (bogus model id) — the failure under test"
SSHC termini-node-c "set -x
  sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash-does-not-exist|' /etc/termini/inference-proxy.env
  sudo systemctl restart termini-inference-proxy; sleep 3
  curl -s -m 30 -w '\nHTTP=%{http_code}\n' http://127.0.0.1:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"gemma\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}],\"max_tokens\":8}'" \
  | tee "$OUT/live-forced-failure-2.txt"

say "3. (d) the controller's deterministic fallback + survival, while it is broken"
SSHC termini-controller "bash /tmp/remote-controller-fallback.sh" | tee "$OUT/live-fallback.txt"

say "4. restore the shipped configuration on node-c"
SSHC termini-node-c "set -x
  sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash|' /etc/termini/inference-proxy.env
  sudo /usr/local/sbin/termini-inference-switch gemini-proxy
  sudo cat /run/termini/inference-backend.json
  curl -s -m 15 http://127.0.0.1:8080/health; echo" | tee "$OUT/live-restore.txt"
echo "PHASE-FALLBACK DONE"
