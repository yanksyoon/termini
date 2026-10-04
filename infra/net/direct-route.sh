#!/usr/bin/env bash
# direct-route.sh — provision and select the Termini inference network path.
#
# Part of kanban t_bcae0231. It pins the llama.cpp data path to a host-level
# underlay route ('direct', the frozen demo path, docs/contracts/infra-decisions.md
# D2) and keeps the OVN overlay ('ovn') available as a documented fallback.
#
#   ./direct-route.sh status                 # active path, URL, MTUs
#   ./direct-route.sh env                    # export lines for the pilot/demo
#   ./direct-route.sh select direct          # persist the toggle
#   ./direct-route.sh up                     # apply route (needs root)
#   ./direct-route.sh up --dry-run           # print the exact commands
#   ./direct-route.sh down                   # remove route (needs root)
#   ./direct-route.sh verify                 # DF-bit probe on both paths
#
# Toggle precedence: TERMINI_INFERENCE_PATH env var  >  config "active".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/path-env.sh
. "${HERE}/lib/path-env.sh"

DRY_RUN=0
SEL=""          # positional for `select`

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    status | env | up | down | verify | init) cmd="$1"; shift ;;
    select) cmd="select"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage 0 ;;
    -*)
      echo "unknown flag: $1" >&2
      usage 1
      ;;
    *)
      SEL="$1"
      shift
      ;;
  esac
done

[ -n "$cmd" ] || usage 1

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "$cmd needs root (use --dry-run to preview)" >&2; exit 2; }
}

case "$cmd" in
  status)
    active="$(termini_active_path)"
    profile="$(termini_active_profile)"
    printf 'config          : %s\n' "$(termini_net_config)"
    printf 'config present  : %s\n' "$(termini_config_exists && echo yes || echo no)"
    printf 'active path     : %s\n' "$active"
    printf 'mtu profile     : %s\n' "$profile"
    printf 'inference url   : %s\n' "$(termini_inference_url)"
    while IFS= read -r p; do
      iface="$(termini_path_interface "$p")"
      iface_mtu="n/a"
      if [ -n "$iface" ] && [ -e "/sys/class/net/${iface}/mtu" ]; then
        iface_mtu="$(cat "/sys/class/net/${iface}/mtu")"
      fi
      printf '  %-7s mtu=%-4s overhead=%-2s interface=%-8s iface_mtu=%s\n' \
        "$p" "$(termini_path_mtu "$p")" "$(termini_path_field "$p" encap_overhead 0)" \
        "${iface:-none}" "$iface_mtu"
    done < <(termini_known_paths)
    ;;

  env)
    active="$(termini_active_path)"
    printf 'export TERMINI_INFERENCE_PATH=%s\n' "$active"
    printf 'export INFERENCE_BASE_URL=%s\n' "$(termini_inference_url)"
    printf 'export TERMINI_MTU_PROFILE=%s\n' "$(termini_active_profile)"
    ;;

  select)
    if [ -z "$SEL" ]; then
      echo "usage: direct-route.sh select <direct|ovn>" >&2
      exit 2
    fi
    termini_path_valid "$SEL" || { echo "unknown path: $SEL (want direct|ovn)" >&2; exit 2; }
    if ! termini_config_exists; then
      echo "no config at $(termini_net_config); run 'direct-route.sh init' first" >&2
      exit 2
    fi
    tmp="$(mktemp)"
    jq --arg p "$SEL" '.active = $p' <"$(termini_net_config)" >"$tmp"
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "[dry-run] would set .active=$SEL in $(termini_net_config)"
      rm -f "$tmp"
    else
      cat "$tmp" >"$(termini_net_config)"
      rm -f "$tmp"
      echo "active inference path -> $SEL"
    fi
    ;;

  init)
    if ! termini_config_exists; then
      if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would install ${HERE}/network.example.json to $(termini_net_config)"
      else
        require_root
        install -d -m 0755 "$(dirname "$(termini_net_config)")"
        install -m 0644 "${HERE}/network.example.json" "$(termini_net_config)"
        echo "installed $(termini_net_config)"
      fi
    else
      echo "config already present at $(termini_net_config)"
    fi
    ;;

  up | down)
    if [ "$DRY_RUN" -ne 1 ]; then
      require_root
    fi
    active="$(termini_active_path)"
    iface="$(termini_path_interface "$active")"
    next_hop="$(termini_path_next_hop "$active")"
    host="$(termini_path_field "$active" host "10.10.0.13")"
    mtu="$(termini_path_mtu "$active")"
    [ -n "$iface" ] || { echo "path '$active' has no interface in config" >&2; exit 2; }

    route_args=(route replace "${host}/32" dev "$iface")
    [ -n "$next_hop" ] && route_args+=(via "$next_hop")
    if [ "$cmd" = "up" ]; then
      run ip "${route_args[@]}"
      # Clamp the guest bridge MTU to the path MTU so guests never emit a frame
      # the underlay must fragment (the D2 failure mode).
      if [ -e /sys/class/net/lxdbr0 ]; then
        run ip link set lxdbr0 mtu "$mtu"
      fi
      echo "direct route to ${host} via ${iface} (path MTU ${mtu}) is up"
    else
      run ip route del "${host}/32" dev "$iface" || true
      echo "removed route to ${host} on ${iface}"
    fi
    ;;

  verify)
    # The acceptance test for this card, run against whatever host/host-set the
    # config points at: direct passes a DF probe at the direct-path MTU, the
    # overlay fails the same probe at that MTU (proving the 50-byte encap cost),
    # and the overlay passes at its own (tunnel-size) MTU.
    rc=0
    direct_mtu="$(termini_path_mtu direct)"
    ovn_mtu="$(termini_path_mtu ovn)"
    direct_host="$(termini_path_field direct host "10.10.0.13")"
    ovn_host="$(termini_path_field ovn host "$direct_host")"

    echo "-- direct path, DF payload $((direct_mtu - 28)) (expect pass)"
    "${HERE}/mtu-probe.sh" --path direct --host "$direct_host" --mtu "$direct_mtu" --expect pass || rc=1

    echo "-- overlay path, DF payload $((direct_mtu - 28)) (expect fail: encap > path MTU)"
    "${HERE}/mtu-probe.sh" --path ovn --host "$ovn_host" --mtu "$direct_mtu" --expect fail || rc=1

    echo "-- overlay path, DF payload $((ovn_mtu - 28)) (tunnel size, expect pass)"
    "${HERE}/mtu-probe.sh" --path ovn --host "$ovn_host" --mtu "$ovn_mtu" --expect pass || rc=1

    [ "$rc" -eq 0 ] && echo "VERIFY: PASS" || echo "VERIFY: FAIL"
    exit "$rc"
    ;;
esac
