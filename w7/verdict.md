# W7 Gate Verdict — Termini Cloud-VM Demo

Date: 2026-10-04 (UTC) · Gate owner: verifier · Reference commit: `origin/main @ 3eebafa76d70b79ebd45c300ae23867f2e51771a`

## Bottom line

**Demo verdict: NO-GO.** The governance claim is **VERIFIED**, but the demo is not
signed off: W6 is unfinished (no timed dry run, pre-warm cluster blocked) and five
independently-reproduced blocking findings remain open across W2/W3/W4. Happy-path
acceptance is solid; the degraded paths that the demo's failure fallbacks depend on
are the part that is not ready.

**Governance claim (data egress): VERIFIED.** No raw workload/application data (C2)
leaves the premises during normal demo operation — independently proven by full-payload
packet capture with a canary prompt payload: 0 occurrences on the internet uplink
(`enp5s0`) and tailnet (`tailscale0`), 1 on the on-prem bridge (`lxdbr0`, the expected
inference leg). Telemetry is C0/C1-only with C1 identifiers already hashed/truncated;
the controller holds zero outbound connections and the operator loop is deterministic.
Evidence: `t_23a76b8a` (governance-verdict-memo + evidence-telemetry-body).

## Per-workstream gate

| Card | Verdict | Basis |
|------|---------|-------|
| W1 `t_961e8dc8` Architecture & integration spec | **GATED-PASS** | Adversarial verify `t_60acac97`; acceptance W1.1 (10/10 contracts) + W1.2 (24/24 questions answered) pass |
| W2 `t_1ccef770` Control plane | **GATED-FAIL** | F1 (passthrough_gpu) RESOLVED on main; F2 (no per-node capacity clamp) + F3 (dropped role-check reasons) still open |
| W3 `t_5783da13` Bare-metal bring-up / LXD + networking | **GATED-FAIL** | Happy path passes (registration <5s, CIDR, llama reachable); degraded path break-the-demo (silent hang, hardcoded latency signal, bulk-transfer stall) |
| W4 `t_cca07879` Agentic OS image & llama.cpp inference | **GATED-PASS** (residual) | Offline boot + cross-node multi-turn streaming verified pass (`t_9549b286`); residual: `network_downloads_at_boot` hardcoded literal, fix reverted (`t_74e496a2`) |
| W5 `t_e3aa4c5e` Customer dashboard & web terminal | **GATED-PASS** | `t_863a6c9e` w5 verification pass; round-2 execution-lens approval (PR #61) |
| W6 `t_5630daf9` Demo reliability & chaos scenarios | **GATED-FAIL** | Not complete — no timed end-to-end dry run (`t_3bb56d97` todo), pre-warm cluster blocked (`t_9fa53ad8`); acceptance W6.1/W6.2 skipped |

## Blocking findings (with owners)

1. **No end-to-end timed dry run.** `t_3bb56d97` (sre) is `todo`; `t_9fa53ad8`
   pre-warm cluster (infra) is `blocked` (iteration budget exhausted). Without a dry run
   the demo has no measured fallback timing. Owner: **sre + infra**.

2. **Registration degraded path hangs silently.** Consumed/invalid token (401/403) and
   DNS-failure / connection-refused all leave the node stuck in `REGISTERING`, never
   reaching `REGISTRATION_FAILED` (`t_8d243ee1` F1/F2). Owner: **infra**
   (`infra/bootstrap/node/termini-bootstrap.py`).

3. **Cold first boot breaches the 30 s demo timeout** (39.45 s, 1/5 runs)
   (`t_8d243ee1` F3). Owner: **infra**.

4. **Control-plane latency signal is inert.** Node telemetry hardcodes
   `net[].latency_ms = 0.3` and `write_latency_ms = 0.5`
   (`infra/bootstrap/node/termini-bootstrap.py:300,317`), so `latency-01` can never
   fire (`t_8d243ee1` G1, CRITICAL). Owner: **infra** (telemetry) + **controller**
   (operator detect).

5. **Bulk cross-node transfer stalls under 30 % loss** — 64 MB transfer reaches 3.0 %
   in 45 s (`t_8d243ee1` G2). Owner: **infra**.

6. **No per-node capacity clamp.** `count × per-VM size` aggregate oversubscription is
   accepted unclamped (64 agents × 16 GiB → 200 accepted);
   `health.capacity_of` hardcodes `cpu_allocated=0 / ram_allocated_gb=0` and
   `_build_plan` does no accounting/commit (`t_ac279f14` F2, HIGH). Owner: **controller**
   (`controller/app/health.py`, `controller/app/server.py::_build_plan`).

7. **Role-check rejection reasons dropped.** 422 `GUARDRAIL_REJECTED` returns
   `details: []` for role-level rejections; reason codes not in the frozen enum
   (`t_ac279f14` F3, MEDIUM). Owner: **controller**.

## Reconciliation notes (conflicts resolved, not averaged)

- **Guardrail F1 (passthrough_gpu) — RESOLVED, report was stale.** `t_ac279f14` tested
  `validator.py @ 9acd4b9` (#13), which predates fix `ca93eea` (#44, `t_70572256`)
  merged 24 min after the report. Independently re-run against `origin/main`: the
  validator now rejects `passthrough_gpu: true` on any class with
  `gpu_passthrough_not_in_demo`, and `inference_vm passthrough_gpu: false`
  (spec-correct) is accepted. F1 is closed.
- **OOM double-remediation — RESOLVED.** `t_3dc57f17` H2 fixed by `t_e6034e05`
  (single-owner operator lease, `b7d10c0`): two-process concurrency single-remediation
  10/10 (was DOUBLE 10/10), recovery no-refire 3/3.
- **Acceptance matrix 12/16 skipped is an environment artifact, not a pass.**
  `t_88a63851` ran against the live controller build `9704aa0`, which predates the
  `/intents` route, the operator loop, and the terminal/log endpoints — hence the skips.
  The skipped rows are not evidence of failure, but they are also not evidence of
  success; they require a dry run on a current build.

## Residual risks accepted for the demo

- **OOM detection race on uncapped ramps** — the kernel OOM-killer beats the
  2-sample/1 s detection rule 10/10 for uncapped 4 s/8 s ramps; the demo is safe only
  because the chaos injection is staged/capped. Accepted on the condition the demo uses
  the staged scenario.
- **No network egress firewall** — the C2 guarantee is by-construction (redactor +
  on-prem-only inference allowlist), not network-enforced; nodes retain a NAT default
  route.
- **Raw MAC in registration body** (`nics[].mac`) — C1 spec deviation (R17), not C2;
  not persisted by the controller.
- **Stock Ubuntu phone-home timers** (apt-daily, motd-news, snapd) remain enabled on
  node images — metadata only, never workload data.
- **`network_downloads_at_boot` is a hardcoded literal** (`t_74e496a2`) — fix PR #72
  was auto-reverted by ci-watchdog; until re-landed, the "zero-network boot" measurement
  is self-reported rather than measured.

## Owners key

infra · controller · agents · sre — resolution cards must land before a W7 re-gate can
flip this verdict to GO.
