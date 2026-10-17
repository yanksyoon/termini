# A fault injected for the demo is SOLVED on a live node (t_895bf6d3)

**Card:** t_895bf6d3 — *Make an injected fault SOLVABLE: the model's target must
exist (action 404s today).*
**Measured:** 2026-10-17T18:39–18:47Z on the standing fleet.
**Deployed tree:** `f1c947a3d5b3a27c60a5ab770de2b2b6e80981ab` (`/opt/termini/DEPLOYED_COMMIT`,
service started 18:18:06Z that day). Code on `main` at the time of writing:
`f9a6207` (only docs/UI commits above the deployed tree).
**Fleet:** `termini-controller` 34.93.190.64, `termini-node-a`, `termini-node-c`
RUNNING; `termini-node-b` TERMINATED (the standing `CPUS_ALL_REGIONS`=12 budget).
No GCP compute was raised or changed for this run.

This document closes the half of the card that the 2026-10-15 evidence
(`solvable-faults-2026-10-15/README.md` §4) left **open**: acceptance (a) on a
live node. The guardrail / fail-closed behaviour (b) and the hermetic run were
already proven there and are unchanged.

---

## 0. What "solved" had to mean here

The card's requirement is that the model's chosen target **exists on the node the
executor dials**, so that the action succeeds and the audit says `ok`. In this
build the *fault* is the fault console's simulated series (the card allows that:
"a simulation of the fault is acceptable, a failure of the action is not"), while
the *target* and the *action* are real LXD calls against a real node.

Two things made node-a unusable as the measurement site, both recorded rather
than worked around:

* `demo-workload` on node-a is restarted every few seconds by the always-on demo
  seeder (`termini-seeder`, synthetic node `demo-node-a` advertising
  `demo-workload`). Measured: `demo-workload` uptime was **3.92 s** at 18:37:11Z.
  An "uptime resetting" observation there is unattributable — someone else's
  restart looks identical.
* node-c had **no** running LXD instance (`lxc_instances: []`), so it had no
  target to resolve to.

So the run used **node-c** (`01M4ZQ7Q2J80ATY3K32PY3PPJH`, registration
`33678f54a5e3e25cc03620b1bd2486e6`, `lxd_endpoint=https://10.10.0.13:8443`),
whose LXD the controller already trusts, and launched **one real container** there
so the model had exactly one candidate and nothing else touched it:

```
$ sudo lxc launch agent-os/w4 termini-verify-target
$ sudo lxc list --format csv -c n,s
termini-verify-target,RUNNING
$ sudo lxc info termini-verify-target | head -8
Status: RUNNING
PID: 181279
Created: 2026/10/17 18:39 UTC
```

The next node-c sample carried it, with the controller-derived id — the same
`vm_id` the guardrail resolves through:

```
{"node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH", "seq": 29149, "ts": "2026-10-17T18:40:29Z",
 "ram_used_bytes": 6707785728, "ram_total_bytes": 33646948352,
 "lxd_instances": [{"vm_id": "R19QBTV8M7FH1ZGSJ8K4YNGB2Z", "state": "running",
                    "ram_used_bytes": 123715584, "ram_limit_bytes": 33646948352,
                    "disk_used_bytes": 0, "cpu_util_pct": 13.83}]}
```

Controller-side read of the same node over mTLS (the controller's own client
identity, not a shell on the node):

```
{"node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH", "registration_id": "node-c",
 "endpoint": "https://10.10.0.13:8443", "reachable": true,
 "instances": ["termini-verify-target"]}
```

## 1. Acceptance (a) — the model's target exists, the action lands, the instance restarts

Injection via the operator fault console API, on the harness host:

```
POST /v1/projects/01J0DEM0WARM000000000000AB/demo/faults
{"scenario":"oom","node_id":"01M4ZQ7Q2J80ATY3K32PY3PPJH","target":"real","duration_s":60}

HTTP 201
{"schema_version": "1.0", "injection_id": "01M55JV7NHP5K1A42VCN6PS1F3",
 "node_id": "01M4ZQ7Q2J80ATY3K32PY3PPJH", "node_registration_id": "33678f54a5e3e25cc03620b1bd2486e6",
 "scenario": "oom", "rule_id": "oom-guard-01", "target": "real", "state": "active",
 "duration_s": 60.0, "started_at": "2026-10-17T18:44:03.377Z",
 "expires_at": "2026-10-17T18:45:03.377Z", "samples_written": 0,
 "recovery_samples": 0, "clear_samples_needed": 3, "open": true}
```

The decision row (**the model**, not the deterministic playbook):

```
{"id": "01M55JVCS9EBTY6F2JDQC8696R", "rule_id": "oom-guard-01",
 "action_kind": "instance.restart",
 "action_params": "{\"instance_name\": \"termini-verify-target\", \"force\": true,
                   \"timeout_s\": 30, \"_vm_id\": \"R19QBTV8M7FH1ZGSJ8K4YNGB2Z\"}",
 "decided_at": "2026-10-17T18:44:05.740Z",
 "decision_source": "model", "confidence": 0.95, "fallback_reason": null,
 "incident_title": "Host memory exhaustion due to a single large instance"}
```

The model answered with the **`vm_id`** it saw in telemetry
(`R19QBTV8M7FH1ZGSJ8K4YNGB2Z`); G5 resolved it against node-c's *live* inventory
to the instance name `termini-verify-target` — the two values are side by side in
`action_params`, which is the exact link that used to be missing.

The audit row:

```
{"id": "01M55JVETNYJ0ARS5Q29X2S2K0", "decision_id": "01M55JVCS9EBTY6F2JDQC8696R",
 "rule_id": "oom-guard-01", "action_kind": "instance.restart", "result": "ok",
 "detail": "operation Success: /1.0/operations/04b79457-05b9-40b5-8097-c1c863261368;
            action=restart; state Running -> Running",
 "lxd_method": "PUT", "lxd_path": "/1.0/instances/termini-verify-target/state",
 "lxd_body": "{\"action\": \"restart\", \"force\": true, \"timeout\": 30}",
 "lxd_endpoint": "https://10.10.0.13:8443",
 "lxd_operation": "/1.0/operations/04b79457-05b9-40b5-8097-c1c863261368",
 "executed_at": "2026-10-17T18:44:05.740Z", "reason": ""}
```

The node-side state change (the observable that a fabricated target cannot
produce). Same commands, before and after the decision:

```
# before the injection (18:41:04Z)
Status: RUNNING     PID: 181279     /proc/uptime: 49.83 49.83

# after the first injection's restart (18:41:36Z)
Status: RUNNING     PID: 182483     /proc/uptime: 17.39 17.39

# after the model's restart (18:44:34Z)
Status: RUNNING     PID: 183606     /proc/uptime: 25.30 25.30
Last Used: 2026/10/17 18:44 UTC
```

`PID` changes and `/proc/uptime` resets at the audit's timestamp: the container
was really restarted, by the action the model chose, on the node the executor
dialled. The client-facing AI-operator line for the same event:

```
{"ts": "2026-10-17T18:44:10.710Z", "severity": "info", "actor": "ai_operator",
 "message": "oom-guard-01: instance.restart termini-verify-target -> ok
             (threshold: >= 95% of ram_total_bytes for 2 consecutive samples)"}
```

### The run before it (recorded, not used as the model evidence)

The first injection (`01M55JP0T1RTWWRGR5FZ71HTAZ`, 18:41:12Z) produced
`decision_source=fallback, fallback_reason=schema_violation` — Gemini returned a
JSON object without `action_id`, so the deterministic playbook made the same
call. Its audit was also `result=ok` and its restart also really happened
(PID 181279 → 182483), but a fallback decision is not "the model's chosen
target", so it is **not** the evidence for (a). It is listed here because leaving
it out would misrepresent the run, and because it shows the same real action
landing twice. The model needs a retry in this deployment; see §4.

## 2. Acceptance (c) — the fault is visible before the decision and recovers after it

Node-c samples (stored series), 1 s cadence from the injector:

```
18:41:02Z  18.21 %   <- node's own series
18:41:09Z  18.21 %   <- last pre-injection sample
18:41:13Z  97.00 %   <- injected fault begins (threshold >= 95 %)
...
18:44:05.740Z  decision (model) -> audit ok -> LXD restart
18:44:48Z  97.00 %   <- operator clear requested at 18:44:48.546Z
18:44:49Z  62.00 %   <- recovery samples (3 needed to re-arm)
18:44:50Z  62.00 %
18:44:51Z  62.00 %   <- injection row closed: state=cleared, recovery_samples=3
18:47:10Z  18.19 %   <- node-c's OWN series resumes (see §4, finding 1)
18:50:00Z  18.16 %   <- after the verification container was deleted: lxd_instances: []
```

The last line is the node's real inventory speaking again — the same field the
guardrail resolves targets through, reporting what the node actually has.

Rule state and log lines:

```
trigger_state: {"rule_id": "oom-guard-01", "state": "armed", "fired_count": 2,
                "last_fired_at": "2026-10-17T18:44:05.722Z",
                "last_cleared_at": "2026-10-17T18:44:52.473Z"}

operator_logs: 18:44:03.377Z human   "fault injected by operator: node 33678f54a5e3e25cc03620b1bd2486e6
                                      scenario=oom rule=oom-guard-01 target=real ttl=60s (injection
                                      01M55JV7NHP5K1A42VCN6PS1F3; node telemetry superseded while active)"
               18:44:48.546Z human   "fault cleared by operator: ... restoring the healthy series,
                                      3 sample(s) to re-arm (injection 01M55JV7NHP5K1A42VCN6PS1F3)"
               18:44:51.197Z human   "fault cleared: ... rule oom-guard-01 back below threshold after
                                      3 healthy sample(s)"
```

## 3. Acceptance (b) — the synthesis path cannot produce a raw 404

Unchanged from the 2026-10-15 evidence, and still true in the same live database
(rows kept, `reason`/`detail` on the audit rather than an LXD 404 body):

```
{"id": "01M50659AFYCBSWTSN0RPA4WMB", "node_id": "01M4NZK474BVARFBXYQBMVA8GV",
 "rule_id": "oom-guard-01", "action_kind": "instance.restart", "result": "error",
 "detail": "target_missing: refused PUT /1.0/instances/01M4N827979BD9D2762EFE24A0/state
            — no LXD instance '01M4N827979BD9D2762EFE24A0' on https://10.10.0.11:8443
            (probe GET /1.0/instances/01M4N827979BD9D2762EFE24A0 -> not found). ...",
 "lxd_method": "GET", "lxd_path": "/1.0/instances/01M4N827979BD9D2762EFE24A0",
 "lxd_endpoint": "https://10.10.0.11:8443", "executed_at": "2026-10-15T16:26:05.746Z"}
```

Five such rows exist (2026-10-15T16:25:53Z … 16:26:05Z, plus 2026-10-16T20:27:29Z).
The mechanism is `LxdExecutor.instance_exists` → `_refuse_missing_target`
(`controller/app/operator/executor.py`): a 404 on the existence probe refuses the
write and names the **target**; `reason=target_missing` is what the audit and the
AI-operator log line carry. The client renders that log line verbatim —
`dashboard/js/ui/logs.js:221` emits `line.message` into the operator panel — so
what a judge reads is the reason, not an LXD error body. The same panel line for
this run's successful action is quoted in §1 ("`instance.restart
termini-verify-target -> ok`"), i.e. the surface is live and carrying the target.

A *new* live refusal was deliberately not manufactured for this run: the
synthesis path now advertises a target that exists (that IS the fix), so
producing one would mean forging telemetry with an invented `vm_id`. The live
refusals on record are the rows above, taken on the same controller; the negative
control in `infra/gcp/evidence/tools/solvable-faults-hermetic.py` (Case B) pins
the behaviour: the same fault with the invented id produces `target_missing`,
**zero** state-changing requests, and no `HTTP 404` anywhere.

**One caveat, recorded rather than glossed.** The database also holds exactly one
raw `HTTP 404` audit row *after* the fix landed:
`2026-10-17T16:32:14.562Z`, node `01M4NZK474BVARFBXYQBMVA8GV` (the synthetic
`demo-node-a`), target `NNB9287WRHGFDPEZ995TKCXDNW`, plain
`PUT /1.0/instances/NNB9287…/state` → `Instance not found`. That is the old
signature, and it is **not** explained by the currently deployed tree (which
contains the fail-closed probe, and whose 18:41/18:44 runs behave correctly); the
controller process that served 16:32Z started before the current one (18:18:06Z).
It was flagged as a follow-up (see §4, finding 2) rather than quietly excluded,
because "the raw 404 cannot recur" is this card's whole claim. **Resolved by card
t_2f4b5ffe** (`post-fix-raw-404-2026-10-17.md`): the row was served by the
post-fix tree `8d7ac5a` itself, through the *unverified-probe* path — node-a's LXD
could not answer the existence probe at 16:32Z (`instance inventory unreadable`,
operator 27.56 s behind), the write was issued by design, and LXD answered 404.
Future rows of that shape now carry `reason=target_unverified` instead of the raw
LXD body.

## 4. Findings this run produced (follow-up cards, not fixed here)

1. **A cleared injection suppresses the node's own series until its `seq` catches
   up.** `store.insert_samples` drops any sample with `seq <= max(seq)`; the fault
   console writes its samples at 1 Hz starting above the node's last `seq`
   (`DemoFaultInjector._next_seq`), so after a 60 s injection the node's own
   agent — which reports at ~0.3 Hz with its own counter — is dropped until it
   passes the injected range. Measured on node-c: last recovery sample
   `18:44:51.195Z` (seq 29271, 62 %), node's own series resumes
   `18:47:10Z` (seq 29272, 18.19 %) → **2 m 19 s of the node's real telemetry
   missing**, and the dashboard showing the injector's fabricated 62 % baseline
   in the meantime. Camera-relevant: the "recovery" a judge sees is not the node.
2. **Confirm the post-fix 404 row** (2026-10-17T16:32:14.562Z) can no longer be
   produced: identify the revision/process that served it and whether
   `instance_exists` answered `None` (transport error → proceed by design) while
   the target was an unresolved `vm_id`. The current tree was not it.
   **Resolved by t_2f4b5ffe** (`post-fix-raw-404-2026-10-17.md`): the process was
   the 13:14:36Z one running `8d7ac5a` (executor sha256 `06e89cb3…`, guard in its
   bytecode); the probe answered `None` because node-a's LXD was unreadable at
   16:32Z. The audit now says `target_unverified`.

## 5. Reproduce

```bash
# 0. a real target on a node that nothing else touches (node-c had no instance)
gcloud compute ssh termini-node-c --zone=asia-south1-a --tunnel-through-iap \
  --command='sudo lxc launch agent-os/w4 termini-verify-target'
#    wait for the node's next sample to list it with a derived vm_id

# 1. inject (from the controller host; the console API is loopback + project token)
python3 infra/gcp/evidence/tools/solvable-faults-live.py inject \
  --node 01M4ZQ7Q2J80ATY3K32PY3PPJH --duration 60

# 2. the rows (decision / audit / samples / logs / trigger state)
python3 infra/gcp/evidence/tools/solvable-faults-live.py status --since <iso>

# 3. the node-side reset
gcloud compute ssh termini-node-c --zone=asia-south1-a --tunnel-through-iap \
  --command='sudo lxc info termini-verify-target | grep -E "Status|PID"; \
             sudo lxc exec termini-verify-target -- cat /proc/uptime'

# 4. clear and watch the recovery
python3 infra/gcp/evidence/tools/solvable-faults-live.py clear --injection <id>

# 5. clean up the verification target
gcloud compute ssh termini-node-c --zone=asia-south1-a --tunnel-through-iap \
  --command='sudo lxc delete --force termini-verify-target'
```

The verification container `termini-verify-target` was deleted at 18:49:52Z (node-c's
next sample, 18:50:00Z, reports `lxd_instances: []` again); the durable record is
the `demo_faults` / `remediation_decisions` / `remediation_audits` /
`operator_logs` rows quoted above.
