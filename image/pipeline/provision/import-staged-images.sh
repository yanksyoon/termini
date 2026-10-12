#!/usr/bin/env bash
# import-staged-images.sh — import the pre-staged WORKLOAD image tarballs into a
# node's LOCAL LXD image store, under every alias the controller/demo uses.
#
# Why this exists (D3 — "first boot never contacts a registry")
# ------------------------------------------------------------
# The node image is baked so a fresh node installs/downloads nothing at boot.
# That includes the *workload* image: the guest `agent-vm/<version>` the
# controller materialises instances from (docs/contracts/agent-runtime.md §3.1,
# §4.2: `source.alias = "agent-vm/2026.10.0"`) and that the demo's warm/placement
# layer launches per-node as `agent-os/w4`. Nothing is pulled at boot, so the
# image must already be resident in the local LXD store by the time the node is
# up. This step imports it from an on-disk tarball the bake pre-staged.
#
# The import is driven by a STAGE MANIFEST, not by the tarball's file name: an
# alias like `agent-vm/2026.10.0` contains a slash and so cannot be encoded in a
# file name. The manifest (written by tools/stage_workload_image.sh) is
# `$IMAGE_STAGE/images.json`:
#
#   {"kind": "termini.image_stage",
#    "images": [{"file": "agent-vm-2026.10.0-<fp12>.tar.gz",
#                "aliases": ["agent-vm/2026.10.0", "agent-os/w4"],
#                "version": "2026.10.0",
#                "sha256": "<hex>"}]}
#
# LOUD BY DESIGN
# --------------
# A node image with no workload image is broken — the operator's
# `create_instance` has no target and the demo cannot launch. Historically this
# step keyed the alias off the tarball basename inside a `if [ -d ... ]` guard,
# so an unpopulated stage made it import ZERO images and still exit 0: the bake
# reported success while shipping an image with nothing to launch. This script
# therefore FAILS on a missing/empty stage or a digest mismatch instead of
# silently importing nothing.
#
# Set TERMINI_ALLOW_MISSING_WORKLOAD_IMAGE=1 to downgrade the *empty-stage* case
# to a warning — only for a deliberate non-demo bake that ships no workload
# image. A corrupt manifest or a digest mismatch always fails.
#
# env: IMAGE_STAGE (default /var/lib/termini/images-staged)
#      TERMINI_ALLOW_MISSING_WORKLOAD_IMAGE (default 0)
set -euo pipefail

IMAGE_STAGE="${IMAGE_STAGE:-/var/lib/termini/images-staged}"
MANIFEST="$IMAGE_STAGE/images.json"
ALLOW_MISSING="${TERMINI_ALLOW_MISSING_WORKLOAD_IMAGE:-0}"

log() { printf '[provision:import-images] %s\n' "$*" >&2; }
die() { printf '[provision:import-images] FATAL: %s\n' "$*" >&2; exit 1; }

# --- stage present? --------------------------------------------------------
if [ ! -d "$IMAGE_STAGE" ] || [ ! -f "$MANIFEST" ]; then
  if [ "$ALLOW_MISSING" = "1" ]; then
    log "WARNING: no workload image staged ($IMAGE_STAGE / $MANIFEST); the node"
    log "WARNING: image will ship WITHOUT a launchable workload image."
    exit 0
  fi
  printf '[provision:import-images] FATAL: the workload image stage is missing.\n' >&2
  printf '[provision:import-images]   expected dir : %s\n' "$IMAGE_STAGE" >&2
  printf '[provision:import-images]   expected file: %s\n' "$MANIFEST" >&2
  printf '[provision:import-images] The node image would ship NO workload image, so the\n' >&2
  printf '[provision:import-images] controller has nothing to create instances from (D3).\n' >&2
  printf '[provision:import-images] Stage one first:\n' >&2
  printf '[provision:import-images]   image/pipeline/tools/stage_workload_image.sh --stage %s\n' "$IMAGE_STAGE" >&2
  printf '[provision:import-images] (set TERMINI_ALLOW_MISSING_WORKLOAD_IMAGE=1 only for a\n' >&2
  printf '[provision:import-images]  deliberate non-demo bake that ships no workload image.)\n' >&2
  exit 1
fi

command -v lxc >/dev/null 2>&1 || die "lxc is not on PATH; cannot import staged workload images"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; cannot read $MANIFEST"

# --- read the manifest: one TAB record per image (file, sha256, aliases) ----
records="$(python3 - "$MANIFEST" <<'PY'
import json, sys, pathlib

p = pathlib.Path(sys.argv[1])
try:
    doc = json.loads(p.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001 - surface any parse error verbatim
    print(f"{p}: not valid JSON: {exc}", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(doc, dict) or doc.get("kind") != "termini.image_stage":
    print(f"{p}: 'kind' must be 'termini.image_stage'", file=sys.stderr)
    raise SystemExit(2)
images = doc.get("images")
if not isinstance(images, list) or not images:
    print(f"{p}: 'images' must be a non-empty list", file=sys.stderr)
    raise SystemExit(2)
for idx, item in enumerate(images):
    if not isinstance(item, dict):
        print(f"{p}: images[{idx}] must be an object", file=sys.stderr)
        raise SystemExit(2)
    name = item.get("file")
    aliases = item.get("aliases")
    digest = item.get("sha256", "")
    if not isinstance(name, str) or not name:
        print(f"{p}: images[{idx}].file is required", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(aliases, list) or not aliases or not all(
        isinstance(a, str) and a for a in aliases
    ):
        print(f"{p}: images[{idx}].aliases must be a non-empty list of strings",
              file=sys.stderr)
        raise SystemExit(2)
    if digest and not isinstance(digest, str):
        print(f"{p}: images[{idx}].sha256 must be a string", file=sys.stderr)
        raise SystemExit(2)
    print(f"{name}\t{digest}\t{','.join(aliases)}")
PY
)" || die "could not read the stage manifest $MANIFEST (see the error above)"

# --- import each staged tarball under every declared alias ------------------
imported=0
while IFS=$'\t' read -r file sha aliases; do
  [ -n "$file" ] || continue
  tarball="$IMAGE_STAGE/$file"
  [ -f "$tarball" ] || die "staged tarball '$tarball' (listed in $MANIFEST) does not exist"
  if [ -n "$sha" ]; then
    got="$(sha256sum "$tarball" | awk '{print $1}')"
    [ "$got" = "$sha" ] || die "staged tarball '$tarball' sha256=$got != manifest sha256=$sha"
  fi
  IFS=',' read -r -a alias_list <<< "$aliases"
  import_args=()
  for a in "${alias_list[@]}"; do import_args+=(--alias "$a"); done
  log "importing $file as [${aliases//,/ }]"
  lxc image import "$tarball" "${import_args[@]}"
  imported=$((imported + 1))
done <<< "$records"

[ "$imported" -gt 0 ] || die "$MANIFEST declared no importable images"

log "imported $imported workload image(s) into the local LXD store from $IMAGE_STAGE"
