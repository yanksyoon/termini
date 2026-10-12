#!/usr/bin/env bash
# bake-node-os.sh — the single-pass bake of a Termini node-OS golden VM.
#
# Runs ON the golden VM as root, in ONE pass, and is the artifact that makes the
# bake reproducible instead of hand-assembled. The orchestrator
# (../tools/bake-golden-node-image.sh) creates the VM, uploads this tree plus the
# workload-image tarball, runs this script, then stops the VM and captures the
# image into the `termini-node-os` family.
#
# What the image MUST contain, and why (each item is asserted at the end):
#
#   1. LXD installed AND initialised, API serving on :8443
#        - installing the snap is not initialising it; a node booted from an
#          un-initialised image has the `lxc` binary, no daemon, no pool, and
#          `lxc list` answers "run lxd init". The contract port is :8443
#          (infra/gcp/README.md, docs/contracts/node-bootstrap.md).
#   2. The WORKLOAD image the controller launches, in the local LXD store under
#      BOTH aliases the repo disagrees about:
#        - `agent-vm/<version>`  — the frozen contract
#          (docs/contracts/agent-runtime.md §3.1/§4.2 `source.alias`), and
#        - `agent-os/w4`         — the demo warm/placement layer's per-node alias.
#      It is placed through the bake's OWN code path — the stage tool exports it
#      and the import helper imports it under every alias the manifest declares —
#      NOT by hand-creating an alias over an unrelated cached image. The import
#      helper FAILS LOUDLY on an empty stage, so a bake that ships nothing cannot
#      report success.
#   3. The CURRENT node bootstrap daemon + unit (infra/bootstrap/node/…), which
#      reports the node's own LXD endpoint at registration (`machine.lxd.endpoint`)
#      so the controller can address one node among a fleet.
#   4. An offline sentinel + image version, so first boot installs/downloads
#      nothing (D3, docs/contracts/infra-decisions.md).
#
# ORDERING IS LOAD-BEARING (see image/pipeline/README.md and the golden-vm README):
#   install/init LXD -> put the workload image in the store -> stage -> import ->
#   install the daemon -> VERIFY -> de-instance-ify LAST.
# De-instancing removes the SSH host keys, so every diagnostic MUST run before it;
# ssh-keygen -A runs at the end so the shipped image does not carry a broken sshd.
#
# env:
#   BAKE_SRC            uploaded repo tree                (default /opt/termini/bake)
#   WORKLOAD_TARBALL    workload image tarball on the VM  (required)
#   TERMINI_IMAGE_VERSION   contract version              (default 2026.10.0)
#   IMAGE_STAGE         workload staging dir              (default /var/lib/termini/images-staged)
#   WORKLOAD_ALIAS      source alias in the local store   (default agent-vm/<version>)
#   LXD_CHANNEL         LXD snap channel                  (default 5.21/stable)
#   KEEP_STAGE=1        keep the staged tarball in the image (default: removed)
set -euo pipefail

BAKE_SRC="${BAKE_SRC:-/opt/termini/bake}"
WORKLOAD_TARBALL="${WORKLOAD_TARBALL:-}"
TERMINI_IMAGE_VERSION="${TERMINI_IMAGE_VERSION:-2026.10.0}"
IMAGE_STAGE="${IMAGE_STAGE:-/var/lib/termini/images-staged}"
WORKLOAD_ALIAS="${WORKLOAD_ALIAS:-agent-vm/${TERMINI_IMAGE_VERSION}}"
NODE_ALIAS="${TERMINI_NODE_IMAGE_ALIAS:-agent-os/w4}"
LXD_CHANNEL="${LXD_CHANNEL:-5.21/stable}"
KEEP_STAGE="${KEEP_STAGE:-0}"

PROVISION_DIR="$BAKE_SRC/image/pipeline/provision"
TOOLS_DIR="$BAKE_SRC/image/pipeline/tools"
NODE_DIR="$BAKE_SRC/infra/bootstrap/node"

log()  { printf '\n===== [bake] %s =====\n' "$*"; }
info() { printf '[bake] %s\n' "$*"; }
die()  { printf '[bake] FATAL: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "must run as root (sudo)"
[ -n "$WORKLOAD_TARBALL" ] || die "WORKLOAD_TARBALL is required (path to the workload image tarball on this VM)"
[ -s "$WORKLOAD_TARBALL" ] || die "workload tarball not found or empty: $WORKLOAD_TARBALL"
for f in \
  "$PROVISION_DIR/10-node-os.sh" "$PROVISION_DIR/import-staged-images.sh" \
  "$PROVISION_DIR/60-node-bootstrap.sh" \
  "$TOOLS_DIR/stage_workload_image.sh" \
  "$NODE_DIR/termini-bootstrap.py" "$NODE_DIR/termini-bootstrap.service"; do
  [ -f "$f" ] || die "required bake input missing: $f (is BAKE_SRC=$BAKE_SRC the uploaded repo tree?)"
done

export DEBIAN_FRONTEND=noninteractive
install -d -m 0755 /etc/termini /var/lib/termini /var/lib/termini/images

# ---------------------------------------------------------------------------
# 0. base packages the image contract needs (idempotent)
# ---------------------------------------------------------------------------
log "0. base packages"
apt-get update -y -q
apt-get install -y -q --no-install-recommends python3 jq curl ca-certificates cloud-init

# ---------------------------------------------------------------------------
# 1. LXD up: install the snap, initialise it, bind the API to :8443
#
#    This prelude is the LXD half of provision/10-node-os.sh, run first ONLY so
#    the stage tool (which needs a live, initialised LXD to export from) can run.
#    The real provisioner re-asserts all of it idempotently in step 4.
#
#    Use `lxc config set core.https_address`, NOT `snap set lxd ...`: the snap
#    form is REJECTED ("invalid option name: https_address"), and a bake that
#    swallows that failure ships an image whose API is unbound.
# ---------------------------------------------------------------------------
log "1. LXD up + API bound on :8443"
if ! command -v lxc >/dev/null 2>&1; then
  snap install lxd --channel="$LXD_CHANNEL"
fi
# Wait for the DAEMON. `lxc info` answers 0 before the daemon is initialised (a
# fresh snap has a database and a device-less `default` profile), so it is only an
# "is the daemon up" probe — NOT an "is it initialised" probe, which is why the
# initialisation itself lives in provision/10-node-os.sh (step 4), which ensures
# the storage pool, the guest bridge and the default-profile devices idempotently
# and asserts them. This prelude exists only so the stage tool has a daemon to
# export from.
for _ in $(seq 1 60); do lxc info >/dev/null 2>&1 && break; sleep 2; done
lxc info >/dev/null 2>&1 || die "the LXD daemon did not start"

lxc config set core.https_address ":8443"
systemctl enable snap.lxd.daemon.service 2>/dev/null || true
systemctl restart snap.lxd.daemon.service || snap restart lxd || true
for _ in $(seq 1 60); do
  curl -sk --max-time 2 https://127.0.0.1:8443/1.0 >/dev/null 2>&1 && break
  sleep 2
done
curl -sk --max-time 2 https://127.0.0.1:8443/1.0 >/dev/null 2>&1 \
  || die "LXD API did not come up on :8443 after init"
info "LXD up: $(lxc config get core.https_address) / $(lxc info | awk '/server_version:/{print $2}' | head -1)"

# ---------------------------------------------------------------------------
# 2. Put the workload image into the local LXD store under the CONTRACT alias
#
#    This models a build host that already holds the published workload image
#    (`image/agent-vm/build-agent-image.sh` publishes `agent-vm/<version>`). The
#    bake does NOT pull from a registry; the tarball was uploaded here.
# ---------------------------------------------------------------------------
log "2. import the workload image into the local store as $WORKLOAD_ALIAS"
if lxc image info "$WORKLOAD_ALIAS" >/dev/null 2>&1; then
  info "alias $WORKLOAD_ALIAS already present; replacing"
  lxc image delete "$WORKLOAD_ALIAS"
fi
lxc image import "$WORKLOAD_TARBALL" --alias "$WORKLOAD_ALIAS"
lxc image info "$WORKLOAD_ALIAS" >/dev/null 2>&1 || die "workload image did not import as $WORKLOAD_ALIAS"

# ---------------------------------------------------------------------------
# 3. Stage it through the bake's OWN staging tool (export + manifest)
# ---------------------------------------------------------------------------
log "3. stage the workload image via tools/stage_workload_image.sh"
TERMINI_STAGE_DIR="$IMAGE_STAGE" TERMINI_IMAGE_VERSION="$TERMINI_IMAGE_VERSION" \
  bash "$TOOLS_DIR/stage_workload_image.sh" --stage "$IMAGE_STAGE"
[ -f "$IMAGE_STAGE/images.json" ] || die "stage tool produced no manifest in $IMAGE_STAGE"

# Drop the scratch source image again, so the provisioner's manifest-driven import
# is the ONLY writer of the contract aliases. This mirrors the packer flow exactly
# (the build VM's store is empty; the stage tarball is what gets uploaded) AND
# avoids `lxc image import` refusing content the store already holds
# ("Image with same fingerprint already exists"). The staging step has already
# captured the tarball + its digest, so nothing is lost.
log "3b. remove the scratch source image (the stage tarball is now the source)"
lxc image delete "$WORKLOAD_ALIAS" 2>/dev/null || true
lxc image info "$WORKLOAD_ALIAS" >/dev/null 2>&1 && die "scratch source image $WORKLOAD_ALIAS still present"

# ---------------------------------------------------------------------------
# 4. Run the REAL provisioner: it installs/refreshes LXD, re-asserts the API
#    binding, and imports the staged workload image under EVERY declared alias
#    (both `agent-vm/<version>` and `agent-os/w4`). This is the bake's own import
#    code path — not a hand-created alias — and it fails loudly on a bad stage.
# ---------------------------------------------------------------------------
log "4. provision/10-node-os.sh (LXD + manifest-driven workload-image import)"
IMAGE_STAGE="$IMAGE_STAGE" LXD_CHANNEL="$LXD_CHANNEL" \
  bash "$PROVISION_DIR/10-node-os.sh"

# ---------------------------------------------------------------------------
# 5. Bake the CURRENT bootstrap daemon + its unit
# ---------------------------------------------------------------------------
log "5. provision/60-node-bootstrap.sh (current termini-bootstrap + unit)"
install -m 0755 "$NODE_DIR/termini-bootstrap.py" /tmp/termini-bootstrap.py
install -m 0644 "$NODE_DIR/termini-bootstrap.service" /tmp/termini-bootstrap.service
bash "$PROVISION_DIR/60-node-bootstrap.sh"

# The offline sentinel + version marker the image contract carries.
touch /var/lib/termini/.baked-offline
printf 'termini-node-os %s\n' "$TERMINI_IMAGE_VERSION" > /etc/termini/image-version

# Do not double the image: the LXD store already holds the workload content, so
# the staged tarball (hundreds of MB) is bake-time only. Keep it with KEEP_STAGE=1.
if [ "$KEEP_STAGE" != "1" ]; then
  rm -f "$IMAGE_STAGE"/*.tar.gz
fi

# ---------------------------------------------------------------------------
# 6. VERIFY from inside the builder — BEFORE de-instancing (which removes the
#    SSH host keys and ends this session's access). A file-existence assertion is
#    not a behaviour test; every check below probes a postcondition.
# ---------------------------------------------------------------------------
log "6. verify the bake (postconditions, not file presence)"
FAILED=0
check() { # NAME EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then printf '  CHECK %-28s PASS  (%s)\n' "$1" "$3"
  else printf '  CHECK %-28s FAIL  expected=%s actual=%s\n' "$1" "$2" "$3"; FAILED=1; fi
}

C1="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 https://127.0.0.1:8443/1.0 || echo 000)"
check "api_8443_http_code" "200" "$C1"
check "core.https_address" ":8443" "$(lxc config get core.https_address 2>/dev/null || echo NONE)"
lxc list --format csv >/dev/null 2>&1; check "lxc_list_works" "0" "$?"
# The hypervisor must be USABLE, not just listening: a node with no storage pool
# or a device-less `default` profile cannot launch anything (the controller sends
# profiles: ["default"] with no devices).
check "storage_pool_default" "0" "$(lxc storage show default >/dev/null 2>&1; echo $?)"
check "guest_bridge_lxdbr0" "1" "$(lxc network show lxdbr0 >/dev/null 2>&1 && echo 1 || echo 0)"
check "default_profile_root_disk" "1" "$(lxc profile device show default 2>/dev/null | grep -c '^root:')"

WF="$(lxc image info "$WORKLOAD_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}')"
NF="$(lxc image info "$NODE_ALIAS" 2>/dev/null | awk '/^Fingerprint:/{print $2}')"
[ -n "$WF" ] && check "alias_${WORKLOAD_ALIAS}" "$WF" "$NF" || { printf '  CHECK alias_%s FAIL  (missing)\n' "$WORKLOAD_ALIAS"; FAILED=1; }
for a in "$WORKLOAD_ALIAS" "$NODE_ALIAS"; do
  # `lxc image list -c l` collapses multiple aliases to "alias (N more)"; the
  # full list is column `L`, which embeds a newline per alias inside one quoted
  # cell. Drop the quotes and match a whole line.
  if lxc image list --format csv -c L | tr -d '"' | grep -qx "$a"; then
    printf '  CHECK lxc_image_list_has_%-12s PASS  (%s)\n' "$a" "$a"
  else
    printf '  CHECK lxc_image_list_has_%-12s FAIL  (%s absent from lxc image list)\n' "$a" "$a"; FAILED=1
  fi
done
check "bootstrap_daemon_exec" "0" "$(test -x /usr/local/bin/termini-bootstrap; echo $?)"
check "bootstrap_unit_enabled" "enabled" "$(systemctl is-enabled termini-bootstrap.service 2>/dev/null || echo no)"
# The current daemon reports its own LXD endpoint at registration. An image baked
# before that change ships a daemon that cannot, which is why a fleet run needed
# the global fallback instead of per-node addressing.
check "bootstrap_reports_endpoint" "0" "$(grep -q '"endpoint"' /usr/local/bin/termini-bootstrap; echo $?)"
check "daemon_py_compiles" "0" "$(python3 -c 'import ast,sys; ast.parse(open("/usr/local/bin/termini-bootstrap").read())'; echo $?)"
check "baked_offline_sentinel" "0" "$(test -f /var/lib/termini/.baked-offline; echo $?)"
check "image_version_present" "0" "$(test -s /etc/termini/image-version; echo $?)"

if [ "$FAILED" != "0" ]; then
  die "bake verification FAILED — refusing to publish a broken image"
fi
info "all bake checks passed"

# ---------------------------------------------------------------------------
# 7. De-instance-ify LAST. The image must not carry this VM's cloud-init state
#    (or its own user-data never runs) nor its SSH host keys, and the machine-id
#    must be empty so the booted nodes are distinct. ssh-keygen -A regenerates
#    per-instance keys so sshd is not left broken.
# ---------------------------------------------------------------------------
log "7. de-instance-ify"
cloud-init clean --logs --machine-id || true
rm -f /etc/ssh/ssh_host_*
rm -rf /var/lib/cloud/instances/* /var/lib/cloud/instance
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id 2>/dev/null || true
rm -rf "$BAKE_SRC"
sync
ssh-keygen -A
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
lxc image info "$WORKLOAD_ALIAS" >/dev/null 2>&1 || die "workload alias vanished during cleanup"
info "cloud-init clean + host keys regenerated; workload aliases still resolve"

echo "BAKE_OK $(date -u +%Y-%m-%dT%H:%M:%SZ) version=${TERMINI_IMAGE_VERSION} alias=${WORKLOAD_ALIAS} node_alias=${NODE_ALIAS}"
