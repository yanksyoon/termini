#!/usr/bin/env bash
# remote-fallback2.sh — runs ON termini-controller while the forwarded backend is
# BROKEN. The first attempt saw no decision at all: the demo's per-rule cooldown
# (TERMINI_OPERATOR_COOLDOWN_S=600) had just fired one. This attempt declares a
# capture-only drop-in that shortens the cooldown to 30 s (removed again right
# after) so a decision lands INSIDE the failure window, and prints it either way.
set -uo pipefail
PROG=/tmp/collect-model-decision.py
DROPIN=/etc/systemd/system/termini-controller.service.d/zzzzz-fallback-capture.conf

echo "--- capture-only override (declared, reversible, removed by phase-fallback2.sh)"
sudo tee "$DROPIN" >/dev/null <<'CONF'
[Service]
Environment=TERMINI_OPERATOR_COOLDOWN_S=30
CONF
sudo systemctl daemon-reload
sudo systemctl restart termini-controller
sleep 6
echo "    effective cooldown + bound:"
systemctl show termini-controller -p Environment | tr ' ' '\n' \
  | grep -E "COOLDOWN|DECISION_TIMEOUT_S|DECISION_MODE" | sed 's/^/    /'
sudo systemctl is-active termini-controller

T1=$(date -u -d '-90 seconds' +%FT%TZ)
echo "T1=$T1  (failure-window rows; 90 s of slack for clock skew)"

echo "--- the controller must stay UP while the model path is broken (the demo survives it)"
for i in 1 2 3; do
  curl -s -m 5 -o /tmp/opdec.json -w "GET /v1/operator/decisions -> %{http_code}\n" \
    http://127.0.0.1:8080/v1/operator/decisions || true
  sleep 2
done
head -c 200 /tmp/opdec.json 2>/dev/null; echo

echo "--- keep the seeder running"
sudo pkill -f seed_demo.py 2>/dev/null || true
sudo systemctl restart termini-seeder 2>/dev/null || (
  echo "seeder service absent; start it adhoc"
  sudo bash -c 'cd /opt/termini && nohup python3 console/seed_demo.py --controller http://127.0.0.1:8080 \
    --token demo-project-token --bootstrap-mint 2 --duration 600 >/tmp/seeder3.log 2>&1 & echo started')
sleep 20

echo "--- polling for a decision recorded while the forwarded backend is broken"
found=no
for i in $(seq 1 36); do
  out=$(python3 "$PROG" --since "$T1" --limit 4 2>&1)
  if printf '%s' "$out" | grep -q "fallback_reason': 'inference_unavailable'"; then
    echo "DETERMINISTIC FALLBACK LANDED after $((i * 10))s of polling"
    found=yes
    break
  fi
  sleep 10
done
echo "poll result: $found"
echo "=== CONTROLLER EVIDENCE ($(date -u +%FT%TZ)) ==="
python3 "$PROG" --since "$T1" --limit 10
echo "=== the operator's own log lines around the fallback ==="
sudo journalctl -u termini-controller --no-pager --since '-8min' \
  | grep -iE "fallback|inference_unavailable|decision" | tail -20
echo "--- still alive after the failure?"
sudo systemctl is-active termini-controller
curl -s -m 5 -o /dev/null -w "GET /v1/operator/decisions -> %{http_code}\n" http://127.0.0.1:8080/v1/operator/decisions || true
