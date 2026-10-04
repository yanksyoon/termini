#!/usr/bin/env bash
# first-boot-preflight.sh — runs on first boot of a Termini pre-baked node.
#
# Its whole job is to PROVE, without touching the network, that the expensive
# artifacts were baked and are being read from local disk:
#   * the llama.cpp runtime binary is present and executable
#   * every pinned GGUF is present at its expected size (local disk, not a network fs)
#   * the bake recorded its pins (llama.cpp commit, staged models)
# It writes a machine-readable boot log to --log-out and exits non-zero on any
# failed check, so systemd can order termini-bootstrap.service after it
# (Before=/After=; see image/README.md §4 — the daemon is deliberately not hard
# Requisite-gated because demo aliases mount weights instead of baking them).
# NB: the bootstrap daemon is installed by image/pipeline/provision/60-node-bootstrap.sh.
#
# Testable off-image: pass --root <dir> to evaluate a staged layout (rehearsal).
set -euo pipefail

ROOT="/"
MODELS_DIR=""
LOCK=""
LLAMA_SERVER=""
LOG_OUT="/var/log/termini/first-boot.json"
VERIFY_SHA=0
ALLOW_STUB=0

usage() {
  cat <<'USAGE'
usage: first-boot-preflight.sh [--root DIR] [--models-dir DIR] [--lock FILE]
                               [--llama-server PATH] [--log-out FILE]
                               [--verify-sha] [--allow-stub]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --models-dir) MODELS_DIR="$2"; shift 2 ;;
    --lock) LOCK="$2"; shift 2 ;;
    --llama-server) LLAMA_SERVER="$2"; shift 2 ;;
    --log-out) LOG_OUT="$2"; shift 2 ;;
    --verify-sha) VERIFY_SHA=1; shift ;;
    --allow-stub) ALLOW_STUB=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="${ROOT%/}"
[ -n "$MODELS_DIR" ] || MODELS_DIR="$ROOT/var/lib/termini/models"
[ -n "$LOCK" ] || LOCK="$ROOT/opt/termini/models.lock.json"
[ -n "$LLAMA_SERVER" ] || LLAMA_SERVER="$ROOT/opt/termini/llama.cpp/bin/llama-server"
BUILD_RECORD="$ROOT/etc/termini/llama.cpp-build.txt"

command -v jq >/dev/null 2>&1 || { echo "FATAL: jq is required" >&2; exit 3; }
command -v sha256sum >/dev/null 2>&1 || { echo "FATAL: sha256sum is required" >&2; exit 3; }

records="$(mktemp)"
trap 'rm -f "$records"' EXIT
rc=0

record_check() { # name ok detail
  printf 'check\t%s\t%s\t%s\n' "$1" "$2" "$3" >>"$records"
  if [ "$2" != "true" ]; then rc=1; fi
}
record_scalar() { # key value
  printf 'scalar\t%s\t%s\n' "$1" "$2" >>"$records"
}

# --- boot-time network accounting --------------------------------------------
# `network_downloads_at_boot` is DERIVED from the kernel's own counters, never a
# literal. Mirrors image/agent-vm/tools/net-accounting.sh (kept in-tree here so
# the node bake stays self-contained). A fetch at boot — e.g. re-pulling model
# weights — lands as non-loopback RX bytes far above the link-local/DHCP
# allowance and fails the preflight.
TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES="${TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES:-8192}"
net_counters() { # prints "<rx_bytes> <tx_bytes> <rx_packets> <tx_packets> <n_ifaces> <iface,list>"
  local src="${TERMINI_PROC_NET_DEV:-/proc/net/dev}"
  [ -r "$src" ] || { printf '0 0 0 0 0 \n'; return 0; }
  awk '
    /:/ {
      line = $0; sub(/^[[:space:]]+/, "", line); c = index(line, ":")
      if (c == 0) next
      iface = substr(line, 1, c - 1); gsub(/[[:space:]]/, "", iface)
      if (iface == "" || iface == "lo") next
      rest = substr(line, c + 1); sub(/^[[:space:]]+/, "", rest)
      split(rest, f, /[[:space:]]+/)
      rb += f[1] + 0; rp += f[2] + 0; tb += f[9] + 0; tp += f[10] + 0; n++
      ifs = (ifs == "" ? iface : ifs "," iface)
    }
    END { printf "%d %d %d %d %d %s\n", rb, tb, rp, tp, n, ifs }
  ' "$src"
}
net_downloaded_bytes() { # <rx_bytes> -> bytes beyond the link-local/DHCP allowance
  local rx="${1:-0}"
  if [ "$rx" -gt "$TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES" ] 2>/dev/null; then
    printf '%d\n' "$((rx - TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES))"
  else
    printf '0\n'
  fi
}

IMAGE_VERSION="unknown"
if [ -f "$ROOT/etc/termini/image-version" ]; then
  IMAGE_VERSION="$(head -n1 "$ROOT/etc/termini/image-version")"
fi
record_scalar image_version "$IMAGE_VERSION"

# --- 1. llama.cpp runtime -----------------------------------------------------
if [ -x "$LLAMA_SERVER" ]; then
  ls_size="$(stat -c %s "$LLAMA_SERVER")"
  ls_sha="$(sha256sum "$LLAMA_SERVER" | awk '{print $1}')"
  record_check llama_server_present true "path=$LLAMA_SERVER size=$ls_size sha256=$ls_sha"
else
  record_check llama_server_present false "missing or not executable: $LLAMA_SERVER"
  ls_size=0
  ls_sha=""
fi
record_scalar llama_server_path "$LLAMA_SERVER"
record_scalar llama_server_size_bytes "$ls_size"
record_scalar llama_server_sha256 "$ls_sha"

if [ -f "$BUILD_RECORD" ]; then
  record_check llama_build_record true "commit=$(grep -m1 '^commit=' "$BUILD_RECORD" | cut -d= -f2)"
else
  record_check llama_build_record false "no ${BUILD_RECORD#$ROOT}"
fi

# --- 2. model weights: present, right size, on local disk ---------------------
stop_net_fs='^(nfs|nfs4|cifs|smb2|smbfs|fuse\.sshfs|9p)$'
fs_type="$(stat -f -c %T "$MODELS_DIR" 2>/dev/null || echo unknown)"
fs_dev="$(stat -f -c %d "$MODELS_DIR" 2>/dev/null || echo 0)"
record_scalar model_fs_type "$fs_type"
record_scalar model_fs_dev "$fs_dev"

if printf '%s' "$fs_type" | grep -Eq "$stop_net_fs"; then
  record_check model_fs_local false "model root is on a network filesystem: $fs_type"
else
  record_check model_fs_local true "fs_type=$fs_type dev=$fs_dev"
fi

if [ -f "$LOCK" ]; then
  mapfile -t aliases < <(jq -r '.aliases | keys[]' "$LOCK")
else
  aliases=()
fi

if [ "${#aliases[@]}" -eq 0 ]; then
  if [ "$ALLOW_STUB" = "1" ]; then
    record_check models_locked true "stub rehearsal: no lock aliases"
  else
    record_check models_locked false "no aliases in $LOCK"
  fi
fi

all_present=true
all_sized=true
all_sidecar=true
for alias in "${aliases[@]:-}"; do
  [ -n "$alias" ] || continue
  file="$(jq -r --arg a "$alias" '.aliases[$a].file' "$LOCK")"
  want_sha="$(jq -r --arg a "$alias" '.aliases[$a].sha256' "$LOCK")"
  want_size="$(jq -r --arg a "$alias" '.aliases[$a].size_bytes' "$LOCK")"
  residency="$(jq -r --arg a "$alias" '.aliases[$a].residency' "$LOCK")"
  path="$MODELS_DIR/$file"

  if [ -f "$path" ]; then
    got_size="$(stat -c %s "$path")"
  else
    got_size=0
    all_present=false
  fi
  [ "$got_size" = "$want_size" ] || all_sized=false
  [ -f "$path.sha256" ] || all_sidecar=false
  if [ -f "$path.sha256" ] && [ "$VERIFY_SHA" = "1" ]; then
    ( cd "$MODELS_DIR" && sha256sum -c "$(basename "$path").sha256" >/dev/null ) || all_sidecar=false
  fi
  printf 'model\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$alias" "$path" "$got_size" "$want_size" "$want_sha" "$residency" >>"$records"
done

if [ "${#aliases[@]}" -gt 0 ]; then
  record_check model_files_present "$all_present" "aliases=${aliases[*]}"
  record_check model_sizes_match "$all_sized" "expected sizes from models.lock.json"
  record_check model_sha256_sidecars "$all_sidecar" "sha256 sidecar per model${VERIFY_SHA:+ (verified=$VERIFY_SHA)}"
fi

# --- 3. offline bake sentinel -------------------------------------------------
sentinel="$ROOT/var/lib/termini/.baked-offline"
if [ -f "$sentinel" ]; then
  record_check offline_bake_sentinel true "sentinel=$sentinel"
else
  if [ "$ALLOW_STUB" = "1" ]; then
    record_check offline_bake_sentinel true "stub rehearsal: sentinel not required"
  else
    record_check offline_bake_sentinel false "missing $sentinel"
  fi
fi

# --- 4. boot-time network accounting (real measurement) -----------------------
# On a real boot (root=/) this is the node's own traffic; against a staged
# --root the host's counters are not the image's, so unless a fixture is
# injected via TERMINI_PROC_NET_DEV we record that it was not applicable rather
# than inventing a number.
NET_MEASURED=0
NET_RX=0; NET_TX=0; NET_RXP=0; NET_TXP=0; NET_NIF=0; NET_IFS=""; NET_DL=0
if [ "$ROOT" = "/" ] || [ -n "${TERMINI_PROC_NET_DEV:-}" ]; then
  read -r NET_RX NET_TX NET_RXP NET_TXP NET_NIF NET_IFS <<<"$(net_counters)"
  NET_RX="${NET_RX:-0}"; NET_TX="${NET_TX:-0}"; NET_RXP="${NET_RXP:-0}"
  NET_TXP="${NET_TXP:-0}"; NET_NIF="${NET_NIF:-0}"; NET_IFS="${NET_IFS:-}"
  NET_DL="$(net_downloaded_bytes "$NET_RX")"
  NET_MEASURED=1
  if [ "$NET_DL" = 0 ]; then
    record_check no_boot_network_downloads true \
      "rx_bytes=$NET_RX (<= allowance $TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES) ifaces=${NET_IFS:-none}"
  else
    record_check no_boot_network_downloads false \
      "downloaded_bytes=$NET_DL rx_bytes=$NET_RX ifaces=${NET_IFS:-none}"
  fi
fi
record_scalar net_measured "$NET_MEASURED"
record_scalar net_allowance_bytes "$TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES"
record_scalar net_rx_bytes "$NET_RX"
record_scalar net_tx_bytes "$NET_TX"
record_scalar net_rx_packets "$NET_RXP"
record_scalar net_tx_packets "$NET_TXP"
record_scalar net_ifaces "$NET_IFS"
record_scalar net_n_ifaces "$NET_NIF"
record_scalar net_downloaded_bytes "$NET_DL"

# --- 5. emit the boot log -----------------------------------------------------
install -d -m 0755 "$(dirname "$LOG_OUT")" 2>/dev/null || mkdir -p "$(dirname "$LOG_OUT")"
verdict="pass"
if [ "$rc" -ne 0 ]; then verdict="fail"; fi
export PREFLIGHT_RECORDS="$records"
python3 - "$LOG_OUT" "$verdict" <<'PY'
import json, os, sys
out, verdict = sys.argv[1], sys.argv[2]
scalars, checks, models = {}, [], []
with open(os.environ["PREFLIGHT_RECORDS"], encoding="utf-8") as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if parts[0] == "scalar":
            scalars[parts[1]] = parts[2]
        elif parts[0] == "check":
            checks.append({"name": parts[1], "ok": parts[2] == "true", "detail": parts[3]})
        elif parts[0] == "model":
            models.append({
                "alias": parts[1], "path": parts[2],
                "size_bytes": int(parts[3]), "expected_size_bytes": int(parts[4]),
                "expected_sha256": parts[5], "residency": parts[6],
            })
doc = {
    "schema_version": "1.0",
    "kind": "termini.first_boot_preflight",
    "ts": os.environ.get("PREFLIGHT_TS") or None,
    "image_version": scalars.get("image_version"),
    "llama_cpp": {
        "path": scalars.get("llama_server_path"),
        "size_bytes": int(scalars.get("llama_server_size_bytes") or 0),
        "sha256": scalars.get("llama_server_sha256") or None,
    },
    "model_root": {"fs_type": scalars.get("model_fs_type"), "fs_dev": scalars.get("model_fs_dev")},
    "models": models,
    "checks": checks,
    "verdict": verdict,
    "network": {
        "method": "proc_net_dev",
        "source": os.environ.get("TERMINI_PROC_NET_DEV") or "/proc/net/dev",
        "measured": scalars.get("net_measured") == "1",
        "allowance_bytes": int(scalars.get("net_allowance_bytes") or 0),
        "rx_bytes": int(scalars.get("net_rx_bytes") or 0),
        "tx_bytes": int(scalars.get("net_tx_bytes") or 0),
        "rx_packets": int(scalars.get("net_rx_packets") or 0),
        "tx_packets": int(scalars.get("net_tx_packets") or 0),
        "nonloopback_interfaces": [i for i in (scalars.get("net_ifaces") or "").split(",") if i],
        "nonloopback_interface_count": int(scalars.get("net_n_ifaces") or 0),
        "downloaded_bytes": int(scalars.get("net_downloaded_bytes") or 0),
    },
    # Derived from the counter above (bytes received beyond the link-local/DHCP
    # allowance); NOT a literal. 0 == no network download happened at boot.
    "network_downloads_at_boot": int(scalars.get("net_downloaded_bytes") or 0),
    "verified_offline": verdict == "pass",
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, sort_keys=False)
    fh.write("\n")
PY

echo "[first-boot] verdict=$verdict log=$LOG_OUT"
echo "[first-boot] models resolved from local disk: ${MODELS_DIR} (fs=${fs_type})"
if [ "$NET_MEASURED" = 1 ]; then
  echo "[first-boot] network_downloads_at_boot=$NET_DL (measured rx_bytes=$NET_RX from /proc/net/dev, allowance=$TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES)"
else
  echo "[first-boot] network_downloads_at_boot=0 (not measured: rehearsal root)"
fi
exit "$rc"
