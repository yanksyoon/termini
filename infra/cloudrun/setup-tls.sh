#!/usr/bin/env bash
# Terminate TLS in front of the Termini controller.
#
# Public HTTPS is not optional here: the dashboard is served from Firebase
# Hosting over https://, and a browser will refuse to call a plain-HTTP
# controller from it (mixed content). This is the one thing Cloud Run would
# have given for free — see issue #96.
#
# The hostname is an sslip.io wildcard-DNS name that resolves to the VM's
# static external IP, so no domain purchase is needed and Let's Encrypt can
# still issue a real certificate.
set -euo pipefail

HOST="34-93-190-64.sslip.io"     # sslip.io maps this to the VM's static IP 34.93.190.64
UPSTREAM="127.0.0.1:8080"        # the controller, loopback-only

echo "==> installing caddy"
sudo apt-get update -qq
sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq caddy >/dev/null
caddy version

echo "==> writing Caddyfile for $HOST -> $UPSTREAM"
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$HOST {
	encode gzip
	reverse_proxy $UPSTREAM
}
CADDY

echo "==> reloading caddy (will obtain a Let's Encrypt certificate)"
sudo systemctl enable --now caddy >/dev/null 2>&1 || true
sudo systemctl restart caddy
sleep 25

echo "==> caddy status: $(sudo systemctl is-active caddy)"
echo "==> certificate state:"
sudo journalctl -u caddy --no-pager -n 40 | grep -iE "certificate|obtain|error|acme" | tail -8 || true

echo "==> verifying HTTPS round-trip from the VM itself"
curl -sS -o /dev/null -w "  https http=%{http_code}  cert_verify=%{ssl_verify_result}\n" \
  "https://$HOST/v1/projects/01J0DEM0WARM000000000000AB/nodes" \
  -H "Authorization: Bearer demo-project-token" || true
