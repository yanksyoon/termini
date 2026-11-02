#!/bin/sh
# termini-vertex-token-refresh — mint a Vertex AI access token on a node that is
# NOT a GCE instance, from the node's own service-account key.
#
# Why this exists: the inference proxy's forwarded backend (`gemini-proxy`) mints
# its OAuth token from the GCE metadata server, which a node running as a local
# LXD VM does not have. This is the same identity (a service account with
# roles/aiplatform.user), obtained from a key file instead of the metadata
# service, and rotated on a timer so no long-lived token is ever written.
#
# It writes the token into the proxy's EnvironmentFile and restarts the unit ONLY
# when the token actually changed, so a 40-minute tick does not bounce the front
# door while it is serving.
set -eu

SA_FILE="${SA_FILE:-/etc/termini/vertex-sa.json}"
ENV_FILE="${ENV_FILE:-/etc/termini/inference-proxy.env}"
TOKEN_FILE="${TOKEN_FILE:-/etc/termini/vertex-access-token}"
UNIT="${UNIT:-termini-inference-proxy.service}"

[ -r "$SA_FILE" ] || { echo "no service-account key at $SA_FILE" >&2; exit 1; }

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$SA_FILE" "$tmp" <<'PY'
import json, sys, time
sa = json.load(open(sys.argv[1]))
out = sys.argv[2]
hdr = {"alg": "RS256", "typ": "JWT"}
now = int(time.time())
claims = {
    "iss": sa["client_email"],
    "scope": "https://www.googleapis.com/auth/cloud-platform",
    "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
    "iat": now,
    "exp": now + 3600,
}
json.dump(hdr, open(out + "/hdr.json", "w"))
json.dump(claims, open(out + "/claims.json", "w"))
open(out + "/key.pem", "w").write(sa["private_key"])
open(out + "/token_uri", "w").write(sa.get("token_uri", "https://oauth2.googleapis.com/token"))
PY

H="$(b64url < "$tmp/hdr.json")"
C="$(b64url < "$tmp/claims.json")"
printf '%s.%s' "$H" "$C" > "$tmp/signing-input"
openssl dgst -sha256 -sign "$tmp/key.pem" -out "$tmp/sig" "$tmp/signing-input"
S="$(b64url < "$tmp/sig")"
ASSERTION="$(cat "$tmp/signing-input").$S"
TOKEN_URI="$(cat "$tmp/token_uri")"

RESP="$(curl -fsS -m 20 -X POST "$TOKEN_URI" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer' \
  --data-urlencode "assertion=$ASSERTION")" || {
  echo "token exchange failed" >&2; exit 1; }

NEW="$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
[ -n "$NEW" ] || { echo "token exchange returned no access_token" >&2; exit 1; }

printf '%s\n' "$NEW" > "$TOKEN_FILE"
chmod 0600 "$TOKEN_FILE"

# Keep the EnvironmentFile authoritative: replace the token line (or append it).
if grep -q '^TERMINI_GEMINI_ACCESS_TOKEN=' "$ENV_FILE" 2>/dev/null; then
  OLD="$(grep '^TERMINI_GEMINI_ACCESS_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  sed -i "s|^TERMINI_GEMINI_ACCESS_TOKEN=.*|TERMINI_GEMINI_ACCESS_TOKEN=$NEW|" "$ENV_FILE"
else
  OLD=""
  printf 'TERMINI_GEMINI_ACCESS_TOKEN=%s\n' "$NEW" >> "$ENV_FILE"
fi
# The project id is a constant of the deployment, not of the token.
if ! grep -q '^TERMINI_GEMINI_PROJECT=' "$ENV_FILE" 2>/dev/null; then
  printf 'TERMINI_GEMINI_PROJECT=%s\n' "${GEMINI_PROJECT:-termini-510609}" >> "$ENV_FILE"
fi

if [ "$OLD" != "$NEW" ]; then
  systemctl restart "$UNIT"
  echo "token rotated; $UNIT restarted"
else
  echo "token unchanged"
fi
