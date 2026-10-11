#!/usr/bin/env bash
# Deploy Gemma on Cloud Run GPU (NVIDIA L4) in asia-southeast1, serving from
# weights mounted out of GCS. See README.md for why the mount matters.
#
# Usage: ./deploy-gemma.sh [service_name] [model_prefix_in_bucket]
set -euo pipefail

SERVICE="${1:-termini-gemma}"
PREFIX="${2:-gemma-3-4b-it}"
PROJECT="${TERMINI_PROJECT:-termini-510609}"
REGION="${TERMINI_REGION:-asia-southeast1}"
BUCKET="${TERMINI_MODELS_BUCKET:-termini-510609-models}"
IMAGE="us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:gemma4"

# Startup probe budget: 30s initial + 100 x 15s = ~25.5 min. Generous because
# vLLM init plus weight load is minutes, not seconds; a tight probe is what
# killed the first attempt.
STARTUP_PROBE="tcpSocket.port=8080,initialDelaySeconds=30,failureThreshold=100,timeoutSeconds=10,periodSeconds=15"

echo "==> deploying $SERVICE ($REGION, 1x L4) from gs://$BUCKET/$PREFIX"
gcloud beta run deploy "$SERVICE" \
  --image "$IMAGE" \
  --project "$PROJECT" --region "$REGION" \
  --execution-environment gen2 \
  --no-allow-unauthenticated \
  --cpu 8 --memory 32Gi \
  --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy \
  --no-cpu-throttling \
  --max-instances 1 --concurrency 4 --timeout 600 \
  --command "vllm" \
  --args="serve,/models/$PREFIX,--served-model-name=gemma,--max-num-seqs=4,--gpu-memory-utilization=0.90,--dtype=bfloat16,--port=8080,--host=0.0.0.0" \
  --add-volume="name=models,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount="volume=models,mount-path=/models" \
  --startup-probe "$STARTUP_PROBE" \
  --quiet

echo "==> service URL:"
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format="value(status.url)"
