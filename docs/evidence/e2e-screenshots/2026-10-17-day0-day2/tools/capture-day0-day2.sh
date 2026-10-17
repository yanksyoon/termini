#!/usr/bin/env bash
# Day-0 → day-2, ONE machine, on the deployed fleet — reproducible capture.
#
# Produces raw/NN-*.txt transcripts (the train of truth: every command and its real
# output). PNGs are rendered from those transcripts by tools/render-terminal-png.sh;
# the dashboard frames are captured in a browser at a stated viewport.
#
# Usage: bash tools/capture-day0-day2.sh [start-step]
#   Steps: 01 image  02 create  03 registered  04 fleet  05 trust  06 workload
#          07 inject 08 decision 09 audit
#
# Requires: gcloud authenticated to termini-510609, the live-window lease held, and
# ~4 free vCPU. The enrollment token is minted on the controller's LOOPBACK route and
# rendered into this VM's cloud-init, so the node registers with a token that exists
# exactly once and is burned by the first registration.
set -uo pipefail
export PATH=$PATH:/home/ubuntu/google-cloud-sdk/bin

HERE="$(cd "$(dirname "$0")/.." && pwd)"
RAW="$HERE/raw"
mkdir -p "$RAW"
ZONE=asia-south1-a
NODE=termini-day0
IP=10.10.0.14
C=https://34-93-190-64.sslip.io/v1
P=01J0DEM0WARM000000000000AB
T=demo-project-token
START="${1:-01}"

cap() {  # cap <slug> <command...>
  local slug="$1"; shift
  local out="$RAW/$slug.txt"
  { echo "\$ $*"; echo "# captured $(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo; } > "$out"
  bash -c "$*" >> "$out" 2>&1
  echo "--- $(basename "$out"): $(wc -l < "$out") lines"
}

step_at_least() { [ "$((10#$1))" -ge "$((10#$START))" ]; }

mint_token() {  # -> /tmp/day0-token.txt (loopback-only route ON the controller)
  gcloud compute ssh termini-controller --zone="$ZONE" --quiet --command \
    "curl -sS -X POST http://127.0.0.1:8080/v1/bootstrap-tokens -H 'Authorization: Bearer $T' -H 'Content-Type: application/json' -d '{\"count\":1}'" \
    > /tmp/day0-token.json 2>/dev/null
  python3 -c "import json;d=json.load(open('/tmp/day0-token.json'));print('minted',d['count'],'single-use token(s) at',d['created_at']);open('/tmp/day0-token.txt','w').write(d['tokens'][0])"
}

render_cloud_init() {  # $1 = enrollment token -> /tmp/day0-cloud-init.yaml
  python3 - "$1" <<'PY'
import sys
token = sys.argv[1]
open("/tmp/day0-cloud-init.yaml", "w").write(f"""#cloud-config
hostname: node-d
fqdn: node-d
prefer_fqdn_over_hostname: false
write_files:
  - path: /etc/termini/config.yaml
    permissions: '0600'
    owner: root:root
    content: |
      controller_url: http://10.10.0.2:8080
      node_name: node-d
      role_hint: compute
      iface: ens4
      underlay_ip: 10.10.0.14/24
      underlay_gw: 10.10.0.1
      bootstrap_seed_ip: 10.10.0.14
      telemetry_interval_ms: 1000
  - path: /var/lib/termini/enrollment.token
    permissions: '0600'
    owner: root:root
    content: |
      {token}
runcmd:
  - [ systemctl, enable, --now, termini-bootstrap.service ]
""")
PY
}

node_id_from_node() {  # authoritative id: the node's own persisted identity
  gcloud compute ssh "$NODE" --zone="$ZONE" --quiet --command \
    "sudo python3 -c \"import json;print(json.load(open('/var/lib/termini/identity/node.json'))['node_id'])\"" 2>/dev/null | tr -d '\r' | tail -1
}

step_at_least 01 && cap 01-image \
  "gcloud compute images list --no-standard-images --filter='family=termini-node-os' --format='table(name,family,status,creationTimestamp)'"

if step_at_least 02; then
  mint_token
  render_cloud_init "$(cat /tmp/day0-token.txt)"
  cap 02-create "date -u +%Y-%m-%dT%H:%M:%SZ; gcloud compute instances create $NODE --zone=$ZONE --machine-type=e2-standard-2 --image-family=termini-node-os --boot-disk-size=20GB --network=termini-vpc --subnet=termini-underlay --private-network-ip=$IP --no-address --tags=termini-node --metadata-from-file=user-data=/tmp/day0-cloud-init.yaml --quiet; date -u +%Y-%m-%dT%H:%M:%SZ"
fi

step_at_least 03 && cap 03-registered \
  "for i in \$(seq 1 24); do S=\$(gcloud compute ssh $NODE --zone=$ZONE --quiet --command 'sudo termini-bootstrap status' 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)[\"state\"])' 2>/dev/null); echo \"\$(date -u +%H:%M:%SZ) state=\$S\"; [ \"\$S\" = ONLINE ] && break; sleep 5; done; gcloud compute ssh $NODE --zone=$ZONE --quiet --command=\"sudo journalctl -u termini-bootstrap --no-pager -o short-iso | grep -E 'REGISTERED|LXD_TRUST|Starting'; echo; sudo termini-bootstrap status\""

step_at_least 04 && cap 04-fleet \
  "curl -sS -H 'Authorization: Bearer $T' '$C/projects/$P/nodes' | python3 -c \"import json,sys;d=json.load(sys.stdin);[print(n['node_id'],n['role'],n['status'],'synthetic='+str(n['synthetic']),n['last_seen_at']) for n in d['nodes']]\""

step_at_least 05 && cap 05-trust \
  "gcloud compute ssh termini-controller --zone=$ZONE --quiet --command=\"sudo journalctl -u termini-controller --no-pager -o short-iso | grep -E 'LXD trust' | tail -5\""

step_at_least 06 && cap 06-workload \
  "gcloud compute ssh $NODE --zone=$ZONE --quiet --command=\"date -u +%H:%M:%SZ; sudo lxc launch agent-vm/2026.10.0 demo-workload 2>&1 | tail -2; date -u +%H:%M:%SZ; sudo lxc list --format=csv -c ns4\""

if step_at_least 07; then
  NID="$(node_id_from_node)"
  cap 07-inject "echo node_id=$NID; curl -sS -X POST '$C/projects/$P/demo/faults' -H 'Authorization: Bearer $T' -H 'Content-Type: application/json' -d '{\"scenario\":\"oom\",\"node_id\":\"$NID\",\"target\":\"real\",\"duration_s\":120}'"
fi

step_at_least 08 && cap 08-decision \
  "sleep 12; curl -sS -H 'Authorization: Bearer $T' '$C/operator/decisions?limit=6' | python3 -c \"import json,sys;[print(r['decided_at'],r['rule_id'],'->',r['action_kind'],'src='+str(r.get('decision_source')),'conf='+str(r.get('confidence')),r.get('action_params')) for r in json.load(sys.stdin)['decisions'][:6]]\""

step_at_least 09 && cap 09-audit \
  "curl -sS -H 'Authorization: Bearer $T' '$C/operator/audits?limit=6' | python3 -c \"import json,sys;[print(r['executed_at'],r['rule_id'],r['action_kind'],'result='+str(r['result']),'ep='+str(r.get('lxd_endpoint')),'path='+str(r.get('lxd_path')),'|',str(r.get('detail'))[:120]) for r in json.load(sys.stdin)['audits'][:6]]\"; echo; gcloud compute ssh $NODE --zone=$ZONE --quiet --command=\"sudo lxc list --format=csv -c ns4; sudo lxc info demo-workload | grep -iE '^Status|PID'; sudo lxc exec demo-workload -- uptime\""
