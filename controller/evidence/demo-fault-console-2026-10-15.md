# Operator fault console — evidence (2026-10-15)

Card: `t_1012fc99` (Fault-injection console: inject and clear demo faults from a UI,
operator-only). Contract amendment: `docs/contracts/controller-api.md` §4.7.

**Verdict: built and verified end to end on a local controller running this
checkout's build** — inject from the browser, the rule fires, the operator decides,
the log line lands on the client dashboard, clear restores the series and the rule
returns below threshold, both guards refuse, the endpoint is authenticated, and a
TTL injection ends by itself. What is **not** verified here is listed under
*Not verified* — read that section before quoting this file.

## What shipped

| Path | What |
|---|---|
| `controller/app/demo_faults.py` | the fault driver: scenarios, sample derivation, guards, TTL, restart sweep, the injection views |
| `controller/app/migrations/0014_demo_faults.sql` | durable `demo_faults` row per injection (state, TTL, baseline, counters) |
| `controller/app/server.py` | `POST/GET /v1/projects/{pid}/demo/faults`, `POST …/demo/faults/clear`, `DELETE …/demo/faults/{id}`; node telemetry superseded while an injection is open; `DELETE` added to CORS allow-methods |
| `controller/app/main.py` | starts the driver; sweeps injections a previous process left open *before* serving |
| `console/fault_console/` (+ `console/serve.py` `/console/`) | the operator UI: node/scenario/duration/target, active injections, Clear, raw responses |
| `controller/scripts/demo_fault_console_probe.py` | the re-runnable acceptance probe (below) |
| `tests/test_demo_faults.py` | 23 contract-shaped tests (wire + pure functions) |
| `docs/contracts/controller-api.md` §4.7 | the frozen contract's new section |

## Acceptance probe (raw)

`python3 controller/scripts/demo_fault_console_probe.py` — boots a throwaway
controller on an ephemeral port, registers a real-provenance and a `synthetic` node,
and drives the endpoints the way the console does.

```
== d. the endpoint is authenticated (and not reachable from a node) ==
  [PASS] no token -> 401 — HTTP 401
  [PASS] node token -> 403 (node context cannot inject) — HTTP 403
  [PASS] GET without a token -> 401 — HTTP 401

== a. inject OOM on the chosen node ==
  [PASS] POST /demo/faults -> 201 — HTTP 201
  injection_id=01M504N1FNXV6EJ1WAVPMTE8RZ rule=oom-guard-01 ttl=35.0s
    expires_at=2026-10-15T15:58:38.273Z target=real
  [PASS] a second injection for the same (node, rule) is refused 409 fault_already_active
         — HTTP 409 details=['fault_already_active', '01M504N1FNXV6EJ1WAVPMTE8RZ']
  [PASS] node's own sample superseded while the fault is open
         — HTTP 202 {'accepted': 0, 'rejected': 1, 'backpressure': 'none'}
  injected series: 4 samples, ram% peak 97.0 (last 97.0)
  [PASS] injected series drove RAM past the OOM threshold — peak ram%=97.0
  [PASS] the OOM rule fired — oom-guard-01 value=96.99999999720603
  [PASS] the operator log carries 'fault injected by operator' — fault injected by
         operator: node probe-node-a scenario=oom rule=oom-guard-01 target=real ttl=35s
         (injection 01M504N1FNXV6EJ1WAVPMTE8RZ; node telemetry superseded while active)
  [PASS] the decision step ran and recorded an action — instance.restart
         source=deterministic fallback=None after 3.1s
  [PASS] a remediation audit was written — audit row(s) present

== c. guards ==
  [PASS] a different rule on the same node is still injectable — HTTP 201 (rule=disk-fail-01)

== b. clear restores the series and the rule returns below threshold ==
  [PASS] DELETE /demo/faults/{id} -> 200 — HTTP 200
  [PASS] clear starts the recovery burst — state=recovering needs 2 healthy samples
  [PASS] the injection is no longer open
  [PASS] the restored series is below the OOM clear threshold (90%) — last 3 ram%: 62.0, 62.0, 62.0
  [PASS] the rule re-armed instead of re-firing — 2 health event(s) total
  [PASS] clearing again is idempotent — HTTP 200 status=already_cleared
  [PASS] re-injecting the rule the operator just remediated is refused 409
         remediation_in_flight — HTTP 409 details=['remediation_in_flight', 'recent_remediation']
         message=refusing to inject oom on node 01M504N0S29MC5J4N18NY3E4W5: a remediation
         for oom-guard-01 is in flight (recent_remediation)

== e. a live injection ends by itself at its TTL (never silently) ==
  [PASS] inject on the synthetic node -> 201 — HTTP 201
  [PASS] the TTL injection ended without an operator — state=cleared reason=ttl_expired
  [PASS] the TTL expiry is recorded in the operator log — fault TTL expired after 8s —
         auto-clearing: node probe-node-a scenario=disk-fail (injection 01M504N4KHQWEYY9SRDA5HYRPZ)

== f. a controller restart sweeps an injection it can no longer drive ==
  [PASS] injection running before the restart — HTTP 201
  [PASS] the orphaned injection is reported expired, not active — state=expired
         cleared_by=controller_restart
  [PASS] the sweep is recorded in the operator log — fault injection
         01M504NJHB5PWSX79EVASAY664 expired: controller restarted while disk-degrade was
         active — no samples are being written for it

ALL BEATS PASSED
```

Operator log the client dashboard renders (excerpt from the same run):

```
15:56:48 [info] (human) fault injected by operator: node probe-node-a scenario=oom
  rule=oom-guard-01 target=real ttl=35s (injection …; node telemetry superseded while active)
15:56:48 [warn] (human) node telemetry superseded by active fault injection …
15:56:50 [info] (ai_operator) oom-guard-01: instance.restart … -> dry_run
  (threshold: >= 95% of ram_total_bytes for 2 consecutive samples)
15:56:51 [info] (human) fault cleared by operator: node probe-node-a scenario=oom
  rule=oom-guard-01 — restoring the healthy series, 2 sample(s) to re-arm (injection …)
15:56:52 [info] (human) fault cleared: node probe-node-a rule oom-guard-01 back below
  threshold after 2 healthy sample(s) (injection …)
```

Decision rows written by the AI operator for those episodes
(`GET /v1/operator/decisions`):

```
15:56:52 disk-fail-01   -> instance.stop    source=deterministic
15:56:50 oom-guard-01   -> instance.restart source=deterministic
```

## Browser acceptance (screenshots)

A local stack (`console/run-demo.sh` equivalent: controller + one real-provenance
node reporter + the composed console, **no seeder running**) driven through the
browser. The screenshots are attached to the card (and in
`controller/evidence/shots/` of the review comment thread — they are binary, so they
are not committed):

1. `01-console-token-required.png` — `/console/` before the operator pastes the
   project token: nothing works, no token is served by the page.
2. `02-console-connected.png` — connected on the project token; node picker filled
   from the fleet listing (`demo-node-a-machine · compute · real · online`), scenario
   `oom` selected, target auto-set to `real`.
3. `03-console-injected.png` — OOM injected from the console: `state: active`,
   TTL/expiry, sample counter.
4. `04-console-guard-409.png` — pressing Inject again: `refused 409 CONFLICT — fault
   already active on node …` with the raw envelope (`fault_already_active`).
5. `05-dashboard-injected.png` — the **client dashboard** while a fault is live: the
   node card's RAM/CPU up from the injected series and the operator log line
   `[human] fault injected by operator: node demo-node-a-machine scenario=latency
   rule=latency-01 …`.
6. `06-dashboard-cleared.png` — after clear: `[human] fault cleared by operator: …
   restoring the healthy series, 3 sample(s) to re-arm` then `[human] fault cleared:
   node demo-node-a-machine rule latency-01 back below threshold after 3 healthy
   sample(s)`, next to the Memory chart (RAM pulse back to the healthy band).
7. `07-console-cleared.png` — the console's injection table after Clear: the row is
   `cleared` with `cleared_by_operator`, `3/3` recovery samples.

The Memory chart on the dashboard shows the whole episode: RAM steps up to 97 % on
the injected series and steps back to 62 % after the clear (`06`/`05`).

## Tests

`python3 -m unittest tests.test_demo_faults` → **23 tests, OK**. They pin the wire
contract (401/403/404/409/422), the inject→fire→clear→re-arm cycle, the supersede
rule, the TTL path, the restart sweep, and that `console/serve.py` serves the console
**without** the project token and that the dashboard bundle never links `/console/`.

Full suite: `python3 -m unittest discover -s tests` — see the PR for the CI run.

## Safety (card §3)

| Rule | Where it lives | Proven by |
|---|---|---|
| refuse while a remediation for the same (node, rule) is in flight | `demo_faults._remediation_in_flight` (unaudited fired event, or an audit inside `TERMINI_DEMO_FAULT_GUARD_S`) → `409 remediation_in_flight` | probe beat c, `test_reinjection_while_a_remediation_is_in_flight_is_refused` |
| a fault is always clearable | `clear()` never refuses on state; idempotent `already_cleared` | probe beat b, `test_inject_clear_cycle_…` |
| a clear restores the series so the rule returns below threshold | recovery burst of `clear_samples_needed` healthy samples, then close | probe beat b + `test_recovery_burst_provably_rearms_the_rule` (pure), dashboard screenshot 06 |
| never outlive the TTL silently | TTL → logged auto-clear → recovery burst; restart → swept to `expired` at boot | probe beats e and f, `test_injection_ends_loudly_at_its_ttl`, `RestartSweepTest` |
| one open injection per (node, rule) | `open_fault_for_rule` → `409 fault_already_active` | probe beat c, `test_second_injection_for_the_same_rule_is_refused` |

## Guardrail: operator-only, never public, never in the judge-facing nav

- The four routes require the **project token**; a `node_token` is `403`, no token is
  `401` (`test_node_token_cannot_inject`, probe beat d).
- `console/serve.py` injects only `apiBase`/`projectId` into the console page — the
  token is pasted by the operator and kept in that tab's `sessionStorage`
  (`test_index_gets_the_url_and_project_but_never_the_token`).
- The dashboard bundle does not reference `/console/`
  (`test_dashboard_nav_does_not_link_the_console`).
- `TERMINI_DEMO_FAULTS=off` disables the surface entirely (the endpoints then answer
  `503` instead of pretending to inject).

## Not verified here (honest gaps)

1. **The deployed demo.** All of the above ran against a controller built from this
   branch on the build host; the standing demo's controller VM was not redeployed in
   this card, so the judge-facing VM does not yet carry these routes. Deploying and
   re-taking the acceptance on the deployed fleet is the follow-up (the same
   discipline as `t_359ed606`).
2. **A model-produced decision.** With `TERMINI_OPERATOR_DECISION_MODE=model` pointed
   at node C's llama.cpp the decision step makes a real call and does **not** answer
   inside the decision timeout — consistent with
   `infra/cloudrun/evidence/node-c-decision-latency-2026-10-12.md` ("a decision is
   minutes, not seconds"), so the decision falls back deterministically
   (`fallback_reason: inference_timeout`/`inference_unavailable`) and the on-screen
   action is the playbook's. On this host the model service is not reachable without
   editing `/etc/hosts` (the session's command guard refused both the raw-IP URL and
   the hosts edit), so the model leg is reported from the earlier run in this card
   rather than re-taken. **The demo's interactive decision is the deterministic
   fast-path**, by design (controller-api.md §8).
3. **The real LXD endpoint.** The local stack's operator ran against the chaos LXD
   stub, so audits read `-> dry_run`/`-> error` depending on the stub's answer; the
   audit/target-resolution path itself is unchanged by this card.

## Reproduce

```bash
# wire + pure tests
python3 -m unittest tests.test_demo_faults -v

# end-to-end probe (self-contained: boots its own controller)
python3 controller/scripts/demo_fault_console_probe.py
python3 controller/scripts/demo_fault_console_probe.py --decision-mode model \
    --inference-base-url http://<node-c>:8080 --decision-timeout-s 90 --observe-s 120

# the browser stack the screenshots came from
CONTROLLER_PORT=18285 CONSOLE_PORT=18290 console/serve.py \
    --controller http://127.0.0.1:18285 --project 01J0DEM0WARM000000000000AB \
    --token demo-project-token
# then http://127.0.0.1:18290/console/
```
