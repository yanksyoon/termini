#!/usr/bin/env bash
# stage_workload_image.sh — stage the WORKLOAD (agent VM) image into a node-image
# bake's staging directory, together with the alias manifest the bake's import
# step reads.
#
# This is the "earlier step that populates the stage" the node image needs. The
# bake imports whatever is under $IMAGE_STAGE; nothing else in the pipeline put
# the workload image there, so a bake used to ship an image with nothing to
# launch. Run this on the host that holds the published workload image:
#
#   image/agent-vm/build-agent-image.sh    publishes `agent-vm/<version>` locally
#   infra/agent-vm/place-agent-image.sh    can also present it on a node
#
# Nothing is pulled from a registry: the image is exported from THIS host's
# local LXD image store (D3 — first boot never contacts a registry).
#
# Output, under $STAGE_DIR:
#   <alias-slug>-<version>-<fp12>.tar.gz     the exported LXD image tarball
#   <same>.sha256                            its digest sidecar
#   images.json                              the stage manifest (merged, idempotent)
#
# images.json shape (read by provision/import-staged-images.sh):
#   {"kind": "termini.image_stage",
#    "images": [{"file": "...", "aliases": ["agent-vm/2026.10.0", "agent-os/w4"],
#                "version": "2026.10.0", "sha256": "<hex>", "fingerprint": "<hex>"}]}
#
# Usage:
#   stage_workload_image.sh [--stage DIR] [--host-alias ALIAS]
#                           [--node-alias ALIAS] [--version V]
#                           [--no-node-alias] [-h|--help]
# env: TERMINI_STAGE_DIR, TERMINI_AGENT_HOST_IMAGE_ALIAS, TERMINI_NODE_IMAGE_ALIAS,
#      TERMINI_IMAGE_VERSION
#
#   --host-alias   image to export from the local store  (default agent-vm/<version>)
#   --node-alias   per-node launch alias to also register (default agent-os/w4)
#   --no-node-alias  register only the contract alias, not the demo's per-node one
set -euo pipefail

VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"
STAGE_DIR="${TERMINI_STAGE_DIR:-/var/lib/termini/images-staged}"
# The contract alias base (docs/contracts/agent-runtime.md §3.1: `agent-vm/<semver>`).
# build-image.sh --check asserts this constant so the stage tool can never drift
# from the alias the controller launches (source.alias in §4.2).
WORKLOAD_IMAGE_ALIAS_BASE="agent-vm"
HOST_ALIAS="${TERMINI_AGENT_HOST_IMAGE_ALIAS:-${WORKLOAD_IMAGE_ALIAS_BASE}/${VERSION}}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w4}"
WANT_NODE_ALIAS=1

usage() { sed -n '2,45p' "$0"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --stage) STAGE_DIR="$2"; shift 2 ;;
    --host-alias) HOST_ALIAS="$2"; shift 2 ;;
    --node-alias) NODE_ALIAS="$2"; shift 2 ;;
    --version) VERSION="$2"; HOST_ALIAS="${TERMINI_AGENT_HOST_IMAGE_ALIAS:-agent-vm/${VERSION}}"; shift 2 ;;
    --no-node-alias) WANT_NODE_ALIAS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '[stage-workload] %s\n' "$*" >&2; }
die() { printf '[stage-workload] FATAL: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

need lxc
need python3
need sha256sum
need awk

lxc info >/dev/null 2>&1 || die "this host's LXD is not initialised (run 'lxd init')"

fingerprint="$(lxc image info "$HOST_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}')"
[ -n "$fingerprint" ] || die "image alias '$HOST_ALIAS' does not resolve in this host's local LXD store"

# File name mirrors the warm layer's convention (infra/images/prewarm-images.sh):
# a slugged alias + fingerprint prefix, so a re-export of the same content is
# reused rather than duplicated.
slug="$(printf '%s' "$HOST_ALIAS" | tr '/ ' '--')"
file="${slug}-${fingerprint:0:12}.tar.gz"
install -d -m 0755 "$STAGE_DIR"

if [ -s "$STAGE_DIR/$file" ] && [ -s "$STAGE_DIR/$file.sha256" ] \
   && ( cd "$STAGE_DIR" && sha256sum -c "$file.sha256" >/dev/null 2>&1 ); then
  log "reusing already-staged $file (digest matches)"
  sha="$(awk '{print $1}' "$STAGE_DIR/$file.sha256")"
else
  log "exporting $HOST_ALIAS (fingerprint ${fingerprint:0:12}) -> $STAGE_DIR/$file"
  rm -f "$STAGE_DIR/$file" "$STAGE_DIR/$file.sha256"
  lxc image export "$HOST_ALIAS" "$STAGE_DIR/${file%.tar.gz}" >/dev/null
  [ -s "$STAGE_DIR/$file" ] || die "image export produced no tarball for $HOST_ALIAS"
  ( cd "$STAGE_DIR" && sha256sum "$file" > "$file.sha256" )
  sha="$(awk '{print $1}' "$STAGE_DIR/$file.sha256")"
fi

aliases="[\"$HOST_ALIAS\""
[ "$WANT_NODE_ALIAS" = 1 ] && aliases="$aliases, \"$NODE_ALIAS\""
aliases="$aliases]"

python3 - "$STAGE_DIR/images.json" "$file" "$sha" "$VERSION" "$fingerprint" "$aliases" <<'PY'
import json, os, sys

manifest, filename, sha, version, fingerprint, aliases_json = sys.argv[1:7]
aliases = json.loads(aliases_json)
doc = {"kind": "termini.image_stage", "images": []}
if os.path.exists(manifest) and os.path.getsize(manifest) > 0:
    try:
        with open(manifest, encoding="utf-8") as fh:
            existing = json.load(fh)
        if isinstance(existing, dict) and existing.get("kind") == "termini.image_stage" \
           and isinstance(existing.get("images"), list):
            doc = existing
    except (json.JSONDecodeError, OSError):
        # A malformed manifest is replaced — it will be validated at import time.
        pass

entry = {"file": filename, "aliases": aliases, "version": version,
         "sha256": sha, "fingerprint": fingerprint}
# Replace any prior entry for the same file (idempotent re-stage); keep the rest.
doc["images"] = [e for e in doc["images"] if not (isinstance(e, dict) and e.get("file") == filename)]
doc["images"].append(entry)
with open(manifest, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
print(f"[stage-workload] manifest -> {manifest}")
PY

log "staged $file (sha256 ${sha:0:12}) aliases=${aliases}"
log "done: stage dir $STAGE_DIR"
