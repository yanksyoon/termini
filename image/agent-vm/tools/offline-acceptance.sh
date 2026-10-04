#!/usr/bin/env bash
# offline-acceptance.sh — runs INSIDE a booted agent VM with no network.
# Emits a JSON verdict. This is the W4 acceptance ("image boots offline with all
# agent deps importable; no network fetch at boot") expressed as executable checks.
set -uo pipefail

AGENT_BIN="/opt/termini/venv/bin/termini-agent"
NODE_BIN="/opt/termini/node/bin/node"
BOOT_LOG="/var/log/termini/first-boot.json"
rec="$(mktemp)"; trap 'rm -f "$rec"' EXIT
rc=0
check() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$rec"; [ "$2" = true ] || rc=1; }

# 1. no network interface besides loopback (the offline proof)
links="$(ip -o link show | awk -F': ' '{print $2}' | grep -v '^lo$' | tr '\n' ' ')"
[ -z "$links" ] && check no_network_interface true "links=lo" \
                || check no_network_interface false "non-loopback links: $links"

# 2. image identity
if [ -f /etc/termini/agent-image ]; then
  check image_identity true "$(cat /etc/termini/agent-image)"
else
  check image_identity false "missing /etc/termini/agent-image"
fi

# 3. termini-agent binary + selfcheck (imports every dep, validates config)
if [ -x "$AGENT_BIN" ]; then
  selfcheck="$("$AGENT_BIN" selfcheck --config /etc/termini/agent.yaml 2>&1)"
  if [ "$?" -eq 0 ] && printf '%s' "$selfcheck" | jq -e '.ok == true' >/dev/null 2>&1; then
    check agent_selfcheck true "runtime + framework venvs + config all ok"
  else
    check agent_selfcheck fail "$(printf '%s' "$selfcheck" | jq -c '.runtime_imports, .framework_venvs, .config' 2>/dev/null || printf '%s' "$selfcheck" | head -c 300)"
  fi
  printf '%s' "$selfcheck" > /tmp/selfcheck.json
else
  check agent_selfcheck fail "missing $AGENT_BIN"
  selfcheck="{}"
fi

# 4. per-module import status (C0 detail)
printf '%s' "${selfcheck:-{}}" | jq -r '
  (.runtime_imports // {} | to_entries[] | "dep\t\(.key)\t\(.value == "ok")\t\(.value)"),
  (.framework_venvs // {} | to_entries[] | "dep\tfw:\(.key)\t\(.value == "ok")\t\(.value)")
' 2>/dev/null | while IFS=$'\t' read -r _ name ok detail; do
  printf '%s\t%s\t%s\n' "$name" "$ok" "$detail" >>"$rec"
done

# 5. first-boot preflight log
if [ -f "$BOOT_LOG" ]; then
  verdict="$(jq -r '.verdict' "$BOOT_LOG" 2>/dev/null)"
  ndl="$(jq -r '.network_downloads_at_boot' "$BOOT_LOG" 2>/dev/null)"
  [ "$verdict" = pass ] && check preflight_verdict true "verdict=pass" \
                        || check preflight_verdict false "verdict=$verdict"
  [ "$ndl" = 0 ] && check network_downloads_at_boot true "network_downloads_at_boot=0" \
                 || check network_downloads_at_boot false "network_downloads_at_boot=$ndl"
  cp "$BOOT_LOG" /tmp/first-boot.json
else
  check preflight_verdict false "missing $BOOT_LOG"
fi

# 6. agent service + local status surface
state="$(systemctl is-active termini-agent 2>/dev/null || true)"
[ "$state" = active ] && check agent_service_active true "systemd state=active" \
                      || check agent_service_active false "systemd state=$state"

if status="$(curl -fsS --max-time 3 http://127.0.0.1:9101/status 2>/dev/null)"; then
  printf '%s' "$status" > /tmp/status.json
  st="$(printf '%s' "$status" | jq -r '.state')"
  egress="$(printf '%s' "$status" | jq -r '.governance.egress')"
  check status_surface true "http 127.0.0.1:9101 state=$st"
  [ "$egress" = deny ] && check governance_egress_deny true "governance.egress=deny" \
                       || check governance_egress_deny false "governance.egress=$egress"
else
  check status_surface false "status endpoint unreachable"
fi

# 7. node agent libs importable
if node_out="$("$NODE_BIN" -e 'require("/opt/termini/nodejs/node_modules/openai");require("/opt/termini/nodejs/node_modules/pino");require("/opt/termini/nodejs/node_modules/undici");console.log("ok")' 2>&1)" && [ "$node_out" = ok ]; then
  check node_libs_importable true "openai+pino+undici"
else
  check node_libs_importable false "$node_out"
fi

# 8. no route to the controller (governance §15): must fail fast
if timeout 3 curl -fsS "https://10.10.0.10/" >/dev/null 2>&1; then
  check no_route_to_controller false "reached the controller — governance violation"
else
  check no_route_to_controller true "controller unreachable (expected)"
fi

verdict=pass; [ "$rc" -eq 0 ] || verdict=fail
export REC="$rec"
python3 - /tmp/offline-acceptance.json "$verdict" <<'PY'
import json, os, sys
out, verdict = sys.argv[1], sys.argv[2]
checks, deps = [], []
with open(os.environ["REC"], encoding="utf-8") as fh:
    for line in fh:
        p = line.rstrip("\n").split("\t")
        rec = {"name": p[0], "ok": p[1] == "true", "detail": p[2] if len(p) > 2 else ""}
        (deps if p[0].startswith(("openai","httpx","pydantic","yaml","tenacity","structlog","langchain","langgraph","google","fw:")) else checks).append(rec)
doc = {"schema_version": "1.0", "kind": "termini.agent_offline_acceptance",
       "checks": checks, "deps": deps, "verdict": verdict}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2); fh.write("\n")
print(json.dumps(doc, indent=2))
PY
exit "$rc"
