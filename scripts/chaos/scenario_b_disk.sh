#!/usr/bin/env bash
# =============================================================================
# scenario_b_disk.sh — Scenario B chaos: a logged EXT4 I/O error on a scratch
# volume, without bricking the node.
#
# Builds a throwaway ext4 volume backed by a loop device, exposes it through a
# device-mapper device, mounts it, then swaps the dm table to an `error` target
# (dm-error) or a `flakey` target (dm-flakey) for the *scratch device only* and
# forces writeback. ext4 sees the EIO, logs "EXT4-fs error ... remounting
# filesystem read-only" to dmesg/journald, and flips the scratch mount read-only.
# That is the exact signature the telemetry/controller consumes (mount_ro=true,
# disk I/O error counters). The error is held for --hold-secs so the scrape has
# a window, then the device is fully restored and torn down.
#
# The root filesystem and every real device are untouched: the whole exercise
# lives on a loop device over a scratch file (or a --fs-image copy) removed on
# teardown. Teardown is idempotent and runs from a trap, so an interrupted run
# leaves no residual dm/loop/mount state.
#
# Usage: scenario_b_disk.sh [OPTIONS]
#   --target SPEC        local | ssh:<[user@]host> | lxd:<instance> | gcloud:<inst>
#                        (default: local; needs root + loop + device-mapper)
#   --fs-image PATH      pre-existing ext4 image to reuse as the scratch volume
#                        (skips mkfs; handy where mkfs is restricted)
#   --size-mb N          scratch volume size when mkfs is used (256)
#   --scratch-dir DIR    scratch dir on the target (/var/lib/termini-chaos)
#   --mount-point DIR    scratch mount on the target (/mnt/termini-chaos)
#   --dm-name NAME       device-mapper name (termini-chaos)
#   --mode error|flakey  dm target used to inject (error)
#   --down-secs N        flakey down interval in seconds (10; flakey only)
#   --hold-secs N        seconds to keep the error active (20)
#   --log-dir DIR        chaos log directory (./chaos-logs)
#   --keep               leave the scratch device in place (debug); skips teardown
#   --dry-run            print the plan; inject nothing
#   -h, --help
#
# Exit: 0 injected+verified, 2 usage, 4 signature not produced,
#       5 target/prerequisite error.
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=chaos_lib.sh
. "$SCRIPT_DIR/chaos_lib.sh"

SCENARIO="scenario_b_disk"

TARGET="local"
FS_IMAGE=""
SIZE_MB="256"
SCRATCH_DIR="/var/lib/termini-chaos"
MOUNT_POINT="/mnt/termini-chaos"
DM_NAME="termini-chaos"
MODE="error"
DOWN_SECS="10"
HOLD_SECS="20"
KEEP="0"
DRY_RUN="0"
LOG_DIR="${CHAOS_LOG_DIR:-./chaos-logs}"

IMG=""
LOOP=""
SECT=""
DM_KERNEL=""

usage() {
  awk 'NR==1{next} /^# =+$/ { if (seen) exit; seen=1; next } seen { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case $1 in
    --target)        TARGET=${2:?--target needs a value}; shift 2 ;;
    --fs-image)      FS_IMAGE=${2:?--fs-image needs a value}; shift 2 ;;
    --size-mb)       SIZE_MB=${2:?--size-mb needs a value}; shift 2 ;;
    --scratch-dir)   SCRATCH_DIR=${2:?--scratch-dir needs a value}; shift 2 ;;
    --mount-point)   MOUNT_POINT=${2:?--mount-point needs a value}; shift 2 ;;
    --dm-name)       DM_NAME=${2:?--dm-name needs a value}; shift 2 ;;
    --mode)          MODE=${2:?--mode needs a value}; shift 2 ;;
    --down-secs)     DOWN_SECS=${2:?--down-secs needs a value}; shift 2 ;;
    --hold-secs)     HOLD_SECS=${2:?--hold-secs needs a value}; shift 2 ;;
    --log-dir)       LOG_DIR=${2:?--log-dir needs a value}; shift 2 ;;
    --keep)          KEEP="1"; shift ;;
    --dry-run)       DRY_RUN="1"; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case $MODE in error|flakey) : ;; *) echo "--mode must be error|flakey" >&2; exit 2 ;; esac

chaos_log_init "$SCENARIO" "$LOG_DIR"
chaos_target_init "$TARGET"
chaos_rc=0
trap 'chaos_rc=$?; chaos_run_cleanup; exit "$chaos_rc"' EXIT
trap 'chaos_rc=$?; chaos_run_cleanup; exit 130' INT
trap 'chaos_rc=$?; chaos_run_cleanup; exit 143' TERM

chaos_event run_start target="$TARGET" mode="$MODE" size_mb:="$SIZE_MB" \
  fs_image="${FS_IMAGE:-<none>}" scratch_dir="$SCRATCH_DIR" mount_point="$MOUNT_POINT" \
  dm_name="$DM_NAME" hold_secs:="$HOLD_SECS" dry_run:="$DRY_RUN"

IMG="$SCRATCH_DIR/chaos-scratch.img"

# --- teardown (idempotent, reverse order) ----------------------------------
teardown() {
  [ "$KEEP" = "1" ] && { chaos_event teardown_skipped reason=keep_flag; return 0; }
  # unmount first (force; the device may be erroring)
  chaos_t_root "umount -f '$MOUNT_POINT' 2>/dev/null || umount -l '$MOUNT_POINT' 2>/dev/null || true" >/dev/null 2>&1 || true
  # restore a sane linear table so the mapper can be removed cleanly, then drop it
  if chaos_t_root "dmsetup info '$DM_NAME' >/dev/null 2>&1"; then
    chaos_t_root "dmsetup suspend '$DM_NAME' 2>/dev/null || true; dmsetup load '$DM_NAME' --table '0 ${SECT:-1} linear ${LOOP:-} 0' 2>/dev/null || true; dmsetup resume '$DM_NAME' 2>/dev/null || true" >/dev/null 2>&1 || true
    chaos_t_root "dmsetup remove -f '$DM_NAME' 2>/dev/null || dmsetup remove '$DM_NAME' 2>/dev/null || true" >/dev/null 2>&1 || true
  fi
  if [ -n "$LOOP" ]; then
    chaos_t_root "losetup -d '$LOOP' 2>/dev/null || true" >/dev/null 2>&1 || true
  fi
  chaos_t_root "rm -f '$IMG' 2>/dev/null || true" >/dev/null 2>&1 || true
  chaos_t_root "rmdir '$MOUNT_POINT' 2>/dev/null || true" >/dev/null 2>&1 || true
  chaos_t_root "rmdir '$SCRATCH_DIR' 2>/dev/null || true" >/dev/null 2>&1 || true
}
chaos_add_cleanup teardown

# scratch_mount_opts — the scratch mount's effective options. Uses findmnt /
# /proc/self/mountinfo (not /proc/mounts) because a remount-ro flips the
# superblock "ro" flag without rewriting the mount-options string in /proc/mounts.
scratch_mount_opts() {
  chaos_t_exec "findmnt -no OPTIONS -M '$MOUNT_POINT' 2>/dev/null || awk -v m='$MOUNT_POINT' '\$5==m{print \$6}' /proc/self/mountinfo 2>/dev/null" 2>/dev/null | tr -d '[:space:]' || true
}

# --- prerequisites ---------------------------------------------------------
if [ "$DRY_RUN" != "1" ]; then
  missing=""
  for tool in losetup dmsetup mount umount blockdev truncate; do
    chaos_t_has "$tool" || missing="$missing $tool"
  done
  if [ -z "$FS_IMAGE" ]; then
    chaos_t_has mkfs.ext4 || missing="$missing mkfs.ext4"
  fi
  if [ -n "$missing" ]; then
    chaos_fatal "target '$TARGET' is missing required tools:$missing"
  fi
  if ! chaos_t_root "id -u >/dev/null 2>&1" >/dev/null 2>&1; then
    chaos_fatal "root is required on target '$TARGET' (loop + device-mapper + mount)"
  fi
fi

# --- plan ------------------------------------------------------------------
if [ -n "$FS_IMAGE" ]; then
  setup_desc="cp $FS_IMAGE -> $IMG"
else
  setup_desc="truncate -s ${SIZE_MB}M $IMG; mkfs.ext4 -q -F $IMG"
fi
if [ "$MODE" = "error" ]; then
  err_table="0 <sectors> error"
else
  err_table="0 <sectors> flakey $LOOP 0 0 ${DOWN_SECS}"
fi
chaos_event plan setup="$setup_desc" \
  "dm=losetup --find --show \$IMG; dmsetup create $DM_NAME --table '0 <sectors> linear \$LOOP 0'; mount /dev/mapper/$DM_NAME $MOUNT_POINT" \
  "inject=dmsetup suspend $DM_NAME; dmsetup load $DM_NAME --table '$err_table'; dmsetup resume $DM_NAME" \
  "trigger=write through $MOUNT_POINT then sync"

if [ "$DRY_RUN" = "1" ]; then
  if [ -n "$FS_IMAGE" ]; then
    chaos_t_has mkfs.ext4 >/dev/null 2>&1 || true
  fi
  chaos_event dry_run_complete "would induce a logged EXT4/dm I/O error on $TARGET via $DM_NAME"
  chaos_summary '"dry_run":true'
  exit 0
fi

# --- setup -----------------------------------------------------------------
chaos_event setup_start
chaos_t_root "mkdir -p '$SCRATCH_DIR'"
if [ -n "$FS_IMAGE" ]; then
  chaos_t_root "cp -f '$FS_IMAGE' '$IMG'"
else
  chaos_t_root "truncate -s ${SIZE_MB}M '$IMG'"
  chaos_t_root "mkfs.ext4 -q -F -E lazy_itable_init=0,lazy_journal_init=0 '$IMG'"
fi

LOOP=$(chaos_t_root "losetup --find --show '$IMG'" | tr -d '[:space:]')
if [ -z "$LOOP" ]; then
  chaos_fatal "could not attach a loop device to $IMG"
fi
SECT=$(chaos_t_root "blockdev --getsz '$LOOP'" | tr -d '[:space:]')
if [ -z "$SECT" ]; then
  chaos_fatal "could not read the sector count of $LOOP"
fi

chaos_t_root "dmsetup create '$DM_NAME' --table '0 $SECT linear $LOOP 0'"
DM_KERNEL=$(chaos_t_root "dmsetup info -c --noheadings -o minor '$DM_NAME'" | tr -d '[:space:]')
DM_KERNEL="dm-${DM_KERNEL}"
chaos_t_root "mkdir -p '$MOUNT_POINT'"
chaos_t_root "mount /dev/mapper/$DM_NAME '$MOUNT_POINT'"
chaos_t_root "head -c 262144 /dev/zero > '$MOUNT_POINT/chaos-probe.bin' 2>/dev/null; sync -f '$MOUNT_POINT' 2>/dev/null || sync"
chaos_event setup_ok loop="$LOOP" sectors:="$SECT" dm="$DM_NAME" dm_kernel="$DM_KERNEL" mount="$MOUNT_POINT"

# --- inject ----------------------------------------------------------------
inject_iso=$(chaos_now_iso)
chaos_event inject_start "ts_inject=$inject_iso" mode="$MODE"

if [ "$MODE" = "error" ]; then
  table="0 $SECT error"
else
  table="0 $SECT flakey $LOOP 0 0 $DOWN_SECS"
fi
chaos_t_root "dmsetup suspend '$DM_NAME'"
chaos_t_root "dmsetup load '$DM_NAME' --table '$table'"
chaos_t_root "dmsetup resume '$DM_NAME'"
chaos_event device_error_armed "table=$table"

# force writeback through the erroring device: ext4 logs the I/O error and
# remounts the scratch filesystem read-only.
chaos_t_root "for n in 1 2 3 4 5 6 7 8; do head -c 262144 /dev/zero > '$MOUNT_POINT/chaos-trigger.\$n' 2>/dev/null; done; sync -f '$MOUNT_POINT' 2>/dev/null || sync; sync; true" >/dev/null 2>&1 || true

# hold the error active so telemetry can scrape it; keep nudging writeback and
# watch for the ext4 remount-ro (the disk-fail-01 signal).
held=0
ro_detected=0
ro_ts=""
while [ "$held" -lt "$HOLD_SECS" ]; do
  sleep 1
  held=$(( held + 1 ))
  chaos_t_root "head -c 262144 /dev/zero > '$MOUNT_POINT/chaos-trigger.$held' 2>/dev/null; sync -f '$MOUNT_POINT' 2>/dev/null || true" >/dev/null 2>&1 || true
  if [ "$ro_detected" = "0" ]; then
    opts=$(scratch_mount_opts)
    case ",${opts}," in
      *,ro,*) ro_detected=1; ro_ts=$(chaos_now_iso); chaos_event mount_readonly_detected "ts_ro=$ro_ts" opts="$opts" ;;
    esac
  fi
done

# --- verify ----------------------------------------------------------------
klog=$(chaos_t_klog_since "$inject_iso" 2>/dev/null || true)
ext4_err=$(printf '%s\n' "$klog" | grep -iE 'EXT4-fs error' | tail -1 || true)
io_err=$(printf '%s\n' "$klog" | grep -iE 'I/O error' | grep -E "(^|[^0-9A-Za-z-])${DM_KERNEL}([^0-9]|$)" | tail -1 || true)
remount_ro_line=$(printf '%s\n' "$klog" | grep -iE 'Remounting filesystem read-only' | grep -F "$DM_KERNEL" | tail -1 || true)
mnt_opts=$(scratch_mount_opts)
mount_ro=0
case ",${mnt_opts}," in *,ro,*) mount_ro=1 ;; esac
if [ "$ro_detected" = "1" ]; then mount_ro=1; fi
if [ -n "$remount_ro_line" ]; then
  mount_ro=1
  if [ -z "$ro_ts" ]; then
    ro_ts=$(printf '%s\n' "$remount_ro_line" | awk '{print $1}')
  fi
fi

chaos_event verify_evidence dm_kernel="$DM_KERNEL" first_ext4_error="${ext4_err:-<none>}" \
  first_io_error="${io_err:-<none>}" remount_ro_line="${remount_ro_line:-<none>}" \
  mount_opts="${mnt_opts:-<none>}" mount_ro:="$mount_ro" readonly_ts="${ro_ts:-<none>}"

rc=0
verified=0
if [ -z "$ext4_err" ] && [ -z "$io_err" ]; then
  chaos_event verify_failed "reason=no_disk_error_in_kernel_log" dm_kernel="$DM_KERNEL"
  rc=4
else
  verified=1
  chaos_event verify_ok dm_kernel="$DM_KERNEL" ext4_error_present:="$([ -n "$ext4_err" ] && echo true || echo false)" \
    io_error_present:="$([ -n "$io_err" ] && echo true || echo false)" mount_ro:="$mount_ro"
fi

summary_body=$(printf '"verified":%s,"dm":"%s","dm_kernel":"%s","loop":"%s","ext4_error":"%s","io_error":"%s","mount_ro":%s,"mode":"%s","hold_secs":%s,"target":"%s"' \
  "$([ "$verified" = 1 ] && echo true || echo false)" \
  "$DM_NAME" "$DM_KERNEL" "$LOOP" \
  "$(chaos_json_escape "${ext4_err:-}")" "$(chaos_json_escape "${io_err:-}")" \
  "$mount_ro" "$MODE" "$HOLD_SECS" "$TARGET")
chaos_summary "$summary_body" >/dev/null

chaos_event run_end exit_code:="$rc" inject_ts="$inject_iso"
exit "$rc"
