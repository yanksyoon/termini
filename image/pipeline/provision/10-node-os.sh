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

# ---------------------------------------------------------------------------
# Initialise LXD. Installing the snap is NOT enough: without `lxd init` there is
# no daemon serving the API and no storage pool, so nothing can be launched and
# the operator's executor has no target. Measured on a node booted from the
# previous image: `lxc`/`lxd` binaries present, NOTHING listening on :8443,
# `curl https://127.0.0.1:8443/1.0` -> no response, and `lxc list` answered
# "Installing LXD snap, please be patient ... run lxd init". That missing step is
# why the deployed executor had to be pointed at a stub (chaos/lxd_stub.py).
#
# This also has to come BEFORE the image import below: `lxc image import`
# requires an initialised LXD, so the previous ordering silently had nothing to
# import into.
# ---------------------------------------------------------------------------
for _ in $(seq 1 30); do lxc info >/dev/null 2>&1 && break; sleep 2; done

if ! lxc info >/dev/null 2>&1; then
  cat <<'PRESEED' | lxd init --preseed
config: {}
networks:
- config:
    ipv4.address: auto
    ipv4.nat: true
  description: termini default bridge
  name: lxdbr0
  type: bridge
storage_pools:
- config: {}
  description: termini default pool
  name: default
  driver: dir
profiles:
- config: {}
  description: termini default profile
  devices:
    eth0:
      name: eth0
      network: lxdbr0
      type: nic
    root:
      path: /
      pool: default
      type: disk
  name: default
PRESEED
fi

# Serve the LXD API on the underlay. 8443 is the port the node contract and the
# GCP firewall rule expect (infra/gcp/README.md), and the port the remediation
# executor PUTs to (/1.0/instances/<id>/state).
snap set lxd core.https_address=":8443" || true
systemctl enable snap.lxd.daemon.service 2>/dev/null || true
snap restart lxd || true
for _ in $(seq 1 30); do
  curl -sk --max-time 2 https://127.0.0.1:8443/1.0 >/dev/null 2>&1 && break
  sleep 2
done
if ! curl -sk --max-time 2 https://127.0.0.1:8443/1.0 >/dev/null 2>&1; then
  echo "[provision:10-node-os] WARNING: LXD API is not answering on :8443 after init" >&2
fi

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
