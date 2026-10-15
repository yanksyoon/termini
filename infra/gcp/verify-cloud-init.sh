#!/usr/bin/env bash
# verify-cloud-init.sh — offline acceptance check for the unified GCP cloud-init
# (task t_5e9c9d96). Proves that infra/gcp renders the SHARED bootstrap template
# and that the rendered user-data carries the SSH custody + Node C serve block,
# WITHOUT any GCP project, credentials or state:
#
#   1. infra/gcp/cloud-init/node.yaml.tftpl is the bootstrap template (symlink);
#   2. Terraform renders it for all three nodes (via the provider-free module);
#   3. no "placeholder"/unresolved token remains in any rendered user-data;
#   4. each node gets the team SSH key, sshd hardening + controller CA, config
#      and enrollment token;
#   5. only Node C carries the llama.cpp serve block;
#   6. the SAME template also renders through the envsubst path
#      (infra/bootstrap/render-cloud-init.sh) — one template, two renderers.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VERIFY_DIR="$HERE/userdata/verify"
SHARED="$REPO/infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl"
GCP_TPL="$HERE/cloud-init/node.yaml.tftpl"
TEAM_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEAMKEYVERIFYEXAMPLE termini-team-key"
CA_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICACALIFORNIAEXAMPLE termini-ssh-ca"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/verify-cloud-init.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
ok()  { printf '  PASS  %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; FAIL=1; }
have() { if eval "$1"; then ok "$2"; else bad "$2"; fi; }
absent() { if eval "$1"; then bad "$2"; else ok "$2"; fi; }

command -v terraform >/dev/null || { echo "terraform not found" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq not found" >&2; exit 2; }

echo "== 1. infra/gcp/cloud-init/node.yaml.tftpl is the bootstrap template"
if [ -L "$GCP_TPL" ] && [ "$(readlink -f "$GCP_TPL")" = "$(readlink -f "$SHARED")" ]; then
  ok "node.yaml.tftpl -> $(readlink "$GCP_TPL")"
else
  bad "node.yaml.tftpl is not a symlink to the bootstrap template"
fi

echo "== 2. terraform renders the shared template (provider-free, offline)"
terraform -chdir="$VERIFY_DIR" init -backend=false -input=false >/dev/null
terraform -chdir="$VERIFY_DIR" apply -auto-approve -input=false >/dev/null
terraform -chdir="$VERIFY_DIR" output -json node_user_data > "$WORK/ud.json"
for n in node-a node-b node-c; do
  jq -r --arg n "$n" '.[$n]' "$WORK/ud.json" > "$WORK/$n.yaml"
  if [ -s "$WORK/$n.yaml" ]; then ok "$n rendered ($(wc -c < "$WORK/$n.yaml") bytes)"; else bad "$n render empty"; fi
done

echo "== 3. no placeholder / unresolved token remains"
# The template's OWN variables (node-bootstrap.yaml.tftpl). This list — not a bare
# `\$\{` regex — is the right test: the injected Node C shell blocks legitimately
# contain shell self-defaults like ${INTENT_PORT:-8090} and
# ${FRONT_DOOR_SRC:-/usr/local/src/termini-inference}, which a plain
# "${ + letter" scan reports as unresolved (it did, on main, until this list made
# the check precise — the scripts are not template variables).
TEMPLATE_VARS="node_name role_hint underlay_ip underlay_gw seed_ip controller_url \
controller_ca_pubkey telemetry_interval_ms team_ssh_pubkey enrollment_token \
image_alias image_version ssh_authorized_keys_block extra_write_files extra_runcmd"
for n in node-a node-b node-c; do
  absent "grep -qi placeholder '$WORK/$n.yaml'" "$n: no 'placeholder' string"
  absent "grep -q '%{' '$WORK/$n.yaml'" "$n: no Terraform directive"
  for v in $TEMPLATE_VARS; do
    if grep -qF "\${$v}" "$WORK/$n.yaml"; then
      bad "$n: unresolved template variable \${$v}"
    fi
  done
  ok "$n: no unresolved template variable (checked $(printf '%s' "$TEMPLATE_VARS" | wc -w))"
  absent "grep -qE '__[A-Z][A-Z_]*__' '$WORK/$n.yaml'" "$n: no '__TOKEN__' left"
done

echo "== 4. SSH custody + config on every node"
for n in node-a node-b node-c; do
  f="$WORK/$n.yaml"
  have "grep -qF '$TEAM_KEY' '$f'" "$n: team SSH key injected"
  have "grep -q 'ssh_authorized_keys:' '$f'" "$n: ssh_authorized_keys stanza present"
  have "grep -q 'PasswordAuthentication no' '$f'" "$n: PasswordAuthentication no"
  have "grep -q 'PermitRootLogin prohibit-password' '$f'" "$n: PermitRootLogin prohibit-password"
  have "grep -q 'TrustedUserCAKeys /etc/ssh/termini_ca.pub' '$f'" "$n: TrustedUserCAKeys wired"
  have "grep -qF '$CA_KEY' '$f'" "$n: controller CA public key present"
  have "grep -q 'path: /etc/termini/config.yaml' '$f'" "$n: config.yaml written"
  have "grep -q 'path: /var/lib/termini/enrollment.token' '$f'" "$n: enrollment token written"
  have "grep -q 'termini-bootstrap.service' '$f'" "$n: bootstrap service start"
done
have "grep -q 'node_name: node-a' '$WORK/node-a.yaml'" "node-a config has its node_name"
have "grep -q 'role_hint: inference' '$WORK/node-c.yaml'" "node-c config has role_hint inference"

echo "== 5. only Node C carries the llama.cpp serve block"
for n in node-a node-b; do
  absent "grep -q 'termini-llama-serve.sh' '$WORK/$n.yaml'" "$n: no llama serve block"
done
have "grep -q 'path: /usr/local/sbin/termini-llama-serve.sh' '$WORK/node-c.yaml'" "node-c: llama serve script written"
have "grep -q '\\[ /usr/local/sbin/termini-llama-serve.sh \\]' '$WORK/node-c.yaml'" "node-c: llama serve runcmd present"
have "grep -q 'MODEL_ALIAS=\"gemma\"' '$WORK/node-c.yaml'" "node-c: model alias resolved"
have "grep -q 'PORT=\"8080\"' '$WORK/node-c.yaml'" "node-c: port resolved"

echo "== 5b. rendered user-data is valid YAML"
if python3 -c 'import yaml' >/dev/null 2>&1; then
  for n in node-a node-b node-c; do
    if python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' "$WORK/$n.yaml" 2>/dev/null; then
      ok "$n: valid YAML"
    else
      bad "$n: invalid YAML"
    fi
  done
else
  printf '  skip  PyYAML unavailable; YAML validity not checked\n'
fi

echo "== 6. the same template also renders through envsubst (bootstrap path)"
controller_url="https://controller.termini.internal" node_name="node-z" \
  enrollment_token="verify-token" team_ssh_pubkey="$TEAM_KEY" role_hint="compute" \
  underlay_ip="10.10.0.9/24" underlay_gw="10.10.0.1" seed_ip="10.10.0.11" \
  controller_ca_pubkey="$CA_KEY" image_alias="agentic-os/w3" image_version="2026.10.0" \
  "$REPO/infra/bootstrap/render-cloud-init.sh" "$SHARED" > "$WORK/envsubst.yaml"
have "grep -qF '$TEAM_KEY' '$WORK/envsubst.yaml'" "envsubst render has the team key"
absent "grep -qE '\\\$\{[A-Za-z_]' '$WORK/envsubst.yaml'" "envsubst render has no unresolved \${var}"
absent "grep -qi placeholder '$WORK/envsubst.yaml'" "envsubst render has no placeholder"

echo "== 7. provision.sh (gcloud fallback) renders the same cloud-init"
printf '%s\n' "$TEAM_KEY" > "$WORK/team.pub"
TEAM_SSH_PUBKEY_FILE="$WORK/team.pub" ENROLLMENT_TOKEN="verify-token" \
  CONTROLLER_CA_PUBKEY="$CA_KEY" \
  bash "$HERE/provision.sh" --render-only --render-out "$WORK/prov" >/dev/null
have "grep -qF '$TEAM_KEY' '$WORK/prov/node-a.yaml'" "provision.sh node-a: team key injected"
have "grep -q 'PasswordAuthentication no' '$WORK/prov/node-a.yaml'" "provision.sh node-a: sshd hardened"
have "grep -qF '$CA_KEY' '$WORK/prov/node-a.yaml'" "provision.sh node-a: controller CA present"
absent "grep -qi placeholder '$WORK/prov/node-a.yaml'" "provision.sh node-a: no placeholder"
absent "grep -q 'termini-llama-serve.sh' '$WORK/prov/node-a.yaml'" "provision.sh node-a: no llama block"
have "grep -q 'termini-llama-serve.sh' '$WORK/prov/node-c.yaml'" "provision.sh node-c: llama block present"
if python3 -c 'import yaml' >/dev/null 2>&1; then
  if python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' "$WORK/prov/node-c.yaml" 2>/dev/null; then
    ok "provision.sh node-c: valid YAML"
  else
    bad "provision.sh node-c: invalid YAML"
  fi
fi

CHECKER="$HERE/cloud-init/node_c_front_door_check.py"

echo "== 8. Node C's FIRST BOOT ships the inference front door (t_359ed606)"
for pair in "terraform:$WORK/node-c.yaml" "provision.sh:$WORK/prov/node-c.yaml"; do
  renderer="${pair%%:*}"; f="${pair#*:}"
  if python3 "$CHECKER" "$f" >/dev/null 2>&1; then
    ok "$renderer render: front door + byte-exact payload"
  else
    bad "$renderer render: front door check FAILED"
    python3 "$CHECKER" "$f" 2>&1 | sed 's/^/        /'
  fi
done
if python3 "$CHECKER" --expect none "$WORK/node-a.yaml" "$WORK/node-b.yaml" \
     "$WORK/prov/node-a.yaml" "$WORK/prov/node-b.yaml" >/dev/null 2>&1; then
  ok "non-inference nodes carry no serve block (all renders)"
else
  bad "a non-inference node carries (part of) Node C's serve block"
fi
# The card's requirement 2, asserted on the RENDERED user-data: what a fresh node
# serves, and that the CPU path is one variable away.
if python3 "$CHECKER" --backend gemini-proxy "$WORK/node-c.yaml" >/dev/null 2>&1; then
  ok "first-boot backend is the shipped default (gemini-proxy)"
else
  bad "first-boot backend is not gemini-proxy in the Terraform render"
fi
if python3 "$CHECKER" --backend local "$WORK/node-c.yaml" >/dev/null 2>&1; then
  bad "the render does not actually pin the backend (a 'local' expectation passed too)"
else
  ok "the backend in the render is pinned (a 'local' expectation is rejected)"
fi
for pair in "terraform:$WORK/node-c.yaml" "provision.sh:$WORK/prov/node-c.yaml"; do
  f="${pair#*:}"
  sz="$(wc -c < "$f")"
  if [ "$sz" -lt 204800 ]; then
    ok "${pair%%:*} node-c user-data is ${sz} bytes (< 200 KiB of the 256 KiB metadata limit)"
  else
    bad "${pair%%:*} node-c user-data is ${sz} bytes — over the 256 KiB GCE metadata value limit"
  fi
done

echo
if [ "$FAIL" = "0" ]; then
  echo "RESULT: PASS — infra/gcp renders the shared bootstrap template with SSH custody, no placeholder, Node C serve block."
else
  echo "RESULT: FAIL — see FAIL lines above."
fi
exit "$FAIL"
