# W1 Verification Review — Adversarial Findings

- Reviewer: `verifier` (kanban task `t_60acac97`)
- Verdict: **FAIL — gate W1 back to owners.** 4 blocking defects, 7 non-blocking gaps.
- Reviewed state: `origin/main` @ `3f065c1` (commit `3f065c11d6d43ce202ff0bc0351d4a8742ea32f0`), i.e. the merged PR state, not any working tree.
- Date: 2026-10-04

This review grades the W1 deliverables — `docs/architecture.md` and `docs/contracts/*.md` —
against the W1 acceptance criteria and the red-team dimensions in the verification task.
Every claim below was checked against the committed files on `origin/main`; the JSON schema and
examples were re-validated locally, and the controller unit/integration suite was re-run.

---

## 0. Verdict summary

| Criterion | Result |
|-----------|--------|
| All deliverables present on a merged commit | PASS |
| Each deliverable landed via a merged PR with green CI | PASS |
| Every other workstream can build without asking | **FAIL** (D1–D4) |
| Each open spec question has an answer with rationale | PASS (R1–R13 present) |
| Governance: redactor before any GCP call, external model scoped to C0 | **FAIL** (D3, N4) |
| No unresolved acceptance-criteria gaps remain hidden | **FAIL** |

The documents are unusually thorough and the R1–R13 reconciliation is real and mostly correct.
However, four load-bearing contradictions survived reconciliation, and at least one of them is
already being built against in two incompatible ways (the controller workstream shipped
`docs/telemetry-schema.md`, which drifts from the frozen `node-telemetry.md`). W1 is **not** yet
"build without asking". The blocking defects below must be fixed before W1 can be signed off.

---

## 1. Delivery compliance (committed state)

Verified against `origin/main` @ `3f065c1`:

| Deliverable | Landed via | Merge commit | CI |
|-------------|-----------|--------------|----|
| `docs/architecture.md` | PR #14 | `c97f8ad` | green |
| `docs/contracts/controller-api.md`, `governance.md`, `infra-decisions.md`, `node-bootstrap.md`, `node-telemetry.md`, `vm-allocation.schema.json` | PR #1 | `4a56d7d` | green |
| `docs/contracts/agent-runtime.md` | PR #4 | `43c3cb3` | green |

`gh api .../commits/3f065c1/check-runs` → `sanity: success`, `revert-red-main: skipped`.
Re-ran acceptance locally: `python3 -m unittest discover -s tests` → **69 tests, 35 pass / 0 fail**;
`vm-allocation.schema.json` is valid Draft 2020-12 JSON Schema (`check_schema` OK).

Delivery compliance **passes**. The failures below are content defects, not delivery defects.

---

## 2. Blocking defects (must fix before sign-off)

### D1 — SSH key custody model is self-contradictory (governance/security critical)

Two incompatible user-access models are frozen in the same change set:

- **CA model** (correct, load-bearing): `infra-decisions.md` D4 (lines ~132–157) — "Users never
  receive a private key from us… the private key never transits the controller"; browser-local
  keypair; short-lived (8 h) CA-signed certs; the "shared project private key shown in the UI" is
  explicitly **rejected**. Reinforced by `node-bootstrap.md` §7 (line ~366) and `architecture.md`
  §5 trust boundary (line ~156).
- **Project-keypair model** (contradicts D4): `controller-api.md` §4.3 (lines ~420–445) —
  `GET /v1/projects/{project_id}/ssh-key` "The private key is generated at project creation, shown
  to the user ONCE in the frontend"; `POST .../ssh-key/rotate` "Rotates the project keypair" and
  pushes `ssh.inject_key` "replacing `/root/.ssh/authorized_keys` with the new public key".

The `ssh.inject_key` command is likewise undefined: `controller-api.md` §6 (line ~584) makes it an
authorized_keys write, while `agent-runtime.md` §14.3 (line ~684) calls it "Host-level (SSH CA, D4)".

**Required fix (owner: architect `t_5fb81d14` + controller `t_260694d8`).** Adopt the CA/browser-key
model everywhere. Rewrite `controller-api.md` §4.3 so the endpoint accepts a **user public key**
(POST) and returns a **signed short-lived certificate** (and a rotate/revoke that rotates the CA or
re-issues certs), never "generate a project keypair and show the private key once". Delete the
"replaces authorized_keys" language from `ssh.inject_key`, or retire the command in favor of cert
issuance. If the keypair model is retained instead, D4 and `governance.md` §10 rule 7 must be
reverted — do not leave both.

### D2 — `inference_vm` + `passthrough_gpu: true` allocation class contradicts the frozen D5 decision

`infra-decisions.md` D5 (lines ~161–187) freezes inference as a **host-exposed llama.cpp service**
on Node C (`:8080`), with GPU passthrough "day-2 only, **not built**"; D3 point 3 and
`node-bootstrap.md` §3.1 both state the inference image is "not launched in the demo (D5)".

Yet the allocation contract keeps a launchable passthrough-VM class:

- `vm-allocation.schema.json` `vm_type` (line ~17): "inference_vm = LLM serving VM; **requires GPU
  passthrough**"; `passthrough_gpu` (line ~61): guardrail "REQUIRES true".
- `controller-api.md` §5.1 (line ~490) "GPU allocation 1 per node / 1 GPU-bound VM max";
  §5.2 step 3 (lines ~505–507) "`inference_vm` must have `passthrough_gpu: true`"; §5.3 reject rows
  (lines ~536, ~541).
- `agent-runtime.md` §14.2 (line ~670) says the opposite: "`inference_vm` → **not launched in the
  demo** (D5)… recorded and surfaced but **realized as the host service**."

The intent extractor + clamp validator shipped in PR #13 (`termini/intent/validator.py` line ~240,
`types.py` line ~40) implement `inference_vm`/`passthrough_gpu` **literally**. The demo prompt
"Deploy … 1 fast LLM model" therefore produces an `inference_vm` allocation that the guardrail gates
on a passthrough GPU that does not exist. Frontend, controller, and infra workstreams are building
three different readings of the same class.

**Required fix (owner: architect + controller).** Pick one semantics and propagate it. Recommended:
`inference_vm` is a **reference to the pre-baked always-on host service**, not a launchable
passthrough VM; drop the `passthrough_gpu` requirement and the "1 GPU-bound VM max" bookkeeping from
`controller-api.md` §5 and the schema, and align §5.2/§5.3 with `agent-runtime.md` §14.2. If a
day-2 passthrough class is kept, gate it behind an explicit "not in demo" flag so the validator
cannot reject the demo path.

### D3 — Registration body carries raw MAC and raw IP, contradicting the governance deny-list and the doc's own prose

`controller-api.md` §4.1 prose (lines ~208–209) states "Raw IP and MAC are NOT sent in the body…
the MAC never egresses". The canonical `NodeRegistration` JSON in the same section (lines ~193–194)
and in `node-bootstrap.md` §5.2 (lines ~197–199) includes `nics[].mac` (`42:01:0a:0a:00:0c`),
`nics[].addresses` (`10.10.0.12/24`), and `machine.underlay_ip`. `governance.md` §4.3 lists raw MAC
and full IP as never-egress.

Registration is Egress A (`governance.md` §1), so the deny-list applies; the one documented C1
bootstrap exception (hostname, `controller-api.md` line ~204) does not cover MAC/IP.

**Required fix (owner: architect + infra `t_134e2f24`).** Either (a) remove raw MAC/IP from the
registration body (controller derives the routable IP from the TLS connection and, if MAC is truly
needed for inventory, hash it as C1), or (b) document a bounded C1 bootstrap exception for these
machine-inventory fields exactly as was done for `hostname` — and make the §4.1 prose agree with the
JSON example either way.

### D4 — Every example identifier is 30 chars, not the 26-char ULID the schema and controller enforce

`vm-allocation.schema.json` `$defs.ulid` pattern is `^[0-9A-HJKMNP-TV-Z]{26}$` (line ~9), and
`controller/app/ulid.py` `is_valid_ulid()` rejects anything `len != 26`. Every example ID across the
docs is **30** characters (e.g. `01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C0D`). Counts: `controller-api.md` 9,
`agent-runtime.md` 8, `node-telemetry.md` 5, `telemetry-schema.md` 5, `node-bootstrap.md` 2,
`architecture.md` 1 — 30 unique 30-char IDs, **zero** 26-char IDs.

Verified mechanically: the schema's own top-level `examples` fail validation against the schema
(`jsonschema` → "not valid under any of the given schemas"), and the `$defs.allocation_spec`
examples likewise. A frontend/controller dev copying any example ID into a fixture gets a validation
failure.

**Required fix (owner: architect).** Regenerate all example IDs to 26-char ULIDs (ULIDs are 26 chars
by definition; the pattern and the implementation are correct — the examples are wrong). Add a CI
check that every `\b[0-9A-HJKMNP-TV-Z]{30}\b` and any 30-char "ULID" in `docs/**` is rejected.

---

## 3. Non-blocking gaps (fix before/alongside W2, do not independently gate)

### N1 — Two telemetry schemas + controller drift (PR #16)

`docs/telemetry-schema.md` (controller workstream, landed in PR #16) duplicates
`node-telemetry.md` and drifts in three ways: (a) `governance_chain_tip` is **required** in
`node-telemetry.md` §2 (line ~71) but **optional** in `telemetry-schema.md` §2 (line ~53);
(b) `hostname` hash includes a "project_id-derived salt" in governance/node-telemetry but not in
`telemetry-schema.md` §3.2; (c) rejection semantics: `node-telemetry.md` §1.1/§7 and
`controller-api.md` §4.1 define per-sample rejection with `{accepted, rejected}` counts, but
`telemetry-schema.md` §4 unilaterally switches to whole-batch all-or-nothing and says so explicitly.

**Fix (owner: controller `t_608afff4`).** Fold `telemetry-schema.md` into `node-telemetry.md` (one
source of truth, registered in `architecture.md` §1) and reconcile the three drifts.

### N2 — Controller→node launch path stated two contradictory ways

`agent-runtime.md` AR-2/§4.1 (line ~147) + `architecture.md` §4.2 + `controller-api.md` §4.2: the
controller fans out typed `vm.launch` commands and "never speaks LXD directly to a node".
`node-bootstrap.md` §6.3 (line ~352) + `infra-decisions.md` D1 consequence (line ~47): "the
controller reaches the cluster via the seed (`https://10.10.0.11:8443`)… and uses the unified API
for placement (`target=…`)". These are two different placement mechanisms. Also reconcile with D6's
"outbound-only" claim vs the D2 port map granting the controller inbound `8443`.

**Fix (owner: architect + infra).** State once who drives placement (recommended: typed `vm.launch`
to the node daemon, consistent with AR-2 and the architecture flow; restrict the seed `8443` trust
cert to cluster-admin/join-token duties only).

### N3 — OOM remediation latency is asserted, not engineered

`controller-api.md` §8 (lines ~684–686) and `infra-decisions.md` D8 assert a remediation "can be
issued before the kernel OOM-killer terminates the fault process" on the strength of 1 s telemetry.
But: (a) no external-model-call latency SLO is defined, yet the remediation path is threshold →
external GCP model → clamp → allowlist → command → ack; (b) the kernel OOM killer fires on cgroup
memory pressure, not a fixed 95% floor, so a spike can kill the process before two 95% samples
arrive; (c) `project.md`'s own simulation is `stress-ng --vm-bytes 85%` (line ~66), which never
reaches the `oom-guard-01` "≥ 95% ×2" threshold (`node-telemetry.md` §5, line ~294), so the demo
trigger will not fire as specified.

**Fix (owner: architect + controller).** Add an external-model latency budget or a local (non-model)
fast-path for the resize; reconcile the demo stress level with the threshold (or add a synthetic
"persistent threshold trigger" injection, per `project.md` advisory #4), and state the OOM-race
fallback honestly rather than asserting a guarantee.

### N4 — `labels` is an unclassified free-text C2 vector into controller persistence

`vm-allocation.schema.json` `labels` (lines ~63–67) is an unbounded free-form string map: no
C-class, no redaction rule, no `maxLength`. `guardrail_audit.raw_spec` persists it server-side
(line ~178). `governance.md` §10 rule 7 forbids C2 in controller storage/logs. A customer-controlled
free-text field flowing into persistent controller audit is an un-governed C2 path.

**Fix (owner: architect + controller).** Bound `labels` (length, key charset, count), classify it
(C1 or C2), or exclude it from `raw_spec` persistence.

### N5 — Missing frontend entry point: no project create/list endpoint

`bootstrap_token` and `project_token` are "issued by Project create" (`controller-api.md` §2,
lines ~92–94), but no `POST /v1/projects` (create) or `GET /v1/projects` (list) is defined anywhere
in §4. The frontend cannot bootstrap a project or obtain a token without an undefined endpoint.

**Fix (owner: controller).** Add `POST /v1/projects` (→ `project_id` + `project_token` +
`bootstrap_token`) and `GET /v1/projects`.

### N6 — Project token in WebSocket query string

`/stream/v1?token=<project_token>` (§7.1) and `/terminal/v1/{vm_id}?token=` (§7.3) place the bearer
token in the URL, which is logged by access logs and proxies. Non-blocking for the demo; prefer a
short-lived ticket or `Sec-WebSocket-Protocol`/header auth day-2.

### N7 — Minor wording/type drift

- `node-telemetry.md` §4.7 (line ~230) "the node hosting the inference **VM**" — D5 says host
  service, not a VM.
- `inference.model` is a single enum value (§4.7) but §11.2 dual-residency can serve both aliases
  simultaneously; a `models[]`/residency map is more accurate.
- `kernel_version` is "sanitized" (§4.1) but the example egresses the full build string
  (`6.8.0-142-generic`), a host fingerprint; define the sanitization.

---

## 4. Mechanical verification evidence

- `vm-allocation.schema.json`: valid JSON, valid Draft 2020-12 JSON Schema (`Draft202012Validator.check_schema` OK).
- Schema embedded `examples` **fail** self-validation (D4).
- Fenced JSON blocks across `docs/**`: all valid (two §7.3 "invalid" hits are multi-object
  illustrative terminal frames, not single documents — false positives).
- Stale-token scan: `node_01HZX`, `X-Bootstrap-Token`, `resize_instance`, `evacuate_instance`,
  `set_telemetry_interval`, `issue_cert`, `run_healthcheck` all still appear in `architecture.md` —
  but only in §8 R-reconciliation mapping prose (benign; the prior "0 matches" claim was inaccurate).
- Controller suite re-run locally: 69 tests, 35 pass, 0 fail.

---

## 5. Decision

**Gate W1 back to owners.** D1–D4 are load-bearing and must be fixed and re-verified before W1 can
sign off. N1–N7 should be fixed in the same pass or explicitly deferred with rationale. Re-review
will check the committed state of the fix PR(s), not any working tree.

---

## 6. Addendum — `docs/contracts/dashboard.md` (PR #19, landed after the initial pass)

`docs/contracts/dashboard.md` ("Customer Dashboard ↔ Controller Contract", 582 lines) landed on
`origin/main` at `2f15771` while this review was being prepared. It is the frontend contract and is
in scope for the "frontend/controller contract holes" red-team dimension. It does **not** resolve the
blocking defects — it confirms one and adds two more.

### A1 — SSH access model is now three-way contradictory (confirms + expands D1)

`dashboard.md` §3.2 + §7 R-D2 make the infra-injected **break-glass key** the standing demo access
path (`ssh -i <key> ubuntu@<public_ip>`), and demote the D4 CA-signed browser-key flow to
"post-MVP, out of the 5-day scope". This contradicts two other frozen docs:

- `node-bootstrap.md` §7: the break-glass key is **removed after successful registration** ("a
  one-time recovery path, not standing access").
- `infra-decisions.md` D4: no standing/shared private key; CA-signed short-lived certs are *the*
  decision, not a post-MVP upgrade.

And `dashboard.md` §4.4 still lists `ssh.inject_key` as "auto (controller-initiated rotation)" —
the exact `controller-api.md` §4.3 project-keypair model that §7 R-D2 just rejected. So four
documents (`controller-api.md` §4.3, `infra-decisions.md` D4, `node-bootstrap.md` §7,
`dashboard.md` §3.2/§4.4) now describe three incompatible access models.

**Fix (owner: architect + frontend + infra + controller).** Pick one model and propagate it across
all four docs, including the break-glass lifecycle (standing vs removed-after-registration) and the
fate of `ssh.inject_key` / §4.3.

### A2 — Terminal target contradiction (new; left as "this doc wins" without reconciling)

`controller-api.md` §7.3 freezes a **VM-level** terminal `/terminal/v1/{vm_id}` (`lxc exec`,
"never node-level"). `dashboard.md` §3.1 + R-D1 freeze a **node-level** terminal
`/terminal/v1/{node_id}` (host shell). R-D1 "resolves" this by declaring dashboard wins, but neither
`controller-api.md` §7.3 nor `architecture.md` (the declared tie-breaker) is updated. Two live docs
freeze opposite terminal surfaces.

**Fix (owner: architect + controller).** Reconcile the terminal path in `controller-api.md` §7.3 and
record the final answer in `architecture.md` §8, per the architecture doc's own "this doc wins"
rule.

### A3 — Confirms N1 (two telemetry schemas)

`dashboard.md` §1/§1.2/§5 cite `docs/telemetry-schema.md` *alongside* `node-telemetry.md` as a
source of truth, entrenching the two-schema drift rather than collapsing it.

### A4 — N5 (project create endpoint) still open

`dashboard.md` adds `GET /nodes/{id}/ssh`, `POST /nodes/{id}/actions/{reboot,drain}`,
`POST /remediations/{id}/{approve,deny}` — but still no `POST /v1/projects` (create) or
`GET /v1/projects` (list). The frontend still cannot bootstrap a project or obtain a `project_token`
from a defined endpoint.

### A5 — N6 (token-in-URL) downgraded to an acknowledged tradeoff

`dashboard.md` §2.2/§3.1 explicitly document `?token=` and note it is because `EventSource` /
`new WebSocket` cannot set an `Authorization` header. This is now a *documented* demo tradeoff, not a
hidden gap; retain as a day-2 note (short-lived ticket / Origin check), not a blocker.

### A6 — Metric name/struct drift (flattened vs nested; new, medium)

`dashboard.md` §1.4 freezes 19 **flat** metric names (`disk_total_bytes`, `net_rx_bytes`,
`gpu_util_pct`, `inference_tokens_per_s`, …), "mirroring `controller/app/store.py METRICS`", while
`node-telemetry.md` §4 emits **nested** objects (`disk[]`, `net[]`, `gpu[]`, `inference{}`,
`lxd_instances[]`). The flattening is undefined: for a host with multiple disks/NICs/GPUs, it is
unspecified whether `disk_total_bytes` is a sum, first device, or per-device series, and the
nested→flat name mapping (`gpu[].util_pct` → `gpu_util_pct`, `inference.tokens_per_s` →
`inference_tokens_per_s`) is nowhere documented. A frontend/controller dev cannot implement the
mapping without asking.

**Fix (owner: controller + architect).** Define the nested→flat mapping (which field, which
aggregation) in one place, and make `dashboard.md` §1.4, `telemetry-schema.md`/`node-telemetry.md`,
and `store.py METRICS` agree.

### Revised verdict

Unchanged: **FAIL — gate W1 back to owners.** D1–D4 stand; A1 and A2 add two more load-bearing
cross-doc contradictions, A6 adds an unresolved schema-mapping ambiguity. The frontend contract
(dashboard.md) was frozen against a controller-api that it explicitly disagrees with in three places
(R-D1, R-D2, R-D3) and defers reconciliation to "when the owning team touches it" — which is not
"build without asking".
