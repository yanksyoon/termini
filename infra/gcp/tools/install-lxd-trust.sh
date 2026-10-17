#!/usr/bin/env bash
# install-lxd-trust.sh — install the CONTROLLER's LXD client identity into a
# node's LXD trust store, from an operator host, idempotently.
#
# WHY THIS EXISTS (task t_d31b8e02, found live 2026-10-17)
# -------------------------------------------------------
# LXD authenticates every REST client by the TLS client certificate it presents
# against the node's trust store (`lxc config trust list`). The controller mints
# ONE client identity (`controller/app/lxdtrust.py`, `/data/lxd-client/`) and
# must have it installed on EVERY node it will place work on. The trust install
# was done by hand on node-a only (a one-off alongside #129): node-c answered
#
#     GET /1.0           -> 200   (reachable)
#     GET /1.0/instances -> 403   (controller cert not trusted)
#
# so a plan with an explicit `target_node` naming node-c applied to
# `{"state":"failed","vm_ids":[],"detail":"… HTTP 403 … Forbidden"}` while the
# same plan on node-a materialised a real RUNNING instance. Agent placement hid
# it by excluding node-c's host (`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`), which
# also hid the 8 vCPU / 29 GiB it can offer.
#
# The lesson is in the shape of the fix: the trust install belongs to the RAISE
# path, not to a one-off. A node that is recreated replaces its boot disk, so its
# LXD trust store comes back EMPTY — `raise-standing-fleet.sh --raise` therefore
# calls this script for every node it raises, and node-b gets the same treatment
# when it is ever raised (`infra/gcp/README.md`: it is TERMINATED today, 12/12
# vCPU quota).
#
# WHAT IT DOES, PER NODE
# ----------------------
#   1. probe from the controller — `python3 -m app.lxdtrust verify --node …`,
#      which is an AUTHENTICATED `GET /1.0/instances` (200 = trusted, 403 = the
#      defect, unreachable/other = unknown):
#        200  -> "already trusted": NO mutation. This is what makes the step
#                safe to run on every raise and every retry.
#   2. otherwise wait (bounded, `--timeout`) for the node's LXD to answer at all
#      — a node raised seconds ago is not listening yet.
#   3. mint a one-time certificate-add trust token ON THE NODE with the node's
#      own bootstrap tool (`termini-bootstrap mint-trust-token`, which writes
#      `/var/lib/termini/lxd-trust-token` 0600 root). This is the node half of
#      the flow documented in `docs/contracts/node-bootstrap.md` §6.4.
#   4. install it on the CONTROLLER with the repo's own tool:
#      `python3 -m app.lxdtrust trust --node https://<node>:8443 --token …`
#      (the token is consumed as `password` in `POST /1.0/certificates`; LXD
#      5.21 pins that field — see `lxdtrust.py`).
#   5. re-probe. Anything other than 200 exits 1: a trust install that cannot
#      prove itself is a failure, never a silent no-op.
#
# The token is single-use and short-lived, and this script never prints it.
#
# USAGE
#   install-lxd-trust.sh --node a --node c        # the standing fleet
#   install-lxd-trust.sh --node b                 # when node-b is raised again
#   install-lxd-trust.sh --all                    # a, b, c; a missing or
#                                                # non-RUNNING instance is skipped
#                                                # (node-b is TERMINATED today)
#   ... --timeout 600                             # bound the wait for a just-raised node
#   ... --dry-run                                 # print what would run, mutate nothing
#
# Only a RUNNING node is considered: a stopped instance has no LXD to trust. An
# explicitly named node that is missing or stopped is an error (exit 1), because
# asking for it means you believe it is up.
#
# EXIT CODES
#   0  every requested node is TRUSTED (installed now, or already)
#   1  a node could not be made trusted (the failing probe/POST is printed)
#   2  usage / missing instance / missing tool
#
# Requires `gcloud` (IAP SSH to the controller and the nodes) and an operator
# identity that can SSH to both. Nothing here needs a secret you have to type:
# the node mints its own token over its local LXD socket.
set -euo pipefail

PROJECT="${PROJECT:-termini-510609}"
ZONE="${ZONE:-asia-south1-a}"
CONTROLLER_INSTANCE="${CONTROLLER_INSTANCE:-termini-controller}"
CONTROLLER_APP_DIR="${CONTROLLER_APP_DIR:-/opt/termini/controller}"
CONTROLLER_PYTHONPATH="${CONTROLLER_PYTHONPATH:-/opt/termini/controller:/opt/termini}"
CONTROLLER_DB_PATH="${CONTROLLER_DB_PATH:-/data/controller.db}"
TRUST_NAME="${TRUST_NAME:-termini-controller}"
LXD_PORT="${LXD_PORT:-8443}"
TIMEOUT_S="${TIMEOUT_S:-300}"
POLL_S="${POLL_S:-5}"

DRY_RUN=0
NODES=()
ALL=0
# One-time trust token in flight (0600, removed on any exit path).
TEMP_TOKEN=""
trap '[ -n "$TEMP_TOKEN" ] && rm -f "$TEMP_TOKEN"' EXIT

log() { printf '[lxd-trust] %s\n' "$*" >&2; }
die() { printf '[lxd-trust] FATAL: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,80p' "$0"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --node) NODES+=("${2#node-}"); shift ;;
    --all) ALL=1 ;;
    --timeout) TIMEOUT_S="$2"; shift ;;
    --poll) POLL_S="$2"; shift ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

node_instance() { printf 'termini-node-%s' "$1"; }
node_dns() { printf '%s.%s.c.%s.internal' "$(node_instance "$1")" "$ZONE" "$PROJECT"; }

gssh() { # $1 = instance name, $2 = remote command
  # --verbosity=error keeps gcloud's tunnel banner off stderr; stdout is the
  # remote command's own output and is what callers parse.
  timeout 300 gcloud compute ssh "$1" --tunnel-through-iap \
    --zone "$ZONE" --project "$PROJECT" --verbosity=error \
    --command "$2"
}

controller_lxdtrust() { # $1 = lxdtrust subcommand line (e.g. "cert")
  gssh "$CONTROLLER_INSTANCE" \
    "cd $CONTROLLER_APP_DIR && PYTHONPATH=$CONTROLLER_PYTHONPATH TERMINI_DB_PATH=$CONTROLLER_DB_PATH python3 -m app.lxdtrust $1"
}

instance_status() { # $1 = instance name -> GCE status (empty when it does not exist)
  gcloud compute instances describe "$1" --zone "$ZONE" --project "$PROJECT" \
    --format 'value(status)' --verbosity=error 2>/dev/null || true
}

# Prints "<state> — <detail>" and returns lxdtrust's own rc:
#   0 TRUSTED  2 UNKNOWN (unreachable / unexpected answer)  3 NOT TRUSTED
probe() { # $1 = node key
  local host out rc=0
  host="$(node_dns "$1")"
  set +e
  out="$(controller_lxdtrust "verify --node https://$host:$LXD_PORT")"
  rc=$?
  set -e
  printf '%s\n' "$out"
  return "$rc"
}

install_for() { # $1 = node key
  local node="$1" host instance out rc=0 deadline mints=""
  instance="$(node_instance "$node")"
  host="$(node_dns "$node")"

  if [ "$DRY_RUN" = "1" ]; then
    log "would probe https://$host:$LXD_PORT from $CONTROLLER_INSTANCE, mint a trust token on $instance only if NOT TRUSTED, then install it with app.lxdtrust trust"
    return 0
  fi

  deadline=$((SECONDS + TIMEOUT_S))
  while :; do
    rc=0
    out="$(probe "$node")" || rc=$?
    if [ "$rc" -eq 0 ]; then
      log "$node: already trusted — no mutation (idempotent no-op)"
      return 0
    fi
    if [ "$rc" -eq 3 ]; then
      log "$node: NOT TRUSTED (403) — installing the controller identity"
      break
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      die "$node: LXD never answered within ${TIMEOUT_S}s — cannot prove trust either way (last probe: ${out:-no output})"
    fi
    log "$node: LXD not answering yet — retrying in ${POLL_S}s"
    sleep "$POLL_S"
  done

  # Node half: mint the one-time certificate-add trust token over the node's own
  # LXD unix socket. Its log lines go to stderr/are dropped; stdout is the token.
  TEMP_TOKEN="$(mktemp "${TMPDIR:-/tmp}/termini-trust-token.XXXXXX")"
  chmod 600 "$TEMP_TOKEN"

  set +e
  gssh "$instance" \
    "sudo termini-bootstrap mint-trust-token --name $TRUST_NAME >/dev/null 2>&1; sudo cat /var/lib/termini/lxd-trust-token" \
    >"$TEMP_TOKEN"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ] || [ ! -s "$TEMP_TOKEN" ]; then
    die "$node: could not mint a trust token on $instance (rc=$rc) — is termini-bootstrap installed and LXD initialised?"
  fi
  mints="$(wc -c <"$TEMP_TOKEN" | tr -d ' ')"
  log "$node: minted a ${mints}-byte one-time trust token on $instance"

  # Controller half: consume it. `--token` is single-use; the value is passed as
  # an argument (never echoed) and the file is removed by the EXIT trap.
  set +e
  out="$(controller_lxdtrust "trust --node https://$host:$LXD_PORT --token '$(cat "$TEMP_TOKEN")'")"
  rc=$?
  set -e
  printf '%s\n' "$out"
  rm -f "$TEMP_TOKEN"; TEMP_TOKEN=""
  if [ "$rc" -ne 0 ]; then
    die "$node: app.lxdtrust trust failed (rc=$rc) — see the line above"
  fi

  # Proof, not faith: the authenticated probe must now answer 200.
  rc=0
  out="$(probe "$node")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "$node: trust was installed but the probe is still not 200 (rc=$rc) — last: ${out:-no output}"
  fi
  log "$node: TRUSTED — controller identity installed and proven (fingerprint below)"
  controller_lxdtrust "cert" | sed 's/^/[lxd-trust]   /' >&2 || true
}

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH"
[ "${#NODES[@]}" -gt 0 ] || [ "$ALL" = "1" ] || die "nothing to do: pass --node a|c|b (repeatable) or --all"

if [ "$ALL" = "1" ]; then
  for n in a b c; do
    inst="$(node_instance "$n")"
    st="$(instance_status "$inst")"
    if [ -z "$st" ]; then
      log "$n: $inst does not exist — skipped"
    elif [ "$st" != "RUNNING" ]; then
      # node-b is TERMINATED (12/12 vCPU quota): a stopped instance has no LXD to
      # trust. It gets this step when it is raised, which is exactly why the step
      # lives in the raise path.
      log "$n: $inst is $st — skipped (only a RUNNING node can be trusted)"
    else
      NODES+=("$n")
    fi
  done
  [ "${#NODES[@]}" -gt 0 ] || die "no RUNNING node to install trust on (looked for a, b, c)"
fi

for n in "${NODES[@]}"; do
  if [ "$DRY_RUN" != "1" ]; then
    st="$(instance_status "$(node_instance "$n")")"
    [ -n "$st" ] || die "$n: instance $(node_instance "$n") does not exist in $PROJECT/$ZONE"
    [ "$st" = "RUNNING" ] || die "$n: $(node_instance "$n") is $st — a stopped node has no LXD to trust; raise/start it first"
  fi
  install_for "$n"
done

log "done — every requested node is trusted by the controller identity"
