# The STANDING demo fleet on real GCP hardware — RAW EVIDENCE (2026-10-13)

Raises the Termini demo on real GCP hardware from the current node image, exercises
every published claim end to end, and leaves the stack RUNNING. Every block below is
verbatim output from a real `gcloud` / `ssh` / `curl` / `lxc` / controller-HTTP run
against the deployed system. The GPU path is out of scope (the billing account is
free-tier and rejects every non-TPU accelerator).

## Provenance

| | |
|---|---|
| Date | 2026-10-13 |
| Controller code | `ce4cd29` (`origin/main`), shipped with `git archive HEAD controller termini \| gzip` → extracted over `/opt/termini`, then `console/` deployed the same way |
| Controller VM | `termini-controller`, `asia-south1-a`, `n2-standard-2`, internal `10.10.0.2`, external `34.93.190.64`, host `34-93-190-64.sslip.io` |
| Compute node | `termini-node-a`, `asia-south1-a`, `n2-standard-2`, internal `10.10.0.11`, no external IP |
| Inference node | `termini-node-c`, `asia-south1-a`, `n2-standard-8`, internal `10.10.0.13`, no external IP |
| Node image | family `termini-node-os` → `termini-node-os-5` (newest) |
| Decision model | `llama.cpp` on node-c, alias `gemma` = Gemma 3 4B IT Q4_K_M, `10.10.0.13:8080` |
| Raise tool | `infra/gcp/tools/raise-standing-fleet.sh` (added by this PR) |
| vCPU quota | `CPUS_ALL_REGIONS` 12/12 used — `controller(2) + node-a(2) + node-c(8)`; `node-b` is TERMINATED |

Raised with the repo's own shared cloud-init renderer, so the user-data is the same
document the Terraform path and the LXD flow produce:

```
[raise-fleet] node-a: creating termini-node-a (n2-standard-2, 10.10.0.11) from family termini-node-os
Created [https://www.googleapis.com/compute/v1/projects/termini-510609/zones/asia-south1-a/instances/termini-node-a].
[raise-fleet] node-c: creating termini-node-c (n2-standard-8, 10.10.0.13) from family termini-node-os
Created [https://www.googleapis.com/compute/v1/projects/termini-510609/zones/asia-south1-a/instances/termini-node-c].
```

---

## Claim (a) — a node boots from the current image, registers, and streams its OWN telemetry — PASS

No seeder involved (`systemctl is-active termini-seeder` → `inactive` while these were
captured). Both nodes register from the **baked** daemon; nothing installs at boot.

```
$ gcloud compute ssh termini-node-a ... 'sudo journalctl -u termini-bootstrap | grep REGISTERED'
2026-10-13T11:29:44Z [termini-bootstrap] REGISTERED node_id=01M4TGD31WXH97JAA5S6A4R10D role=compute register_ms=37 boot_to_registered_ms=176120

$ gcloud compute ssh termini-node-c ... 'sudo journalctl -u termini-bootstrap | grep REGISTERED'
2026-10-13T11:30:28Z [termini-bootstrap] REGISTERED node_id=01M4TGEEZEHJEQB01T5PBBE97M role=compute register_ms=37 boot_to_registered_ms=203290
```

`register_ms=37` is the contract §12.5 metric (daemon start → controller ack) — met
with ~135× margin against the `< 5 s` budget. `boot_to_registered_ms` is from power-on
and is dominated by firmware/OS/cloud-init, as designed.

The image is the current one, and its LXD is *usable* (not merely listening):

```
$ curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/1.0
200
$ sudo lxc image list
| agent-os/w4 (1 more) | c3d039642a61 | no | Ubuntu 24.04 LTS server (20260926) | x86_64 | CONTAINER | 764.36MiB |
```

The controller's own DB corroborates the registration independently of the node's log,
and the batches are accepted, at 1 s cadence:

```
registration_id=ba1f1faeacb4501aed12cf8ea1e5713b role=compute synthetic=0 lxd_endpoint=10.10.0.11:8443
registration_id=02086295203773b7113811a1e1fec4ad role=compute synthetic=0 lxd_endpoint=10.10.0.13:8443

== REAL node telemetry acceptance (newest batches) ==
  ba1f1faeacb4501aed12cf8ea1e5713b 2026-10-13T12:36:00Z accepted=1 rejected=0
  02086295203773b7113811a1e1fec4ad 2026-10-13T12:36:00Z accepted=1 rejected=0
```

The node's own sample (`telemetry_batches.payload_json`, node-c) is real hardware:

```json
{"seq": 214, "cpu_core_count": 8, "ram_total_bytes": 33646960640,
 "hostname": "092cd5e29db96478", "ip_local": "10.10.0.0/24",
 "inference": {"server_up": true, "model": "gemma", ...},
 "services": [{"name":"termini-bootstrap","status":"running"},{"name":"llama-cpp","status":"running"}]}
```

**Caveats attached to this PASS**

1. The first registration FAILED — the controller was bound to `127.0.0.1:8080` (the
   pre-existing unit was built for the same-VM seeder) and no firewall rule let the
   underlay reach it. The daemon correctly went terminal
   (`REGISTRATION_FAILED kind=unreachable_timeout`, and held, no auto-retry). Fixed by
   binding `0.0.0.0` and adding `termini-allow-controller-underlay` (tcp:8080 from
   `10.10.0.0/24` to tag `termini-controller`). The frozen port map documents 443 for
   the controller API; the demo path is plain `:8080` behind Caddy, so the rule opens
   8080, not 443.
2. node-c's cloud-init `runcmd` stalled (the `systemctl enable --now termini-bootstrap`
   step waits on a `Type=notify` unit that never sends READY when registration fails,
   so the following llama.cpp step started late). The llama.cpp build itself then ran
   correctly, unattended — it was already running when checked
   (`bash /usr/local/sbin/termini-llama-serve.sh`, `cmake --build ... -j 8`).
3. node-c has **no Cloud NAT**, so it has no egress: the first-boot build (`apt-get`,
   `git clone`, the 2.5 GB GGUF) cannot work without internet. A temporary ephemeral
   external IP was added for the build and **removed afterwards**; node-c is now
   reachable only over the underlay + IAP SSH, matching the documented shape.
4. The image's baked daemon predates #143, so it reports the pre-#143 model alias
   (`deepseek-flash`) in its `inference` snapshot. `model_alias: gemma` was added to
   node-c's `/etc/termini/config.yaml` (the per-node input the daemon reads) and the
   daemon restarted; the sample above now reports `gemma`. The **image should be
   rebaked** so this needs no hand-edit.

## Claim (b) — apply a plan → real LXD instances exist AND ARE RUNNING on node-a — PASS

```
POST /vms/apply 01M4THVQ6TZMYVD5ZFY0W5FJSH -> HTTP 202
{"job_id":"01M4THYETT1X0FG20AH98DT34Y","state":"ready",
 "vm_ids":["termini-01m4thvq6tzmyvd5zfy0w5fj-0-0","termini-01m4thvq6tzmyvd5zfy0w5fj-0-1"],
 "allocations":[{"index":0,"status":"ready","node_id":"01M4TGD31WXH97JAA5S6A4R10D",
   "realized_as":"vm","created":0,"already_applied":2}]}
```

Proven **on the node**, not from the API response:

```
$ sudo lxc list            # ON termini-node-a
| demo-workload                        | STOPPED |
| termini-01m4thvq6tzmyvd5zfy0w5fj-0-0 | RUNNING |
| termini-01m4thvq6tzmyvd5zfy0w5fj-0-1 | RUNNING |
$ for i in termini-01m4thvq6tzmyvd5zfy0w5fj-0-*; do echo "$i cpu=$(lxc config get $i limits.cpu) mem=$(lxc config get $i limits.memory)"; done
termini-01m4thvq6tzmyvd5zfy0w5fj-0-0 cpu=1 mem=1GiB
termini-01m4thvq6tzmyvd5zfy0w5fj-0-1 cpu=1 mem=1GiB
```

A replay is idempotent (`created:0`, `already_applied:2`) and both stay `RUNNING`.

**Caveat that had to be fixed to get here:** the FIRST apply failed
`create ... failed: operation Failure: Image not provided for instance creation`.
The controller's workload-image alias default is the bare `agent-vm`
(`controller/app/config.py` → `or "agent-vm"`), but the node bake stores
`agent-vm/2026.10.0` (agent-runtime.md §3.1/§4.2) — so out of the box no instance can
be created from a freshly-baked node. Fixed operationally with
`TERMINI_LXD_WORKLOAD_IMAGE=agent-vm/2026.10.0`. **The code default and the bake
disagree and should be reconciled.**

Also: `auto` does NOT resolve to node-a — it picks the real node with the most free
RAM, and that is node-c (32 GiB) over node-a (8 GiB). The plan above therefore named
node-a explicitly. See Claim (g) for the auto path.

## Claim (c) — a remediation takes effect against real LXD, and the audit row carries the real endpoint — PASS

`oom-guard-01` fired on the **real** node-a and the loop closed against node-a's own
LXD endpoint (not a stub, not the global fallback):

```
2026-10-13 12:26:57 INFO termini.operator.loop node 01M4TGD31WXH97JAA5S6A4R10D rule oom-guard-01 FIRED: detection_latency=2.101s
2026-10-13 12:28:04 INFO termini.operator.loop node 01M4TGD31WXH97JAA5S6A4R10D rule oom-guard-01 -> instance.stop demo-workload: ok (remediation_latency=67.540s)
```

The audit row (`GET /v1/operator/audits`):

```json
{"id":"01M4TKQXN19FCGR7EH4HRZ7G44",
 "node_id":"01M4TGD31WXH97JAA5S6A4R10D",
 "rule_id":"oom-guard-01","action_kind":"instance.stop","result":"ok",
 "detail":"operation Success: /1.0/operations/a37c55b1-b7cf-4599-9e35-df0f689c383e; action=stop; state ? -> Stopped",
 "lxd_method":"PUT","lxd_path":"/1.0/instances/demo-workload/state",
 "lxd_body":"{\"action\": \"stop\", \"force\": true}",
 "executed_at":"2026-10-13T12:26:57.101Z",
 "lxd_endpoint":"https://10.10.0.11:8443"}
```

and the real state change, read back ON the node:

```
before: demo-workload,RUNNING
after : demo-workload,STOPPED
```

**Caveats attached**

1. The fault was injected at the **telemetry boundary** (the node's own sample with
   `ram_used_bytes` raised to 97 % of `ram_total_bytes`), not by genuinely driving the
   host to ≥95 % RAM — everything downstream of ingest is the production path. To make
   the injected run the tail of the window, node-a's daemon was paused for the test
   window and its persisted `seq` was restored afterwards (verified ONLINE again,
   ingest clean).
2. **The first attempt failed**: the model chose `instance.stop` for an already-STOPPED
   instance and the audit recorded `result=error`,
   `detail="operation Failure: The instance is already stopped"`. LXD's *already
   running* answer was folded to a no-op by #133; the symmetric *already stopped*
   answer is NOT, so a stop of a stopped instance reads as a failure. The successful
   run above therefore had the workload RUNNING first.
3. `detection_latency` is large in the earlier-fire case (455 s) because the injected
   samples were stamped ahead of the controller clock; the second fire measured 2.1 s.
4. This ran with `TERMINI_OPERATOR_WINDOW=3` (see the DB-lock finding below): the whole
   remediation incl. the model call took 67.5 s.

## Claim (d) — the operator's decision is model-driven and served by Gemma on node-c — PASS

The decision row the real node-a episode produced:

```
id=01M4THK888SAQTSQN19JBJZFDQ node=01M4NZK474BVARFBXYQBMVA8GV rule=oom-guard-01
action_kind=instance.restart decision_source=model confidence=0.87 fallback_reason=None
reason="host RAM at 94.7% of total with one instance holding 29%; restarting the heaviest instance reclaims its memory."
```

and the boundary wrapper on the LIVE path, naming the destination and asserting the
payload is C0/transformed-C1 before sending:

```
INFO termini.operator.external_call externalModelCall before: {'request_id':'01M4TH6M0X0QN501VJBMYYP94K',
  'template_version':'operator-decision v1','destination':'http://10.10.0.13:8080','phase':'before',
  'outcome':'ok','payload_sha256':'a46da99e...','substituted_fields':1979,'detail':'','violations':[]}
INFO termini.operator.external_call externalModelCall after: {... 'phase':'after','outcome':'returned',
  'detail':'DecisionResponse','violations':[]}
```

The model is where the wiring says, and it is the pinned artifact:

```
$ curl -s http://10.10.0.13:8080/v1/models
{"models":[{"name":"gemma","model":"gemma",...}],
 "data":[{"id":"gemma","aliases":["gemma"],"object":"model","owned_by":"llamacpp",
          "meta":{"n_ctx":16384,"n_params":3880263168,"ftype":"Q4_K - Medium"}}]}
$ sha256sum /var/lib/termini/models/gemma-3-4b-it-Q4_K_M.gguf
04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19   (= the pin in image/models.lock.json)
```

Measured latency on CPU, matching #143: a 20-sample-evidence decision took
**413.9 s**; a 3-sample one **67.5 s**.

**Caveats attached:** adherence at 4B is not universal, and it is left visible rather
than tuned away — one decision in the same window returned invalid JSON
(`model decision fell back (reason=parse_error): model output is not valid JSON`), and
another fell back `inference_unavailable` when it was issued in the 4-second gap
before node-c's server bound its port. `decision_source` counts accumulated in the DB:
`model 70  fallback 21  deterministic 16`. And on the 20-sample evidence window the
model call is **minutes**, not seconds — see the DB-lock finding.

## Claim (e) — a C2 payload is rejected WHOLE, and the model-call guard asserts C0-only first — PASS

Negative probe with its CONTROL (the control is what makes the rejection mean
something — a fixture that fails both ways proves nothing):

```
CONTROL (no C2 field):
  -> HTTP 202 {"accepted": 1, "rejected": 0, "backpressure": "none"}
C2-BEARING (card_number = 4111 1111 1111 1111):
  -> HTTP 422 {"error": {"code": "GOVERNANCE_REJECT", "message": "C2 content detected",
       "details": [{"reason": "samples.card_number: card-number signature"}]}}

STORED? control call_id rows=1   C2 call_id rows=0
VERDICT: PASS (control accepted, C2 rejected whole, nothing stored)
```

The `externalModelCall()` guard is not documentary — it runs on the live decision path
(the `externalModelCall before/after` lines above, `violations: []`), and its unit
tests pass against this commit:

```
$ python3 -m unittest tests.test_external_model_call -v
Ran 22 tests in 0.007s
OK
```

## Claim (f) — allocation release: the demo CANNOT saturate itself; a stale commitment self-heals — PASS

Three apply→release cycles against node-a. Live committed capacity does not grow:

```
live capacity at start: {'c': 38, 'r': 70, 'n': 6}
round 0: plan=01M4TJFBT59K5XXCW5WMSSGJTH apply=202 ready vm_ids=[...-0-0, ...-0-1] release=200 released | live={'c': 38, 'r': 70, 'n': 6}
round 1: plan=01M4TJHSM3CJSBNXZ2QGTARY1J apply=202 ready vm_ids=[...-0-0, ...-0-1] release=200 released | live={'c': 38, 'r': 70, 'n': 6}
round 2: plan=01M4TJMN7QN3G9KN05H17W431V apply=202 ready vm_ids=[...-0-0, ...-0-1] release=200 released | live={'c': 38, 'r': 70, 'n': 6}
```

Every round is accepted (no `422 exceeds_node_ram_ceiling`) and the live total returns
to the same value — the defect this fixes was permanent growth.

Self-heal: a commit whose instances were destroyed is released with the proof recorded:

```
$ for n in termini-01m4tjrvak3zqcm16e268n2f-0-0 termini-01m4tjrvak3zqcm16e268n2f-0-1; do lxc delete "$n" --force; done
$ POST /vms/reconcile {}
{"nodes_checked": 1,
 "released": [{"plan_id":"01M4TJRVAK3ZQCM16E268N2FMH","node_id":"01M4TGD31WXH97JAA5S6A4R10D",
               "reason":"instances_gone","vm_ids":["termini-01m4tjrvak3zqcm16e268n2f-0-0","...-0-1"]}],
 "released_count": 1}
$ select released_at, release_reason from vm_allocations where plan_id='01M4TJRVAK3ZQCM16E268N2FMH'
2026-10-13T12:14:41Z | instances_gone
```

**Caveats attached**

1. The test had to be redone once: the first `lxc delete` loop matched the **full**
   plan id, but LXD instance names use a 24-char slug, so nothing was deleted and
   reconcile correctly returned `released_count: 0`. That was a broken probe, not a
   product defect — recorded here because "0 released" looked exactly like one.
2. **Five pre-existing stale commitments remain live** (`36 vCPU / 68 GiB` across 5
   phantom nodes from earlier runs). They are deliberately NOT reconciled: a commit
   whose instances were never recorded (`vm_ids_json IS NULL`) has materialised
   nothing, so absence cannot be proven. All five are attributed to phantom nodes, so
   they do not clamp a real node's plan — but they are visible in
   `sum(...) where released_at is null` and there is **no route to release them**.
3. The release path is what makes a standing link survivable; without it the headline
   intent eventually 422s forever with no API way back. That is now exercised.

## Claim (g) — real hardware beats the synthetic seeder node in auto-selection — PASS

```
POST /intents {"spec":{"allocations":[{"type":"agent_vm","count":2,"vcpus":1,"ram_gb":1,"disk_gb":8,"target_node":"auto"}]}}
-> 200, allocation resolved to node_id=01M4TGEEZEHJEQB01T5PBBE97M

  that node is: registration_id=02086295203773b7113811a1e1fec4ad role=compute synthetic=0 lxd_endpoint=10.10.0.13:8443
  candidate: demo-node-a            synthetic=1 ram_bytes=68719476736
  candidate: demo-node-c            synthetic=1 ram_bytes=68719476736
  ...
  candidate: 02086295203773b7113811        synthetic=0 ram_bytes=33646960640
VERDICT: REAL node chosen
```

The two synthetic rows declare 68.7 GB each (their realistic GCE sibling reports 8.3 /
33.6 GB), so before #132 a pure capacity comparison picked a node that does not exist.
The controller now prefers real rows, and the seeder's rows declare `synthetic: true`.

The seeder's rows are marked synthetic on the wire and re-marked on re-registration —
the pre-existing rows (older than migration 0011) were **not** marked until the
`#132`-era seeder re-registered them:

```
  01M4NZK46WXG5ZEFAGAAV5JRXM demo-node-a            compute   synth=1
  01M4NZK474BVARFBXYQBMVA8GV demo-node-c            inference synth=1
  01M4TGD31WXH97JAA5S6A4R10D ba1f1faeacb4501aed12cf compute   synth=0 ep=10.10.0.11:8443
  01M4TGEEZEHJEQB01T5PBBE97M 02086295203773b7113811 compute   synth=0 ep=10.10.0.13:8443
```

## Claim (h) — the dashboard/console a judge would open actually works — PASS (with caveats)

`https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token`

Loaded in a real browser: the app reports **`backend ok`** and **`stream open`**, node
cards render live telemetry, and the two REAL nodes render ONLINE:

```
nodes 8 · 6 offline        backend ok     stream open
ba1f1faeacb4501aed12cf8ea1e5713b  01M4TG…R10D  COMPUTE  ONLINE  CPU 0.0%  RAM 10.2%  seen 3s ago
02086295203773b7113811a1e1fec4ad  01M4TG…E97M  COMPUTE  ONLINE  RAM 19.3%
                                   termini-bootstrap:running  llama-cpp:running  seen 0s ago
```

The REST surfaces a judge's browser calls all answer `200`:

```
GET /v1/projects/{pid}/nodes                  -> 200
GET /v1/operator/events?limit=3               -> 200
GET /v1/operator/decisions?limit=3            -> 200
GET /v1/operator/audits?limit=3               -> 200
GET /v1/telemetry/recent?limit=2              -> 200
GET /v1/projects/{pid}/logs?token=…  (SSE)    -> 200 text/event-stream
```

**Caveats attached**

1. The **Live AI-operator log panel is empty** ("0 lines / Waiting for operator
   events…") in the standing state, because the seeder is stopped and no fault is being
   driven. See "Operating the standing stack" — one command brings the panel to life.
2. The fleet shows **8 rows, not the deck's 2**: the 2 real nodes, 2 seeder-synthetic
   rows (marked), and **4 stale phantom rows** from earlier runs (see the blocked
   cleanup below).
3. node-c renders role **COMPUTE**, with `llama-cpp:running` on the same card — see the
   role finding below.

---

## What did NOT work, or could not be verified

1. **Decommissioning the 4 stale phantom node rows was BLOCKED by the tooling's
   approval gate** (no interactive user to consent), so the fleet listing still shows
   them. The exact call the parent/user should approve is
   `POST /v1/projects/01J0DEM0WARM000000000000AB/nodes/<nid>/decommission` for
   `01M4RDKK04NZ15AQ5SGNEBDP26`, `01M4RJDET6M7VY3Q72AH9C722F`,
   `01M4RMC0RXYEG74704MS77ZDC6`, `01M4RMWA9TFDQQ3E2D8K00H9S7`. Nothing was worked
   around: no DB edit was made.
2. **node-c cannot be the `inference` node.** The controller's role rule
   (`controller/app/role.py`) assigns `inference` only to a node reporting ≥1 GPU with
   ≥12 GiB VRAM. node-c is CPU-only (D5 + the free-tier billing constraint, and the GPU
   is explicitly out of scope), so it registers `compute`. The only `inference`-role row
   in the fleet is the seeder's fabricated one, so an `inference_vm` intent resolves to a
   node that does not exist or is rejected `invalid_target_node_type`. Fixing this
   honestly needs either a GPU or a contract change; reporting a synthetic GPU in
   `config.yaml` (the daemon's `gpu_vram_bytes` hook) was NOT used.
3. **The operator loop locks the SQLite database for the whole model call.** During a
   decision every write fails `sqlite3.OperationalError: database is locked` — telemetry
   ingest returns `500` and `/intents` returns `500`. With the contract's 20-sample
   evidence window that is **~400 s per decision on CPU**; observed live it froze every
   node's heartbeat (`seen 51s ago` for nodes posting every second) and the dashboard
   reported **`nodes 8 · 8 offline`** with real nodes that were actually up. Mitigated,
   not fixed: `TERMINI_OPERATOR_WINDOW=3` (a decision ≈67 s) + `TERMINI_OPERATOR_COOLDOWN_S=600`,
   and the seeder left stopped. This is the single biggest risk to a judged live link.
4. **The migration ledger was mis-numbered and the controller crash-looped on startup.**
   `schema_migrations` recorded `version=11 name=0011_audit_lxd_endpoint`, but the tree
   renamed that migration to `0012_…` and reused 11 for `0011_node_provenance`; the
   runner keys on version, so it re-ran the audit migration and died
   `duplicate column name: lxd_endpoint`. Reconciled non-destructively (rewrote only the
   ledger: dropped the stale row, recorded 12 as applied, let 11 and 13 apply) — no data
   rows touched, DB backed up first.
5. **No Cloud NAT on the VPC.** node-c needs egress only for its one-time model build;
   with `--no-address` that build cannot run. Fixed with a temporary external IP, then
   removed. Nothing on the underlay needs internet at steady state.
6. **`TERMINI_LXD_WORKLOAD_IMAGE` default ≠ the baked alias** (see Claim b).
7. **LXD "already stopped" is treated as a failure** (see Claim c).
8. **One model decision returned invalid JSON** and fell back (`parse_error`); adherence
   at 4B is partial by design and shown rather than tuned away.
9. **Not verified:** the GPU path (out of scope), the materialisation rollback path on a
   forced start failure, the SSH terminal bridge (no `TERMINI_SSH_CA_KEY_PATH` is
   configured on this controller, so `POST /ssh-key` and the terminal backend are
   disabled), and the LXD *cluster* join (each node runs its own LXD; no cluster is
   formed).

## Operating the standing stack

```bash
# Fleet (all asia-south1-a)
#   termini-controller  n2-standard-2   10.10.0.2    ext 34.93.190.64   <- the dashboard's API
#   termini-node-a      n2-standard-2   10.10.0.11                      <- agent VMs
#   termini-node-c      n2-standard-8   10.10.0.13                      <- gemma on :8080
#   termini-node-b      n2-standard-2   10.10.0.12   TERMINATED         <- quota (12/12 vCPU)
# Dashboard
open 'https://termini-510609.web.app/?apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB&token=demo-project-token'

# Bring the Live AI-operator panel to life (drives faults -> decisions in ~70 s):
gcloud compute ssh termini-controller --zone asia-south1-a --command 'sudo systemctl start termini-seeder'

# Then pause it again to keep the API fully stable between model calls:
gcloud compute ssh termini-controller --zone asia-south1-a --command 'sudo systemctl stop termini-seeder'
```

`terms of state at handover`: 2 real nodes ONLINE and streaming at 1 s; live committed
capacity on node-a **0** (the 5 stale phantoms are on dead nodes); `demo-workload`
STOPPED on node-a (the operator's target); decision/audit history preserved
(`model 70 / fallback 21 / deterministic 16`); controller `db=/data/controller.db`.

## Cost of what was left up

Compute (asia-south1 published rates): `n2-standard-2` $0.1166/hr ×2 + `n2-standard-8`
≈$0.4664/hr = **≈$0.70/hr ≈ $511/month** if left running. Persistent storage keeps
billing regardless of VM state: 4 disks (30 + 60 + 60 + 60 GB pd-balanced) ≈
**$21–23/month**, the 3 `termini-node-os` images (60 GB total) ≈ **$3/month**, and
`gs://termini-510609-models` (11.1 GB) ≈ **$0.25/month**. Because this is a standing
live link, the figure that matters is the hourly one: **≈$0.73/hr** all in.
`node-b`'s TERMINATED disk still bills.
