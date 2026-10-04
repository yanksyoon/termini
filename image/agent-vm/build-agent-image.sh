#!/usr/bin/env bash
# build-agent-image.sh — reproducible builder for the Termini AGENT VM image.
#
#   ./build-agent-image.sh --check          # validate the recipe on any checkout (no LXD needed)
#   sudo ./build-agent-image.sh             # build + publish + export via LXD (needs LXD + KVM/container)
#   ./build-agent-image.sh --build-offline  # build with the build container's NIC removed (proves no live fetch)
#
# Unlike the node image (image/pipeline, t_ab18d582) this builds the GUEST image:
# Python 3.12 + Node.js 20 + LangChain/CrewAI/AutoGen + Google ADK + the termini-agent
# runtime, all resolved at bake time. First boot installs and downloads nothing.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX_DIR="$SELF_DIR"                      # this directory IS the build context
VERSION="${TERMINI_VERSION:-2026.10.0}"
NODE_VERSION="20.20.2"
ALIAS_BASE="agent-vm"
NAME="termini-agent-build-$$"
MODE="check"
KEEP=0
LXD="lxc"

usage() {
  cat <<'USAGE'
usage: build-agent-image.sh [--check | --build] [--version X.Y.Z] [--out DIR]
                            [--name CONTAINER] [--keep] [--alias-base NAME]
USAGE
}

OUT_DIR="${TERMINI_OUT_DIR:-/var/lib/termini/build}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    --build) MODE="build"; shift ;;
    --version) VERSION="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --alias-base) ALIAS_BASE="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

GITSHA="$(git -C "$CTX_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
IMMUTABLE_TAG="${ALIAS_BASE}-${VERSION}+g${GITSHA}"
ALIAS="${ALIAS_BASE}/${VERSION}"

REQUIRED=(
  "$CTX_DIR/requirements/runtime.in" "$CTX_DIR/requirements/runtime.lock"
  "$CTX_DIR/requirements/langchain.in" "$CTX_DIR/requirements/langchain.lock"
  "$CTX_DIR/requirements/crewai.in" "$CTX_DIR/requirements/crewai.lock"
  "$CTX_DIR/requirements/autogen.in" "$CTX_DIR/requirements/autogen.lock"
  "$CTX_DIR/node/package.json" "$CTX_DIR/node/package-lock.json"
  "$CTX_DIR/agent/pyproject.toml"
  "$CTX_DIR/provision/00-base.sh" "$CTX_DIR/provision/10-runtime.sh"
  "$CTX_DIR/provision/20-agent-libs.sh" "$CTX_DIR/provision/30-termini-agent.sh"
  "$CTX_DIR/provision/35-agent-selftest.sh"
  "$CTX_DIR/systemd/termini-agent.service"
  "$CTX_DIR/systemd/termini-firstboot-preflight.service"
  "$CTX_DIR/config/agent.yaml.template"
  "$CTX_DIR/first-boot-preflight.sh"
  "$CTX_DIR/tools/offline-acceptance.sh"
  "$CTX_DIR/manifests/agent-image-manifest.schema.json"
  "$CTX_DIR/tools/validate_manifest.py"
)

# -----------------------------------------------------------------------------
check_recipe() {
  local rc=0 f
  echo "== required files"
  for f in "${REQUIRED[@]}"; do
    if [ -f "$f" ]; then echo "  ok   ${f#"$CTX_DIR"/}"; else echo "  MISS ${f#"$CTX_DIR"/}"; rc=1; fi
  done

  echo "== syntax: bash -n + python compile"
  while IFS= read -r sh; do
    bash -n "$sh" || { echo "  SYNTAX FAIL $sh"; rc=1; }
  done < <(find "$CTX_DIR" -name '*.sh' -type f | sort)
  while IFS= read -r py; do
    python3 -m py_compile "$py" || { echo "  PY FAIL $py"; rc=1; }
  done < <(find "$CTX_DIR/agent" -name '*.py' -type f | sort)

  echo "== json + yaml"
  python3 -m json.tool "$CTX_DIR/node/package.json" >/dev/null || rc=1
  python3 -m json.tool "$CTX_DIR/node/package-lock.json" >/dev/null || rc=1
  python3 - "$CTX_DIR/config/agent.yaml.template" <<'PY' || rc=1
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as fh:
    yaml.safe_load(fh)
print("  ok   config/agent.yaml.template parses")
PY

  echo "== locks are pinned + hashed"
  python3 - "$CTX_DIR/requirements" <<'PY' || rc=1
import pathlib, sys
root = pathlib.Path(sys.argv[1]); bad = 0
for lock in sorted(root.glob("*.lock")):
    text = lock.read_text(encoding="utf-8")
    has_hash = "--hash=sha256:" in text
    if not has_hash:
        print(f"  FAIL {lock.name}: no hashes"); bad = 1
    if not (root / (lock.stem + ".in")).exists():
        print(f"  FAIL {lock.name}: missing {lock.stem}.in"); bad = 1
    print(f"  ok   {lock.name} ({text.count('--hash=')} hashes)")
sys.exit(bad)
PY

  if command -v shellcheck >/dev/null 2>&1; then
    echo "== shellcheck -S warning"
    while IFS= read -r sh; do
      shellcheck -S warning "$sh" || { echo "  SHELLCHECK FAIL $sh"; rc=1; }
    done < <(find "$CTX_DIR" -name '*.sh' -type f | sort)
  else
    echo "== shellcheck: not installed, skipped"
  fi

  [ "$rc" -eq 0 ] && echo "RECIPE OK (alias=$ALIAS version=$VERSION tag=$IMMUTABLE_TAG)" \
                   || echo "RECIPE FAILED" >&2
  return "$rc"
}

# -----------------------------------------------------------------------------
push_context() { # container
  local c="$1"
  "$LXD" exec "$c" -- mkdir -p /opt/termini/build
  tar -C "$CTX_DIR" -cf - . | "$LXD" exec "$c" -- tar -C /opt/termini/build -xf -
}

build() {
  command -v lxc >/dev/null 2>&1 || { echo "FATAL: lxc required for --build" >&2; exit 4; }
  "$LXD" info >/dev/null 2>&1 || { echo "FATAL: LXD not initialized (run 'lxd init')" >&2; exit 4; }

  echo "[build] launching base $NAME (from ubuntu:24.04)"
  "$LXD" launch ubuntu:24.04 "$NAME"
  "$LXD" exec "$NAME" -- bash -lc 'cloud-init status --wait >/dev/null 2>&1 || true'
  "$LXD" exec "$NAME" -- bash -lc 'systemctl is-system-running --wait || true'

  push_context "$NAME"

  export TERMINI_VERSION="$VERSION" TERMINI_GITSHA="$GITSHA"
  export TERMINI_BUILD_CTX="/opt/termini/build"

  for step in 00-base 10-runtime 20-agent-libs 30-termini-agent 35-agent-selftest; do
    echo "[build] provision:$step"
    # pass the pins explicitly: `lxc exec` does not inherit this shell's environment
    "$LXD" exec "$NAME" -- env \
      TERMINI_VERSION="$VERSION" TERMINI_GITSHA="$GITSHA" \
      TERMINI_BUILD_CTX="/opt/termini/build" \
      bash "/opt/termini/build/provision/$step.sh"
  done

  # trim build-time cruft (keeps the image small; boot never needs it)
  "$LXD" exec "$NAME" -- bash -lc 'rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache /tmp/* 2>/dev/null || true'

  echo "[build] publishing image $ALIAS ($IMMUTABLE_TAG)"
  # read the bake's own digest BEFORE stopping (lxc exec needs a running container)
  digest="$("$LXD" exec "$NAME" -- cat /etc/termini/agent-image.digest 2>/dev/null || true)"
  "$LXD" stop "$NAME"
  "$LXD" publish "$NAME" --alias "$ALIAS" --alias "$IMMUTABLE_TAG" --reuse
  "$LXD" image set-property "$ALIAS" version "$VERSION" >/dev/null 2>&1 || true
  "$LXD" image set-property "$ALIAS" gitsha "$GITSHA" >/dev/null 2>&1 || true
  "$LXD" image set-property "$ALIAS" immutable_tag "$IMMUTABLE_TAG" >/dev/null 2>&1 || true
  "$LXD" image set-property "$ALIAS" description \
    "Termini agent VM image (offline-baked agent runtimes)" >/dev/null 2>&1 || true

  install -d -m 0755 "$OUT_DIR"
  artifact_base="$OUT_DIR/${ALIAS_BASE}-${VERSION}-${GITSHA}"
  echo "[build] exporting -> $artifact_base"
  "$LXD" image export "$ALIAS" "$artifact_base"
  artifact="$(ls "${artifact_base}.tar.gz" 2>/dev/null || ls "${artifact_base}".tar 2>/dev/null || ls "${artifact_base}".* | head -n1)"
  size="$(stat -c %s "$artifact")"
  sha="$(sha256sum "$artifact" | awk '{print $1}')"
  printf '%s  %s\n' "$sha" "$(basename "$artifact")" > "$artifact.sha256"

  manifest="$OUT_DIR/image-manifest.json"
  MANIFEST_ALIAS="$ALIAS" MANIFEST_TAG="$IMMUTABLE_TAG" MANIFEST_VERSION="$VERSION" \
  MANIFEST_GITSHA="$GITSHA" MANIFEST_ARTIFACT="$artifact" MANIFEST_SIZE="$size" \
  MANIFEST_SHA="$sha" MANIFEST_NODE_VERSION="$NODE_VERSION" MANIFEST_DIGEST="$digest" \
  MANIFEST_CTX="$CTX_DIR" python3 - "$manifest" <<'PY'
import json, os, sys
e = os.environ; out = sys.argv[1]
def freeze(venv_file):
    pins = {}
    with open(venv_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if "==" in line and not line.startswith("#"):
                name, _, ver = line.partition("==")
                pins[name] = ver.split()[0]
    return pins
ctx = e["MANIFEST_CTX"]
doc = {
    "schema_version": "1.0",
    "alias": e["MANIFEST_ALIAS"],
    "version": e["MANIFEST_VERSION"],
    "immutable_tag": e["MANIFEST_TAG"],
    "gitsha": e["MANIFEST_GITSHA"],
    "image_digest": e["MANIFEST_DIGEST"],
    "artifacts": [{
        "name": os.path.basename(e["MANIFEST_ARTIFACT"]),
        "format": "lxd-image",
        "size_bytes": int(e["MANIFEST_SIZE"]),
        "sha256": e["MANIFEST_SHA"],
    }],
    "pins": {
        "python": "3.12",
        "node_version": e["MANIFEST_NODE_VERSION"],
        "runtime": freeze(f"{ctx}/requirements/runtime.lock"),
        "frameworks": {
            "langchain": freeze(f"{ctx}/requirements/langchain.lock"),
            "crewai": freeze(f"{ctx}/requirements/crewai.lock"),
            "autogen": freeze(f"{ctx}/requirements/autogen.lock"),
        },
    },
    "contents": {
        "runtime_venv": "/opt/termini/venv",
        "framework_venvs": "/opt/termini/venvs/{langchain,crewai,autogen}",
        "node_dir": "/opt/termini/node",
        "agent_bin": "/opt/termini/venv/bin/termini-agent",
        "config": "/etc/termini/agent.yaml",
    },
    "first_boot": {
        "preflight_unit": "termini-firstboot-preflight.service",
        "boot_log_path": "/var/log/termini/first-boot.json",
        "network_required": False,
    },
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2); fh.write("\n")
print(f"[build] manifest -> {out}")
PY
  python3 "$CTX_DIR/tools/validate_manifest.py" "$manifest" \
    "$CTX_DIR/manifests/agent-image-manifest.schema.json"

  echo "[build] artifact=$artifact size=$size sha256=$sha"
  echo "[build] alias=$ALIAS immutable_tag=$IMMUTABLE_TAG digest=$digest"

  if [ "$KEEP" = "1" ]; then
    echo "[build] keeping container $NAME (alias resolves; run verify-agent-image.sh separately)"
  else
    "$LXD" delete "$NAME" --force 2>/dev/null || true
  fi
}

if [ "$MODE" = "check" ]; then check_recipe; else build; fi
