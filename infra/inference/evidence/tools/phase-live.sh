#!/usr/bin/env bash
# phase-live.sh — the live acceptance run for t_f466824c on the raised fleet.
# Everything is teed into the workspace as raw output; a step that fails is
# captured failing (it does not abort the rest).
set -uo pipefail
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
cd /home/ubuntu || exit 1
PROJECT=termini-510609; ZONE=asia-south1-a
OUT=/home/ubuntu/.hermes/kanban/boards/termini/workspaces/t_f466824c
BRANCH=wt/t_f466824c-simulated-gpu-proxy-r2
say() { printf '\n########## %s ##########\n' "$*"; }

SSHC() { # $1=instance, $2=command ; retries through transient ssh failures
  local inst="$1" cmd="$2" i
  for i in 1 2 3; do
    if gcloud compute ssh "$inst" --zone="$ZONE" --project="$PROJECT" --quiet \
         --ssh-flag="-o StrictHostKeyChecking=accept-new" --ssh-flag="-o ConnectTimeout=20" \
         --ssh-flag="-o ServerAliveInterval=15" --command "$cmd" 2>&1; then
      return 0
    fi
    echo "(ssh $inst failed, retry $i)"; sleep 15
  done
  echo "(ssh $inst gave up)"; return 1
}

SCP() { # $1..$n-1 local paths, last = remote
  local last="${!#}"
  local files=("${@:1:$#-1}")
  for i in 1 2 3; do
    if gcloud compute scp "${files[@]}" "$last" --zone="$ZONE" --project="$PROJECT" --quiet 2>&1 | tail -1; then
      return 0
    fi
    echo "(scp to $last failed, retry $i)"; sleep 10
  done
  return 1
}

say "A. node-c: install the CURRENT branch's front door (:8080 proxy, llama.cpp worker on loopback :8081)"
SCP infra/inference/proxy.py infra/inference/termini-inference-proxy.service \
    infra/inference/install-inference-proxy.sh infra/inference/termini-inference-switch.sh \
    infra/inference/verify-proxy.sh termini-node-c:/tmp/
SCP "$OUT/remote-nodec-install.sh" termini-node-c:/tmp/remote-nodec-install.sh
SSHC termini-node-c "bash /tmp/remote-nodec-install.sh" | tee "$OUT/live-nodec-install.txt"

say "B. (a) PEER acceptance from node-a against 10.10.0.13:8080 — forwarded backend"
SCP infra/inference/verify-proxy.sh termini-node-a:/tmp/verify-proxy.sh
SSHC termini-node-a "hostname; bash /tmp/verify-proxy.sh 10.10.0.13 gemini-proxy; echo VERIFY_EXIT=\$?" \
  | tee "$OUT/live-peer-gemini.txt"

say "C. controller: deploy the branch tree, then model mode against the front door"
infra/gcp/tools/deploy-termini-tree.sh --ref "$BRANCH" 2>&1 | tail -12 | tee "$OUT/live-controller-deploy.txt"
SCP "$OUT/collect-model-decision.py" termini-controller:/tmp/collect-model-decision.py
SCP "$OUT/remote-controller-model.sh" termini-controller:/tmp/remote-controller-model.sh

say "D. (b) a REAL forwarded decision: seeder -> operator loop -> Node C -> Vertex"
SSHC termini-controller "bash /tmp/remote-controller-model.sh" | tee "$OUT/live-model-decision.txt"

say "E. node-c: the proxy's own request log for the forwarded call (latency_ms + destination)"
SSHC termini-node-c "sudo journalctl -u termini-inference-proxy --no-pager --since '-15min' | grep -E 'inference_request' | tail -5" \
  | tee "$OUT/live-proxy-journal.txt"

say "F. (c) ONE-STEP switch to the local CPU path (no code change), then the same peer run"
SSHC termini-node-c "set -x; sudo /usr/local/sbin/termini-inference-switch local; sudo cat /run/termini/inference-backend.json; curl -s -m 10 http://127.0.0.1:8080/health; echo" \
  | tee "$OUT/live-switch-local.txt"
SSHC termini-node-a "bash /tmp/verify-proxy.sh 10.10.0.13 local; echo VERIFY_EXIT=\$?" | tee "$OUT/live-peer-local.txt"

say "G. back to the shipped configuration (forwarded) + the peer run again"
SSHC termini-node-c "set -x; sudo /usr/local/sbin/termini-inference-switch gemini-proxy; sudo cat /run/termini/inference-backend.json" \
  | tee "$OUT/live-switch-back.txt"
SSHC termini-node-a "bash /tmp/verify-proxy.sh 10.10.0.13 gemini-proxy; echo VERIFY_EXIT=\$?" | tee "$OUT/live-peer-gemini-final.txt"

say "H. (d) FORCED Vertex AI failure: a model id that does not exist -> loud error, node and peer"
SSHC termini-node-c "set -x
  sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash-does-not-exist|' /etc/termini/inference-proxy.env
  sudo systemctl restart termini-inference-proxy; sleep 3
  curl -s -m 30 -w '\nHTTP=%{http_code}\n' http://127.0.0.1:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"gemma\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}],\"max_tokens\":8}'
  echo '--- the loud failure line in the proxy log'
  sudo journalctl -u termini-inference-proxy --no-pager -n 8 | tail -4" | tee "$OUT/live-forced-failure.txt"
SSHC termini-node-a "curl -s -m 30 -o /tmp/forced.json -w 'peer_http=%{http_code}\n' http://10.10.0.13:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{\"model\":\"gemma\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}],\"max_tokens\":8}'; cat /tmp/forced.json" \
  | tee -a "$OUT/live-forced-failure.txt"

say "I. (d) the controller falls back deterministically and the demo survives while it is broken"
SSHC termini-controller "bash /tmp/remote-controller-fallback.sh" | tee "$OUT/live-fallback.txt"

say "J. restore the shipped configuration and report the final node state"
SSHC termini-node-c "set -x
  sudo sed -i 's|^TERMINI_GEMINI_MODEL=.*|TERMINI_GEMINI_MODEL=gemini-2.5-flash|' /etc/termini/inference-proxy.env
  sudo /usr/local/sbin/termini-inference-switch gemini-proxy
  sudo cat /run/termini/inference-backend.json
  curl -s -m 15 http://127.0.0.1:8080/health; echo" | tee "$OUT/live-restore.txt"

echo "PHASE-LIVE DONE"
