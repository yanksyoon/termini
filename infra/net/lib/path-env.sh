#!/usr/bin/env bash
# path-env.sh — shared resolution of the Termini inference network path.
#
# Source-only (no side effects). Owns the single place where the demo decides
# which network path carries llama.cpp traffic and what MTU that path has.
# See infra/net/README.md for the runbook.
#
# Config is a JSON file (`network.example.json`); override its location with
# TERMINI_NET_CONFIG. The active path can be forced for one process with
# TERMINI_INFERENCE_PATH=direct|ovn, which always wins over the file.

TERMINI_NET_CONFIG_DEFAULT="/etc/termini/network.json"
TERMINI_INFERENCE_PATH_DEFAULT="direct"

termini_net_config() {
  printf '%s\n' "${TERMINI_NET_CONFIG:-$TERMINI_NET_CONFIG_DEFAULT}"
}

termini_config_exists() {
  [ -r "$(termini_net_config)" ]
}

# Paths defined in the example config; used for validation and help output.
termini_known_paths() {
  printf '%s\n' "direct" "ovn"
}

# MTU profile: env override wins, else config "active_profile", else onprem.
termini_active_profile() {
  if [ -n "${TERMINI_MTU_PROFILE:-}" ]; then
    printf '%s\n' "$TERMINI_MTU_PROFILE"
    return 0
  fi
  if termini_config_exists; then
    local profile
    profile="$(jq -r '.active_profile // empty' <"$(termini_net_config)" 2>/dev/null)"
    if [ -n "$profile" ]; then
      printf '%s\n' "$profile"
      return 0
    fi
  fi
  printf '%s\n' "onprem"
}

termini_path_valid() {
  case "${1:-}" in
    direct | ovn) return 0 ;;
    *) return 1 ;;
  esac
}

# Active path: env override (one-shot) wins, else the config's "active", else default.
termini_active_path() {
  if [ -n "${TERMINI_INFERENCE_PATH:-}" ]; then
    printf '%s\n' "$TERMINI_INFERENCE_PATH"
    return 0
  fi
  if termini_config_exists; then
    local active
    active="$(jq -r '.active // empty' <"$(termini_net_config)" 2>/dev/null)"
    if [ -n "$active" ]; then
      printf '%s\n' "$active"
      return 0
    fi
  fi
  printf '%s\n' "$TERMINI_INFERENCE_PATH_DEFAULT"
}

# Read one field from a path entry: termini_path_field <path> <field> [default]
termini_path_field() {
  local path="$1" field="$2" fallback="${3:-}"
  if ! termini_config_exists; then
    printf '%s\n' "$fallback"
    return 0
  fi
  local value
  value="$(jq -r --arg p "$path" --arg f "$field" '.paths[$p][$f] // empty' <"$(termini_net_config)" 2>/dev/null)"
  if [ -n "$value" ]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$fallback"
  fi
}

# Full inference base URL for the active path, honoring an explicit override.
termini_inference_url() {
  if [ -n "${INFERENCE_BASE_URL:-}" ]; then
    printf '%s\n' "$INFERENCE_BASE_URL"
    return 0
  fi
  local path host port
  path="$(termini_active_path)"
  host="$(termini_path_field "$path" "host" "10.10.0.13")"
  port="$(termini_path_field "$path" "port" "8080")"
  printf 'http://%s:%s\n' "$host" "$port"
}

# Path MTU in bytes (what the payload may be, encap overhead already subtracted).
# Resolution: mtu_profiles[<profile>][<path>] -> paths[<path>].mtu -> 1500.
termini_path_mtu() {
  local path="$1" profile value
  profile="$(termini_active_profile)"
  if termini_config_exists; then
    value="$(jq -r --arg p "$path" --arg prof "$profile" \
      '.mtu_profiles[$prof][$p] // .paths[$p].mtu // empty' <"$(termini_net_config)" 2>/dev/null)"
    if [ -n "$value" ]; then
      printf '%s\n' "$value"
      return 0
    fi
  fi
  printf '%s\n' "1500"
}

# Interface carrying the path, used for MTU cross-checks on the node.
termini_path_interface() {
  local path="$1"
  termini_path_field "$path" "interface" ""
}

termini_path_next_hop() {
  local path="$1"
  termini_path_field "$path" "next_hop" ""
}
