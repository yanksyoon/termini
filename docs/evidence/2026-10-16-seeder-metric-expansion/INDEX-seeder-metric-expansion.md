# Seeder metric-expansion evidence — t_a1b5e136

The demo's telemetry stream is synthesised by `console/seed_demo.py`, not by the
node daemon, so the seeder must carry the §4.12–§4.14 metric groups itself.
This directory holds the raw samples the seeder emits (one per fault window)
plus the cumulative-counter evolution, so a reviewer can check the fault
narrative by reading — without booting a controller.

Regenerate everything with:

    python3 docs/evidence/2026-10-16-seeder-metric-expansion/tools/emit_seeder_samples.py

## Which fault window produced which values

The seeder drives two synthetic nodes through an out-of-phase fault pattern
(period 6 s, faulted half then healthy half). `sample()` maps the active fault
window to fixed, deterministic values — never random noise.

| Window | `pressure{}` | `mem_available_bytes` | `disk[].io_*` | cumulative counters |
|---|---|---|---|---|
| healthy (`fault=None`) | all four 0.0 | 40 GiB | `io_in_flight=0`, `io_ticks_ms` drifts +40/tick | `pgmajfault` +3/tick, `tcp_retrans` +1 every 4th tick |
| disk-degrade (`fault="disk"`) | `io_some_avg10=68.9`, memory 0.0 | 30 GiB (page-cache pressure, not exhaustion) | `io_in_flight=42`, `io_ticks_ms` climbs +3240/tick | memory/OOM counters flat |
| memory-pressure (`fault="memory"`) | `memory_some_avg10=72.4`, `memory_full_avg10=41.3`, `io_some_avg10=0.0` | 512 MiB (craters) | `io_in_flight=0`, `io_ticks_ms` drifts normally | `oom_kill_count` +1 every 3rd fault tick, `pgmajfault` +163/tick, target instance's `oom_kill_count`/`pgmajfault_count` advance in lock-step |

The two rules that make the synthetic evidence honest:

1. **A disk-degrade window never fabricates memory pressure.** `memory_full_avg10`
   stays 0.0 and `oom_kill_count` never advances, so the AI diagnosis cannot
   mis-attribute a disk stall to RAM. Conversely a memory-pressure window keeps
   `io_some_avg10` at 0.0 — the stall is on memory, not I/O.
2. **The cumulative counters advance only under the fault that names them, and
   never run backwards.** `disk[].io_ticks_ms` climbs steeply only in the
   disk-degrade window; `oom_kill_count` / `pgmajfault_count` (node-wide and the
   target instance's, `lxd_instances[0]`) advance only in the memory-pressure
   window. A watchdog respawn inherits the counters (like `seq`), so a healed
   ticker cannot emit a counter that runs backwards.

The "target" instance is the first entry in `lxd_instances[]` — the workload the
seeder advertises first, and the one whose §4.14 counters advance under memory
pressure (the others stay 0, a real "idle" measurement).

## Contract validity

Every sample in `seeder-samples.json` validates under
`controller/app/validate.py` (`errors == []`) and scans clean under
`app.governance.scan_c2` and `check_c1_transforms` (`[]`), and survives
`clean == raw` — i.e. no emitted key is an invented field that the
deny-by-default stripper would have to drop. All new fields are C0 integers or
bounded 0–100 percentages; the only C1 fields are the pre-existing
`hostname`/`ip_local`/`mac_address`/`project_id`, still in their transformed
forms. See `c2-scan.txt` for the per-sample results.

The full acceptance suite is `python3 -m unittest discover -s tests -t .`, with
the seeder tests in `tests/test_seeder_metric_expansion.py`.
