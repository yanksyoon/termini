# F01 runbook — OOM with a leak signature, live

Owner: controller. Card: t_979fa31b. What this documents: how to run the
graded F01 demo ("oom with a leak signature") against the **real** standing
fleet — inject on a real node, watch the leak ramp, the rule fire, the model
decide `instance.restart` on the real instance, and RAM fall back under
threshold — and how to reproduce the acceptance.

The scenario itself is defined in `controller/app/demo_faults.py`
(`SCENARIOS["oom"]`): host RAM ramps from 62 % to 97 % over ~16 s while the
leaking instance's `ram_used_bytes` climbs to ~99 % of its `ram_limit_bytes`
and the leak signature advances (node `oom_kill_count` / `pgmajfault_count` /
`swap_used_bytes` / `mem_available_bytes` / `pressure.memory_*`, per-instance
`oom_kill_count` / `pgmajfault_count`). See
`docs/contracts/controller-api.md` §4.7 for the wire contract.

## Preconditions (do not fake these)

1. The controller is the deployed tree carrying this scenario. Check:

       ssh ubuntu@34.93.190.64 'cat /opt/termini/DEPLOYED_COMMIT'

   It must match the commit you built. Deploy with
   `infra/gcp/tools/deploy-termini-tree.sh --ref <sha>` when it does not.

2. Node A's LXD REST API answers, from the controller, with the controller
   identity trusted:

       ssh ubuntu@34.93.190.64 \
         'curl -sk https://termini-node-a.asia-south1-a.c.termini-510609.internal:8443/1.0 -o /dev/null -w "%{http_code}\n"'

   `200` = reachable. `000`/timeout or a `403` on
   `GET /1.0/instances` = **stop**: the run ends in `executed: error` and no
   decision can execute. Raise node-a or re-install the trust
   (`infra/gcp/tools/install-lxd-trust.sh --node a`) first. The card's
   precondition is explicit: no passing demo on a decision that never executed.

3. The real node is reporting telemetry. On the controller:

       sqlite3 /data/controller.db \
         "SELECT registration_id, synthetic, lxd_endpoint, last_seen_at FROM nodes WHERE synthetic=0 ORDER BY last_seen_at DESC LIMIT 3"

   `lxd_endpoint` must be `10.10.0.11:8443` and `last_seen_at` within the last
   minute. The seeder (`termini-seeder`) registers the *synthetic* nodes
   (`demo-node-a`/`demo-node-c`); the **real** node-a is the `synthetic=0` row.

## The run (UI — /demo/)

1. Open `https://34-93-190-64.sslip.io/demo/`.
2. Paste the project token (`demo-project-token`) and **Connect**.
3. The node picker defaults to the first real node. Confirm the hint reads
   "Real node …" and the card's rule is `oom-guard-01`.
4. Press **Inject fault** on the **Memory exhaustion (OOM)** card (duration
   60 s is fine; the ramp only needs ~20 s).

Then, in order, watch:

1. **Ramp (~16 s).** The node's RAM chart climbs from ~62 % toward ~97 % —
   a visible slope, not a step. The leak signature (OOM kills, major faults,
   swap, free memory) advances with it.
2. **Rule fires (~3-5 s after the ramp crosses 95 %).** Two consecutive
   samples at or above 95 % trip `oom-guard-01` (1 Hz cadence + a 2 s poll).
3. **Incident card.** Open the AI-operator panel's incident card and expand
   the **thinking log**: `threshold_fired` → `evidence_window` → the model call
   (`answered_by`/`model`) → the gates → `action_decided` (`instance.restart`
   on the real instance) → `executed` (with the LXD operation id) →
   `audit_recorded`. The incident `evidence[]` cites the leak fields
   (`oom_kill_count`, `lxd_instances.ram_used_bytes` vs `ram_limit_bytes`,
   `pgmajfault_count`, `swap_used_bytes`), not just "RAM high".
4. **Recovery.** The operator restarts the one leaking instance. The injected
   series is cleared (or the TTL expires) and the driver writes the healthy
   baseline; RAM falls back under 90 % and `oom-guard-01` re-arms after its
   clear samples.

## The run (API — same thing, scriptable)

```bash
export TOKEN=demo-project-token
PID=01J0DEM0WARM000000000000AB
BASE=https://34-93-190-64.sslip.io
# 1. pick the real node
NODE=$(curl -s -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/projects/$PID/nodes?include=synthetic" \
  | python3 -c 'import sys,json; ns=json.load(sys.stdin)["nodes"]; print(next(n["node_id"] for n in ns if not n["synthetic"]))')
# 2. inject
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$BASE/v1/projects/$PID/demo/faults" \
  -d "{\"node_id\":\"$NODE\",\"scenario\":\"oom\",\"duration_s\":60,\"target\":\"real\"}"
# 3. read the card (decision + audit)
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/v1/operator/decisions?limit=5"
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/v1/operator/audits?node=$NODE&limit=5"
# 4. watch recovery (clear, then the rule re-arms)
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/projects/$PID/demo/faults/<injection_id>"
```

The automated, 5-run form is
`python3 controller/scripts/f01_oom_acceptance.py` (see its `--help`).

## Acceptance

- 5 consecutive runs, each: injection → detection latency → decision
  (`decision_source=model`, `action=instance.restart`, target = a real instance
  on the node) → audit row carrying an LXD operation id and the state
  transition → telemetry back under threshold → rule re-armed.
- The 5-row table (timestamps, latencies, target, operation id) is in the PR.
- Negative control: inject the instance's memory as reclaimable cache (no
  `oom_kill`) — the operator must take no destructive action. See
  `controller/scripts/f01_oom_acceptance.py --negative-control`.
