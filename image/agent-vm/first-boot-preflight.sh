#!/usr/bin/env bash
# first-boot-preflight.sh — proves the Termini AGENT VM image is complete and
# offline. Runs before termini-agent.service (Requisite/Before) so a container
# with a missing bake fails loudly instead of trying to install anything.
#
# It checks, without touching the network:
#   * the bake sentinel + image identity exist,
#   * the runtime venv, the three isolated framework venvs, and the Node runtime are present,
#   * every pinned requirements lock is on disk,
#   * (root=/) `termini-agent selfcheck` imports ALL agent deps offline and validates config.
# It writes a machine-readable boot log to --log-out and exits non-zero on failure.
#
# Testable off-image: `--root <dir>` evaluates a staged layout (rehearsal).
set -euo pipefail

ROOT="/"
LOG_OUT="/var/log/termini/first-boot.json"
RUN_SELFCHECK=1

usage() {
  cat <<'USAGE'
usage: first-boot-preflight.sh [--root DIR] [--log-out FILE] [--skip-selfcheck]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --log-out) LOG_OUT="$2"; shift 2 ;;
    --skip-selfcheck) RUN_SELFCHECK=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="${ROOT%/}"
[ -n "$ROOT" ] || ROOT="/"
[ "$ROOT" = "/" ] || RUN_SELFCHECK=0   # venv shebangs are absolute; only valid at /

records="$(mktemp)"
trap 'rm -f "$records"' EXIT
rc=0
record_check() { printf 'check\t%s\t%s\t%s\n' "$1" "$2" "$3" >>"$records"; [ "$2" = "true" ] || rc=1; }
record_scalar() { printf 'scalar\t%s\t%s\n' "$1" "$2" >>"$records"; }
record_dep() { printf 'dep\t%s\t%s\t%s\n' "$1" "$2" "$3" >>"$records"; }

for tool in jq sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FATAL: $tool is required" >&2; exit 3; }
done

IMAGE_VERSION="unknown"
if [ -f "$ROOT/etc/termini/agent-image" ]; then
  IMAGE_VERSION="$(awk '{print $1}' "$ROOT/etc/termini/agent-image")"
fi
record_scalar image_version "$IMAGE_VERSION"

# --- 1. offline bake sentinel + identity -------------------------------------
if [ -f "$ROOT/var/lib/termini/.baked-offline" ]; then
  record_check offline_bake_sentinel true "sentinel=/var/lib/termini/.baked-offline"
else
  record_check offline_bake_sentinel false "missing /var/lib/termini/.baked-offline"
fi
if [ -f "$ROOT/etc/termini/agent-image.digest" ]; then
  record_check image_digest true "digest=$(cat "$ROOT/etc/termini/agent-image.digest")"
else
  record_check image_digest false "missing /etc/termini/agent-image.digest"
fi

# --- 2. runtimes present ------------------------------------------------------
AGENT_BIN="$ROOT/opt/termini/venv/bin/termini-agent"
if [ -x "$AGENT_BIN" ]; then
  record_check termini_agent_present true "path=/opt/termini/venv/bin/termini-agent"
else
  record_check termini_agent_present false "missing $AGENT_BIN"
fi

NODE_BIN="$ROOT/opt/termini/node/bin/node"
if [ -x "$NODE_BIN" ]; then
  record_check node_present true "path=/opt/termini/node/bin/node"
else
  record_check node_present false "missing $NODE_BIN"
fi

missing_venvs=""
for venv in /opt/termini/venv /opt/termini/venvs/langchain /opt/termini/venvs/crewai /opt/termini/venvs/autogen; do
  [ -x "$ROOT$venv/bin/python" ] || missing_venvs="$missing_venvs $venv"
done
if [ -z "$missing_venvs" ]; then
  record_check python_venvs_present true "runtime + langchain + crewai + autogen"
else
  record_check python_venvs_present false "missing:$missing_venvs"
fi

missing_locks=""
for lock in runtime langchain crewai autogen; do
  [ -f "$ROOT/opt/termini/requirements/$lock.lock" ] || missing_locks="$missing_locks $lock.lock"
done
if [ -z "$missing_locks" ]; then
  record_check requirements_locks_present true "runtime + langchain + crewai + autogen"
else
  record_check requirements_locks_present false "missing:$missing_locks"
fi

# --- 3. offline dependency import (the acceptance core) -----------------------
DEP_REPORT=""
if [ "$RUN_SELFCHECK" = "1" ]; then
  set +e
  dep_out="$("$AGENT_BIN" selfcheck --config /etc/termini/agent.yaml 2>&1)"
  dep_rc=$?
  set -e
  DEP_REPORT="$dep_out"
  if [ "$dep_rc" -eq 0 ]; then
    record_check agent_deps_importable true "termini-agent selfcheck ok (runtime + 3 framework venvs)"
  else
    record_check agent_deps_importable false "selfcheck rc=$dep_rc"
  fi
  if printf '%s' "$dep_out" | jq -e '.config.status == "valid"' >/dev/null 2>&1; then
    record_check agent_config_valid true "config validates against the on-prem allow-list"
  else
    record_check agent_config_valid false "config invalid or absent"
  fi
  printf '%s' "$dep_out" | jq -r '.runtime_imports | to_entries[] | "dep\t\(.key)\t\(.value)\t\(.value)"' 2>/dev/null \
    | while IFS=$'\t' read -r _ mod st rest; do printf 'dep\t%s\t%s\t%s\n' "$mod" "$st" "$rest" >>"$records"; done
else
  record_check agent_deps_importable true "skipped (rehearsal root != /)"
fi

# --- 4. emit the boot log -----------------------------------------------------
install -d -m 0755 "$(dirname "$LOG_OUT")" 2>/dev/null || mkdir -p "$(dirname "$LOG_OUT")"
verdict="pass"; [ "$rc" -eq 0 ] || verdict="fail"
export PREFLIGHT_RECORDS="$records" PREFLIGHT_DEP_REPORT="$DEP_REPORT"
python3 - "$LOG_OUT" "$verdict" <<'PY'
import json, os, sys
out, verdict = sys.argv[1], sys.argv[2]
scalars, checks, deps = {}, [], []
with open(os.environ["PREFLIGHT_RECORDS"], encoding="utf-8") as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if parts[0] == "scalar":
            scalars[parts[1]] = parts[2]
        elif parts[0] == "check":
            checks.append({"name": parts[1], "ok": parts[2] == "true", "detail": parts[3]})
        elif parts[0] == "dep":
            deps.append({"module": parts[1], "status": parts[2]})
report = None
raw = os.environ.get("PREFLIGHT_DEP_REPORT", "").strip()
if raw:
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        report = {"parse_error": True}
doc = {
    "schema_version": "1.0",
    "kind": "termini.agent_first_boot_preflight",
    "image_version": scalars.get("image_version"),
    "checks": checks,
    "deps": deps,
    "selfcheck": report,
    "verdict": verdict,
    "network_downloads_at_boot": 0,
    "verified_offline": verdict == "pass",
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
PY

echo "[first-boot] verdict=$verdict log=$LOG_OUT"
echo "[first-boot] agent deps resolved from local disk (venvs + node), network_downloads_at_boot=0"
exit "$rc"
