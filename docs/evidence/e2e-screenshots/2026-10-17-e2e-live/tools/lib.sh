# shellcheck shell=bash
# Shared helpers for the backend E2E capture steps (t_2191eca4).
#
# Every step snippet is executed through `script -qec` by capture-backend-live.sh,
# so its transcript is what a reader verifies. Nothing in here rewrites command
# output: `run` prints the command it is about to execute (prefixed with `$ `) and
# then runs it; `cmdx` prints a command that is executed as part of a pipeline.

API="${API:-https://34-93-190-64.sslip.io/v1}"
PROJ="${PROJ:-01J0DEM0WARM000000000000AB}"
TOKEN="${TOKEN:-demo-project-token}"
STATE="${STATE:-/tmp/termini-e2e-backend/state.env}"
GCLOUD="${GCLOUD:-/home/ubuntu/google-cloud-sdk/bin/gcloud}"
ZONE="${ZONE:-asia-south1-a}"
NODE_A_IP="${NODE_A_IP:-10.10.0.11}"
NODE_C_IP="${NODE_C_IP:-10.10.0.13}"
NODE_A_ID="${NODE_A_ID:-01M4TGD31WXH97JAA5S6A4R10D}"
NODE_C_ID="${NODE_C_ID:-01M4ZQ7Q2J80ATY3K32PY3PPJH}"

# Keep the transcripts about the fleet, not about the CLI's update nag.
export CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=1
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
export NO_COLOR=1

# The echoed command goes to stderr so stdout stays the command's own bytes and
# can be piped into jq without the narration corrupting the JSON.
run() { printf '$ %s\n' "$*" >&2; "$@"; }
cmdx() { printf '$ %s\n' "$*" >&2; }

# api <METHOD> <path-with-leading-slash> [json-body]
api() {
  local m="$1" p="$2" body="${3:-}"
  if [ -n "$body" ]; then
    printf '$ curl -sS -X %s %s%s -H "Authorization: Bearer %s" -H "Content-Type: application/json" -d %s\n' \
      "$m" "$API" "$p" "$TOKEN" "$body" >&2
    curl -sS -X "$m" "$API$p" -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' -d "$body"
  else
    printf '$ curl -sS -X %s %s%s -H "Authorization: Bearer %s"\n' "$m" "$API" "$p" "$TOKEN" >&2
    curl -sS -X "$m" "$API$p" -H "Authorization: Bearer $TOKEN"
  fi
}

# on_node <node-a|node-c> <remote shell command>
# stdin is /dev/null: under `script` stdin is a TTY, and gcloud would rather prompt
# than fail — the capture must not block on a prompt nobody can answer.
on_node() {
  local host="$1"; shift
  printf '$ gcloud compute ssh %s --zone=%s --tunnel-through-iap --command="%s"\n' "$host" "$ZONE" "$*" >&2
  "$GCLOUD" compute ssh "$host" --zone="$ZONE" --tunnel-through-iap --quiet \
    --command="$*" -- \
    -o ControlMaster=auto -o ControlPath=/tmp/termini-cm-%r@%h -o ControlPersist=1800 \
    </dev/null 2>/dev/null
}

note() { printf '\n== %s\n' "$*"; }
