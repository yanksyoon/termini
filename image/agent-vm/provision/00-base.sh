#!/usr/bin/env bash
# 00-base.sh — agent VM base layer (agent-runtime.md §3.2).
# Minimal Ubuntu 24.04 userland the runtime needs. Everything here is resolved at
# BAKE time; first boot installs nothing.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates \
  chrony \
  curl \
  xz-utils \
  jq \
  git \
  python3.12 \
  python3.12-venv \
  python3.12-dev \
  systemd \
  nftables \
  openssh-server \
  logrotate \
  procps \
  iproute2 \
  less

# No unattended upgrades: the image must never try to fetch packages on its own.
systemctl disable --now unattended-upgrades.service 2>/dev/null || true
rm -f /etc/apt/apt.conf.d/20auto-upgrades
printf '%s\n' \
  'APT::Periodic::Enable "0";' \
  'Acquire::Retries "0";' \
  > /etc/apt/apt.conf.d/99termini-offline

echo "[provision:00-base] base packages baked"
