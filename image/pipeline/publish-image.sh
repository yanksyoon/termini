#!/usr/bin/env bash
# publish-image.sh — publish a baked artifact under the stable alias
# `agentic-os/w3` with a version tag and a checksum, and record the release.
#
# Two distribution paths (D3): the demo preloads the artifact into every node's
# local LXD image store (no registry pull at boot); a registry is the day-2 path.
set -euo pipefail

ALIAS="${TERMINI_IMAGE_ALIAS:-agentic-os/w3}"
ARTIFACT="${1:-}"
MANIFEST="${2:-}"

usage() {
  cat <<'USAGE'
usage: publish-image.sh <artifact.(qcow2|tar.gz)> [image-manifest.json]
env:   TERMINI_IMAGE_ALIAS (default agentic-os/w3), TERMINI_REGISTRY (optional)
USAGE
}

if [ -z "$ARTIFACT" ] || [ "$ARTIFACT" = "-h" ] || [ "$ARTIFACT" = "--help" ]; then
  usage; exit 2
fi
[ -f "$ARTIFACT" ] || { echo "FATAL: artifact not found: $ARTIFACT" >&2; exit 1; }

sha="$(sha256sum "$ARTIFACT" | awk '{print $1}')"
size="$(stat -c %s "$ARTIFACT")"
printf '%s  %s\n' "$sha" "$(basename "$ARTIFACT")" > "$ARTIFACT.sha256"

version="unversioned"
variant="compute"
if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
  version="$(jq -r '.version' "$MANIFEST")"
  variant="$(jq -r '.variant' "$MANIFEST")"
else
  MANIFEST=""
fi
immutable_tag="${ALIAS//\//-}-${version}+${sha:0:12}"

echo "[publish] artifact=$(basename "$ARTIFACT") size=$size sha256=$sha"
echo "[publish] alias=$ALIAS version=$version variant=$variant immutable_tag=$immutable_tag"

# Import into the local LXD image store under the stable alias (offline target).
if command -v lxc >/dev/null 2>&1; then
  lxc image import "$ARTIFACT" --alias "$ALIAS" --alias "$immutable_tag"
  lxc image list "$ALIAS"
else
  echo "[publish] lxc not present on this host; skipping local import."
  echo "[publish] downstream reference: lxc launch ${ALIAS} <name>  (after preload)"
fi

# Day-2: push to a registry when one is configured.
if [ -n "${TERMINI_REGISTRY:-}" ]; then
  lxc image copy "$ARTIFACT" "$TERMINI_REGISTRY:" --alias "$ALIAS" --public
  echo "[publish] copied to registry $TERMINI_REGISTRY"
fi

record="$(dirname "$ARTIFACT")/published.json"
jq -n \
  --arg alias "$ALIAS" --arg tag "$immutable_tag" --arg version "$version" \
  --arg variant "$variant" --arg artifact "$(basename "$ARTIFACT")" \
  --arg sha256 "$sha" --argjson size "$size" \
  '{alias:$alias, immutable_tag:$tag, version:$version, variant:$variant,
    artifact:$artifact, sha256:$sha256, size_bytes:$size}' > "$record"
echo "[publish] release record -> $record"
