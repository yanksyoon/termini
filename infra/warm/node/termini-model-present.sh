#!/bin/sh
# termini-model-present — ExecCondition for llama-cpp.service.
# Succeeds only when the pinned GGUF is present on local disk, so the compute
# nodes (which have no model volume) skip the inference unit cleanly instead of
# crash-looping. Never downloads anything.
set -eu
. /etc/termini/model.env
[ -f "${MODEL_DIR}/${MODEL_FILE}" ]
