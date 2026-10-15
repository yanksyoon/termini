#!/usr/bin/env bash
# Capture the OFFLINE verification evidence for the inference-surface split
# (kanban t_aa7a70f9). No GCP: everything here runs from the checkout.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../../.."   # repo root
RENDER="$(mktemp -d -t termini-render-XXXXXX)"

echo "################################################################################"
echo "# t_aa7a70f9 — inference surface split: OFFLINE verification"
echo "# commit under test: $(git rev-parse HEAD)"
echo "#   (this evidence file is committed AFTER the run, so the sha above is the"
echo "#    revision the code was verified at; compare with 'git diff <sha> -- .' )"
echo "# tree:   $(git rev-parse HEAD^{tree})"
echo "# dirty:  $(git status --porcelain | wc -l) uncommitted path(s) at capture time"
echo "# date:   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "# host:   $(uname -srm)"
echo "################################################################################"
echo
echo "### 1. the split's own test file"
echo "\$ python3 -m unittest tests.test_inference_surfaces -v"
python3 -m unittest tests.test_inference_surfaces -v 2>&1 | grep -vE '^\{' | tail -25
echo
echo "### 2. the surfaces' renderer, run with no privileges (--render-dir)"
echo "\$ bash infra/inference/setup-inference-surfaces.sh --render-dir \$TMP"
bash infra/inference/setup-inference-surfaces.sh --render-dir "$RENDER" 2>&1
echo
echo "### 2a. rendered provider env file (what the controller's listener reads)"
echo "\$ cat \$TMP/etc/termini/inference-proxy.env"
cat "$RENDER/etc/termini/inference-proxy.env"
echo
echo "### 2b. rendered controller drop-in (where the diagnosis goes)"
echo "\$ cat \$TMP/etc/systemd/system/termini-controller.service.d/zz-inference-surfaces.conf"
cat "$RENDER/etc/systemd/system/termini-controller.service.d/zz-inference-surfaces.conf"
echo
echo "### 2c. rendered provider unit"
echo "\$ cat \$TMP/etc/systemd/system/termini-inference-provider.service"
cat "$RENDER/etc/systemd/system/termini-inference-provider.service"
echo
echo "### 2d. the rendered proxy is the committed proxy, byte for byte"
echo "\$ cmp infra/inference/proxy.py \$TMP/opt/termini/inference/proxy.py"
if cmp infra/inference/proxy.py "$RENDER/opt/termini/inference/proxy.py"; then
  echo "identical ($(sha256sum < infra/inference/proxy.py | cut -d' ' -f1))"
fi
echo
echo "### 3. Node C's shipped first-boot default is local (rendered user-data fragment)"
echo "\$ INFERENCE_BACKEND=local infra/gcp/cloud-init/node-c-serve-block.sh --write-files \\"
echo "      | grep BACKEND=   (env: the pins from infra/gcp/variables.tf)"
LLAMACPP_COMMIT=7fe450e19305b828c199d602c23a8337aaa1f03b \
MODEL_URL=https://storage.googleapis.com/termini-510609-models/gemma-3-4b-it-gguf/gemma-3-4b-it-Q4_K_M.gguf \
MODEL_FILE=gemma-3-4b-it-Q4_K_M.gguf \
MODEL_SHA256=04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19 \
MODEL_ALIAS=gemma PORT=8080 \
  infra/gcp/cloud-init/node-c-serve-block.sh --write-files 2>/dev/null \
  | grep -nE 'BACKEND="|install-inference-proxy.sh|--backend' | head -10
echo
echo "### 3a. the installer's own default (no --backend given)"
grep -n 'BACKEND="\${BACKEND:-' infra/inference/install-inference-proxy.sh
echo
echo "### 4. the pre-split script refuses to run without --allow-unsplit"
echo "\$ bash infra/cloudrun/setup-operator-node-c.sh ; echo exit=\$?"
bash infra/cloudrun/setup-operator-node-c.sh; echo "exit=$?"
echo
echo "### 5. the whole unit suite (this checkout)"
echo "\$ python3 -m unittest discover -s tests"
python3 -m unittest discover -s tests 2>&1 | tail -4
echo
echo "### 6. repo checks"
echo "\$ python3 scripts/check-doc-ulids.py"
python3 scripts/check-doc-ulids.py
echo "\$ python3 scripts/check-readme-links.py"
python3 scripts/check-readme-links.py
echo "\$ shellcheck -S warning <changed shell scripts>"
shellcheck -S warning \
  infra/inference/setup-inference-surfaces.sh \
  infra/inference/install-inference-proxy.sh \
  infra/cloudrun/setup-operator-node-c.sh \
  infra/gcp/cloud-init/node-c-serve-block.sh \
  infra/gcp/cloud-init/llama-serve.sh \
  infra/gcp/verify-cloud-init.sh \
  infra/gcp/provision.sh \
  infra/gcp/tools/raise-standing-fleet.sh \
  && echo "shellcheck: clean (rc=0)"
echo
echo "### DONE"
