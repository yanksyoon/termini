# A cleared fault injection no longer mutes the node's own telemetry (t_c37aa0dc)

**Card:** t_c37aa0dc — *Demo fault console: a cleared injection drops the node's own
telemetry until its seq catches up (2m19s measured).*
**Measured:** 2026-10-17T19:48:40–19:49:36Z on the standing fleet.
**Deployed tree:** `a89f8521254c1f2b1af25f24d91d4bbc12194873` (merged `main`; it
contains the fix from PR #237 = squash `43f7102`). Read back from
`/opt/termini/DEPLOYED_COMMIT` before the run.
**Node:** `termini-node-c` / `01M4ZQ7Q2J80ATY3K32PY3PPJH`
(registration `33678f54a5e3e25cc03620b1bd2486e6`), `lxd_endpoint=https://10.10.0.13:8443`.
**Injection:** `01M55PHJC9PP6FRA4NPFRDJQFN` (`oom`, `target=real`, `duration_s=60`).

Everything below is raw output from the live controller's own database and API;
commands are in [Reproduce](#reproduce).

**Result: the node's own sample was stored 3.714 s after the operator's clear**
(`0.618 s` after the injection stopped superseding the series). Before the fix the
same measurement was **2 m 19 s** (`solvable-faults-live-2026-10-17.md` §4 finding 1).

## 1. What was wrong

`store.insert_samples` dropped every sample whose `seq` was not greater than the
node's highest stored `seq`. `DemoFaultInjector` wrote its 1 Hz series through that
same space starting just above the node's own head, so a 60 s injection occupied
~40–60 seq values the node's ~0.3 Hz counter had not reached yet. After a clear the
node kept POSTing successfully — `202` every ~3.2 s — and the store kept dropping
them until its counter passed the injected range: the dashboard showed the
injector's fabricated 62.0 % healthy baseline, not the node.

## 2. The fix

Migration `0021_telemetry_source_channel` gives each sample source its own
`seq` namespace and records it per row:

* `telemetry_samples.source` ∈ `node` | `demo_fault`; unique key
  `(node_id, source, seq)`; existing rows backfilled as `node`.
* `store.insert_samples(…, source=…)` heads/dedupes per `(node_id, source)`;
  ingest passes `store.NODE_SOURCE`, the injector passes
  `store.DEMO_FAULT_SOURCE` and resumes above **its own** head.
* The injected series can therefore never occupy a `seq` the node has not sent, and
  the stored series still says which samples were simulated. Contract updates:
  `docs/contracts/controller-api.md` §4.7, `docs/contracts/node-telemetry.md` §1.1.

Cost of the live migration, measured on a **copy** of the live DB before it ran on
the real one (297–298 k samples, 469 MB of payloads):

```
backup /tmp/pre0021b.db in 16.05 s
integrity ok
schema_version 20
samples (298400, 298400)
BEFORE {'count': 298400, 'max_id': 298400, 'max_seq_nodec': 30051,
        'sample_sha': (298400, 469360730)}
applied ['0021_telemetry_source_channel'] in 70.68 s
integrity ok
AFTER counts {'n': 298400, 'max_id': 298400, 'bytes': 469360730,
              'node_rows': 298400, 'fault_rows': 0, 'max_seq': 1792264117}
same seq in both channels: ok; new id 298403 > old max 298400
```

On the live DB (`/data/controller.db`, 1.4 GB) the migration is recorded once:

```
sqlite> select version, name, applied_at from schema_migrations order by version desc limit 2;
(21, '0021_telemetry_source_channel', '2026-10-17T19:32:38Z')
(20, '0020_audit_operation_reason',      '2026-10-17T17:27:47Z')
```

## 3. Acceptance (a) — the node's own series resumes at once

The driver injects a 60 s OOM fault, holds it until 50 injected samples exist, then
phase-aligns the clear to the node's own POST cadence (the supersede path still
records a `telemetry_batches` row per POST) and clears it with
`DELETE /v1/projects/{pid}/demo/faults/{iid}`.

```
=== PRE-STATE 01M4ZQ7Q2J80ATY3K32PY3PPJH ===
{"stored_head": {"source": "node", "seq": 30399, "ts": "2026-10-17T19:48:35Z",
                 "ram_pct": 17.92, "lxd_instances": []}}
{"node_channel_max_id": 301035}

=== POST /v1/projects/01J0DEM0WARM000000000000AB/demo/faults (scenario=oom duration=60s) ===
HTTP 201
{"injection_id": "01M55PHJC9PP6FRA4NPFRDJQFN", "state": "active",
 "duration_s": 60.0, "started_at": "2026-10-17T19:48:40.969Z",
 "expires_at": "2026-10-17T19:49:40.969Z", "open": true}

=== holding the fault ===
{"injected_samples_written": 50, "injection_state": "active"}
{"last_node_post_before_clear": "2026-10-17T19:49:31Z"}
{"node_just_posted_at": "2026-10-17T19:49:32Z"}

=== DELETE /v1/projects/01J0DEM0WARM000000000000AB/demo/faults/01M55PHJC9PP6FRA4NPFRDJQFN
    at 2026-10-17T19:49:32.607Z (node-channel head id 301043) ===
HTTP 200   (state=recovering, clear_samples_needed=3)

{"recovery_closed": {"at": "2026-10-17T19:49:35.704Z", "state": "cleared",
                     "recovery_samples": 3, "samples_written": 54,
                     "cleared_at": "2026-10-17T19:49:35.629Z"}}

=== THE NODE'S OWN SAMPLE, STORED AFTER THE CLEAR (raw row) ===
{"id": 301222, "source": "node", "seq": 30417, "ts": "2026-10-17T19:49:34Z",
 "ram_pct": 17.93, "ram_used_bytes": 6031458304, "ram_total_bytes": 33646948352,
 "cpu_util_pct": 0.0, "lxd_instances": [], "hostname": "092cd5e29db96478"}

=== LATENCY ===
{"clear_requested_at": "2026-10-17T19:49:32.607Z",
 "recovery_closed_at": "2026-10-17T19:49:35.704Z",
 "node_sample_ts": "2026-10-17T19:49:34Z",
 "node_sample_observed_stored_at": "2026-10-17T19:49:36.322Z",
 "seconds_clear_to_stored": 3.714,
 "seconds_recovery_closed_to_stored": 0.618,
 "sample_ts_is_after_clear": true,
 "node_clock_minus_controller_at_store_s": -2.322}
```

The node's own stream then continues uninterrupted at its own ~3 s cadence, with the
node's real RAM (not the injector's 62.0 % baseline):

```
{"id": 301222, "source": "node", "seq": 30417, "ts": "2026-10-17T19:49:34Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301230, "source": "node", "seq": 30418, "ts": "2026-10-17T19:49:37Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301240, "source": "node", "seq": 30419, "ts": "2026-10-17T19:49:40Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301248, "source": "node", "seq": 30420, "ts": "2026-10-17T19:49:43Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301256, "source": "node", "seq": 30421, "ts": "2026-10-17T19:49:47Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301264, "source": "node", "seq": 30422, "ts": "2026-10-17T19:49:50Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301272, "source": "node", "seq": 30423, "ts": "2026-10-17T19:49:53Z", "ram_pct": 17.93, "lxd_instances": []}
{"id": 301282, "source": "node", "seq": 30424, "ts": "2026-10-17T19:49:56Z", "ram_pct": 17.93, "lxd_instances": []}
```

Why the first stored row's `ts` is ~2.3 s behind the controller clock: node-c's
clock runs that far behind, so the node stamped only *stored-at* time. The
authoritative proof that the row is post-clear is its identity —
`id 301222 > 301043` (the node-channel head captured at the DELETE) — and the
observation time (`19:49:36.322Z`), which is 3.714 s after the clear.

## 4. Acceptance (b) — injected samples stay distinguishable

Both provenances are in the same stored series, tagged per row (`source`), for the
same window:

```
{'source': 'demo_fault', 'n': 54, 'first_ts': '2026-10-17T19:48:41.775Z', 'last_ts': '2026-10-17T19:49:35.629Z'}
{'source': 'node',       'n': 87, 'first_ts': '2026-10-17T19:49:34Z',       'last_ts': '2026-10-17T19:54:13Z'}
```

(read at 19:54Z; the node's count keeps growing — the stream never stops again)

```
{"source": "demo_fault", "seq": 33, "ts": "2026-10-17T19:48:41.775Z", "ram_pct": 97.0, "cpu_util_pct": 92.0, "lxd_instances": []}
…
{"source": "demo_fault", "seq": 82, "ts": "2026-10-17T19:49:31.585Z", "ram_pct": 97.0, "cpu_util_pct": 92.0, "lxd_instances": []}
{"source": "demo_fault", "seq": 83, "ts": "2026-10-17T19:49:32.596Z", "ram_pct": 97.0, "cpu_util_pct": 92.0, "lxd_instances": []}
{"source": "demo_fault", "seq": 84, "ts": "2026-10-17T19:49:33.607Z", "ram_pct": 62.0, "cpu_util_pct": 31.2, "lxd_instances": []}   <- recovery burst
{"source": "demo_fault", "seq": 85, "ts": "2026-10-17T19:49:34.618Z", "ram_pct": 62.0, "cpu_util_pct": 31.2, "lxd_instances": []}
{"source": "demo_fault", "seq": 86, "ts": "2026-10-17T19:49:35.629Z", "ram_pct": 62.0, "cpu_util_pct": 31.2, "lxd_instances": []}
{"source": "node", "seq": 30417, "ts": "2026-10-17T19:49:34Z", "ram_pct": 17.93, "cpu_util_pct": 0.0, "lxd_instances": []}
```

The injector's 62.0 % "recovery" is now explicitly labelled as the simulation, and
the node's 17.93 % is labelled as the node. The `demo_faults` row and the operator
log lines below remain the durable injection record, as before.

## 5. Acceptance (c) — the rule is pinned by tests

`tests/test_telemetry_seq_channels.py` (store + migration, no server) proves on a
real migrated DB that 60 injected samples at `seq 1..60` do not stop the node's
`seq 4` sample from being stored and becoming the latest, that node-channel dedup
still drops duplicates/out-of-order samples, that both channels coexist and are
distinguishable, and that migration 0021 preserves row identity/count and backfills
`source='node'`. `tests/test_demo_faults.py::…test_a_clear_resumes_the_nodes_own_series_on_its_next_sample`
drives the real HTTP controller: inject → 6+ injected samples → node posts seq 2,3,4
(superseded) → clear → node posts seq 5, **below** the injected head → `202
{"accepted": 1}`, raw row `source='node', seq=5`, 62.0 % real RAM; injected rows
remain `source='demo_fault'`.

```
$ python3 -m unittest tests.test_demo_faults tests.test_telemetry_seq_channels
Ran 31 tests in 38.669s
OK
```

CI on the merged PR: `sanity` pass (5m04s — its `Run unit tests` step is
`python3 -m unittest discover -s tests`, the whole suite) and `dashboard` pass.

## 6. Run integrity (what could have fooled this evidence)

* The live fleet is shared with two other `infra` workers tonight. An **earlier
  attempt at 19:40:56Z was closed by a third party** after 31 s (`cleared_by=human`
  is the default attribution for *any* caller, including another worker's
  clear-all), so the tool aborted and the run was discarded. The reported run is the
  clean one: our `DELETE` returned `200` with `state=recovering`, 50 injected samples
  were written before it, and no other actor touched the injection.
* The injection did drive the frozen rule (`oom-guard-01` fired at
  `19:48:43.746Z`); the loop's remediation refused itself with
  `target_missing: … no LXD instance 'demo-workload' on https://10.10.0.13:8443`
  because node-c has no running instance. Nothing on the fleet was changed by this
  run; that refusal is also why `lxd_instances[]` in the node's sample is genuinely
  empty rather than a fabricated list.
* Two controller redeploys by peer workers landed around this window (a branch tree
  at ~19:30:45Z, then merged `main`). `DEPLOYED_COMMIT` was read as
  `a89f852…` immediately before the run and again after it, so the numbers above
  come from the fixed tree; nothing was measured against a mixed tree.

## Reproduce

```bash
# 0. deploy merged main (contains the fix) and confirm what is live
infra/gcp/tools/deploy-termini-tree.sh --ref a89f852
gcloud compute ssh termini-controller --zone=asia-south1-a --tunnel-through-iap \
  --command='cat /opt/termini/DEPLOYED_COMMIT'

# 1. the run (inject 60 s, hold 50 injected samples, phase-align, clear, watch storage)
gcloud compute scp infra/gcp/evidence/tools/demo-fault-clear-telemetry.py \
  termini-controller:/tmp/ --zone=asia-south1-a --tunnel-through-iap
gcloud compute ssh termini-controller --zone=asia-south1-a --tunnel-through-iap \
  --command='python3 /tmp/demo-fault-clear-telemetry.py run \
             --node 01M4ZQ7Q2J80ATY3K32PY3PPJH --duration 60'

# 2. the stored rows, both channels (raw)
gcloud compute scp infra/gcp/evidence/tools/demo-fault-clear-rows.py \
  termini-controller:/tmp/ --zone=asia-south1-a --tunnel-through-iap
gcloud compute ssh termini-controller --zone=asia-south1-a --tunnel-through-iap \
  --command='python3 /tmp/demo-fault-clear-rows.py \
             --since 2026-10-17T19:48:40Z \
             --injection 01M55PHJC9PP6FRA4NPFRDJQFN'
```
