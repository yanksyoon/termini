#!/usr/bin/env bash
# remote-controller-model.sh — runs ON termini-controller: flip the operator to
# model mode against the Node C front door (contract's 120 s decision bound),
# drive the demo seeder, and POLL the DB until a decision_source=model row shows
# up, then print the raw evidence (decision row, externalModelCall audit pair,
# the forwarded call's wall clock in seconds, the dashboard log rows).
set -uo pipefail
PROG=/tmp/collect-model-decision.py
DB=/data/controller.db
T0=$(date -u +%FT%TZ)
echo "T0=$T0  (rows before this instant are ignored)"

set -x
sudo rm -f /etc/systemd/system/termini-controller.service.d/zz-node-c-model.conf
sudo mkdir -p /etc/systemd/system/termini-controller.service.d
sudo tee /etc/systemd/system/termini-controller.service.d/zz-proxy-model.conf >/dev/null <<'DROPIN'
[Service]
Environment=TERMINI_OPERATOR_ENABLED=true
Environment=TERMINI_OPERATOR_DECISION_MODE=model
Environment=TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080
Environment=TERMINI_INFERENCE_MODEL=gemma
Environment=TERMINI_INFERENCE_TIMEOUT_S=120
Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=120
Environment=TERMINI_INTENT_INFERENCE_BASE_URL=http://10.10.0.13:8090
DROPIN
sudo systemctl daemon-reload
sudo systemctl restart termini-controller
set +x
for i in $(seq 1 30); do
  curl -s -m 3 -o /dev/null http://127.0.0.1:8080/healthz && { echo controller-up; break; }
  sleep 2
done
echo "--- operator environment in the running unit"
sudo systemctl show termini-controller -p Environment | tr ' ' '\n' | grep -E 'DECISION_MODE|INFERENCE|OPERATOR_ENABLED'

echo "--- reset the per-(node,rule) fallback/cooldown ledger (the demo's RESET; printed, not silent)"
python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
before = con.execute("SELECT COUNT(*) FROM decision_fallback_state").fetchone()[0]
con.execute("DELETE FROM decision_fallback_state")
con.commit()
print(f"decision_fallback_state rows deleted: {before}")
PY

echo "--- start the demo seeder (synthetic nodes -> the real operator loop)"
sudo pkill -f seed_demo.py 2>/dev/null || true
sudo systemctl restart termini-seeder 2>/dev/null || \
  sudo bash -c 'cd /opt/termini && nohup python3 console/seed_demo.py --controller http://127.0.0.1:8080 \
    --token demo-project-token --bootstrap-mint 2 --duration 900 >/tmp/seeder.log 2>&1 & echo "seeder started adhoc"'
sleep 20

echo "--- polling for a decision_source=model row decided after $T0"
found=no
for i in $(seq 1 24); do
  out=$(python3 "$PROG" --since "$T0" --limit 4 2>&1)
  if printf '%s' "$out" | grep -q "decision_source': 'model'"; then
    echo "MODEL DECISION LANDED after $((i * 10))s of polling"
    found=yes
    break
  fi
  sleep 10
done
echo "poll result: $found"
echo "--- seeder log tail"
tail -8 /tmp/seeder.log 2>/dev/null || sudo journalctl -u termini-seeder --no-pager -n 8
echo "=== CONTROLLER EVIDENCE ($(date -u +%FT%TZ)) ==="
python3 "$PROG" --since "$T0" --limit 8
