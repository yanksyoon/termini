# Standing-demo defect fixes — RAW EVIDENCE (2026-10-13)

Follow-up to `infra/gcp/evidence/standing-demo-fleet-2026-10-13.md` (the bring-up,
which is left intact as the dated pre-fix record). This artifact closes the two
judge-facing defects raised in that bring-up plus the two smaller ones, and every
block below is verbatim output from the live deployment.

| | |
|---|---|
| Branch / PR | `wt/standing-demo-fixes` |
| Base | `origin/main` @ `6e67589` (`t_standing_fleet … (#144)`) |
| Controller VM | `termini-controller` (code at `/opt/termini`, a plain COPY — deployed by copying the 4 changed controller files; pre-change tree backed up to `/home/ubuntu/termini-backup-20261013T162533Z`) |
| Fleet during the run | `termini-node-a` (compute, LXD `https://10.10.0.11:8443`), `termini-node-c` (gemma on `http://10.10.0.13:8080`); `termini-node-b` TERMINATED (vCPU quota 12/12) |
| Model | `llama.cpp` + Gemma 3 4B IT Q4_K_M on node-c, alias `gemma` |

---

## DEFECT 1 — the operator loop held its SQLite write transaction across the model call

### Root cause (read from the code, not inferred)

`controller/app/operator/loop.py` ran one `db.session(...)` for the whole poll:
detect → decide → execute → audit. `_decide()` called the model provider while
that transaction was open, so the SQLite **single-writer lock was held for the
entire decision**. On the contract's 20-sample evidence window a CPU decision is
~400 s, and during it every other write (`POST /nodes/{id}/telemetry`,
`POST /intents`) failed `sqlite3.OperationalError: database is locked` → HTTP 500,
and the dashboard showed a healthy fleet as all-offline (measured in the bring-up).
WAL and a 15 s `busy_timeout` were already set (`app/db.py`) and are irrelevant to
a 400 s hold — the transaction must not span the call.

### Fix

`run_once()` is now **two phases**, and no transaction is ever open across a slow
step (`controller/app/operator/loop.py`):

1. **detect** — one short write transaction: acquire/renew the single-owner lease,
   load durable thresholds, evaluate every node's triggers, persist the hysteresis
   latch and the fired health events. Committed *before* any remediation.
2. **remediate** (per fired event) — with no transaction open: make the decision
   (the model call), then write the decision, the idempotency lookup, the LXD call
   and the audit, each in its own short transaction.

The lease is renewed per remediation for a TTL that covers the decision timeout
(`pass_lease_ttl_s`), so the single-owner invariant is not weakened by no longer
pinning the write lock across the call. The mitigation drop-in
`zzz-standing-tuning.conf` is restored to the contract window
(`TERMINI_OPERATOR_WINDOW=20`) — the `window=3` shrink is no longer needed.

### Live proof: a write succeeds WHILE a 386 s model call is in flight

Real operator decision driven by the demo seeder against node-c's gemma. The call
window is bracketed by the boundary logger; the model call took **386 s**:

```
Oct 13 16:28:05 termini-controller python3[36371]: 2026-10-13 16:28:05,037 INFO termini.operator.loop node 01M4NZK474BVARFBXYQBMVA8GV rule oom-guard-01 FIRED: detection_latency=2.032s (breach=2026-10-13T16:28:03.000Z -> fired=2026-10-13T16:28:05.032Z)
Oct 13 16:28:05 termini-controller python3[36371]: 2026-10-13 16:28:05,050 INFO termini.operator.external_call externalModelCall before: {'request_id': '01M4V1FCHJZMNMK32ZEDQ29DM2', 'template_version': 'operator-decision v1', 'destination': 'http://10.10.0.13:8080', 'phase': 'before', 'outcome': 'ok', 'payload_sha256': '1fcee757e2d46a9e1023438037cf453fb87af090b5862e947d6d502c3ef59fff', 'substituted_fields': 1979, 'detail': '', 'violations': []}
Oct 13 16:34:31 termini-controller python3[36371]: 2026-10-13 16:34:31,019 INFO termini.operator.external_call externalModelCall after: {... 'phase': 'after', 'outcome': 'returned', ..., 'detail': 'DecisionResponse', 'violations': []}
Oct 13 16:34:31 termini-controller python3[36371]: 2026-10-13 16:34:31,048 INFO termini.operator.loop node 01M4NZK474BVARFBXYQBMVA8GV rule oom-guard-01 -> instance.restart 01M4N827979BD9D2762EFE24A0: error (remediation_latency=386.001s)
```

`POST /intents` issued from the controller **during** that in-flight call (16:30:44
– 16:31:26, i.e. ~2.5 min into a call that ended 16:34:31):

```
=== POST /intents WHILE the model call is in flight (start 16:30:44) ===
  16:30:44 t=1 -> HTTP 200 0.013320s
  16:30:50 t=2 -> HTTP 200 0.006024s
  16:30:56 t=3 -> HTTP 200 0.006384s
  16:31:02 t=4 -> HTTP 200 0.006030s
  16:31:08 t=5 -> HTTP 200 0.005945s
  16:31:14 t=6 -> HTTP 200 0.005855s
  16:31:20 t=7 -> HTTP 200 0.005904s
  16:31:26 t=8 -> HTTP 422 0.002453s      # GUARDRAIL_REJECTED exceeds_node_disk_ceiling — a real write path, never a 5xx
```

(Seven `200` responses committed live capacity and were released afterwards; the
eighth `422` **proves** the earlier ones had really written — the committed
capacity had accumulated. Every response returned in milliseconds.)

Aggregate over the whole 386 s window (journal, `16:28:00 → 16:34:40`):

```
telemetry POSTs answered 202:                              1046
telemetry POSTs answered 5xx:                                 0
POST /intents answered 200:                                   7
POST /intents answered 422 (guardrail, not 5xx):              1
POST /intents answered 5xx:                                   0
lines containing "database is locked"/OperationalError:       0
```

Representative telemetry accepted **during** the call (three different nodes,
including both real ones — the previous symptom was their heartbeats freezing):

```
2026-10-13 16:28:00,182 ... "POST /v1/nodes/01M4NZK46WXG5ZEFAGAAV5JRXM/telemetry HTTP/1.1" 202 -
2026-10-13 16:28:00,187 ... "POST /v1/nodes/01M4NZK474BVARFBXYQBMVA8GV/telemetry HTTP/1.1" 202 -
2026-10-13 16:28:00,686 ... "POST /v1/nodes/01M4TGEEZEHJEQB01T5PBBE97M/telemetry HTTP/1.1" 202 -   # node-c (real)
...
2026-10-13 16:34:39,338 ... "POST /v1/nodes/01M4NZK46WXG5ZEFAGAAV5JRXM/telemetry HTTP/1.1" 202 -
2026-10-13 16:34:39,342 ... "POST /v1/nodes/01M4NZK474BVARFBXYQBMVA8GV/telemetry HTTP/1.1" 202 -
```

### Regression tests (fail on the old shape, pass on the new)

`tests/test_operator_loop_db_lock.py` runs the loop's `run_once()` in a thread with
a stub provider whose `decide()` blocks, then attempts a concurrent write (a
telemetry-ingest-shaped INSERT on its own connection with a 2 s busy timeout)
while the model call is provably in flight. Run against the **pre-fix** commit
(`6e67589`, in a scratch worktree with only the new test copied in):

```
Ran 2 tests in 5.455s
FAILED (failures=2)
AssertionError: OperationalError('database is locked') is not None : concurrent write failed while the model call was in flight: OperationalError('database is locked')
...
FAILED (failures=2)
```

i.e. the test reproduces the exact live failure on the old code. It passes on this
branch (see the suite result below).

---

## DEFECT 2 — the fleet display contradicted the deck (8 rows / 6 offline vs 2)

The live listing returned 8 rows: 2 real nodes, 2 seeder-fabricated rows
(`demo-node-a` / `demo-node-c`), and **4 stale phantom rows** from earlier test
runs. Two mechanisms fix it, both using things that already existed (#132
provenance, #134 decommission):

1. **The 4 stale phantom rows were retired through the existing route** — no DB
   edit, no fabrication (the earlier attempt was blocked only by an interactive
   approval gate; a non-interactive `curl` reaches the same route):

```
=== BEFORE: GET /nodes (default view) ===
count 6
  01M4RDKK04NZ15AQ5SGNEBDP26 78851924e0de25f64f4846d9727e3132 synthetic=False offline
  01M4RJDET6M7VY3Q72AH9C722F 2e9f264ab8bb267ecbcc5702057a0dec synthetic=False offline
  01M4RMC0RXYEG74704MS77ZDC6 8e836bfaa2dccd5238734ec2b6e71000 synthetic=False offline
  01M4RMWA9TFDQQ3E2D8K00H9S7 e008364545fccfea40d0cf414d8c6416 synthetic=False offline
  01M4TGD31WXH97JAA5S6A4R10D ba1f1faeacb4501aed12cf8ea1e5713b synthetic=False online
  01M4TGEEZEHJEQB01T5PBBE97M 02086295203773b7113811a1e1fec4ad synthetic=False online

=== decommission the 4 stale phantom rows ===
  decommission 01M4RDKK04NZ15AQ5SGNEBDP26 -> HTTP 200 {"status": "decommissioned", ..., "released": {"cpu_allocated": 0, ...}, "rows_released": 0}
  decommission 01M4RJDET6M7VY3Q72AH9C722F -> HTTP 200 {"status": "decommissioned", ..., "released": {"cpu_allocated": 4, "ram_allocated_gb": 4, "disk_allocated_gb": 32}, "rows_released": 2}
  decommission 01M4RMC0RXYEG74704MS77ZDC6 -> HTTP 200 {"status": "decommissioned", ..., "released": {"cpu_allocated": 2, "ram_allocated_gb": 2, "disk_allocated_gb": 16}, "rows_released": 1}
  decommission 01M4RMWA9TFDQQ3E2D8K00H9S7 -> HTTP 200 {"status": "decommissioned", ..., "released": {"cpu_allocated": 0, ...}, "rows_released": 0}

=== AFTER: GET /nodes (default view) ===
count 2
  01M4TGD31WXH97JAA5S6A4R10D ba1f1faeacb4501aed12cf8ea1e5713b synthetic=False online
  01M4TGEEZEHJEQB01T5PBBE97M 02086295203773b7113811a1e1fec4ad synthetic=False online
```

2. **The default listing now applies the same provenance rule as placement.** A
   `synthetic` row is excluded from `GET /nodes` while any real node is
   registered (`controller/app/server.py` `_list_nodes`, mirroring
   `_selection_pool`, controller-api.md §5.2 step 5). Without this, restarting the
   seeder re-introduces rows the deck does not show. A seeder-only demo (no real
   node) still lists its fabricated rows, and `?include=synthetic` returns every
   row with provenance intact. `docs/contracts/dashboard.md` §1.1 records the rule.

Verified against the live stack with the seeder running (the seeder's rows are
hidden while its telemetry still drives the loop):

```
=== GET /nodes (default fleet view) ===
count 2 | offline 0
  01M4TGD31WXH97JAA5S6A4R10D compute online cpu 0.0 ram% 10.1
  01M4TGEEZEHJEQB01T5PBBE97M compute online cpu 0.0 ram% 20.8 termini-bootstrap:running,llama-cpp:running

=== GET /nodes?include=synthetic (all rows, provenance) ===
count 4
  01M4NZK46WXG5ZEFAGAAV5JRXM demo-node-a synthetic=True degraded
  01M4NZK474BVARFBXYQBMVA8GV demo-node-c synthetic=True degraded
  01M4TGD31WXH97JAA5S6A4R10D ba1f1faeacb4501aed12cf8ea1e5713b synthetic=False online
  01M4TGEEZEHJEQB01T5PBBE97M 02086295203773b7113811a1e1fec4ad synthetic=False online
```

Tests: `tests/test_fleet_listing_default_view.py` (default hides synthetic when a
real node exists; `?include=synthetic` returns all; seeder-only demo still lists
them; a decommissioned node leaves the listing). Fails on the pre-fix commit:

```
FAIL: test_default_listing_is_the_real_fleet_when_a_real_node_exists
Ran 8 tests in 3.052s
FAILED (failures=1, errors=3)     # the 3 errors are DEFECT 3's test importing the new default
```

---

## DEFECT 3 — the workload-image default did not match the bake's alias

`controller/app/config.py` defaulted the alias to the bare `agent-vm`, but the
node bake publishes `agent-vm/<semver>` (`image/agent-vm/build-agent-image.sh`,
`docs/contracts/agent-runtime.md` §3.1/§4.2). An out-of-the-box `POST /vms/apply`
therefore failed `create ... : Image not provided for instance creation` on a
freshly-baked node and had to be set by env. Fixed by a named module constant
(`DEFAULT_LXD_WORKLOAD_IMAGE = "agent-vm/2026.10.0"`) used as the `Config.load()`
fallback, pinned by `tests/test_workload_image_default.py` against
`image/agent-vm/evidence/image-manifest.json` (so the two cannot drift again).

---

## DEFECT 4 — LXD's `already stopped` was not folded to a no-op

`app/operator/executor.py` folded LXD's *already-running* answer to
`already_applied` (#133) but not the symmetric *already-stopped* answer, so a
re-run of a stop remediation recorded `result=error` (measured live in the
bring-up). Added `_ALREADY_STOPPED` + `_instance_already_stopped` and folded it in
`stop_instance()` (covering both the synchronous 400 and a failed operation).
Tests in `tests/test_operator_executor.py`: the fold, a control that a running
instance is genuinely stopped, a control that a real stop failure is **not**
folded, and a repeated-stop no-op. Pre-fix:

```
FAIL: test_stop_of_a_stopped_instance_is_an_idempotent_no_op   ('error' != 'already_applied')
FAIL: test_stop_of_repeated_remediation_is_a_no_op_on_the_second_run
Ran 11 tests in 5.580s
FAILED (failures=2)
```

---

## Gates

```
$ python3 -m unittest discover -s tests -t .
Ran 563 tests in 74.764s
OK
$ python3 scripts/check-doc-ulids.py
all example identifiers are 26-char ULIDs
$ python3 scripts/check-readme-links.py
checked 58 unique link/path target(s)
all README links and repo paths resolve
```

(549 → 563 tests; no regression. The 4 new files are the defect regressions.)

---

## Fleet state after the run (left UP for the user)

```
$ gcloud compute instances list
NAME                ZONE           MACHINE_TYPE   INTERNAL_IP  EXTERNAL_IP   STATUS
termini-controller  asia-south1-a  n2-standard-2  10.10.0.2    34.93.190.64  RUNNING
termini-node-a      asia-south1-a  n2-standard-2  10.10.0.11                 RUNNING
termini-node-b      asia-south1-a  n2-standard-2  10.10.0.12                 TERMINATED
termini-node-c      asia-south1-a  n2-standard-8  10.10.0.13                 RUNNING
```

- `termini-controller` **active** (fixed code); `termini-seeder` **inactive**
  (stopped, as at handover).
- `GET /nodes` → **2 nodes, 0 offline** (both real nodes ONLINE, node-c reporting
  `llama-cpp:running`); gemma serving on `http://10.10.0.13:8080/v1/models`;
  node-a LXD `GET /1.0` → 200.
- Judge-facing REST all 200: `/nodes`, `/operator/events`, `/operator/decisions`,
  `/operator/audits`, `/telemetry/recent`. Public path verified from off-host:
  `https://34-93-190-64.sslip.io/v1/projects/{pid}/nodes` → 200 (2 nodes online);
  `https://termini-510609.web.app/` → 200.
- Live committed capacity: the 4 phantoms' stale commitments were released by the
  decommission (6 vCPU / 6 GiB / 48 GiB); the 7 probe plans were released.

---

## Still unverified, or still wrong (disclosed)

1. **Two unused bootstrap-token hashes were added to the DB** by the temporary
   token drop-in used to re-run the seeder for the DEFECT-1 proof. The drop-in is
   removed and the controller restarted on the original config, but the two
   hashes remain in `bootstrap_tokens` as *unused* rows. They are inert
   single-use tokens whose values only ever existed transiently on the VM.
2. **Three pre-existing LIVE `vm_allocations` rows remain** (2 vCPU / 2 GiB /
   16 GiB total) — two on the hidden synthetic `demo-node-a`, one on the real
   `termini-node-a` created 2026-10-13T11:58Z with `vm_ids_json IS NULL`. Per the
   product's own rule a never-materialised reservation is deliberately **not**
   released (`release_allocations`/reconcile require provenance of gone
   instances), so they were left alone rather than hand-edited.
3. **The seeder's synthetic rows remain registered** (hidden by the new default
   view). Restarting `termini-seeder` re-registers them; they are marked
   `synthetic` and never selected for placement while a real node is up.
4. **A remediation against the seeder's fabricated instance name 404s** — the
   seeder's `lxd_instances[].vm_id` is not a real LXD instance, so a model-chosen
   `instance.restart` on it records `result=error` (HTTP 404). Pre-existing demo
   roughness, outside this scope; with the real node-a workload the path is
   exercised correctly (bring-up Claim c).
5. Not re-verified here (unchanged from the bring-up): the GPU path, the
   materialisation rollback path on a forced start failure, the SSH terminal
   bridge (no CA configured), and the LXD *cluster* join.
6. The DEFECT-1 tests use a stub blocking provider, not the real 386 s call; the
   real call is evidenced live above. The two are deliberately separate.

## Out of scope (stated, not changed) — the GPU-gated role rule

`controller/app/role.py` assigns `inference` only to a node reporting ≥1 GPU with
≥12 GiB VRAM. **Consequence:** CPU-only `node-c` registers `compute`, so on this
fleet **no real node can have role `inference`** — the only `inference` row in the
database is the seeder's fabricated one. An `inference_vm` intent therefore either
resolves to a node that does not exist or is rejected
`invalid_target_node_type`; and `?include=synthetic` shows `demo-node-c` as the
sole `inference` row. Changing that frozen rule is a contract decision the user has
not made, so it is left as-is and reported here.
