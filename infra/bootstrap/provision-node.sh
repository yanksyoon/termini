#!/usr/bin/env bash
# provision-node.sh — create/configure a Termini node from the pre-baked image
# alias using the cloud-init bootstrap user-data (task t_066b6e3d).
#
#   provision-node.sh NAME ROLE TOKEN
#
# env / flags:
#   TERMINI_IMAGE_ALIAS   image alias to create from   (default agentic-os/w3)
#   TERMINI_IMAGE_VERSION image version label          (default 2026.10.0)
#   CONTROLLER_URL        controller base URL           (required)
#   TEAM_SSH_PUBKEY_FILE  file holding the team SSH public key (required)
#   CONTROLLER_CA_PUBKEY  controller SSH CA public key  (optional)
#   ROLE_HINT, UNDERLAY_IP, UNDERLAY_GW, SEED_IP, TELEMETRY_INTERVAL_MS
#
# Idempotent: re-running re-renders the user-data on the existing instance; it
# never deletes an instance. Creating from the alias and writing the config is a
# single, repeatable step (the node's own daemon is what makes boot idempotent).
set -eu
# shellcheck source=/dev/null
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME="${1:?usage: provision-node.sh NAME ROLE TOKEN}"
ROLE="${2:?usage: provision-node.sh NAME ROLE TOKEN}"
TOKEN="${3:?usage: provision-node.sh NAME ROLE TOKEN}"

: "${CONTROLLER_URL:?CONTROLLER_URL is required (controller base URL)}"
: "${TEAM_SSH_PUBKEY_FILE:?TEAM_SSH_PUBKEY_FILE is required (team SSH public key file)}"
[ -s "$TEAM_SSH_PUBKEY_FILE" ] || { echo "team key file empty/missing: $TEAM_SSH_PUBKEY_FILE" >&2; exit 1; }

TERMINI_IMAGE_ALIAS="${TERMINI_IMAGE_ALIAS:-agentic-os/w3}"
TERMINI_IMAGE_VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"

command -v lxc     >/dev/null || { echo "lxc not found" >&2; exit 1; }
command -v envsubst >/dev/null || { echo "envsubst not found" >&2; exit 1; }

lxc image list "$TERMINI_IMAGE_ALIAS" --format csv -c f 2>/dev/null | grep -q . \
  || { echo "image alias '$TERMINI_IMAGE_ALIAS' is not present in the local store" >&2; exit 1; }

if lxc info "$NAME" >/dev/null 2>&1; then
  echo "[provision] node $NAME already exists; refreshing cloud-init user-data" >&2
else
  echo "[provision] creating $NAME from alias $TERMINI_IMAGE_ALIAS" >&2
  lxc init "$TERMINI_IMAGE_ALIAS" "$NAME" </dev/null >/dev/null
fi

# Refresh the canonical bootstrap runtime (daemon + systemd unit) into the fresh
# node before first boot. The image pipeline bakes these into the alias
# (image/pipeline); this repo-side push keeps a node booted from a pre-existing
# image on the canonical runtime. It is a local file copy — no network fetch —
# and is idempotent. Set REFRESH_RUNTIME=0 to verify a pure baked image instead.
if [ "${REFRESH_RUNTIME:-1}" = "1" ]; then
  lxc file push "$HERE/node/termini-bootstrap.py" "$NAME/usr/local/bin/termini-bootstrap" >/dev/null
  lxc file push "$HERE/node/termini-bootstrap.service" "$NAME/etc/systemd/system/termini-bootstrap.service" >/dev/null
  echo "[provision] $NAME: canonical termini-bootstrap runtime pushed" >&2
fi

UD="$(controller_url="$CONTROLLER_URL" node_name="$NAME" enrollment_token="$TOKEN" \
      team_ssh_pubkey="$(cat "$TEAM_SSH_PUBKEY_FILE")" role_hint="$ROLE" \
      underlay_ip="${UNDERLAY_IP:-10.10.0.0/24}" underlay_gw="${UNDERLAY_GW:-10.10.0.1}" \
      seed_ip="${SEED_IP:-}" telemetry_interval_ms="${TELEMETRY_INTERVAL_MS:-1000}" \
      controller_ca_pubkey="${CONTROLLER_CA_PUBKEY:-}" \
      image_alias="$TERMINI_IMAGE_ALIAS" image_version="$TERMINI_IMAGE_VERSION" \
      "$HERE/render-cloud-init.sh")"

lxc config set "$NAME" cloud-init.user-data "$UD" >/dev/null
echo "[provision] $NAME configured (cloud-init user-data $(printf '%s' "$UD" | wc -c) bytes)" >&2
