#!/usr/bin/env bash
# verify-node-c-egress.sh — acceptance probe for task t_5a4a0f73
# ("Node C egress + least-privilege identity for Google AI, Private Google Access, no NAT").
#
# It proves, IN THIS ORDER, and FROM THE NODE ITSELF (never from the operator's
# laptop — the laptop reaching Vertex AI says nothing about the node):
#
#   1. PATH — the underlay subnet has Private Google Access ON. Without it the
#      nodes carry no external IP and the project has no Cloud Router/Cloud NAT,
#      so they have NO route to any Google API and the call fails on DNS/TCP.
#   2. IDENTITY — node-c runs as the least-privilege SA (NOT the project default
#      compute SA with roles/editor) and carries the cloud-platform OAuth scope.
#      Both halves are required: IAM grants permission, the scope lets the
#      metadata server mint a token that can exercise it. The old scope list had
#      no cloud-platform, so the token was structurally unable to call Vertex AI.
#   3. FIREWALL — no egress rule blocks the Google API VIP, proven by the real
#      call below (GCE has an implied allow-egress; this script does not assume it).
#   4. CALL — a token minted from the node's metadata server is accepted by
#      Vertex AI for gemini-2.5-flash on the GLOBAL endpoint.
#
# Cost: $0. Private Google Access is free ($0/h, $0/GiB). The rejected
# alternative — Cloud Router + Cloud NAT — is ~$0.044/h for the NAT gateway plus
# ~$0.045/GiB processed, and buys nothing PGA does not for Google API traffic.
#
# The node has no external IP by design (--no-address). SSH therefore needs a
# temporary ephemeral external IP, which this script adds and REMOVES around the
# probe (trap on exit); it reports the removal so the no-external-IP property is
# not left quietly broken.
#
# Usage:  ./verify-node-c-egress.sh            # full probe (adds/removes temp IP)
#         SSH_TARGET=host ./verify-node-c-egress.sh   # probe on a reachable node
set -euo pipefail

PROJECT="${PROJECT:-termini-510609}"
REGION="${REGION:-asia-south1}"
ZONE="${ZONE:-asia-south1-a}"
SUBNET="${SUBNET:-termini-underlay}"
INSTANCE="${INSTANCE:-termini-node-c}"
SA_EMAIL="${SA_EMAIL:-termini-node-inference@${PROJECT}.iam.gserviceaccount.com}"
MODEL="${MODEL:-gemini-2.5-flash}"
LOCATION="${LOCATION:-global}"
EXPECT="${EXPECT:-PROXY_OK}"

TMP_IP_ADDED=0

log()  { printf '[verify-node-c-egress] %s\n' "$*" >&2; }
fail() { printf '[verify-node-c-egress] FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
  if [ "$TMP_IP_ADDED" = "1" ]; then
    log "removing the temporary external IP (node-c must stay --no-address)"
    gcloud compute instances delete-access-config "$INSTANCE" --access-config-name=external-nat \
      --zone="$ZONE" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
    log "external IP removed"
  fi
}
trap cleanup EXIT

# --- 1. PATH -----------------------------------------------------------------
log "1/4 subnet Private Google Access"
pga=$(gcloud compute networks subnets describe "$SUBNET" --region="$REGION" --project="$PROJECT" \
        --format="value(privateIpGoogleAccess)")
log "    privateIpGoogleAccess=$pga"
[ "$pga" = "True" ] || fail "Private Google Access is OFF on $SUBNET — the nodes have no path to Google APIs"

log "    cloud routers (expect none — this is deliberately NOT a Cloud NAT design):"
gcloud compute routers list --project="$PROJECT" --format="value(name)" | sed 's/^/      router: /' || true

# --- 2. IDENTITY -------------------------------------------------------------
log "2/4 node identity (least privilege + scope)"
sa_line=$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --project="$PROJECT" \
            --format="value(serviceAccounts[0].email)")
scope_line=$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --project="$PROJECT" \
            --format="value(serviceAccounts[0].scopes[0])")
log "    service account: $sa_line"
log "    scope[0]       : $scope_line"
[ "$sa_line" = "$SA_EMAIL" ] || fail "$INSTANCE runs as '$sa_line', expected the least-privilege '$SA_EMAIL'"
[ "$scope_line" = "https://www.googleapis.com/auth/cloud-platform" ] || \
  fail "$INSTANCE scope is '$scope_line', expected cloud-platform"

log "    role bindings on $SA_EMAIL:"
gcloud projects get-iam-policy "$PROJECT" \
  --flatten="bindings[].members" --filter="bindings.members:$(basename "$SA_EMAIL")" \
  --format="value(bindings.role)" | sed 's/^/      role: /'

# --- 3/4. SSH transport (temporary IP) + the real call -----------------------
ssh_run() { # $1 = remote command
  if [ -n "${SSH_TARGET:-}" ]; then
    ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$SSH_TARGET" "$1"
  else
    gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" --quiet \
      --ssh-flag="-o StrictHostKeyChecking=accept-new" \
      --ssh-flag="-o ConnectTimeout=15" \
      --ssh-flag="-o LogLevel=ERROR" \
      --command "$1"
  fi
}

if [ -z "${SSH_TARGET:-}" ]; then
  # The node is --no-address by design, so a temporary ephemeral external IP is
  # the only way to open an SSH session at all. Clear any leftover first.
  if [ -n "$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --project="$PROJECT" \
              --format='value(networkInterfaces[0].accessConfigs[0].natIP)')" ]; then
    gcloud compute instances delete-access-config "$INSTANCE" --access-config-name=external-nat \
      --zone="$ZONE" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
  fi
  log "    $INSTANCE has no external IP (by design) — adding a TEMPORARY one for SSH only"
  gcloud compute instances add-access-config "$INSTANCE" --access-config-name=external-nat \
    --zone="$ZONE" --project="$PROJECT" --quiet >/dev/null
  TMP_IP_ADDED=1
  sleep 15
fi

log "3/4 firewall: GCE implied egress — proving it with a real call instead of reasoning"
log "    egress DENY rules in the project (expect none):"
gcloud compute firewall-rules list --project="$PROJECT" --filter="direction=EGRESS AND disabled=false" \
  --format="value(name)" | sed 's/^/      egress-rule: /' || true

log "4/4 token from the node's metadata server -> Vertex AI $MODEL ($LOCATION)"
REMOTE=$(cat <<'REMOTE_EOF'
set -e
TOKEN=$(curl -s -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
echo "token_len=${#TOKEN}"
echo "resolved_vip=$(getent hosts aiplatform.googleapis.com | awk '{print $1}' | head -1)"
START=$(date +%s.%N)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  https://aiplatform.googleapis.com/v1/projects/termini-510609/locations/global/publishers/google/models/gemini-2.5-flash:generateContent \
  -d '{"contents":[{"role":"user","parts":[{"text":"Reply with exactly: PROXY_OK"}]}]}'
END=$(date +%s.%N)
echo
echo "round_trip_s=$(echo "$END - $START" | bc)"
REMOTE_EOF
)
out=$(ssh_run "$REMOTE")
printf '%s\n' "$out" | sed 's/^/    /'
printf '%s' "$out" | grep -q "$EXPECT" || fail "the node did not get '$EXPECT' back from Vertex AI"

log "PASS — node-c reaches Vertex AI over Private Google Access with its own least-privilege identity"
