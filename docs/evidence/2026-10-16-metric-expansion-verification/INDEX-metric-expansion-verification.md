# Independent adversarial verification — metric expansion (§4.12–§4.14)

**Provenance.** Commissioned by the operator (kanban card `t_7c2f3f52`, "Independent
verification: the metric expansion … with C2 adversarial controls and a falsified
diagnosis use"). Run by the `verifier` profile, adversarial, on a fresh detached
worktree of `origin/main` @ commit `e706b08b17b300b3f32838bfc7d3a73de3f29a8a`
(PR #194, the last of the metric-expansion PRs). Nothing below was taken from an
implementer summary: every row was re-run on the committed state, and the
implementers' own scripts/tests were used only as fixtures to plant against, never
as evidence.

Scope: the five metric-expansion cards — research `t_cae0dfa8` (deb939f), contract
+ingest `t_98a685c1` (3879af5), node collector `t_92aac3c8` (1592fff), dashboard
`t_8b60acb2` (028e756), diagnosis `t_922b3268` (e706b08) — plus the demo seeder
`t_a1b5e136` (7891246).

---

## 0. Committed state — acceptance commands re-run

| Command | Result (raw tail) |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | `Ran 966 tests in 228.087s` … `OK` |
| `node --test dashboard/tests/core.test.mjs` | `tests 62, pass 62, fail 0` |
| `node --test frontend/test/*.test.mjs` | `tests 38, pass 38, fail 0` |
| `python3 -m unittest tests.test_telemetry_metric_expansion` (contract/ingest) | `Ran 21 tests … OK` |
| `python3 -m unittest tests.test_metric_expansion_diagnosis tests.test_external_model_call` (diagnosis) | `Ran 39 tests … OK` |
| `python3 -m unittest tests.test_bootstrap_telemetry tests.test_telemetry_processes tests.test_seeder_metric_expansion` (collector+seeder) | `Ran 76 tests … OK` |

Each card's named acceptance command was located by reading the file that card
added, not its PR body. The only test-suite output line that resembles a failure
(`RuntimeError: sink down` in `test_external_model_call`) is an *expected* fixture:
that test asserts the audit sink failing does not break the model call, and the
module still reports `Ran 39 tests … OK`.

---

## 1. Result table — one row per claim

| # | Claim | Verdict | Command + raw output behind it |
| --- | --- | --- | --- |
| 1 | Metrics are **collectable** (contract fields declared in `node-telemetry.md`, mirrored in `fields.py`) | **CONFIRMED** | `grep`/read of `docs/contracts/node-telemetry.md` §4.12–§4.14 vs `controller/app/fields.py`: every new field name appears in both (pressure 4 keys, 6 flat scalars, 4 disk, 4 net, 2 lxd). |
| 2 | Metrics are **governed** (C-class declared AND enforced at ingest — a constraint with no reader is documentation, not a control) | **CONFIRMED** | Every new field is `cclass: "C0"` in `fields.py` and registered in `governance.md` §4.1.1. The **consumer** of each constraint is found in code: `validate.py:_check_scalar` (type/min/max/enum/pattern) and `governance.scan_c2`/`is_c2_field_name` (field-name denylist + value signatures), both invoked in `server.py:_ingest_telemetry` **before** schema validation (lines 1511–1531). |
| 3 | C2-shaped values planted in every new C0 field **fail closed** | **CONFIRMED** | `verify_c2.py` (this run): 80 plants (20 new fields × email / card-digit-run / `/proc/<pid>/cmdline`-path / file-path). Output: `ALL 80 field/value plants fail-closed: True`. Email & card caught by `scan_c2` (`samples.oom_kill_count: email signature`); path strings caught by schema (`mem_available_bytes: expected integer, got str`). |
| 4 | The C2 **instrument is real** (not a vacuously-clean scanner) | **CONFIRMED** | Planted `attacker@example.com` in `oom_kill_count` → `scan_c2` returns `['samples.oom_kill_count: email signature']`; clean batch → `[]`; positive control `cmdline/app_logs/customer_notes/argv/environ/message` all trip `is_c2_field_name`. |
| 5 | **Prompt side** is C0-only (assembled request carries only registered fields; `externalModelCall()` assertion enforces it) | **CONFIRMED** | Re-ran `run_metric_diagnosis.py`; parsed the captured request in `stub-model-requests.jsonl`: `state field count: 61 non-C0: []`, `evidence field count: 183 non-C0: []`, `assert_allowed_classes(assembled request): []`. The trace quotes `externalModelCall()`'s own assertion: `template=operator-decision v2 … C0_assertion=ok`. |
| 6 | Metrics are **real, not fixtures** (collected on a real node, matching host reality) | **CONFIRMED** | `verify_real_metrics.py` (this run): 17 shipped values re-derived by hand from `/proc`, `/sys`, cgroup and compared to the collector functions — every one `MATCH` (e.g. `oom_kill_count by-hand=37 daemon=37`, `pgmajfault_count=3473530`, `mem_available_bytes=2406641664`, `conntrack_max=262144`, `inode_total=12976128`, per-instance `bsp-verify` `pgmajfault_count=40099`). Committed collector + dashboard payload samples byte-identical (`sha256 … = bdd536ffa99d20fe9f0b35c3d930fa1b61f41492d1cb923488cfe69b82c3dd61`), and consistent with the monotonic counters I read live. |
| 7 | Metrics are **used by the diagnosis** (enriched path changes something observable; falsify degrades honestly) | **CONFIRMED** | See §2 below. |
| 8 | Metrics are **visible** (dashboard charts the new groups, absent ≠ zero) | **CONFIRMED** | `node --test dashboard/tests/core.test.mjs` → 62 pass. The test is **non-vacuous**: it asserts concrete values and semantics, e.g. `assert.equal(rowOf(p, "pressure.cpu_some_avg10").reported, true)`, `assert.equal(rowOf(mem, "oom_kill_count").cumulative, true)`, `assert.match(rowOf(mem, "mem_available_bytes").display, /^2\.2[45]$/)`, `assert.equal(ratio.derivedFrom, "inode_used / inode_total")`, `assert.match(ratio.display, /^8\.7$/)`. |
| 9 | **Bounds** — over-long array REJECTED not truncated | **PARTIAL / FALSIFIED (half)** | `processes[]` (the only array with an ingest `max_items`) rejects: `processes: 11 entries exceeds the contract maximum 10`. BUT `disk[]`/`net[]`/`lxd_instances[]` — the arrays the §4.13/§4.14 fields ride — carry **no** ingest `max_items`, so 50 entries each are **accepted** (`disk[] 50 entries -> accepted=True`). See Finding F1. |
| 10 | **Cost** — worst-case bytes per sample | **CONFIRMED** | `measure_bytes.py`: new-fields-only worst case = **744 bytes** (exactly `node-telemetry.md` §4.15's figure); full sample (base+new) = 866 bytes ≤ `METRIC_EXPANSION_MAX_BYTES = 1024`. |
| 11 | **Cost** — prompt-token delta | **CONFIRMED (recomputed; window-dependent)** | `measure_token_delta2.py`: static v1→v2 = +1,782 chars = **+446 tokens** (matches the implementer's +445); dynamic = **+470 tokens at a 3-sample window** (implementer's +359 was a 2-sample window). My total: **+916 tokens/diagnosis at 3 samples**. See Finding F2. |
| 12 | No GCP compute; GCE-only groups honestly deferred | **CONFIRMED** | No shipped field is GCE-sourced. The GCE metadata (`preemptible`/`maintenance-event`/instance-id) and SMART/EDAC groups are **SHIP-LATER** in the research card §3, each with its exact live-node command; none is claimed as delivered, so nothing needed a live GCE node. |

---

## 2. Falsifying the diagnosis use (claim row 7)

Reproduced the enriched path end-to-end with my own run of
`docs/evidence/2026-10-16-metric-expansion/tools/run_metric_diagnosis.py`
(real controller in a temp DB, stub model + stub LXD — no network, no GCP):

* **Enriched path is observable and attributable.** Fresh capture (my run):
  `decision_source=model`, `incident_generated=model`,
  `incident_evidence_fields = ["pressure.memory_some_avg10", "pressure.cpu_some_avg10", "ram_used_bytes", "lxd_instances.ram_used_bytes"]`
  — the PSI group is quoted **first** in `evidence[]`. The trace's `prompt_built`
  step reads `template=operator-decision v2 sha256=… substituted_fields=547 C0_assertion=ok`,
  and `decided_by=model` (the DECIDER field is asserted, not assumed).
* **Honest degradation (old-shaped sample, no new fields).** `build_window_deltas`
  over an old-shaped sample returns `{"window_samples": 1}` — **no fabricated
  delta**. With the model unavailable, the provider returns `source=fallback`,
  `fallback_reason=inference_unavailable`, and still produces an incident record
  with `generated=fallback` and `evidence=[{field: ram_used_bytes}]` (event C0 only).
  It never invents a metric and never crashes.
* **The §7.2 assertion actually fires.** A `cmdline` field planted in `state` →
  `assert_allowed_classes` returns `['state.cmdline: C2 field name', 'state/evidence: cmdline: C2 field name']`;
  `externalModelCall()` raises and the model is **never called**
  (`model_called=False`, `source=fallback`, `fallback_reason=schema_violation`).

---

## 3. Findings

**F1 (defense-in-depth gap, PRE-EXISTING — not introduced by this expansion).**
`disk[]`, `net[]` and `lxd_instances[]` have no ingest-enforced `max_items`
(only `processes[]` does). An over-long array is **accepted, neither rejected nor
truncated**. The §4.15 "cannot silently grow the ingest payload" guarantee rests
entirely on the producer-side caps (`DISK_MAX=8`, `NET_MAX=1`, running-instance
cardinality) plus the controller's `MAX_BODY = 1 MiB` backstop — a compromised or
buggy collector can send up to ~1 MiB of C0-valid array entries. This is **not a
C2 leak** (every entry is a validated C0 numeric field); it is a size-boundary
gap that predates the metric expansion. Recorded here, not fixed (independence).

**F2 (documentation nuance — the token figure is window-dependent, not a fixed
+804).** The diagnosis card's "+804 tokens/diagnosis" was measured at a 2-sample
evidence window. My independent 3-sample capture measures **+916 tokens/diagnosis**
(+446 static, +470 dynamic). Both are consistent with the research card's linear
model (~25 chars/field/sample); the number scales with window size and instance
count, so the "≈ +80k/day @ 100 diagnoses" headline should be read as a floor for
the smallest realistic window, not a ceiling.

**F3 (non-defect, stated for the record).** The C2 *value* scanner
(`governance.scan_c2`) has no file-path signature — a path string planted in a
numeric field is caught by the schema **type** check, not by `scan_c2`. Fail-closed
holds either way; the two gates split the work.

---

## 4. Unverified items (nothing silently accepted)

* **Static token-delta sub-figure was NOT independently re-derivable from the
  committed tree** until `git archive` of the parent commit (`028e756`) was used;
  once the v1 template was recovered, the static delta **matched** the implementer's
  (+446 vs +445 tokens). No residual uncertainty.
* **Live GCE node:** nothing required. Every shipped group is `/proc`/`/sys`/cgroup/
  LXD-sourced and was re-derived on this host. The GCE-only and SMART/EDAC groups are
  SHIP-LATER with their live commands already recorded in the research card §3.
* **Model output** in the captured pair is a **stub** (as the diagnosis card states);
  the *mechanics* (input assembly, C0 assertion, guardrail, persistence) are real.
  A live provider answer is out of scope under the no-GCP-compute cost guard.

**Verdict: CONFIRMED** — collectable, governed, real, used by the diagnosis, and
visible, with one pre-existing defense-in-depth gap (F1) and one documentation
nuance (F2) recorded above. No blocking defect was found in the metric expansion
itself.
