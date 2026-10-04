#!/usr/bin/env bash
# net-accounting.sh — boot-time network accounting from the kernel's own
# interface counters (/proc/net/dev).
#
# This is the MEASUREMENT behind `network_downloads_at_boot`. It reads live
# kernel counters; it never trusts a value that was previously written into a
# boot log (that circular check is the bug this file exists to kill).
#
# Sourced by `first-boot-preflight.sh` and `tools/offline-acceptance.sh`, and
# runnable standalone for tests/rehearsal:
#
#   net-accounting.sh            # human summary of non-loopback counters
#   net-accounting.sh --json     # JSON metrics
#   net-accounting.sh --check    # exit 1 if a real download happened
#
# What counts as a download (RFC 1122/3927/4861 distinctions):
#   * RX bytes on every interface except `lo`, i.e. everything the machine
#     *received* that could be a remote payload;
#   * MINUS a small, documented allowance for the link-local / DHCP / IPv6-ND
#     chatter that any boot on a managed bridge produces (DHCPv4 DORA, IPv6
#     router/neighbor solicitation + advertisement). The allowance is the only
#     heuristic here and it is explicit + configurable so it can be audited.
#   A genuine fetch (an apt/pip/npm/HTTP pull) is orders of magnitude larger
#   than the allowance, so the check fails closed on it.
#
# Env knobs:
#   TERMINI_PROC_NET_DEV                    override the counter source (tests)
#   TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES link-local/DHCP budget (default 8192)
#
# shellcheck shell=bash disable=SC2034

TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES="${TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES:-8192}"

# termini_net_counters [proc_net_dev]
#   Prints one line: "<rx_bytes> <tx_bytes> <rx_packets> <tx_packets> <n_ifaces> <iface,name,list>"
#   summing every non-loopback interface in a /proc/net/dev-style file.
termini_net_counters() {
  local src="${1:-${TERMINI_PROC_NET_DEV:-/proc/net/dev}}"
  if [ ! -r "$src" ]; then
    printf '0 0 0 0 0 \n'
    return 0
  fi
  awk '
    /:/ {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      c = index(line, ":")
      if (c == 0) next
      iface = substr(line, 1, c - 1); gsub(/[[:space:]]/, "", iface)
      if (iface == "" || iface == "lo") next
      rest = substr(line, c + 1)
      sub(/^[[:space:]]+/, "", rest)
      split(rest, f, /[[:space:]]+/)
      rb += f[1] + 0; rp += f[2] + 0; tb += f[9] + 0; tp += f[10] + 0
      n++
      ifs = (ifs == "" ? iface : ifs "," iface)
    }
    END { printf "%d %d %d %d %d %s\n", rb, tb, rp, tp, n, ifs }
  ' "$src"
}

# termini_net_downloaded_bytes <rx_bytes>
#   Bytes received beyond the link-local/DHCP allowance, floored at zero.
termini_net_downloaded_bytes() {
  local rx="${1:-0}" allow="${TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES:-8192}"
  if [ "$rx" -gt "$allow" ] 2>/dev/null; then
    printf '%d\n' "$((rx - allow))"
  else
    printf '0\n'
  fi
}

termini_net_json() {
  local src="${TERMINI_PROC_NET_DEV:-/proc/net/dev}"
  local rb tb rp tp nif ifs dl
  read -r rb tb rp tp nif ifs <<<"$(termini_net_counters "$src")"
  ifs="${ifs:-}"
  dl="$(termini_net_downloaded_bytes "${rb:-0}")"
  # build the interface array JSON
  local arr=""
  local IFS=','
  for i in $ifs; do
    [ -n "$i" ] || continue
    arr="${arr:+$arr,}\"$i\""
  done
  unset IFS
  printf '{"method":"proc_net_dev","source":"%s","measured":true,"allowance_bytes":%s,"rx_bytes":%s,"tx_bytes":%s,"rx_packets":%s,"tx_packets":%s,"nonloopback_interfaces":[%s],"downloaded_bytes":%s}\n' \
    "$src" "$TERMINI_LINK_LOCAL_DHCP_ALLOWANCE_BYTES" "${rb:-0}" "${tb:-0}" "${rp:-0}" "${tp:-0}" "$arr" "$dl"
}

termini_net_main() {
  case "${1:-}" in
    --json) termini_net_json ;;
    --check)
      local line rb dl
      line="$(termini_net_counters)"
      rb="$(printf '%s' "$line" | awk '{print $1}')"
      dl="$(termini_net_downloaded_bytes "${rb:-0}")"
      if [ "$dl" = 0 ]; then
        printf 'network_downloads_at_boot=0 (rx_bytes=%s)\n' "${rb:-0}"
        return 0
      fi
      printf 'network_downloads_at_boot=%s (rx_bytes=%s)\n' "$dl" "${rb:-0}"
      return 1
      ;;
    ''|*) termini_net_json ;;
  esac
}

# Only run when executed directly — sourcing must define functions only.
if [ "${BASH_SOURCE[0]:-}" = "${0:-}" ]; then
  termini_net_main "$@"
fi
