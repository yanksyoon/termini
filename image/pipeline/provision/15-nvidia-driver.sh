#!/usr/bin/env bash
# 15-nvidia-driver.sh — bake the NVIDIA kernel driver stack into the Node C (gpu)
# image variant.
#
# Contract: docs/contracts/infra-decisions.md D3 ("the NVIDIA driver stack on the
# Node C image variant") and docs/contracts/node-bootstrap.md §3.1 ("Node C
# variant only: the NVIDIA driver stack (nvidia-driver-*) + nvidia-smi, installed
# and loaded at build time"). Nothing here runs at boot — first boot only loads
# the already-baked modules (D3).
#
# No-op on the compute variant. Runs before 20-gpu-cuda.sh, which adds the CUDA
# *runtime* libraries that a CUDA llama.cpp build links against.
#
# Environment:
#   TERMINI_VARIANT        compute|gpu      (default compute)
#   TERMINI_DRIVER_SOURCE  baked|image     (default baked)
#                          baked = install the pinned datacenter branch from the
#                                  NVIDIA CUDA apt repo (self-contained image);
#                          image = assert the base image already ships the stack
#                                  (e.g. a GCP GPU-optimised image) and install
#                                  nothing.
#   NVIDIA_DRIVER_BRANCH   e.g. 550        (default 550; a pin, never "latest")
set -euo pipefail

VARIANT="${TERMINI_VARIANT:-compute}"
if [ "$VARIANT" != "gpu" ]; then
  echo "[provision:15-nvidia-driver] variant=${VARIANT}; skipping NVIDIA driver"
  exit 0
fi

DRIVER_SOURCE="${TERMINI_DRIVER_SOURCE:-baked}"
DRIVER_BRANCH="${NVIDIA_DRIVER_BRANCH:-550}"
CUDA_REPO_DISTRO="${CUDA_REPO_DISTRO:-ubuntu2404}"
CUDA_REPO_ARCH="${CUDA_REPO_ARCH:-x86_64}"
KEYRING="/usr/share/keyrings/cuda-archive-keyring.gpg"

install -d -m 0755 /etc/termini
RECORD="/etc/termini/nvidia-driver.txt"
: >"$RECORD"

record() { printf '%s=%s\n' "$1" "$2" >>"$RECORD"; }

record source "$DRIVER_SOURCE"
record branch "$DRIVER_BRANCH"

smi_path="$(command -v nvidia-smi 2>/dev/null || true)"

if [ "$DRIVER_SOURCE" = "image" ]; then
  # The base image is responsible for the driver; we only assert it is really
  # there, because a gpu-variant image without nvidia-smi is a broken bake.
  if [ -z "$smi_path" ]; then
    echo "FATAL: TERMINI_DRIVER_SOURCE=image but nvidia-smi is absent from the base image" >&2
    exit 1
  fi
  echo "[provision:15-nvidia-driver] driver supplied by base image: ${smi_path}"
else
  export DEBIAN_FRONTEND=noninteractive
  if [ ! -f "$KEYRING" ]; then
    curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/${CUDA_REPO_DISTRO}/${CUDA_REPO_ARCH}/cuda-keyring_1.1-1_all.deb" \
      -o /tmp/cuda-keyring.deb
    dpkg -i /tmp/cuda-keyring.deb
    rm -f /tmp/cuda-keyring.deb
  fi
  apt-get update -qq
  # cuda-drivers-<branch> pulls the datacenter driver + nvidia-smi + libnvidia-*.
  apt-get install -y --no-install-recommends "cuda-drivers-${DRIVER_BRANCH}"
  apt-get clean
  rm -rf /var/lib/apt/lists/*
  smi_path="$(command -v nvidia-smi 2>/dev/null || true)"
  if [ -z "$smi_path" ]; then
    echo "FATAL: cuda-drivers-${DRIVER_BRANCH} did not provide nvidia-smi" >&2
    exit 1
  fi
fi

# Keep the open-source nouveau driver away from the NVIDIA device.
cat >/etc/modprobe.d/termini-blacklist-nouveau.conf <<'CONF'
# Termini Node C (GPU): the NVIDIA datacenter driver owns the device.
blacklist nouveau
options nouveau modeset=0
CONF

# Load the NVIDIA modules at boot so the node comes up GPU-ready with no boot-time
# install (D3). nvidia_uvm is what CUDA runtime calls (incl. llama.cpp) need.
cat >/etc/modules-load.d/termini-nvidia.conf <<'CONF'
nvidia
nvidia_modeset
nvidia_uvm
CONF

# Driver version: nvidia-smi is the ground truth (it reports the loaded driver).
driver_version=""
if [ -n "$smi_path" ]; then
  driver_version="$("$smi_path" --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)"
  if [ -z "$driver_version" ]; then
    driver_version="$("$smi_path" --version 2>/dev/null | sed -n 's/.*Driver Version: *\([0-9.]*\).*/\1/p' | head -n1 || true)"
  fi
fi
record smi_path "${smi_path:-none}"
record driver_version "${driver_version:-unknown}"

# Rebuild the initramfs so the nouveau blacklist is in effect before first GPU touch.
if command -v update-initramfs >/dev/null 2>&1; then
  update-initramfs -u >/dev/null 2>&1 || \
    echo "[provision:15-nvidia-driver] warn: update-initramfs failed (non-fatal)"
fi

# Best-effort module load. A build VM usually has no GPU attached and the running
# kernel may differ from the target, so a failure here is not a bake failure: the
# live acceptance is verify-gpu.sh on the node, and node-bootstrap.md §9 keeps a
# driverless node registering (gpus: [], DEGRADED) rather than failing the bake.
if ! lsmod 2>/dev/null | grep -q '^nvidia '; then
  modprobe nvidia >/dev/null 2>&1 || \
    echo "[provision:15-nvidia-driver] note: nvidia module not loadable at bake time (no GPU/kernel match in build env)"
fi

# Record whatever GPU (if any) is visible during the bake — evidence, not a gate.
if [ -n "$smi_path" ] && "$smi_path" -L >/tmp/termini-smi-l.txt 2>/dev/null && [ -s /tmp/termini-smi-l.txt ]; then
  while IFS= read -r line; do record gpu_at_bake "$line"; done </tmp/termini-smi-l.txt
else
  record gpu_at_bake "none-visible-at-bake"
fi
rm -f /tmp/termini-smi-l.txt

echo "[provision:15-nvidia-driver] source=${DRIVER_SOURCE} branch=${DRIVER_BRANCH} smi=${smi_path:-none} driver=${driver_version:-unknown}"
