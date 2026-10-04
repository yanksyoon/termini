#!/usr/bin/env bash
# build-image.sh — reproducible builder for the Termini pre-baked node image.
#
#   ./build-image.sh --check                 # validate the recipe on any checkout (no build host needed)
#   ./build-image.sh --variant gpu           # bake the Node C (GPU) variant
#   ./build-image.sh --variant compute       # bake the compute variant (Node A/B)
#
# The build host needs `packer` (preferred, see pipeline/packer/) or
# `distrobuilder` (container variant, see pipeline/distrobuilder/). The pins
# (base image, llama.cpp commit, model digests) live in models.lock.json and in
# this file — nothing is resolved from a moving tag at build time.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_DIR="$(cd "$SELF_DIR/.." && pwd)"
LOCK="$IMAGE_DIR/models.lock.json"
SCHEMA="$IMAGE_DIR/manifests/image-manifest.schema.json"
PROVISION_DIR="$SELF_DIR/provision"
PACKER_DIR="$SELF_DIR/packer"
VERIFY_MANIFEST="$SELF_DIR/tools/verify_manifest.py"

# --- frozen build inputs (bump deliberately) ---------------------------------
BASE_NAME="ubuntu-24.04-server-cloudimg-amd64"
BASE_RELEASE="20260911"
BASE_ARCH="amd64"
BASE_URL="https://cloud-images.ubuntu.com/noble/${BASE_RELEASE}/noble-server-cloudimg-amd64.img"
BASE_SHA256="612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354"

ALIAS="agentic-os/w3"
VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"
VARIANT="compute"
OUT_DIR="${TERMINI_OUT_DIR:-/var/lib/termini/build}"
MODE="build"

usage() {
  cat <<'USAGE'
usage: build-image.sh [--variant compute|gpu] [--version X.Y.Z] [--out DIR]
                      [--alias NAME] [--check]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --alias) ALIAS="$2"; shift 2 ;;
    --check) MODE="check"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$VARIANT" in
  compute|gpu) ;;
  *) echo "FATAL: --variant must be compute or gpu" >&2; exit 2 ;;
esac

LLAMACPP_COMMIT="$(jq -r '.llamacpp.commit' "$LOCK")"
FLASH_SHA="$(jq -r '.aliases["deepseek-flash"].sha256' "$LOCK")"
PRO_SHA="$(jq -r '.aliases["deepseek-v4-pro"].sha256' "$LOCK")"
export LLAMACPP_COMMIT FLASH_SHA PRO_SHA VARIANT

# -----------------------------------------------------------------------------
check_recipe() {
  local rc=0 f
  local required=(
    "$LOCK" "$SCHEMA"
    "$PROVISION_DIR/00-base.sh" "$PROVISION_DIR/10-node-os.sh"
    "$PROVISION_DIR/20-gpu-cuda.sh" "$PROVISION_DIR/30-llamacpp.sh"
    "$PROVISION_DIR/40-models.sh" "$PROVISION_DIR/50-agent-deps.sh"
    "$SELF_DIR/first-boot-preflight.sh" "$SELF_DIR/publish-image.sh"
    "$PACKER_DIR/termini-node.pkr.hcl" "$VERIFY_MANIFEST"
    "$SELF_DIR/distrobuilder/termini-node.yaml"
  )
  for f in "${required[@]}"; do
    if [ -f "$f" ]; then
      echo "  ok   $f"
    else
      echo "  MISS $f"
      rc=1
    fi
  done

  echo "== syntax: bash -n"
  while IFS= read -r sh; do
    bash -n "$sh" || { echo "  SYNTAX FAIL $sh"; rc=1; }
  done < <(find "$IMAGE_DIR" -name '*.sh' -type f | sort)

  echo "== json: models.lock.json + schema"
  python3 -m json.tool "$LOCK" >/dev/null || rc=1
  python3 -m json.tool "$SCHEMA" >/dev/null || rc=1

  echo "== python: verify_manifest.py imports"
  python3 -m py_compile "$VERIFY_MANIFEST" || rc=1

  echo "== manifest + lock cross-check"
  python3 "$VERIFY_MANIFEST" --lock "$LOCK" --schema "$SCHEMA" || rc=1

  if command -v shellcheck >/dev/null 2>&1; then
    echo "== shellcheck -S warning"
    while IFS= read -r sh; do
      shellcheck -S warning "$sh" || { echo "  SHELLCHECK FAIL $sh"; rc=1; }
    done < <(find "$IMAGE_DIR" -name '*.sh' -type f | sort)
  else
    echo "== shellcheck: not installed, skipped"
  fi

  if [ "$rc" -eq 0 ]; then
    echo "RECIPE OK (alias=$ALIAS variant=$VARIANT version=$VERSION)"
  else
    echo "RECIPE FAILED" >&2
  fi
  return "$rc"
}

# -----------------------------------------------------------------------------
bake() {
  command -v jq >/dev/null 2>&1 || { echo "FATAL: jq required" >&2; exit 3; }
  gitsha="$(git -C "$IMAGE_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
  immutable_tag="agentic-os-w3-${VERSION}+g${gitsha}"
  install -d -m 0755 "$OUT_DIR"
  artifact=""

  if command -v packer >/dev/null 2>&1; then
    echo "[build] packer build variant=$VARIANT version=$VERSION"
    packer init "$PACKER_DIR"
    packer build \
      -var "variant=$VARIANT" \
      -var "version=$VERSION" \
      -var "base_url=$BASE_URL" \
      -var "base_sha256=$BASE_SHA256" \
      -var "llamacpp_commit=$LLAMACPP_COMMIT" \
      -var "out_dir=$OUT_DIR" \
      "$PACKER_DIR"
    artifact="$OUT_DIR/${BASE_NAME}-${VARIANT}-${VERSION}.qcow2"
  elif command -v distrobuilder >/dev/null 2>&1; then
    echo "[build] distrobuilder build variant=$VARIANT version=$VERSION"
    distrobuilder build-lxd "$SELF_DIR/distrobuilder/termini-node.yaml" "$OUT_DIR" \
      --timeout 60 -o "image.variant=$VARIANT"
    artifact="$OUT_DIR/${BASE_NAME}-${VARIANT}-${VERSION}.tar.gz"
  else
    echo "FATAL: neither packer nor distrobuilder is installed on this build host." >&2
    echo "       Run './build-image.sh --check' for recipe validation only." >&2
    exit 4
  fi

  [ -f "$artifact" ] || { echo "FATAL: builder produced no artifact at $artifact" >&2; exit 5; }
  size="$(stat -c %s "$artifact")"
  sha="$(sha256sum "$artifact" | awk '{print $1}')"
  printf '%s  %s\n' "$sha" "$(basename "$artifact")" > "$artifact.sha256"

  manifest="$OUT_DIR/image-manifest.json"
  export MANIFEST_ALIAS="$ALIAS" MANIFEST_VERSION="$VERSION" \
         MANIFEST_TAG="$immutable_tag" MANIFEST_VARIANT="$VARIANT" \
         MANIFEST_GITSHA="$gitsha" MANIFEST_BASE_NAME="$BASE_NAME" \
         MANIFEST_BASE_RELEASE="$BASE_RELEASE" MANIFEST_BASE_ARCH="$BASE_ARCH" \
         MANIFEST_BASE_URL="$BASE_URL" MANIFEST_BASE_SHA="$BASE_SHA256" \
         MANIFEST_ARTIFACT="$artifact" MANIFEST_SIZE="$size" MANIFEST_SHA="$sha"
  python3 - "$manifest" "$LOCK" <<'PY'
import json, os, sys, datetime
manifest, lock = sys.argv[1], sys.argv[2]
e = os.environ
with open(lock, encoding="utf-8") as fh:
    lk = json.load(fh)
doc = {
    "schema_version": "1.0",
    "alias": e["MANIFEST_ALIAS"],
    "version": e["MANIFEST_VERSION"],
    "immutable_tag": e["MANIFEST_TAG"],
    "variant": e["MANIFEST_VARIANT"],
    "gitsha": e["MANIFEST_GITSHA"],
    "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "base_image": {
        "name": e["MANIFEST_BASE_NAME"], "release": e["MANIFEST_BASE_RELEASE"],
        "arch": e["MANIFEST_BASE_ARCH"], "url": e["MANIFEST_BASE_URL"],
        "sha256": e["MANIFEST_BASE_SHA"],
    },
    "artifacts": [{
        "name": os.path.basename(e["MANIFEST_ARTIFACT"]),
        "format": "qcow2" if e["MANIFEST_ARTIFACT"].endswith(".qcow2") else "lxd-image",
        "path": e["MANIFEST_ARTIFACT"], "size_bytes": int(e["MANIFEST_SIZE"]),
        "sha256": e["MANIFEST_SHA"],
    }],
    "pins": {
        "llamacpp_commit": lk["llamacpp"]["commit"],
        "lxd_snap_channel": lk.get("lxd_snap_channel", "5.21/stable"),
        "lxd_snap_revision": None,
        "models": {
            "deepseek-flash": lk["aliases"]["deepseek-flash"]["sha256"],
            "deepseek-v4-pro": lk["aliases"]["deepseek-v4-pro"]["sha256"],
        },
    },
    "contents": {
        "llama_server_path": "/opt/termini/llama.cpp/bin/llama-server",
        "model_root": lk["model_root_in_image"],
        "agent_vm_alias": "agent-vm/2026.10.0",
        "bootstrap_unit": "termini-bootstrap.service",
    },
    "first_boot": {
        "preflight_unit": "termini-firstboot-preflight.service",
        "boot_log_path": "/var/log/termini/first-boot.json",
        "network_required": False,
    },
}
with open(manifest, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
print(f"[build] manifest -> {manifest}")
PY

  python3 "$VERIFY_MANIFEST" --lock "$LOCK" --schema "$SCHEMA" --manifest "$manifest"
  echo "[build] artifact=$artifact size=$size sha256=$sha"
  echo "[build] alias=$ALIAS version=$VERSION immutable_tag=$immutable_tag"
}

if [ "$MODE" = "check" ]; then
  check_recipe
else
  bake
fi
