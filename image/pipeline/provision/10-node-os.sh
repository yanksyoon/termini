#!/usr/bin/env bash
# 10-node-os.sh — LXD snap (pinned channel) + offline-preloaded workload images.
# Bakes the hypervisor so that first boot never contacts a registry (D3).
set -euo pipefail

LXD_CHANNEL="${LXD_CHANNEL:-5.21/stable}"
IMAGE_STAGE="${IMAGE_STAGE:-/var/lib/termini/images-staged}"

snap install lxd --channel="$LXD_CHANNEL"
# Pin the revision that exists at *build* time; do not auto-refresh at boot.
snap refresh lxd --channel="$LXD_CHANNEL"

lxd_rev="$(snap list lxd | awk 'NR==2 {print $3}')"
install -d -m 0755 /etc/termini
printf '%s\n' "channel=${LXD_CHANNEL}" > /etc/termini/lxd-snap.txt
printf '%s\n' "revision=${lxd_rev}" >> /etc/termini/lxd-snap.txt
snap refresh --hold lxd || true

# Preload workload image tarballs into the local LXD image store.
install -d -m 0755 /var/lib/termini/images
if [ -d "$IMAGE_STAGE" ]; then
  for tarball in "$IMAGE_STAGE"/*.tar.gz "$IMAGE_STAGE"/*.tar; do
    [ -e "$tarball" ] || continue
    alias_name="$(basename "$tarball")"
    alias_name="${alias_name%.tar.gz}"
    alias_name="${alias_name%.tar}"
    lxc image import "$tarball" --alias "$alias_name"
  done
fi

echo "[provision:10-node-os] lxd channel=${LXD_CHANNEL} revision=${lxd_rev}"
