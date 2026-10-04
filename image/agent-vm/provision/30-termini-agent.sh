#!/usr/bin/env bash
# 30-termini-agent.sh — install the termini-agent runtime, identity, units, sentinel.
# (agent-runtime.md §3.1/§3.2, §5.1)
set -euo pipefail

BUILD_CTX="${TERMINI_BUILD_CTX:-/opt/termini/build}"
VERSION="${TERMINI_VERSION:-2026.10.0}"
GITSHA="${TERMINI_GITSHA:-nogit}"
RUNTIME_VENV="/opt/termini/venv"

# --- unprivileged runtime user ------------------------------------------------
if ! id -u agent >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/termini-agent \
          --shell /usr/sbin/nologin agent
fi

# --- install the runtime package (offline: no deps, no build isolation) -------
cp -a "${BUILD_CTX}/agent" /opt/termini/agent-src
"$RUNTIME_VENV/bin/pip" install --quiet --no-deps --no-build-isolation /opt/termini/agent-src
test -x "$RUNTIME_VENV/bin/termini-agent"
"$RUNTIME_VENV/bin/termini-agent" --version

# --- identity + config template ----------------------------------------------
install -d -m 0755 /etc/termini /usr/local/sbin
install -d -m 0755 /opt/termini/requirements
install -m 0644 "${BUILD_CTX}"/requirements/*.lock "${BUILD_CTX}"/requirements/*.in \
  /opt/termini/requirements/
install -d -m 0750 -o agent -g agent /var/log/termini
install -d -m 0750 -o agent -g agent /var/lib/termini/agent/tasks
install -d -m 0755 /run/termini

# short image digest over the pinned inputs (stable, not a security hash)
inputs_sha="$(cat \
  "${BUILD_CTX}/requirements/runtime.lock" \
  "${BUILD_CTX}/requirements/langchain.lock" \
  "${BUILD_CTX}/requirements/crewai.lock" \
  "${BUILD_CTX}/requirements/autogen.lock" \
  "${BUILD_CTX}/node/package-lock.json" | sha256sum | awk '{print $1}')"
digest="sha256:${inputs_sha:0:12}"
printf '%s %s\n' "agent-vm-${VERSION}+g${GITSHA}" "$digest" > /etc/termini/agent-image
printf '%s\n' "$digest" > /etc/termini/agent-image.digest

install -m 0644 "${BUILD_CTX}/config/agent.yaml.template" /etc/termini/agent.yaml.example
install -m 0644 "${BUILD_CTX}/config/agent.yaml.template" /etc/termini/agent.yaml
install -m 0600 -o agent -g agent /dev/null /etc/termini/inference.token || true

# --- systemd units ------------------------------------------------------------
install -m 0644 "${BUILD_CTX}/systemd/termini-firstboot-preflight.service" \
  /etc/systemd/system/termini-firstboot-preflight.service
install -m 0644 "${BUILD_CTX}/systemd/termini-agent.service" \
  /etc/systemd/system/termini-agent.service
install -m 0755 "${BUILD_CTX}/first-boot-preflight.sh" \
  /usr/local/sbin/termini-agent-preflight.sh
# boot-time network-accounting library (derives network_downloads_at_boot)
install -d -m 0755 /usr/local/lib/termini
install -m 0644 "${BUILD_CTX}/tools/net-accounting.sh" \
  /usr/local/lib/termini/net-accounting.sh
systemctl enable termini-firstboot-preflight.service termini-agent.service
# ssh stays enabled (default): the controller injects the project's public key at launch.
# With no injected key there is nothing to log in with, so an idle sshd is harmless.

# --- offline-bake sentinel (read by the preflight) ----------------------------
install -d -m 0755 /var/lib/termini
{
  printf 'baked_offline=true\n'
  printf 'image_version=%s\n' "$VERSION"
  printf 'gitsha=%s\n' "$GITSHA"
  printf 'baked_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > /var/lib/termini/.baked-offline

echo "[provision:30-termini-agent] termini-agent + units + identity baked ($digest)"
