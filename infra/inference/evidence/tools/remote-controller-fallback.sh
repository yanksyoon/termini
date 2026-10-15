#!/usr/bin/env bash
# remote-controller-fallback.sh — runs ON termini-controller while the forwarded
# backend is BROKEN: proves the API and the operator loop survive, and polls until
# the controller records a DETERMINISTIC fallback (decision_source=fallback,
# fallback_reason=inference_unavailable) instead of hanging on the decision bound.
set -uo pipefail
PROG=/tmp/collect-model-decision.py

echo "--- the decision-bound configuration on this controller (contract says 120 s)"
ls -la /etc/systemd/system/termini-controller.service.d/
grep -rn "DECISION_TIMEOUT_S\|INFERENCE_TIMEOUT_S" /etc/systemd/system/termini-controller.service.d/ \
  /etc/systemd/system/termini-controller.service 2>/dev/null | sed 's/^/    /'
echo "    effective:"
systemctl show termini-controller -p Environment | tr ' ' '\n' \
  | grep -E "INFERENCE_TIMEOUT_S|DECISION_TIMEOUT_S" | sed 's/^/    /'

echo "--- wait out the demo's per-rule cooldown (TERMINI_OPERATOR_COOLDOWN_S=600)"
last=$(python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/data/controller.db")
row = con.execute("SELECT MAX(decided_at) FROM remediation_decisions").fetchone()
print(row[0] or "")
PY
)
echo "    last decision at: ${last:-none}"
if [ -n "$last" ]; then
  last_epoch=$(date -u -d "$last" +%s 2>/dev/null || echo 0)
  target=$((last_epoch + 615))
  now=$(date -u +%s)
  wait_s=$((target - now))
  if [ "$wait_s" -gt 0 ] && [ "$wait_s" -le 400 ]; then
    echo "    sleeping ${wait_s}s so the next decision is not suppressed"
    sleep "$wait_s"
  else
    echo "    no wait needed (wait_s=${wait_s})"
  fi
fi

T1=$(date -u +%FT%TZ)
echo "T1=$T1  (the forwarded backend is broken from this instant; earlier rows are ignored)"

echo "--- the controller must stay UP while the model path is broken (the demo survives it)"
sudo systemctl is-active termini-controller
for i in 1 2 3; do
  curl -s -m 5 -o /tmp/opdec.json -w "GET /v1/operator/decisions -> %{http_code}\n" \
    http://127.0.0.1:8080/v1/operator/decisions || true
  sleep 2
done
echo "--- body head (raw)"
head -c 300 /tmp/opdec.json 2>/dev/null; echo

echo "--- clear the per-(node,rule) fallback ledger so the next decision is not suppressed by a cooldown"
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/data/controller.db")
n = con.execute("SELECT COUNT(*) FROM decision_fallback_state").fetchone()[0]
con.execute("DELETE FROM decision_fallback_state")
con.commit()
print(f"decision_fallback_state rows deleted: {n}")
print("trigger_state before:", [dict(zip([d[0] for d in con.execute('SELECT * FROM trigger_state LIMIT 1').description], r))
                               for r in con.execute("SELECT * FROM trigger_state")][:4])
PY

echo "--- keep the seeder running"
sudo pkill -f seed_demo.py 2>/dev/null || true
sudo systemctl restart termini-seeder 2>/dev/null || (
  echo "seeder service absent; start it adhoc"
  sudo bash -c 'cd /opt/termini && nohup python3 console/seed_demo.py --controller http://127.0.0.1:8080 \
    --token demo-project-token --bootstrap-mint 2 --duration 600 >/tmp/seeder2.log 2>&1 & echo started')
sleep 20

echo "--- polling for the deterministic fallback"
found=no
for i in $(seq 1 24); do
  out=$(python3 "$PROG" --since "$T1" --limit 4 2>&1)
  if printf '%s' "$out" | grep -q "fallback_reason': 'inference_unavailable'"; then
    echo "DETERMINISTIC FALLBACK LANDED after $((i * 10))s of polling"
    found=yes
    break
  fi
  sleep 10
done
echo "poll result: $found"
echo "--- seeder log tail"
tail -6 /tmp/seeder2.log 2>/dev/null || sudo journalctl -u termini-seeder --no-pager -n 6
echo "=== CONTROLLER EVIDENCE ($(date -u +%FT%TZ)) ==="
python3 "$PROG" --since "$T1" --limit 8
echo "--- still alive after the failure?"
sudo systemctl is-active termini-controller
curl -s -m 5 -o /dev/null -w "GET /v1/operator/decisions -> %{http_code}\n" http://127.0.0.1:8080/v1/operator/decisions || true
