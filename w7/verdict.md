# W7 Gate Verdict — Termini Cloud-VM Demo

Date: 2026-10-04 (UTC) · Gate owner: verifier · Reference commit: `origin/main @ 3eebafa76d70b79ebd45c300ae23867f2e51771a`

## Re-gate — 2026-10-11 (supersedes the 2026-10-04 bottom line below)

Reference commit: `main @ b9d7a6b`. Gate owner: verifier.

**Demo verdict: GO.** All seven blocking findings from the 2026-10-04 gate are closed
or rescoped, **and the one carve-out raised while re-verifying the judge-facing
claims — the day-0 registration claim — is now demonstrated on the deployed
footprint** (`register_ms=291`, against a `<5 s` claim). Accepted residuals are
listed at the end.

Read this section as the current state; the 2026-10-04 verdict below is kept as the
historical record of that gate, not as a live statement.

> **Independence caveat, stated up front.** This re-gate was produced by the same
> agent session that then fixed several of the findings. A verdict with that
> provenance is weaker evidence than the original independent one, and it should be
> re-run by a separate reviewer before it is treated as final. Every claim below is
> nonetheless backed by a re-runnable command, not by assertion.

### Blocking findings from the 2026-10-04 gate

| # | Finding | Disposition |
|---|---|---|
| 1 | No timed end-to-end dry run | **CLOSED** — a timed record existed and was never read. `scripts/timing/evidence/20261009T053937Z/runs.jsonl` holds three runs, all `status: PASS`, ~45 s each. `W6.2` searched for *filenames containing "dry"* (matching only the script) and had no pass branch, so it could not pass whatever was on disk. Fixed; `W6.2` now reads the record and passes. |
| 2 | Registration degraded path hangs silently | **CLOSED** — red-team, re-run: delay (30 s), connection-refused, DNS failure and consumed-token all reach terminal `REGISTRATION_FAILED` instead of hanging. |
| 3 | Cold first boot breaches the 30 s demo timeout | **CLOSED** — baseline 5/5 `ONLINE`, `start_to_online` mean 4.87 s, p95 6.82 s, `within_demo_timeout=True`. |
| 4 | Control-plane latency signal inert (hardcoded) | **CLOSED** — the daemon now *measures* RTT and disk write latency; 8 tests cover it. |
| 5 | Bulk cross-node transfer stalls under 30 % loss | **RESCOPED, not fixed** — reproduced (1.66 MB of 64 MB in 41 s, `BrokenPipeError`) and recorded as an accepted residual with the boundary. |
| 6 | No per-node capacity clamp | **CLOSED** — `clamp_to_node_capacity()` with committed-capacity accounting; 9 tests. |
| 7 | Role-check rejection reasons dropped | **CLOSED** — the 422 now returns `reject_reasons`. |

### Day-0 claim — now DEMONSTRATED on the deployed footprint (was the carve-out)

The deck claims a node *"registers to the controller in under 5 seconds"* from a
pre-baked image, with no package installs. This was originally a carve-out: the
deployed footprint had **no baked image at all**
(`gcloud compute images list --no-standard-images` was empty;
`variables.tf` defaulted to stock `ubuntu-2404-lts-amd64`; the activation-only
cloud-init then failed to start a unit that was never baked, and a real VM never
registered). It is now **built and measured**.

**Built:** `termini-node-os-1` (family `termini-node-os`, 20 GB), containing
`/usr/local/bin/termini-bootstrap`, `termini-bootstrap.service` (enabled) and the
`/var/lib/termini/.baked-offline` sentinel. Its cloud-init stays activation-only
(D3): write `/etc/termini/config.yaml` + the enrollment token, start the unit.

**Measured** on a node created from that image family, from the daemon's own
timing line:

```
[termini-bootstrap] REGISTERED node_id=01M4Q1MQJ1C0Y26F6MGT745DTT role=compute
                    register_ms=291  boot_to_registered_ms=18560
```

| Metric | Value | Meaning |
|---|---|---|
| `register_ms` | **291 ms** | daemon start → controller acknowledgement. **This is the deck's "<5 s" claim, met with ~17× margin.** |
| `boot_to_registered_ms` | **18.56 s** | VM power-on → registered — dominated by firmware/OS/cloud-init, not by registration. |

Corroborated independently: the node appears in the controller's fleet as
`status=online role=compute`; `cloud-init status: done`; the daemon binary is
timestamped at **bake time the previous day** (so it was not installed at boot);
and the `.baked-offline` sentinel is present. That is the "pre-baked image, no
package installs" half of the claim, checked rather than asserted.

**Caveat that remains:** the *standing* fleet (`termini-node-a/b/c`) still boots
stock Ubuntu. Switching a node's image family recreates the node, so adopting the
baked image is a deliberate migration rather than a side effect — documented in
`infra/gcp/README.md`.

### Other claims, re-verified against the live deployment

| Claim | Result |
|---|---|
| Plain English → validated plan | **VERIFIED** — `200 accepted`; the fast-LLM allocation resolves `realized_as: host_service` (the inference service, not a VM); 128 vCPU/512 GB → **clamped** to 8/16 with `above_host_max` → `exceeds_node_cpu_ceiling` reasons. |
| Model-driven OOM remediation | **VERIFIED** — model-decided remediations executing `result=ok`. |
| Zero C2 leaves the premises | **VERIFIED in substance.** Nothing C2 reaches storage, checked three ways: unknown fields stripped, C2 in an allowlisted field rejected (`422`), untransformed C1 rejected (`422`). One wording correction: an unknown field is **stripped silently (202)** by design (§3), while `422 GOVERNANCE_REJECT` covers schema/type/C1 violations. Field-name detection is now token-based so `app_logs` is C2 exactly as `logs` is, and stripping is logged. |

### Harness defects found and fixed during this re-gate

Recorded because a gate is only as good as its instruments, and two of these were
producing confident false results:

- `node_ip()` returned a CSV-quoted guest-bridge address, so **the entire net
  red-team phase measured nothing** while emitting well-formed JSON (#107).
- `W6.2` could not pass and had never read the record it was checking for (#110).
- `land.sh` refused green PRs on transient `UNKNOWN` mergeability — a false red in
  the component meant to prevent false greens (#111).

---

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
- **Bulk cross-node transfer collapses under heavy packet loss (finding G2/5) —
  RESCOPED, not fixed.** Measured with the W7 net harness (`scenario_loss`, `tc netem
  loss 30%` on node A's underlay, re-run against a fixed `node_ip()`):

  | | baseline | 30 % loss |
  |---|---|---|
  | 64 MB A→C TCP transfer | 64 MB in 0.022 s | **1.66 MB in 41 s (2.5 %)**, then `BrokenPipeError` |
  | inference calls | 20/20 ok, 2.32 s mean | **15/15 ok**, 2.23 s mean |

  Accepted rather than engineered because **the demo does not depend on bulk
  transfer**: small traffic is unaffected (15/15 inference calls under the same
  loss, latency indistinguishable from baseline), and no product or deck claim
  asserts bulk-transfer resilience. What *does* use bulk transfer is **LXD image
  pre-warm** (`infra/images/prewarm-images.sh` — an `lxc image import` over the
  underlay), so this is the boundary to revisit before claiming degraded-network
  pre-warm. 30 % sustained loss is a tail case on wired underlay; the realistic
  trigger is the MTU mismatch named in `project.md`, which produces exactly this
  small-passes / bulk-fails signature.

## Owners key

infra · controller · agents · sre — resolution cards must land before a W7 re-gate can
flip this verdict to GO.
