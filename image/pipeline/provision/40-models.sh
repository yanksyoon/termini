#!/usr/bin/env bash
# 40-models.sh — download, checksum-verify and stage the pinned GGUF weights so
# that first boot loads them from local disk (no live multi-GB download).
#
# The resulting layout is what first-boot-preflight.sh asserts and what
# llama-cpp@<alias>.service consumes.
set -euo pipefail

LOCK="${MODELS_LOCK:-/opt/termini/models.lock.json}"
ROOT="${TERMINI_MODELS_DIR:-/var/lib/termini/models}"
ENVDIR="${LLAMACPP_ENVDIR:-/etc/termini/llama-cpp}"

if [ ! -f "$LOCK" ]; then
  echo "FATAL: model lock $LOCK not present" >&2
  exit 1
fi
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq required" >&2; exit 1; }

install -d -m 0755 "$ROOT" "$ENVDIR"

# alias -> loopback port for the per-alias llama.cpp worker (agent-runtime.md §8.1)
port_for() {
  case "$1" in
    deepseek-flash) printf '%s' 8081 ;;
    deepseek-v4-pro) printf '%s' 8082 ;;
    *) printf '%s' "" ;;
  esac
}

mapfile -t aliases < <(jq -r '.aliases | keys[]' "$LOCK")
if [ "${#aliases[@]}" -eq 0 ]; then
  echo "FATAL: no model aliases in $LOCK" >&2
  exit 1
fi

for alias in "${aliases[@]}"; do
  file="$(jq -r --arg a "$alias" '.aliases[$a].file' "$LOCK")"
  url="$(jq -r --arg a "$alias" '.aliases[$a].url' "$LOCK")"
  want="$(jq -r --arg a "$alias" '.aliases[$a].sha256' "$LOCK")"
  want_size="$(jq -r --arg a "$alias" '.aliases[$a].size_bytes' "$LOCK")"
  ctx="$(jq -r --arg a "$alias" '.aliases[$a].context_length' "$LOCK")"
  par="$(jq -r --arg a "$alias" '.aliases[$a].max_parallel' "$LOCK")"
  gpu="$(jq -r --arg a "$alias" '.aliases[$a].n_gpu_layers' "$LOCK")"
  port="$(port_for "$alias")"
  if [ -z "$port" ]; then
    echo "FATAL: no loopback port mapped for alias $alias" >&2
    exit 1
  fi

  dest="$ROOT/$file"
  if [ -f "$dest" ] && [ "$(sha256sum "$dest" | awk '{print $1}')" = "$want" ]; then
    echo "[provision:40-models] $alias already staged + verified"
  else
    echo "[provision:40-models] fetching $alias ($want_size bytes)"
    curl -fL --retry 3 --retry-delay 2 -o "$dest.part" "$url"
    echo "$want  $dest.part" | sha256sum -c - >/dev/null
    chmod 0644 "$dest.part"
    mv "$dest.part" "$dest"
  fi

  # Reproducible, pre-resolved worker config (no network at boot).
  {
    printf '%s\n' "MODEL_PATH=$dest"
    printf '%s\n' "PORT=$port"
    printf '%s\n' "CTX=$ctx"
    printf '%s\n' "PARALLEL=$par"
    printf '%s\n' "N_GPU_LAYERS=$gpu"
  } > "$ENVDIR/$alias.env"

  # Sidecar checksum so the boot preflight can re-verify without network.
  printf '%s  %s\n' "$want" "$dest" > "$dest.sha256"
done

echo "[provision:40-models] staged ${#aliases[@]} model(s) under $ROOT"
