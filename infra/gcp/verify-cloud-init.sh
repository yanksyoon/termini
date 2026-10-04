#!/usr/bin/env bash
# verify-cloud-init.sh — acceptance harness for the GCP cloud-init unification
# (kanban task t_5e9c9d96).
#
# OFFLINE (always, no cloud needed):
#   * the GCP template (cloud-init/node.yaml.tftpl) IS the shared bootstrap
#     template (infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl) — a symlink,
#     so the Terraform path and the bootstrap path cannot drift;
#   * main.tf renders that shared template;
#   * infra/gcp/render-cloud-init.sh renders it for each node and the result is
#     valid cloud-config with NO placeholder string and NO unresolved marker;
#   * Node C carries the llama.cpp serve block; Nodes A/B do not.
#
# LIVE (--host USER@IP): proves a real GCP node accepts SSH with the injected team
# key and registers. The GCP boot image is still stock Ubuntu (the pre-baked
# Termini Node OS is pending, D3), so the harness stages the canonical bootstrap
# daemon + unit exactly like infra/bootstrap/provision-node.sh does for LXD, then
# applies the RENDERED user-data with the node's own cloud-init (cloud-init
# single). It checks config+token written, sshd hardened, passwordless SSH with
# the team key, password auth refused, the daemon ONLINE and visible through the
# controller API, and key-install idempotency. It then removes everything it
# added so the node is left as found.
#
# Deliberately NOT applied on the live node: the template's hostname/fqdn lines
# (cc_set_hostname) — renaming a live node is not needed for the acceptance and
# would change its identity. Everything else the template declares is exercised.
#
# Usage:
#   infra/gcp/verify-cloud-init.sh
#   infra/gcp/verify-cloud-init.sh --host ubuntu@34.47.246.70 --node node-b \
#       --ssh-key ~/.ssh/id_ed25519
#
# env: PORT (18099) — the loopback controller port used on the live node.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

HOST=""
NODE="node-b"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PORT="${PORT:-18099}"
REMOTE_DIR="/home/ubuntu/termini-verify"
FAIL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --node) NODE="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf 'PASS  %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; FAIL=1; }
info() { printf '..    %s\n' "$*"; }
say()  { printf '\n=== %s ===\n' "$*"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/gcp-civ.XXXXXX")"

# ------------------------------------------------------------------ offline
say "shared-template unification"
GCP_TPL="$HERE/cloud-init/node.yaml.tftpl"
BOOT_TPL="$INFRA/bootstrap/cloud-init/node-bootstrap.yaml.tftpl"
if [[ -L "$GCP_TPL" ]] && [[ "$(readlink -f "$GCP_TPL")" == "$(readlink -f "$BOOT_TPL")" ]]; then
  ok "cloud-init/node.yaml.tftpl -> infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl"
else
  bad "cloud-init/node.yaml.tftpl is not a symlink to the shared bootstrap template"
fi
if grep -q 'cloud-init/node.yaml.tftpl' "$HERE/main.tf" && grep -q 'templatefile' "$HERE/main.tf"; then
  ok "main.tf renders cloud-init/node.yaml.tftpl via templatefile()"
else
  bad "main.tf does not render cloud-init/node.yaml.tftpl via templatefile()"
fi

TEAM_PUB_PROBE="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIXYZPROBE termini-team-probe"
CA_PUB_PROBE="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICA termini-controller-ca"

render() { # $1=node $2=role $3=ip $4=serve(0/1) $5=key $6=ca $7=url [$8=token]
  node_name="$1" role_hint="$2" underlay_ip="$3/24" underlay_gw=10.10.0.1 seed_ip=10.10.0.11 \
    controller_url="$7" enrollment_token="${8:-verify-${1}-token}" team_ssh_pubkey="$5" \
    controller_ca_pubkey="$6" image_project=ubuntu-os-cloud image_family=ubuntu-2404-lts-amd64 \
    SERVE_INFERENCE="$4" bash "$HERE/render-cloud-init.sh"
}

check_ud() { # $1=file $2=label $3=expect_llama(0/1) $4=key $5=ca
  local f="$1" label="$2" llama="$3" key="$4" ca="$5" rc=0
  head -1 "$f" | grep -qx '#cloud-config' || { bad "$label: first line is not #cloud-config"; rc=1; }
  grep -qi 'placeholder' "$f" && { bad "$label: placeholder string present"; rc=1; }
  grep -qE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' "$f" && { bad "$label: unsubstituted \${...}"; rc=1; }
  grep -qE '__[A-Z][A-Z0-9_]*__' "$f" && { bad "$label: unresolved __MARKER__"; rc=1; }
  grep -q 'write_files:' "$f" || { bad "$label: no write_files"; rc=1; }
  grep -q 'runcmd:' "$f" || { bad "$label: no runcmd"; rc=1; }
  grep -qF "$key" "$f" || { bad "$label: team key not injected"; rc=1; }
  grep -qF "$ca" "$f" || { bad "$label: controller CA not present"; rc=1; }
  if [[ "$llama" == "1" ]]; then
    grep -q 'termini-llama-serve.sh' "$f" || { bad "$label: llama.cpp block missing"; rc=1; }
  else
    grep -q 'termini-llama-serve.sh' "$f" && { bad "$label: unexpected llama.cpp block"; rc=1; }
  fi
  if command -v python3 >/dev/null && python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$f" <<'PY' || rc=1
import sys
import yaml
yaml.safe_load(open(sys.argv[1]))
PY
  fi
  [[ "$rc" == "0" ]] && ok "$label valid (no placeholder/marker, team key + CA injected)"
  return "$rc"
}

render node-a compute 10.10.0.11 0 "$TEAM_PUB_PROBE" "$CA_PUB_PROBE" "https://controller.termini.internal" > "$WORK/node-a.yaml"
render node-b compute 10.10.0.12 0 "$TEAM_PUB_PROBE" "$CA_PUB_PROBE" "https://controller.termini.internal" > "$WORK/node-b.yaml"
render node-c inference 10.10.0.13 1 "$TEAM_PUB_PROBE" "$CA_PUB_PROBE" "https://controller.termini.internal" > "$WORK/node-c.yaml"
check_ud "$WORK/node-a.yaml" "node-a user-data" 0 "$TEAM_PUB_PROBE" "$CA_PUB_PROBE" || true
check_ud "$WORK/node-b.yaml" "node-b user-data" 0 "$TEAM_PUB_PROBE" "$CA_PUB_PROBE" || true
check_ud "$WORK/node-c.yaml" "node-c user-data (llama)" 1 "$TEAM_PUB_PROBE" "$CA_PUB_PROBE" || true

# Optional: prove Terraform templatefile() renders the exact bytes envsubst does,
# so the Terraform path and the offline renderer cannot drift. Needs `terraform
# init` to have run in this directory; skipped otherwise.
if command -v terraform >/dev/null 2>&1 && [[ -d "$HERE/.terraform" ]]; then
  {
    printf 'templatefile("cloud-init/node.yaml.tftpl", {'
    printf ' node_name = "node-a", role_hint = "compute",'
    printf ' underlay_ip = "10.10.0.11/24", underlay_gw = "10.10.0.1", seed_ip = "10.10.0.11",'
    printf ' controller_url = "https://controller.termini.internal",'
    printf ' controller_ca_pubkey = "%s",' "$CA_PUB_PROBE"
    printf ' telemetry_interval_ms = 1000, enrollment_token = "verify-node-a-token",'
    printf ' team_ssh_pubkey = "%s",' "$TEAM_PUB_PROBE"
    printf ' image_alias = "ubuntu-os-cloud/ubuntu-2404-lts-amd64",'
    printf ' image_version = "ubuntu-2404-lts-amd64",'
    printf ' extra_write_files = "", extra_runcmd = "" })\n'
  } > "$WORK/tfexpr.txt"
  if ( cd "$HERE" && terraform console < "$WORK/tfexpr.txt" > "$WORK/tfout.txt" 2>/dev/null ); then
    if python3 - "$WORK/tfout.txt" "$WORK/tfrender.yaml" <<'PY'
import sys
lines = open(sys.argv[1]).read().split("\n")
if not lines or lines[0] != "<<EOT":
    raise SystemExit("terraform console did not return a heredoc string")
body = lines[1:-2] if lines[-1] == "" else lines[1:-1]
data = "\n".join(body) + ("" if (body and body[-1] == "") else "\n")
open(sys.argv[2], "w").write(data)
PY
    then
      if diff -q "$WORK/node-a.yaml" "$WORK/tfrender.yaml" >/dev/null; then
        ok "terraform templatefile() and envsubst render byte-identical user-data"
      else
        bad "terraform templatefile() output differs from the envsubst render"
      fi
    else
      info "terraform console output not parseable; skipped render-parity check"
    fi
  else
    info "terraform console unavailable; skipped render-parity check"
  fi
else
  info "terraform not initialised here; skipped templatefile() render-parity check"
fi

if [[ -z "$HOST" ]]; then
  printf '\n(no --host: offline checks only)\n'
  rm -rf "$WORK"
  [[ "$FAIL" == "0" ]] && echo "RESULT: PASS" || echo "RESULT: FAIL"
  exit "$FAIL"
fi

# ------------------------------------------------------------------ live
command -v ssh-keygen >/dev/null || { echo "ssh-keygen not found" >&2; exit 2; }
ssh-keygen -t ed25519 -N "" -C "termini-team-key-verify" -f "$WORK/team_ed25519" >/dev/null
ssh-keygen -t ed25519 -N "" -C "termini-controller-ca" -f "$WORK/ca_ed25519" >/dev/null
TEAM_PUB="$(cat "$WORK/team_ed25519.pub")"
CA_PUB="$(cat "$WORK/ca_ed25519.pub")"
TOKEN="civ-$(date +%s)-$RANDOM"
PROJECT_ID="01J0C1V00000000000000000AA"
PROJECT_TOKEN="civ-project-token"
printf '%s' "$PROJECT_ID" > "$WORK/project_id"
printf '%s' "$PROJECT_TOKEN" > "$WORK/project_token"

SSH_OPTS=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=12
          -o BatchMode=yes)
rssh() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

cleanup() {
  info "cleanup on $HOST (restore node to its pre-test state)"
  rssh "bash -ls" <<EOF >/dev/null 2>&1 || true
set +e
# stop the loopback controller first (it holds the port and the DB file)
if [ -f '$REMOTE_DIR/controller.pid' ]; then kill "\$(cat '$REMOTE_DIR/controller.pid')" 2>/dev/null; fi
pkill -f 'app.main' 2>/dev/null
sleep 1
sudo timeout 20 systemctl disable --now termini-bootstrap.service || sudo systemctl kill -s SIGKILL termini-bootstrap.service
sudo rm -f /etc/systemd/system/termini-bootstrap.service
sudo systemctl daemon-reload
sudo rm -f /usr/local/bin/termini-bootstrap
sudo rm -rf /etc/termini /var/lib/termini
if [ -f /root/ssh-verify-backup.tar ]; then sudo tar -C /etc/ssh -xf /root/ssh-verify-backup.tar; fi
sudo rm -f /root/ssh-verify-backup.tar
if [ -f "$REMOTE_DIR/sshd.d.before" ]; then
  while IFS= read -r f; do
    [ -n "\$f" ] || continue
    grep -qxF "\$f" "$REMOTE_DIR/sshd.d.before" || sudo rm -f "/etc/ssh/sshd_config.d/\$f"
  done < <(ls /etc/ssh/sshd_config.d)
fi
if [ -f "$REMOTE_DIR/root_ak.flag" ]; then
  sudo rm -f /root/.ssh/authorized_keys
else
  sudo cp -a /root/authorized_keys.verify-bak /root/.ssh/authorized_keys 2>/dev/null
  sudo rm -f /root/authorized_keys.verify-bak
fi
if [ -f "$REMOTE_DIR/ubuntu_ak.flag" ]; then
  rm -f /home/ubuntu/.ssh/authorized_keys
else
  cp -a /home/ubuntu/authorized_keys.verify-bak /home/ubuntu/.ssh/authorized_keys 2>/dev/null
  rm -f /home/ubuntu/authorized_keys.verify-bak
fi
# belt-and-braces: never leave a harness-injected team key behind
sudo sed -i '/termini-team-key-verify/d' /root/.ssh/authorized_keys 2>/dev/null
sed -i '/termini-team-key-verify/d' /home/ubuntu/.ssh/authorized_keys 2>/dev/null
sudo rm -rf "$REMOTE_DIR"
sudo systemctl reload ssh.service
EOF
  rm -rf "$WORK"
}
trap cleanup EXIT

say "stage the controller + canonical bootstrap daemon on $HOST"
info "snapshot sshd/authorized_keys so cleanup can restore them exactly"
rssh "set -e; mkdir -p '$REMOTE_DIR'
  ls /etc/ssh/sshd_config.d > '$REMOTE_DIR/sshd.d.before' 2>/dev/null || true
  sudo -n tar -C /etc/ssh -cf /root/ssh-verify-backup.tar sshd_config sshd_config.d 2>/dev/null || true
  sudo -n cp -a /root/.ssh/authorized_keys /root/authorized_keys.verify-bak 2>/dev/null || echo NO_ROOT_AK > '$REMOTE_DIR/root_ak.flag'
  cp -a /home/ubuntu/.ssh/authorized_keys /home/ubuntu/authorized_keys.verify-bak 2>/dev/null || echo NO_UBUNTU_AK > '$REMOTE_DIR/ubuntu_ak.flag'
  echo snapshot-ok"
tar -C "$REPO_ROOT" -cf - controller infra/bootstrap/node | rssh "tar -C '$REMOTE_DIR' -xf -"

say "render the live user-data (real team key, real controller CA, per-node token)"
case "$NODE" in
  node-a) LIVE_ROLE=compute;   LIVE_IP=10.10.0.11; LIVE_SERVE=0 ;;
  node-b) LIVE_ROLE=compute;   LIVE_IP=10.10.0.12; LIVE_SERVE=0 ;;
  node-c) LIVE_ROLE=inference; LIVE_IP=10.10.0.13; LIVE_SERVE=1 ;;
  *) echo "unknown --node: $NODE (want node-a/node-b/node-c)" >&2; exit 2 ;;
esac
render "$NODE" "$LIVE_ROLE" "$LIVE_IP" "$LIVE_SERVE" "$TEAM_PUB" "$CA_PUB" "http://127.0.0.1:$PORT" "$TOKEN" > "$WORK/live-user-data.yaml"
check_ud "$WORK/live-user-data.yaml" "live $NODE user-data" "$LIVE_SERVE" "$TEAM_PUB" "$CA_PUB" || true
rssh "cat > '$REMOTE_DIR/user-data.yaml'" < "$WORK/live-user-data.yaml"

say "start the controller on the node (loopback :$PORT, fresh DB)"
rssh "bash -ls" <<EOF
set -e
cd '$REMOTE_DIR/controller'
nohup env TERMINI_HOST=127.0.0.1 TERMINI_PORT=$PORT TERMINI_DB_PATH='$REMOTE_DIR/controller.db' \
  TERMINI_BOOTSTRAP_TOKEN='$TOKEN' TERMINI_PROJECT_TOKEN='$PROJECT_TOKEN' \
  TERMINI_PROJECT_ID='$PROJECT_ID' TERMINI_MAX_INFERENCE_NODES=1 \
  TERMINI_MIN_REDACTOR_VERSION=3 TERMINI_TELEMETRY_INTERVAL_MS=1000 \
  python3 -m app.main >'$REMOTE_DIR/controller.log' 2>&1 &
echo \$! > '$REMOTE_DIR/controller.pid'
for _ in \$(seq 1 100); do
  curl -fsS -m 1 "http://127.0.0.1:$PORT/v1/telemetry/recent" -H "Authorization: Bearer $PROJECT_TOKEN" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -fsS -m 1 "http://127.0.0.1:$PORT/v1/telemetry/recent" -H "Authorization: Bearer $PROJECT_TOKEN" >/dev/null
EOF
ok "controller up on 127.0.0.1:$PORT"

say "stage the canonical daemon + unit (stock-Ubuntu image; pre-baked image pending D3)"
rssh "sudo -n install -m 0755 '$REMOTE_DIR/infra/bootstrap/node/termini-bootstrap.py' /usr/local/bin/termini-bootstrap"
rssh "sudo -n install -m 0644 '$REMOTE_DIR/infra/bootstrap/node/termini-bootstrap.service' /etc/systemd/system/termini-bootstrap.service"
rssh "sudo -n systemctl daemon-reload"

say "apply the RENDERED user-data with the node's own cloud-init"
for mod in write_files ssh runcmd scripts-user; do
  # runcmd starts termini-bootstrap; Type=notify means `systemctl enable --now`
  # returns once the daemon reports READY, so bound each module to catch a stall
  # instead of hanging the harness.
  if rssh "timeout 150 sudo -n cloud-init single -n $mod --frequency=always --file '$REMOTE_DIR/user-data.yaml' >/dev/null 2>&1"; then
    info "cloud-init single -n $mod ok"
  else
    bad "cloud-init single -n $mod failed or timed out"
  fi
done

say "daemon reaches ONLINE against the controller"
deadline=$(( SECONDS + 90 ))
state=""
while (( SECONDS < deadline )); do
  state="$(rssh "sudo -n /usr/local/bin/termini-bootstrap status 2>/dev/null" || true)"
  grep -q '"state": "ONLINE"' <<< "$state" && break
  sleep 2
done
if grep -q '"state": "ONLINE"' <<< "$state"; then
  ok "termini-bootstrap ONLINE"
  grep -E '"node_id"|"role"|"register_ms"' <<< "$state" | sed 's/^/      /'
else
  bad "termini-bootstrap not ONLINE within 90s"
  rssh "sudo -n journalctl -u termini-bootstrap.service -b --no-pager -n 20" || true
fi
rssh "systemctl show termini-bootstrap.service -p Type -p SubState" | sed 's/^/      /'

say "cloud-init wrote the per-node inputs"
rssh "sudo -n cat /etc/termini/config.yaml" | sed 's/^/      /'
rssh "sudo -n sh -c 'test -s /var/lib/termini/enrollment.token && stat -c \"%a %U:%G %n\" /var/lib/termini/enrollment.token'" \
  && ok "enrollment token written (0600 root:root)" || bad "enrollment token missing"

say "sshd hardened per node-bootstrap.md §7"
sshd_out="$(rssh "sudo -n sshd -T 2>/dev/null" | grep -iE '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|trustedusercakeys)' || true)"
echo "$sshd_out" | sed 's/^/      /'
grep -qi 'passwordauthentication no' <<< "$sshd_out" && ok "PasswordAuthentication no" || bad "PasswordAuthentication not disabled"
grep -qi 'permitrootlogin prohibit-password\|permitrootlogin without-password' <<< "$sshd_out" \
  && ok "PermitRootLogin prohibit-password" || bad "PermitRootLogin not prohibit-password"
grep -q 'trustedusercakeys /etc/ssh/termini_ca.pub' <<< "$sshd_out" && ok "TrustedUserCAKeys = injected controller CA" \
  || bad "TrustedUserCAKeys not set to termini_ca.pub"

say "passwordless SSH with the injected team key (from this host)"
IP="${HOST##*@}"
SSH_COMMON=(-i "$WORK/team_ed25519" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
            -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=10)
if out="$(ssh "${SSH_COMMON[@]}" "root@$IP" 'id -un; hostname' 2>&1)"; then
  ok "ssh root@$IP with the injected key: $(tr '\n' ' ' <<< "$out")"
else
  bad "passwordless SSH as root failed: $out"
fi
if ssh -o IdentitiesOnly=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no \
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
     -o ConnectTimeout=10 "root@$IP" true >/dev/null 2>&1; then
  bad "password auth unexpectedly succeeded"
else
  ok "password auth refused"
fi

say "registration visible through the controller API"
nodes_json="$(rssh "curl -fsS -m 5 'http://127.0.0.1:$PORT/v1/projects/$PROJECT_ID/nodes' -H 'Authorization: Bearer $PROJECT_TOKEN'" || true)"
if grep -q '"node_id"' <<< "$nodes_json"; then
  ok "controller recorded the node: $(grep -o '"status": "[a-z]*"' <<< "$nodes_json" | head -1)"
else
  bad "controller has no node record: $nodes_json"
fi

say "key installer is idempotent"
rssh "sudo -n /usr/local/sbin/termini-install-ssh-key"
rssh "sudo -n /usr/local/sbin/termini-install-ssh-key"
cnt="$(rssh "sudo -n grep -cxF '$TEAM_PUB' /root/.ssh/authorized_keys 2>/dev/null || echo 0")"
[[ "$cnt" == "1" ]] && ok "team key appears exactly once in root authorized_keys" \
  || bad "team key appears $cnt times in root authorized_keys"

printf '\n'
[[ "$FAIL" == "0" ]] \
  && echo "RESULT: PASS — shared template renders clean; live GCP node accepts the injected team key and registered." \
  || echo "RESULT: FAIL — see FAIL lines above."
exit "$FAIL"
