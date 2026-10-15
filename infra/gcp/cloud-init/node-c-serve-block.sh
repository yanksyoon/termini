#!/usr/bin/env bash
# node-c-serve-block.sh — render Node C's cloud-init "serve block" (task t_359ed606).
#
# Node C's first boot has TWO halves and both are emitted HERE, so the two gcloud
# renderers cannot drift from each other or from the Terraform path:
#
#   1. /usr/local/sbin/termini-llama-serve.sh — written VERBATIM with its
#      __TOKEN__ placeholders resolved (the pinned llama.cpp build, the pinned
#      GGUF fetch + fail-closed digest check, the intent-extraction unit, and the
#      call that installs the inference front door).
#   2. the front-door PAYLOAD — infra/inference/{install-inference-proxy.sh,
#      proxy.py, termini-inference-proxy.service, termini-inference-switch.sh} —
#      written to FRONT_DOOR_DIR base64-encoded, i.e. byte-for-byte identical to
#      the committed files. Cloud-init therefore CALLS the committed installer
#      instead of carrying a copy of its unit heredocs: install-inference-proxy.sh
#      stays the SINGLE SOURCE OF TRUTH for the front door's units and env files,
#      and the baked (first-boot) path and the manual path cannot drift.
#
# The Terraform path (infra/gcp/main.tf -> infra/gcp/userdata) renders the same
# block natively; infra/gcp/verify-cloud-init.sh asserts that BOTH renders carry
# the same payload bytes (sha256 per file), so the three entry points stay equal.
#
# Usage — stdout is the fragment the shared cloud-init template inserts:
#
#   LLAMACPP_COMMIT=... MODEL_URL=... MODEL_FILE=... MODEL_SHA256=... \
#   MODEL_ALIAS=gemma PORT=8080 INFERENCE_BACKEND=gemini-proxy \
#     node-c-serve-block.sh --write-files   # write_files list entries (indent 2)
#   ... node-c-serve-block.sh --runcmd      # the runcmd list entry
#
# Overridable for tests: LLAMA_SERVE_SCRIPT, INFERENCE_DIR, FRONT_DOOR_DIR.
set -euo pipefail

MODE="${1:---write-files}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# infra/gcp/cloud-init -> repo root
REPO="$(cd "$HERE/../../.." && pwd)"

LLAMA_SERVE_SCRIPT="${LLAMA_SERVE_SCRIPT:-$HERE/llama-serve.sh}"
INFERENCE_DIR="${INFERENCE_DIR:-$REPO/infra/inference}"

LLAMACPP_COMMIT="${LLAMACPP_COMMIT:-}"
MODEL_URL="${MODEL_URL:-}"
MODEL_FILE="${MODEL_FILE:-}"
MODEL_SHA256="${MODEL_SHA256:-}"
MODEL_ALIAS="${MODEL_ALIAS:-}"
PORT="${PORT:-8080}"
# The backend a FRESH node serves. `gemini-proxy` is the shipped demo
# configuration (the forwarded managed model that emulates the GPU node);
# `local` is one env var / one command away on the running node
# (`termini-inference-switch local`). This is the explicit first-boot default.
INFERENCE_BACKEND="${INFERENCE_BACKEND:-gemini-proxy}"

# Where cloud-init stages the front-door payload ON THE NODE. llama-serve.sh
# defaults FRONT_DOOR_SRC to the same path — keep the two in step.
FRONT_DOOR_DIR="${FRONT_DOOR_DIR:-/usr/local/src/termini-inference}"

die() { printf '[node-c-serve-block] FATAL: %s\n' "$*" >&2; exit 1; }

[ -f "$LLAMA_SERVE_SCRIPT" ] || die "llama serve script not found: $LLAMA_SERVE_SCRIPT"
[ -d "$INFERENCE_DIR" ] || die "inference dir not found: $INFERENCE_DIR"
case "$INFERENCE_BACKEND" in
  local|gemini-proxy) : ;;
  *) die "INFERENCE_BACKEND must be local or gemini-proxy (got '$INFERENCE_BACKEND')" ;;
esac

# The front-door payload, in the order it is written: the installer first, then
# what it consumes (path on the node, mode, source file). Both this script and
# the Terraform module list the same four files; verify-cloud-init.sh compares
# the two renders so a path or a byte cannot silently diverge.
FRONT_DOOR_FILES=(
  "$FRONT_DOOR_DIR/install-inference-proxy.sh|0755|$INFERENCE_DIR/install-inference-proxy.sh"
  "$FRONT_DOOR_DIR/proxy.py|0644|$INFERENCE_DIR/proxy.py"
  "$FRONT_DOOR_DIR/termini-inference-proxy.service|0644|$INFERENCE_DIR/termini-inference-proxy.service"
  "$FRONT_DOOR_DIR/termini-inference-switch.sh|0755|$INFERENCE_DIR/termini-inference-switch.sh"
)

emit_write_files() {
  local script
  script="$(cat "$LLAMA_SERVE_SCRIPT")"
  script="${script//__LLAMACPP_COMMIT__/$LLAMACPP_COMMIT}"
  script="${script//__MODEL_URL__/$MODEL_URL}"
  script="${script//__MODEL_FILE__/$MODEL_FILE}"
  script="${script//__MODEL_SHA256__/$MODEL_SHA256}"
  script="${script//__MODEL_ALIAS__/$MODEL_ALIAS}"
  script="${script//__INFERENCE_BACKEND__/$INFERENCE_BACKEND}"
  script="${script//__PORT__/$PORT}"

  printf '  - path: /usr/local/sbin/termini-llama-serve.sh\n'
  printf '    permissions: %s\n' "'0755'"
  printf '    owner: root:root\n'
  printf '    content: |\n'
  while IFS= read -r line; do printf '      %s\n' "$line"; done <<< "$script"

  local entry path mode src b64
  for entry in "${FRONT_DOOR_FILES[@]}"; do
    path="${entry%%|*}"; entry="${entry#*|}"
    mode="${entry%%|*}"; src="${entry#*|}"
    [ -s "$src" ] || die "front-door payload missing: $src"
    b64="$(base64 -w0 "$src")"
    printf '  - path: %s\n' "$path"
    printf '    permissions: %s\n' "'$mode'"
    printf '    owner: root:root\n'
    # base64 keeps the payload byte-exact (no YAML indentation/escaping risk for
    # a 50 KB python file) and is what cloud-init writes verbatim.
    printf '    encoding: b64\n'
    printf '    content: "%s"\n' "$b64"
  done
}

case "$MODE" in
  --write-files) emit_write_files ;;
  --runcmd) printf '  - [ /usr/local/sbin/termini-llama-serve.sh ]\n' ;;
  -h|--help) sed -n '2,34p' "$0" ;;
  *) die "unknown mode: $MODE (use --write-files or --runcmd)" ;;
esac
