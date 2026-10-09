# Timed demo dry run + fallback runbook (`scripts/timing/`)

Owner: kanban task **`t_3bb56d97`** — *Run and time the full end-to-end demo dry
run with per-failure fallbacks*. Parent workstream: `t_5630daf9` (W6 demo
reliability).

This directory is the rehearsal and the safety net for the live demo: a timed,
scripted end-to-end dry run (`demo_dry_run.sh`) plus a per-failure runbook the
presenter and operator follow when something misbehaves on stage.

```
scripts/timing/
  demo_dry_run.sh          the timed harness (6 beats, per-beat + total wall clock)
  demo_fault_injector.py   deterministic node-fault injector (reuses chaos/node_sim)
  demo_probe.py            read-only controller probes (audits, nodes, SSE logs, triggers)
  lxd_bridge.py            LXD unix-socket -> TCP transport for the operator executor
  evidence/<stamp>/        captured runs (text only; no DBs/pids)
  README.md                this file
```

---

## 1. Run it

```bash
scripts/timing/demo_dry_run.sh                 # 3 runs back-to-back (run 3 cold)
scripts/timing/demo_dry_run.sh --runs 1        # one run (fast smoke)
scripts/timing/demo_dry_run.sh --window 90     # override the 60 s remediation window
scripts/timing/demo_dry_run.sh --out /tmp/dr   # override the evidence dir
```

Exit `0` = every beat of every run passed; `1` = a beat failed; `2` = bad
environment. Each run writes text evidence under `evidence/<stamp>/runN/`.

### The six beats (in demo order)

| Beat | Proves | PASS when |
|------|--------|-----------|
| 0 `intro` | handoff / context | always |
| 1 `cluster_health` | pre-warmed cluster + direct inference route | `infra/warm/warm_cluster.sh` all green **and** `infra/net/direct-route.sh status` active path `direct` |
| 2 `scenario_a_oom` | OOM injection → AI-operator remediation | `oom-guard-01` audit with `result=ok` lands within `WINDOW_S` |
| 3 `scenario_b_disk` | EXT4 remount-ro injection → remediation | `disk-fail-01` audit with `result=ok` lands within `WINDOW_S`, **and** the drained node is observed `STOPPED` |
| 4 `dashboard` | the live AI-operator stream | dashboard served and the operator stream captured |
| 5 `closing` | operator audit + live cluster receipts | warm cluster still green |

### Measured results (this host)

From `evidence/20261009T053937Z/` — 3/3 runs PASS:

| Run | Total | Scenario A (OOM→remediation) | Scenario B (disk→remediation) | Cold operator |
|-----|-------|------------------------------|-------------------------------|---------------|
| 1 | 45.20 s | 4.16 s | 2.11 s | no |
| 2 | 45.28 s | 4.15 s | 2.11 s | no |
| 3 | 46.50 s | 4.15 s | 2.11 s | **yes** (loaded 4 persisted latch rows) |

Both scenarios land ~20× inside the 60 s window, so the fallback lines are a
safety net, not a crutch. Run 3 restarts the operator process **cold** against
the same DB and shows the persisted `trigger_config` + hysteresis `trigger_state`
latch survive it (`run3/scenario_a/triggers_*.json`) — a cold operator re-arms
from persisted triggers instead of re-seeding.

---

## 2. What is REAL vs. emulated (read before quoting a number on stage)

The GCP nodes A/B/C of `infra/gcp/` are **not provisioned** in this build
environment; the demo runs **emulated on a single host** (see
`infra/warm/README.md` §2). The dry run is honest about which layer is which:

| Layer | Beat 1 (health) | Beats 2/3 (inject → remediate) |
|-------|-----------------|--------------------------------|
| Pre-warmed LXD cluster (`demo-node-a/b/c`) | **real** — asserted live | **real** — the remediation mutates these nodes |
| Controller + AI-operator loop | live shared controller | **real current build**, run as a private instance (see below) |
| Thresholds / hysteresis / playbooks | — | **real production code** (`controller/app/operator/*`) |
| LXD remediation | — | **real LXD REST** over `lxd_bridge.py`; the audit records the true LXD response |
| Fault | — | **deterministic injector** (reuses `chaos/node_sim` sample generator) |

Two emulation shims, both documented:

1. **Private controller instance.** The long-running shared controller was
   started from an older checkout and cannot be restarted as a service in this
   session; its `/v1/operator/*` routes 404. The injection beats therefore start
   their own controller from the current checkout on a scratch DB
   (`start_sb` in `demo_dry_run.sh`) — same code, same operator, same playbooks.
   Beat 1 still asserts the live shared controller.
2. **`lxd_bridge.py`.** LXD (snap) exposes only a unix socket here, but the
   operator executor speaks HTTP(S). The bridge relays requests to the unix
   socket so the **unmodified** executor issues real LXD REST calls (the audit's
   `detail` is LXD's own response, e.g. `"Restarting instance"
   resources.instances=["/1.0/instances/demo-node-a"]`).

The injected node advertises **no** `lxd_instances` inventory (the injector strips
it), so the operator's `target: heaviest` resolves to the configured default
instance — the node itself (`TERMINI_LXD_DEFAULT_INSTANCE_NAME`). That is what
makes the remediation land on a *real* LXD instance.

---

## 3. Fallback runbook (per failure mode)

**Hard budget:** a scenario must remediate within **60 s** (`TERMINI_DEMO_WINDOW_S`).
The harness enforces this in beats 2/3. On stage, if the fault is not remediated
within 60 s, speak the ~10 s fallback line, run the recovery command, and pivot
to the next beat. Every line below is deliverable in ~10 s and ends on a pivot
back into the story (source lines: `docs/demo-script.md` §2).

Commands use these env knobs (all overridable):
`WINDOW_S`, `TERMINI_CONTROLLER_URL_HOST`, `TERMINI_SB_PORT_A/B`,
`TERMINI_LXD_DEFAULT_INSTANCE_NAME`, `TERMINI_LXD_BASE_URL`.

### F1 — OOM-killer wins the race (kernel kills `stress-ng` before 95 % × 2)

- **Presenter line (10 s):** *"That's the honest race we documented — the kernel
  OOM-killer sometimes wins. We re-drive the deterministic chaos ramp instead, so
  the detect → decide → execute → audit loop still runs end to end."*
- **Operator command (re-drive the ramp):**
  ```bash
  scripts/timing/demo_fault_injector.py \
    --url http://127.0.0.1:${TERMINI_SB_PORT_A:-18085} --bootstrap-token demo-boot-a \
    --hostname demo-fault-oom --mode oom --duration 12 \
    --params '{"ram_start_pct":62,"ram_peak_pct":97,"ramp_s":2}'
  ```
- **Budget:** re-drive immediately (fires in ~4 s; fallback line at 60 s).

### F2 — EXT4 error never appears (Scenario B produces no `mount_ro`)

- **Presenter line (10 s):** *"The scratch volume didn't flip read-only on the
  first pass — the injection is scoped to a throwaway loop device so nothing real
  is at risk. I'm re-driving it with a longer hold; the operator still drains the
  node on the remount-ro signature."*
- **Operator command (longer hold / deterministic ramp):**
  ```bash
  # real kernel EXT4 signature on the node's scratch device (root; scratch only)
  sudo scripts/chaos/scenario_b_disk.sh --target lxd:demo-node-b --hold-secs 30
  # deterministic fallback (same frozen rule, no root)
  scripts/timing/demo_fault_injector.py \
    --url http://127.0.0.1:${TERMINI_SB_PORT_B:-18086} --bootstrap-token demo-boot-b \
    --hostname demo-fault-disk --mode disk-fail --duration 12 \
    --params '{"fail_after_s":1.0}'
  ```
- **Budget:** re-drive immediately (fires in ~2 s; fallback line at 60 s).

### F3 — Trigger storm / noisy thresholds (rule flaps or fires on noise)

- **Presenter line (10 s):** *"The operator is quiet because the rule hasn't
  crossed its threshold — the guardrail refuses to act on noise. The hysteresis
  latch only fires on sustained breaches, exactly so a demo never flaps."*
- **Operator command (prove the latch, then widen the band if needed):**
  ```bash
  # show the persisted threshold + latch state (should be armed/clear, not flapping)
  python3 scripts/timing/demo_probe.py --url http://127.0.0.1:${TERMINI_SB_PORT_A:-18085} triggers \
      --db /tmp/dryrun-scenario-a/controller.db
  # if a threshold is genuinely too tight for the room, widen it and restart the
  # private operator with a larger debounce/cooldown:
  TERMINI_OPERATOR_COOLDOWN_S=10 TERMINI_OOM_CONSECUTIVE=3 scripts/timing/demo_dry_run.sh --runs 1
  ```
- **Budget:** no action needed for a single noisy sample (debounce absorbs it);
  if a rule is genuinely flapping, restart with the wider band within 60 s.

### F4 — Inference route stalls (stream hangs / no tokens)

- **Presenter line (10 s):** *"If the stream stalls, that's the point of the direct
  route — no tunnel to misconfigure on this leg. Re-running the route check:
  three lines, it ends in PASS."*
- **Operator command (route is up? proof; then documented overlay fallback):**
  ```bash
  bash infra/net/direct-route.sh status                 # active path must be 'direct'
  # the inference node's resident llama.cpp must answer on the direct leg:
  python3 scripts/timing/demo_probe.py inference --url http://<inference-node-ip>:8080
  # DF/MTU proof of the path (root; netns lab, deterministic PASS):
  sudo infra/net/lab/overlay-vs-direct-lab.sh --out infra/net/evidence
  # documented overlay fallback:
  TERMINI_INFERENCE_PATH=ovn bash infra/net/direct-route.sh status
  ```
  Note: `infra/net/mtu-probe.sh --path direct` probes the *spec* underlay IP
  `10.10.0.13`, which is not provisioned in this single-host emulation — so it
  reports `fail` here even though the direct path is active. Use the HTTP probe
  above (or the netns lab) to verify the leg on this host.
- **Budget:** route status < 5 s; inference `/v1/models` 200 within 10 s; switch
  to `ovn` within 60 s if the direct leg is genuinely down.

### F5 — Node fails to register (`REGISTERING`, never `ONLINE`)

- **Presenter line (10 s):** *"The token didn't land — that's the fail-closed path
  doing its job: no identity, no join. I'll re-issue and re-provision in a second.
  The point stands: in the happy path there is no human in the loop."*
- **Operator command (see which node, then re-bring it up):**
  ```bash
  bash infra/warm/warm_cluster.sh --hold 6        # names the cold node + reason
  bash infra/warm/demo-env.sh up                  # idempotent: re-start + re-enroll
  lxc start demo-node-c                           # or bring up the specific node
  ```
- **Budget:** `warm_cluster.sh` < 8 s; node back `ONLINE` within 60 s, else pivot
  into Beat 2 (the intent beat does not depend on the fenced node).

### F6 — Dashboard / telemetry lag (charts behind the cluster)

- **Presenter line (10 s):** *"The dashboard is lagging the live cluster — a UI
  refresh, not a control-plane problem. The nodes and controller keep working; the
  charts will catch up. Meanwhile the terminal is the real-time view."*
- **Operator command (prove the control plane is live; reload the console):**
  ```bash
  # the control plane is the source of truth; these must answer instantly
  python3 scripts/timing/demo_probe.py --url http://127.0.0.1:8085 --token "$TERMINI_PROJECT_TOKEN" nodes
  python3 scripts/timing/demo_probe.py --url http://127.0.0.1:${TERMINI_SB_PORT_B:-18086} \
      --token demo-project-token audits
  # reload the dashboard origin (console) if the tab is stale
  open "http://127.0.0.1:${TERMINI_CONSOLE_PORT:-18090}/"   # or hit refresh
  ```
- **Budget:** if the control-plane probes answer within 5 s, the lag is UI-only —
  keep narrating; reload the tab within 60 s.

### Secondary modes (carried from `docs/demo-script.md` §2)

| # | Failure mode | Presenter line (10 s) |
|---|--------------|-----------------------|
| C6 | Inference `503` / model unavailable | *"The inference box is returning a busy signal — that's the degrade path, never a wrong-but-plausible answer. The flash alias on the same node takes over."* |
| C7 | Governance-gate anomaly | *"If the gate sees anything it can't classify, it fails closed — the batch is rejected. You saw the healthy case; the anomaly case is simply: no egress, plus a rejection record."* |
| C8 | Cross-node stream stalls | (same as F4) *"No tunnel to misconfigure on this leg — re-run the route check."* |

---

## 4. Verification

```bash
bash scripts/timing/demo_dry_run.sh --runs 3        # gate: 3/3 PASS
echo $?                                             # 0
```

Evidence for the run above lives in `scripts/timing/evidence/<stamp>/`:
`beats.json` (per-beat wall clock), `runs.json` (per-run totals + cold flag),
`runN/scenario_a/audit.json` (the `oom-guard-01` remediation record with the real
LXD response), `runN/scenario_b/node_b_state_after.txt` (`STOPPED`),
`runN/dashboard/operator_stream.txt` (the dashboard's AI-operator stream),
`runN/scenario_a/triggers_*.json` (persisted triggers across the cold restart).

No SQLite DBs or pid files are kept (runtime state); only text evidence.

A live dashboard capture is included as `evidence/<stamp>/dashboard_ai_operator_log.png`
(the AI-operator panel showing `oom-guard-01: instance.restart demo-node-a -> ok`),
and `runbook_selftest.txt` records each fallback recovery command being exercised
on this host (F1 re-drive, F2 dry-run, F3/F6 probes, F4 route + inference probe,
F5 warm assertion).
