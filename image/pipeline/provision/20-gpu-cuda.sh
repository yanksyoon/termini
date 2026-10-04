#!/usr/bin/env bash
# 20-gpu-cuda.sh — CUDA/cuDNN *runtime* libraries for the Node C (gpu) variant.
#
# Scope: what a CUDA llama.cpp build links against at runtime. The NVIDIA kernel
# driver itself is provided by the GPU base image / GCP accelerator driver, not
# baked here (a driver is kernel-coupled and must match the running kernel).
# No-op on the compute variant.
set -euo pipefail

VARIANT="${TERMINI_VARIANT:-compute}"
if [ "$VARIANT" != "gpu" ]; then
  echo "[provision:20-gpu-cuda] variant=${VARIANT}; skipping CUDA runtime"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
CUDA_PKG_SUFFIX="${CUDA_PKG_SUFFIX:-12-4}"

install -d -m 0755 /usr/share/keyrings
curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
  -o /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb
rm -f /tmp/cuda-keyring.deb
apt-get update -qq

# Runtime-only set (no nvcc / toolkit compiler): cublas + cudart + nvrtc + cuDNN 9.
apt-get install -y --no-install-recommends \
  "cuda-cudart-${CUDA_PKG_SUFFIX}" \
  "libcublas-${CUDA_PKG_SUFFIX}" \
  "cuda-nvrtc-${CUDA_PKG_SUFFIX}" \
  libcudnn9-cuda-12
apt-get clean
rm -rf /var/lib/apt/lists/*

install -d -m 0755 /etc/termini
printf '%s\n' "cuda_pkg_suffix=${CUDA_PKG_SUFFIX}" > /etc/termini/cuda-runtime.txt
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader >> /etc/termini/cuda-runtime.txt || true
else
  echo "WARN: nvidia-smi absent at bake time (driver supplied by base image / GCP)" \
    >> /etc/termini/cuda-runtime.txt
fi
echo "[provision:20-gpu-cuda] CUDA runtime ${CUDA_PKG_SUFFIX} installed"
