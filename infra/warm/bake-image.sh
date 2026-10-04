#!/usr/bin/env bash
# bake-image.sh — build and publish the pre-baked Termini demo node image.
#
# Bakes every runtime dependency of a demo node into a single LXD image and
# publishes it under the pinned alias (`agentic-os/w3`) plus an immutable tag,
# so `demo-env.sh up` never installs, downloads, or upgrades anything.
#
#   infra/warm/bake-image.sh [--force] [--keep-build-container]
#
# env overrides:
#   TERMINI_LLAMA_BIN_SRC=<dir>   reuse prebuilt llama-server/llama-cli (skip the build)
#   TERMINI_LLAMA_SRC=<dir>       reuse a llama.cpp checkout (skip the clone)
#
# What is baked (see ../image/README.md for the image contract):
#   * Ubuntu 24.04 base + python3/jq/curl/ca-certificates (installed at bake, not boot)
#   * llama.cpp binaries pinned to image/models.lock.json::llamacpp.commit
#   * /usr/local/bin/termini-bootstrap (node agent) + termini-llama + model helper
#   * systemd units termini-bootstrap.service + llama-cpp.service (enabled, not booted)
#   * /etc/termini/{image-version,model.env}, /opt/termini/{models.lock.json,README.md}
#   * /var/lib/termini/.baked-offline sentinel (proves nothing is fetched at boot)
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

FORCE=0
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --keep-build-container) KEEP=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

need lxc; need jq; need curl

BUILD_CONTAINER="termini-bake"
LOCK="$REPO_ROOT/image/models.lock.json"
LLAMA_COMMIT="$(jq -r '.llamacpp.commit' "$LOCK")"
GITSHA="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
IMMUTABLE_TAG="${IMAGE_ALIAS//\//-}-${IMAGE_VERSION}+${GITSHA}"

log "bake: alias=$IMAGE_ALIAS version=$IMAGE_VERSION immutable_tag=$IMMUTABLE_TAG llama.cpp=$LLAMA_COMMIT"

# ---------------------------------------------------------------------------
# 0. already baked?
if [ "$FORCE" -eq 0 ] && image_pinned; then
  log "image alias '$IMAGE_ALIAS' already present; use --force to rebuild"
  exit 0
fi

mkdir -p "$BUILD_DIR" "$PUBLISH_DIR" "$LLAMA_BIN_DIR"

# ---------------------------------------------------------------------------
# 1. llama.cpp binaries (pinned commit)
build_llamacpp() {
  local src="$BUILD_DIR/llama.cpp-src" build="$BUILD_DIR/llama.cpp-build"
  if [ -n "${TERMINI_LLAMA_BIN_SRC:-}" ]; then
    log "using prebuilt llama.cpp from $TERMINI_LLAMA_BIN_SRC"
    install -m 0755 "$TERMINI_LLAMA_BIN_SRC/llama-server" "$LLAMA_BIN_DIR/llama-server"
    install -m 0755 "$TERMINI_LLAMA_BIN_SRC/llama-cli" "$LLAMA_BIN_DIR/llama-cli"
    return 0
  fi
  if [ ! -d "$src/.git" ]; then
    if [ -n "${TERMINI_LLAMA_SRC:-}" ]; then
      src="$TERMINI_LLAMA_SRC"
    else
      log "cloning llama.cpp @ $LLAMA_COMMIT"
      rm -rf "$src"
      git clone --quiet https://github.com/ggml-org/llama.cpp "$src"
    fi
  fi
  git -C "$src" fetch --quiet origin "$LLAMA_COMMIT" 2>/dev/null || true
  git -C "$src" checkout --quiet "$LLAMA_COMMIT"
  log "building llama.cpp (static) at $(git -C "$src" rev-parse --short=12 HEAD)"
  cmake -S "$src" -B "$build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
        -DBUILD_SHARED_LIBS=OFF -DLLAMA_CURL=OFF >/dev/null
  cmake --build "$build" --config Release -j"$(nproc)" --target llama-server llama-cli >/dev/null
  install -m 0755 "$build/bin/llama-server" "$LLAMA_BIN_DIR/llama-server"
  install -m 0755 "$build/bin/llama-cli" "$LLAMA_BIN_DIR/llama-cli"
  "$LLAMA_BIN_DIR/llama-server" --version 2>&1 | grep -m1 'version:' > "$BUILD_DIR/llama-version.txt" || true
}
build_llamacpp
log "llama.cpp binaries: $(ls -1 "$LLAMA_BIN_DIR" | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# 2. base image
resolve_base() {
  # Explicit override wins (e.g. a base whose packages are already present when
  # the build host's containers have no working DNS — see image/README.md).
  if [ -n "${TERMINI_BAKE_BASE:-}" ]; then printf '%s\n' "$TERMINI_BAKE_BASE"; return 0; fi
  # Prefer an already-imported vanilla Ubuntu 24.04 (noble) image, excluding our
  # own published aliases so a re-bake never stacks on top of a previous bake.
  # Falls back to the remote images: source (still a one-time bake step).
  local fp
  fp="$(lxc image list --format json | jq -r '
    [ .[]
      | select(.properties.os=="ubuntu" and (.properties.release=="noble"))
      | select(([ .aliases[]?.name | startswith("agentic-os") ] | any) | not)
    ] | first | .fingerprint // empty')"
  if [ -n "$fp" ]; then printf '%s\n' "$fp"; return 0; fi
  printf '%s\n' "images:ubuntu/24.04"
}
BASE="$(resolve_base)"
log "base image: $BASE"

cleanup() {
  if [ "$KEEP" -eq 0 ]; then
    lxc delete --force "$BUILD_CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

lxc delete --force "$BUILD_CONTAINER" >/dev/null 2>&1 || true
log "launching build container from $BASE"
lxc launch "$BASE" "$BUILD_CONTAINER"
wait_until 60 "build container running" lxc_running "$BUILD_CONTAINER" || die "build container failed to start"

# ---------------------------------------------------------------------------
# 3. provision
log "installing runtime packages (python3/jq/curl/ca-certificates) — skipped if already present"
lxc exec "$BUILD_CONTAINER" -- bash -euo pipefail -c '
  export DEBIAN_FRONTEND=noninteractive
  # Install only what the base is missing, so a base image whose packages are
  # already present bakes without any apt/network access (no-DNS build hosts).
  missing=""
  for p in python3 jq curl ca-certificates libgomp1; do
    dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
  done
  if [ -n "$missing" ]; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends $missing >/dev/null
  else
    echo "[bake] runtime packages already present in the base image"
  fi
  apt-get clean
  rm -rf /var/lib/apt/lists/*
  python3 --version
'

log "installing Termini node components"
lxc exec "$BUILD_CONTAINER" -- install -d -m 0755 /opt/termini/llama.cpp/bin /etc/termini /var/lib/termini
lxc file push "$LLAMA_BIN_DIR/llama-server" "$BUILD_CONTAINER/opt/termini/llama.cpp/bin/llama-server"
lxc file push "$LLAMA_BIN_DIR/llama-cli" "$BUILD_CONTAINER/opt/termini/llama.cpp/bin/llama-cli"
lxc exec "$BUILD_CONTAINER" -- chmod 0755 /opt/termini/llama.cpp/bin/llama-server /opt/termini/llama.cpp/bin/llama-cli

lxc file push "$WARM_DIR/node/termini-llama.sh" "$BUILD_CONTAINER/usr/local/bin/termini-llama"
lxc file push "$WARM_DIR/node/termini-model-present.sh" "$BUILD_CONTAINER/usr/local/bin/termini-model-present"
lxc file push "$WARM_DIR/node/llama-cpp.service" "$BUILD_CONTAINER/etc/systemd/system/llama-cpp.service"
lxc exec "$BUILD_CONTAINER" -- chmod 0755 /usr/local/bin/termini-llama /usr/local/bin/termini-model-present

# Canonical node bootstrap daemon + unit. Run the SAME provision step the image
# pipeline uses (image/pipeline/provision/60-node-bootstrap.sh) against the
# canonical sources (infra/warm/node/* are symlinks to infra/bootstrap/node/*),
# so the published alias ships exactly what the image recipe installs — no drift,
# and no provision-time runtime push needed downstream.
lxc file push "$REPO_ROOT/image/pipeline/provision/60-node-bootstrap.sh" "$BUILD_CONTAINER/tmp/60-node-bootstrap.sh"
lxc file push "$WARM_DIR/node/termini-bootstrap.py" "$BUILD_CONTAINER/tmp/termini-bootstrap.py"
lxc file push "$WARM_DIR/node/termini-bootstrap.service" "$BUILD_CONTAINER/tmp/termini-bootstrap.service"
lxc exec "$BUILD_CONTAINER" -- env TERMINI_BOOTSTRAP_PY=/tmp/termini-bootstrap.py \
  TERMINI_BOOTSTRAP_UNIT=/tmp/termini-bootstrap.service bash /tmp/60-node-bootstrap.sh
lxc exec "$BUILD_CONTAINER" -- rm -f /tmp/60-node-bootstrap.sh /tmp/termini-bootstrap.py /tmp/termini-bootstrap.service

# static model env + image markers
lxc exec "$BUILD_CONTAINER" -- bash -euo pipefail -c "
  cat > /etc/termini/model.env <<EOF
MODEL_DIR=${MODELS_DIR}
MODEL_FILE=${MODEL_FILE}
MODEL_ALIAS=${MODEL_ALIAS}
MODEL_SIZE_BYTES=${MODEL_SIZE_BYTES}
INFERENCE_PORT=${INFERENCE_PORT}
N_GPU_LAYERS=0
CTX_SIZE=4096
PARALLEL=2
LOAD_MODE=none
EOF
  echo '${IMAGE_VERSION}' > /etc/termini/image-version
  echo '{\"alias\":\"${IMAGE_ALIAS}\",\"immutable_tag\":\"${IMMUTABLE_TAG}\",\"llamacpp_commit\":\"${LLAMA_COMMIT}\"}' > /etc/termini/image-manifest.json
  touch /var/lib/termini/.baked-offline
  install -d -m 0755 /var/lib/termini/identity /var/lib/termini/telemetry
"
lxc file push "$LOCK" "$BUILD_CONTAINER/opt/termini/models.lock.json"
lxc file push "$WARM_DIR/README.md" "$BUILD_CONTAINER/opt/termini/README.md" 2>/dev/null || true

# enable units (they boot on first start; no network, no installs)
lxc exec "$BUILD_CONTAINER" -- bash -euo pipefail -c '
  systemctl enable termini-bootstrap.service llama-cpp.service >/dev/null 2>&1 || true
  systemctl stop termini-bootstrap.service llama-cpp.service >/dev/null 2>&1 || true
  # make every launch a true first boot: no stale cloud-init state, no node config
  cloud-init clean --logs --machine-id >/dev/null 2>&1 || cloud-init clean --logs >/dev/null 2>&1 || true
  rm -rf /var/lib/cloud/instances /var/lib/cloud/instance /var/lib/cloud/data
  rm -f /etc/termini/config.yaml /var/lib/termini/enrollment.token
  # no baked node identity/state: a node from this alias always enrolls fresh
  rm -f /var/lib/termini/state.json
  find /var/lib/termini/identity -mindepth 1 -delete 2>/dev/null || true
'

# ---------------------------------------------------------------------------
# 4. publish
log "publishing image under alias '$IMAGE_ALIAS' (+ '$IMMUTABLE_TAG')"
lxc stop "$BUILD_CONTAINER" >/dev/null 2>&1 || true
# drop stale aliases (the underlying fingerprint image is kept)
lxc image delete "$IMAGE_ALIAS" >/dev/null 2>&1 || true
lxc image delete "$IMMUTABLE_TAG" >/dev/null 2>&1 || true
lxc publish "$BUILD_CONTAINER" --alias "$IMAGE_ALIAS" --alias "$IMMUTABLE_TAG" >/dev/null

FP="$(lxc image list "$IMAGE_ALIAS" --format csv -c f | head -n1)"
SIZE="$(lxc image list "$IMAGE_ALIAS" --format csv -c s | head -n1)"
jq -n --arg alias "$IMAGE_ALIAS" --arg tag "$IMMUTABLE_TAG" --arg version "$IMAGE_VERSION" \
      --arg fp "$FP" --arg size "$SIZE" --arg commit "$LLAMA_COMMIT" --arg gitsha "$GITSHA" \
      '{alias:$alias, immutable_tag:$tag, version:$version, fingerprint:$fp,
        size:$size, llamacpp_commit:$commit, gitsha:$gitsha}' \
      > "$PUBLISH_DIR/published.json"
log "published: fingerprint=$FP size=$SIZE"
log "manifest -> $PUBLISH_DIR/published.json"
