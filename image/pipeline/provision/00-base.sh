#!/usr/bin/env bash
# 00-base.sh — base OS packages for the Termini pre-baked node image.
# Runs during the bake (Packer shell provisioner / distrobuilder `run`), as root.
# Idempotent: safe to re-run.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  gnupg \
  jq \
  chrony \
  nftables \
  openssh-server \
  python3 \
  python3-venv \
  python3-pip \
  ruby-full \
  locales \
  tzdata \
  zstd \
  xz-utils \
  build-essential \
  cmake \
  git

apt-get clean
rm -rf /var/lib/apt/lists/*

# Deterministic locale (image builds must not depend on the build host's locale).
sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
locale-gen en_US.UTF-8

install -d -m 0755 /opt/termini /etc/termini /var/lib/termini
echo "[provision:00-base] base packages installed"
