# OOM race-condition red team — `oom-guard-01` under memory contention

Task: `t_3dc57f17` (W7 adversarial verification). Spec reference: `termini/project.md`
advisory #4; frozen rule `oom-guard-01` in `docs/contracts/node-telemetry.md` §5.

## Verdict

**UNSAFE — with mitigation for the flagged race, and one unmitigated defect.**

1. **The flagged race (H1, detection-vs-kernel-kill) is real and reproducible at 100 %**
   for the memory ramp an uncapped `stress-ng --vm` actually produces: the kernel
   releases the memory *before* the controller can accumulate the two consecutive
   ≥95 % samples the rule requires, so the operator never fires. Reproduced 10/10
   (4 s ramp) and 10/10 (8 s ramp) end-to-end; the raw kernel window above 95 % was
   measured at **0.06 s median for `stress-ng --vm 1`** and 0.22 s at 80 MB/s.
   This is contained — not fixed — by the demo's shipped injection
   (`scripts/chaos/scenario_a_oom.sh`), which stages the ramp and **holds** at a
   capped target instead of racing to 100 %; every capped run fired and remediated
   (3/3 at each ramp).
2. **Concurrent remediation (H2) is not idempotent across operator instances.**
   Two independent operators acting on the *same* node issued **2 real LXD restart
   commands for one fault episode in 10/10 runs** — both for two standalone loops
   and for the shipped in-process loop plus a standalone loop (the documented
   `OPERATOR_CMD` path). The DB-level latch does serialise two in-process loops
   sharing one controller DB (10/10 single command), but nothing prevents a second
   operator process, and each writes its own latch store. This defect is **not**
   mitigated in the current tree.
3. **Forced-kill recovery is clean.** SIGKILLing the controller mid-breach and
   restarting on the same DB produced **0 re-fires in 3/3** runs (persisted latch).

The demo therefore survives *only if* it injects the fault the way
`scenario_a_oom.sh` does (staged ramp + hold) **and** runs exactly one operator.
Neither the spec nor `controller-api.md` states a single-operator requirement, and
the demo's own evidence path (`OPERATOR_CMD`) adds a second one.

---

## 1. The hypothesised unsafe interleaving

Frozen rule (`docs/contracts/node-telemetry.md` §5): `oom-guard-01` fires when
`ram_used_bytes >= 95 % of ram_total_bytes for 2 consecutive samples`. Cadence is
1000 ms during the demo (`controller-api.md` §8), operator poll 1 s
(`TERMINI_OPERATOR_POLL_INTERVAL_S`), `oom_consecutive = 2`.

    t0        workload allocates; guest RAM crosses 95 %
    t0+w      kernel OOM-killer fires (w = the "window")
    t_sample  next telemetry sample is posted (<= 1 s later)
    t_fire    operator has seen 2 consecutive >=95 % samples -> fires

The unsafe case is **`w < the debounce window`** (roughly 1–2 s at the demo
cadence): the kernel frees the memory before the second breaching sample arrives,
`breach_streak` collapses, `oom-guard-01` never fires, and the operator issues no
command. If the kernel's victim is the demo workload (not the synthetic stressor),
the demo loses its subject as well as its self-healing beat.

The second interleaving, **H2**: two operator instances both read the same
per-node latch as `armed` before either writes `fired`; each mints its own
`event_id` (so the `remediation_decisions.event_id UNIQUE` guard never sees a
collision) and each issues its own LXD command (`remediation_audits.idempotency_key
UNIQUE` only bites *after* the command has already been sent).

## 2. Method

Two independent measurements, both against the **production** operator code in
`controller/app/operator/` (no reimplementation):

| # | Harness | What it measures |
|---|---------|------------------|
| 1 | `kernel_oom.py` | Real kernel OOM timing. Runs an allocator inside a **256 MiB transient systemd cgroup** (`-p MemoryMax=…, MemorySwapMax=0`) and polls the cgroup at 20 ms to record breach-95 % → `oom_kill`, capturing `dmesg`. Host-safe: the kill is confined to the cgroup. |
| 2 | `oom_race.py` | Full isolated stack per run — temp controller (`app.main`) + recording LXD REST stub (`chaos/lxd_stub.py`) + `oom_guest.py` (a node that streams `oom` telemetry and **models the kernel kill by releasing memory and stopping the breach**) + the production operator (`app.operator.cli`, or the controller's in-process loop). Counts real LXD mutations as the ground truth. |

`oom_guest.py` models a linear ramp `62 % → peak` over `ramp_s`; when the modelled
utilisation reaches `kill_pct` (100 % = the kernel has nothing left to reclaim) the
workload is "killed", RAM drops to baseline, and every later sample is healthy — so
the breaching run ends exactly as a real OOM kill ends it. The kill model is
calibrated against measurement #1 (see §3.1).

## 3. Results

### 3.1 Real kernel window: 95 % → OOM-kill (measurement #1)

256 MiB cgroup, N = 5 per rate. "fire" = would the 1 s / 2-consecutive-sample rule
have seen a breach before the kill.

| allocation profile | window above 95 % (s) min / median / max | rule would fire |
|---|---|---|
| `stress-ng --vm 1` (the demo's Scenario-A tool) | 0.000 / **0.057** / 0.094 | **0/5** |
| controlled 80 MB/s | 0.201 / 0.225 / 0.229 | 0/5 |
| controlled 20 MB/s | 0.612 / 0.619 / 1.334 | 0/5 |
| controlled 7 MB/s | 1.813 / 1.913 / 1.949 | 4/5 |

Raw `dmesg` (verbatim, from `evidence/kernel_oom.json`):

    [49848.479435] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=oomrt-5541-0.service,
      mems_allowed=0,oom_memcg=/system.slice/oomrt-5541-0.service,task_memcg=/system.slice/oomrt-5541-0.service,
      task=stress-ng-vm,pid=407092,uid=0
    [49848.479472] Memory cgroup out of memory: Killed process 407092 (stress-ng-vm)
      total-vm:585072kB, anon-rss:243980kB, file-rss:640kB, shmem-rss:0kB, UID:0 pgtables:556kB oom_score_adj:1000

    [49577.817359] Memory cgroup out of memory: Killed process 382851 (python3)
      total-vm:306496kB, anon-rss:260992kB, file-rss:9344kB, shmem-rss:0kB, UID:0 pgtables:620kB

**Reading:** the kernel gives the controller a window that is 1–30× *smaller* than
the ~1–2 s the debounced rule needs, across the realistic allocation rates. Only a
deliberately throttled ramp (≤ ~7 MB/s, i.e. a *staged and held* injection) buys a
window wide enough to fire.

### 3.2 End-to-end reproduction (measurement #2)

`oom_race.py detect-vs-kill`, production `app.operator.cli`, 1 s poll, telemetry
1 s. `peak=100` = uncapped (kernel kills); `peak=97` = the capped ramp the shipped
chaos scenario uses. N = 10 uncapped / N = 3 capped per ramp.

| ramp | uncapped (peak 100 %) — unsafe runs | detected+remediated | breaching samples seen |
|---|---|---|---|
| 4 s | **10/10 (100 %)** | 0/10 | 0 |
| 8 s | **10/10 (100 %)** | 0/10 | 1–2 |
| 20 s | 2/10 (20 %) | 8/10 | 1–3 |
| 4 s capped (97 %) | **0/3** | 3/3 | 4–8 |
| 8 s capped (97 %) | **0/3** | 3/3 | 4–7 |
| 20 s capped (97 %) | **0/3** | 3/3 | 2–8 |

The 20 s uncapped case is a genuine race, not a threshold: 8/10 runs fired before
the kill, 2/10 lost to sample phase (`UNSAFE: kernel killed the workload before the
operator fired`). Detection latency when it does fire: 1.6–2.4 s (median), i.e.
exactly the debounce cost — the local fast-path contributes ~0.03 s of remediation
latency, so the cost is **detection**, not execution.

**Captured bad interleaving** (uncapped, 8 s ramp; `evidence/detect_vs_kill.json`):

    {"ramp_s": 8.0, "peak_pct": 100.0, "killed": true,
     "kill_ts": "2026-10-04T10:39:38.438Z", "kill_seq": 8, "last_breach_seq": 8,
     "breaching_samples": 1, "oom_fired": false, "detection_latency_s": null,
     "lxd_restart_commands": 0,
     "outcome": "UNSAFE: kernel killed the workload before the operator fired"}

One breaching sample (seq 8) reached the controller, then the kill released memory;
`breach_streak` never reached 2, no `health_event`, no `decision`, no LXD command.

**Captured good interleaving** (capped 97 %, 8 s ramp — the mitigation):

    {"ramp_s": 8.0, "peak_pct": 97.0, "killed": false, "oom_fired": true,
     "fired_at": "2026-10-04T10:43:42.062Z", "detection_latency_s": 1.733,
     "remediation_latency_s": 0.031, "lxd_restart_commands": 1,
     "outcome": "safe: remediated before kill"}

### 3.3 H2 — concurrent remediation / idempotency

| setup | real LXD restart commands for one episode | result |
|---|---|---|
| 2 standalone `app.operator.cli` processes (separate `--state-file` latches) | 2, every run | **DOUBLE — 10/10** |
| 2 in-process `OperatorLoop`s sharing one controller DB (`ThreadingBarrier` start) | 1, every run | single — 0/10 double |
| shipped in-process loop **+** standalone CLI on the same node (the documented `OPERATOR_CMD` integration) | 2, every run | **DOUBLE — 10/10** |

The DB schema does what it claims *inside one DB*: the two in-process loops produced
1 restart, 1 decision, 1 audit with no worker errors (the SQLite write lock serialises
them). The failure is at the deployment boundary: the standalone loop keeps its latch
in a JSON `--state-file`, not the controller DB, so a second operator has no shared
dedup. The demo's own W6.1 evidence path tells SREs to run exactly that standalone
loop against the live controller — where the in-process loop is also enabled by
default (`TERMINI_OPERATOR_ENABLED=1`). That combination doubles every remediation.

### 3.4 Forced-kill recovery

`oom_race.py resilience` (SIGKILL the controller while the breach is live, relaunch
on the same DB, keep the stored breaching samples):

| run | audits before / after restart | restart commands | `trigger_state.fired_count` | verdict |
|---|---|---|---|---|
| 0–2 | 1 / 1 | 1 | 1 | `state-recovers-no-refire` (3/3) |

The persisted latch (`0004_triggers.sql` `trigger_state`) is reloaded and the
already-remediated episode does **not** re-fire. State recovery is clean.

### 3.5 Guardrail scorecard

| question | answer | evidence |
|---|---|---|
| Does the system detect before the OOM killer fires? | **No** for uncapped ramps (0 % at 4 s/8 s); **yes** for the capped/staged injection (100 %) | §3.1, §3.2 |
| Local fast-path avoids model latency? | yes — remediation latency ~0.03 s | §3.2 audit records |
| Is remediation idempotent under concurrent triggers? | **No** across operator processes (2 commands 10/10); yes within one DB | §3.3 |
| Does state recover cleanly after a forced kill? | yes — persisted latch, 0 re-fires | §3.4 |
| Live in-process trigger works (W6.1)? | yes — 1 event → 1 audit → 1 real LXD restart | `evidence/live_inprocess.json` |

## 4. Minimal reproduction

Real kernel bound (no controller needed):

    python3 w7/redteam/oom/kernel_oom.py --runs 5 --rates "80,20,7" \
        --out /tmp/kernel_oom.json          # needs sudo (transient cgroup + dmesg)

End-to-end race (10/10 at 4 s, 8 s ramps):

    python3 w7/redteam/oom/oom_race.py detect-vs-kill --runs 10 --ramps 4 8 20 \
        --out-dir /tmp/oom-evidence

Concurrency (double remediation) and recovery:

    python3 w7/redteam/oom/oom_race.py concurrency --runs 10 --out-dir /tmp/oom-evidence
    python3 w7/redteam/oom/oom_race.py resilience  --runs 10 --out-dir /tmp/oom-evidence

Everything is stdlib-only. Per-run JSON lands under `evidence/raw/` (gitignored);
the curated summaries are committed next to this file.

## 5. Additional finding — the remediation action contradicts the contract

`docs/contracts/node-telemetry.md` §5 and `docs/spec-answers.md` state the
`oom-guard-01` action is **`vm.resize`** (raise the VM's RAM ceiling, "resize/migrate
agent VM"). The shipped playbook `controller/playbooks/oom.yaml` actually issues
`instance.restart` with `force: true` ("restart the heaviest workload"). A forced
restart under memory pressure is a materially different — and riskier — action: it
can take down the very workload the demo is showing, and it is not what the audit
contract (`action: "vm.resize"`) promises. The red-team playbook mapping should be
reconciled (either implement `vm.resize` or correct the contracts).

## 6. Residual risks accepted / to fix

* **Unmitigated (fix before claiming idempotency).** A second operator instance
  doubles remediation. Options: run the standalone CLI against the controller DB
  (persisted latch) instead of a JSON state-file, or add a single-owner guard
  (e.g. a DB advisory lock / `operator_instance` lease) so only one loop may act
  per project. Until then the demo must run exactly one operator.
* **Contained by the injection recipe.** Detection cannot beat a fast kernel kill.
  The demo must keep injecting the way `scenario_a_oom.sh` does (staged chunks,
  target 94 %, safety abort 97 %, hold ≥ 5 s) — never an uncapped `stress-ng` ramp
  to 100 %.
* **Deployment note.** The currently *running* demo controller build (`t_9fa53ad8`,
  commit `9704aa0`) predates the operator (PR #25) and runs no loop at all, so the
  race is presently moot on the live box; it becomes live as soon as the loop is
  deployed, which is where §3.3 bites.
