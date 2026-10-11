#!/usr/bin/env bash
# Stage Gemma weights into GCS so Cloud Run does not pull them from the
# rate-limited public Hugging Face hub on every cold start.
#
# Usage: ./stage-weights.sh [hf_repo] [gcs_prefix]
#   defaults: unsloth/gemma-3-4b-it  gemma-3-4b-it
#
# unsloth/gemma-3-4b-it is an ungated mirror of google/gemma-3-4b-it (same
# weights). The google/* repos are gated=manual on HF, i.e. they need the Gemma
# licence accepted and a token; the mirror does not.
set -euo pipefail

REPO="${1:-unsloth/gemma-3-4b-it}"
PREFIX="${2:-gemma-3-4b-it}"
BUCKET="${TERMINI_MODELS_BUCKET:-gs://termini-510609-models}"
DEST="${TERMINI_WEIGHTS_DIR:-$HOME/.hermes/cache/scratch/gemma-weights}"

mkdir -p "$DEST"
echo "==> downloading $REPO into $DEST"
python3 - "$REPO" "$DEST" <<'PY'
import json, os, pathlib, sys, time, urllib.request

repo, dest = sys.argv[1], pathlib.Path(sys.argv[2])
with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}", timeout=60) as r:
    meta = json.load(r)

# Everything except GGUF quants; vLLM consumes the safetensors shard set.
want = [s["rfilename"] for s in meta["siblings"] if not s["rfilename"].endswith(".gguf")]
print(f"{len(want)} files to fetch")
t0 = time.time()
for i, name in enumerate(want, 1):
    out = dest / name
    if out.exists() and out.stat().st_size > 0:
        print(f"  [{i}/{len(want)}] cached {name}", flush=True)
        continue
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{out}.part"
    urllib.request.urlretrieve(f"https://huggingface.co/{repo}/resolve/main/{name}", tmp)
    os.replace(tmp, out)
    print(f"  [{i}/{len(want)}] {name} {out.stat().st_size/1e6:.1f} MB ({time.time()-t0:.0f}s)", flush=True)
PY

echo "==> uploading to $BUCKET/$PREFIX/"
gcloud storage cp -r "$DEST" "$BUCKET/$PREFIX" --quiet
echo "==> bucket now holds:"
gcloud storage ls "$BUCKET/$PREFIX/"
