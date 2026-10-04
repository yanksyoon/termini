# Chaos scenarios — Scenario A (OOM) and Scenario B (disk degradation)

Deterministic fault injection for the Cloud-VM demo. Two scripts reproduce the
failure signatures the AI operator must remediate, both leaving a machine-
readable timeline the telemetry/controller workstream tunes its thresholds
against (see `t_6219d569`).

| File | What it does | Kernel/telemetry signature |
|------|--------------|----------------------------|
| `scenario_a_oom.sh` | Runs `stress-ng --vm` inside a target VM to push memory utilisation above a trigger threshold and hold it | rising `ram_used_bytes`, memory PSI (`some avg10`), falling `MemAvailable` |
| `scenario_b_disk.sh` | Induces a real, logged EXT4 I/O error on a **scratch** volume via a device-mapper `error`/`flakey` target | `EXT4-fs error (device dm-N)`, `I/O error`, `Remounting filesystem read-only`, `disk[].mount_ro=true` |
| `chaos_lib.sh` | Shared logging + target-transport + cleanup/dry-run helpers | — |

Both scripts are self-contained bash (no runtime deps beyond coreutils on the
control host; `stress-ng` on the Scenario A target; `losetup`/`dmsetup`/`mount`
on the Scenario B target).

## Quick start

```bash
# Scenario A: hold a VM at ~94% memory for 25 s (crossing 90%), against a live LXD VM
scripts/chaos/scenario_a_oom.sh --target lxd:inference-vm \
    --target-pct 94 --trigger-pct 90 --hold-secs 25

# Scenario B: EXT4 I/O error on a scratch volume on Node A, held 20 s
scripts/chaos/scenario_b_disk.sh --target ssh:ubuntu@10.10.0.11 --hold-secs 20

# Plan only, inject nothing
scripts/chaos/scenario_a_oom.sh --target lxd:inference-vm --dry-run
scripts/chaos/scenario_b_disk.sh --target local --dry-run
```

Every run writes `<scenario>-<UTC>-<pid>.{jsonl,log,summary.json}` under
`--log-dir` (default `./chaos-logs`, or `$CHAOS_LOG_DIR`).

## Target transports (`--target`)

| Spec | Meaning | Needs root prefix? |
|------|---------|--------------------|
| `local` | run on this host | `sudo -n` if not root |
| `lxd:<instance>` | `lxc exec <instance> -- …` (use an LXD **VM** for Scenario A) | runs as root |
| `ssh:<[user@]host>` | plain `ssh`, extras via `$CHAOS_SSH_OPTS` | `sudo -n` if not root |
| `gcloud:<instance>` | `gcloud compute ssh`, extras via `$CHAOS_GCLOUD_OPTS` | `sudo -n` if not root |

Scenario B needs `CAP_SYS_ADMIN` (loop + device-mapper + mount): use a VM or
bare node, **not** an unprivileged container. Scenario A only needs a writable
`/proc`, so any VM/container with `stress-ng` works — but a container shares the
host's `/proc/meminfo`, so use an LXD VM to isolate the pressure from the host.

## Scenario A — memory pressure

`stress-ng` is launched **inside** the target in `--ramp-steps` staged chunks
(each `--vm-keep`, so the allocation accumulates) up to
`target_pct% - current_used%` of RAM, then held. A control-side monitor polls
`/proc/meminfo` + `/proc/pressure/memory` every `--sample-every` seconds and:

* logs the **first sample at/above `--trigger-pct`** as `threshold_crossed`
  (the exact timestamp the telemetry owner needs);
* keeps the error active until utilisation has stayed above the trigger for
  `--hold-secs` (the 20-40 s remediation window), then kills stress-ng;
* aborts early if utilisation reaches `--safety-pct`, so a runaway allocation
  cannot OOM the target;
* stress-ng is additionally started with a hard `--timeout`, so a tool failure
  cannot leave the VM under pressure.

The `used` metric used for the trigger is `100*(MemTotal-MemAvailable)/MemTotal`
(the "committed" view that excludes reclaimable page cache). `free`-based and
`MemFree` figures are logged too, so the threshold owner can map the value to
whatever the node reports as `ram_used_bytes`.

**Tuning to the frozen `oom-guard-01` rule** (`ram_used_bytes >= 95%` for 2
consecutive samples): on a VM with a few percent of headroom (or swap), run
`--target-pct 96 --trigger-pct 95 --safety-pct 99 --hold-secs 25`; the monitor's
safety abort is what keeps the VM off the OOM killer. On a small no-swap VM keep
the defaults (`94/90/97`) and align the controller debounce to the recorded
crossing timestamp instead.

Exit: `0` injected+verified, `3` OOM-killer fired during injection (with
`--no-fail-on-oom` this is downgraded to a warning), `4` signature not produced,
`2` usage, `5` target/prerequisite error.

## Scenario B — EXT4 I/O error

1. Creates a scratch ext4 image (`--size-mb`, default 256 MiB) in
   `--scratch-dir` and attaches it to a loop device — or copies a `--fs-image`
   when one is supplied (skips `mkfs`).
2. Maps it through a device-mapper device (`--dm-name`) and mounts the scratch
   mount (`--mount-point`).
3. Swaps the dm table to an `error` target (`--mode error`) or a `flakey` target
   (`--mode flakey --down-secs N`) covering **only the scratch device**, then
   writes through the mount and `sync`s. ext4 logs the I/O error and remounts
   the scratch filesystem read-only.
4. Holds the error for `--hold-secs` so telemetry has a scrape window.
5. Restores the linear table and tears everything down (dm device, loop, image,
   mount dirs) — idempotently, from a trap, so an interrupted run leaves no
   residual state.

The **root filesystem and every real device are never touched**: all state lives
on the loop device over the scratch file.

Exit: `0` injected+verified, `4` no disk error found in the kernel log,
`2` usage, `5` target/prerequisite error.

### Where `mkfs` is restricted

Some execution environments block `mkfs`. In that case pass a pre-existing ext4
image with `--fs-image PATH` (the default path only runs `mkfs.ext4` when
`--fs-image` is absent). Any small ext4 image works, e.g. a Firecracker
`rootfs.ext4`.

## Chaos log format

One JSON object per line (`.jsonl`), plus a human `.log` and a final
`.summary.json`. Every event carries `ts` (RFC3339 UTC, ms), `epoch`, `run_id`,
`scenario`, `event`, and event-specific keys.

Scenario A events: `run_start`, `baseline`, `plan`, `inject_start`,
`stress_start`, `sample` (1 Hz metric timeline), `threshold_crossed`,
`safety_abort`, `inject_stop`, `recovery`, `oom_detected`, `verify_ok`/
`verify_failed`, `run_end`.
Scenario B events: `run_start`, `plan`, `setup_start`, `setup_ok`,
`inject_start`, `device_error_armed`, `mount_readonly_detected`,
`verify_evidence`, `verify_ok`/`verify_failed`, `run_end`.

`threshold_crossed` (A) and `inject_start` / `device_error_armed` /
`mount_readonly_detected` (B) are the timestamps to align triggers to. The
`epoch` field is fractional Unix seconds for latency math.

## Verifying a run

```bash
# JSONL is valid JSON, one object per line
jq -c . chaos-logs/<run>.jsonl >/dev/null && echo ok

# Scenario A: crossing + dwell
jq -r 'select(.event=="threshold_crossed") | .ts'   chaos-logs/<run>.jsonl
jq -r 'select(.event=="run_end") | .trigger_ts, .dwell_secs, .oom_detected' chaos-logs/<run>.jsonl

# Scenario B: the kernel-log evidence captured by the run
jq -r 'select(.event=="verify_evidence") | .first_ext4_error, .remount_ro_line, .mount_ro' chaos-logs/<run>.jsonl
```

## Safety properties

* **Bounded by construction:** Scenario A's stress-ng has a hard timeout and a
  control-side safety abort; Scenario B's error target covers only a loop device
  over a scratch file.
* **Idempotent teardown:** both scripts run a LIFO cleanup stack from an `EXIT`/
  `INT`/`TERM` trap; re-running after a crash first tears down any prior state.
* **`--dry-run`** prints the full plan and injects nothing.
* **Non-zero exit on failed injection**, so CI/runbooks can gate on it.
