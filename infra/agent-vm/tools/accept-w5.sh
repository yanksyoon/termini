#!/usr/bin/env bash
# accept-w5.sh — raw acceptance evidence for a FRESH launch from agent-os/w5.
#
# Card: t_94303203 ("Rebuild + republish the agent VM image (agent-os/w5)").
#
# Runs the card's three acceptance points against an instance that was created
# by `lxc launch` from the placed alias (nothing copied in by hand):
#   1. termini-agent.service is `active`;
#   2. /etc/termini/agent.yaml names inference.default_model: gemma,
#      allowed_models: [gemma] and a tasks.max_concurrent bound;
#   3. the task surface is loopback-only AND live: from inside the instance
#      `curl -sS 127.0.0.1:9101/v1/tasks` answers, from outside (the node that
#      hosts it) it does not.
#
# Usage: accept-w5.sh NODE INSTANCE OUTDIR
set -euo pipefail
NODE="${1:?node}"; INST="${2:?instance}"; OUT="${3:?outdir}"
mkdir -p "$OUT"
in_vm() { lxc exec "$NODE" -- lxc exec "$INST" -- "$@"; }

echo "== the instance exists on the node (created by lxc launch) =="
lxc exec "$NODE" -- lxc list "$INST" --format csv -c n4sS > "$OUT/instance.csv"
cat "$OUT/instance.csv"

IP="$(lxc exec "$NODE" -- lxc list "$INST" --format csv -c 4 | cut -d' ' -f1)"
printf 'instance_ip=%s\n' "$IP" > "$OUT/instance-ip.txt"; cat "$OUT/instance-ip.txt"

echo "== 1. service state =="
in_vm systemctl is-active termini-agent > "$OUT/service-active.txt"
cat "$OUT/service-active.txt"
in_vm systemctl status termini-agent --no-pager -l > "$OUT/service-status.txt" 2>&1 || true

echo "== 2. baked config (/etc/termini/agent.yaml) =="
in_vm cat /etc/termini/agent.yaml > "$OUT/agent.yaml"
cat "$OUT/agent.yaml"
{
  echo "-- assertions --"
  in_vm grep -n 'default_model' /etc/termini/agent.yaml
  in_vm grep -n 'allowed_models' /etc/termini/agent.yaml
  in_vm grep -n 'max_concurrent' /etc/termini/agent.yaml
} > "$OUT/config-assertions.txt" 2>&1
cat "$OUT/config-assertions.txt"

echo "== 3a. task surface is LOOPBACK-ONLY (in-instance listener table) =="
in_vm ss -ltnp > "$OUT/listeners.txt"
cat "$OUT/listeners.txt"

echo "== 3b. it is LIVE from inside the instance =="
in_vm curl -sS -w '\nhttp=%{http_code}\n' --max-time 10 http://127.0.0.1:9101/v1/tasks \
  > "$OUT/inside-tasks.txt" 2>&1 || true
cat "$OUT/inside-tasks.txt"

echo "== 3d. the surface is LIVE, not just answering: submit a task and read it back =="
in_vm curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"prompt":"reply with exactly: pong"}' --max-time 10 \
  http://127.0.0.1:9101/v1/tasks > "$OUT/post-task.txt" 2>&1 || true
cat "$OUT/post-task.txt"
TID="$(sed -n 's/.*"task_id": *"\([^"]*\)".*/\1/p' "$OUT/post-task.txt" | head -1)"
printf 'task_id=%s\n' "$TID" > "$OUT/post-task-id.txt"; cat "$OUT/post-task-id.txt"
if [ -n "$TID" ]; then
  for _ in $(seq 1 15); do
    in_vm curl -sS --max-time 10 "http://127.0.0.1:9101/v1/tasks/$TID" > "$OUT/task-state.txt" 2>&1 || true
    grep -qE '"(state|status)": *"(done|error)"' "$OUT/task-state.txt" && break
    sleep 2
  done
  cat "$OUT/task-state.txt"
fi

echo "== 3e. task enumeration (GET /v1/tasks) =="
cat "$OUT/inside-tasks.txt"

echo "== 3c. and REFUSED from outside (from the node that hosts it) =="
lxc exec "$NODE" -- curl -sS -o /dev/null -w 'http=%{http_code}\n' --max-time 5 \
  "http://$IP:9101/v1/tasks" > "$OUT/outside-tasks.txt" 2>&1 || true
cat "$OUT/outside-tasks.txt"
lxc exec "$NODE" -- curl -sS -o /dev/null -w 'http=%{http_code}\n' --max-time 5 \
  "http://$IP:9101/status" > "$OUT/outside-status.txt" 2>&1 || true
cat "$OUT/outside-status.txt"

echo "== image identity inside the instance =="
in_vm cat /etc/termini/agent-image > "$OUT/agent-image.txt" 2>&1 || true
in_vm cat /etc/termini/agent-image.digest > "$OUT/agent-image.digest.txt" 2>&1 || true
in_vm /opt/termini/venv/bin/termini-agent --version > "$OUT/runtime-version.txt" 2>&1 || true
in_vm /opt/termini/venv/bin/python -c \
  "import termini_agent, os; print('pkg=', os.path.dirname(termini_agent.__file__)); print('has_taskapi=', os.path.exists(os.path.join(os.path.dirname(termini_agent.__file__), 'taskapi.py')))" \
  > "$OUT/runtime-package.txt" 2>&1 || true
cat "$OUT/agent-image.txt" "$OUT/agent-image.digest.txt" "$OUT/runtime-version.txt" "$OUT/runtime-package.txt"

echo "done -> $OUT"
