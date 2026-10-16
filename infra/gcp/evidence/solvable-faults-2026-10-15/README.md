# A fault injected for the demo must be SOLVABLE (t_895bf6d3)

**Card:** t_895bf6d3 — *Make an injected fault SOLVABLE: the model's target must
exist (action 404s today).*
**Branch:** `wt/t_895bf6d3-solvable-faults`
**Commits referenced below:** controller `8432f0d` (LIVE on `termini-controller`
when the measurements in §2 were taken), node agent `a328c19`.

---

## 1. What was wrong

Measured on the live demo, 2026-10-15T15:29:48Z, against `termini-controller`
(`TERMINI_LXD_BASE_URL=https://10.10.0.11:8443` = termini-node-a's LXD):

```
decisions row:  rule=oom-guard-01  action=instance.restart  decision_source=model
                confidence=0.9  fallback_reason=None
audit row:      result=error
                detail="HTTP 404: Failed to fetch instance \"01M4N827979BD9D2762EFE24A0\"
                        in project \"default\": Instance not found"
                lxd_method=PUT  lxd_path=/1.0/instances/01M4N827979BD9D2762EFE24A0/state
                lxd_endpoint=https://10.10.0.11:8443
```

The model diagnosed an injected OOM correctly; the action failed. Root cause, in
two halves:

1. **The seeder advertised a target that does not exist.** `console/seed_demo.py`
   registered SYNTHETIC nodes whose `lxd_instances[].vm_id` was an INVENDED ULID
   (`instance_vm_id()` → `01M4N827979BD9D2762EFE24A0`). The guardrail (G5)
   resolves a remediation target against the evidence window, so the model's
   `vm_id` passed through unchanged and the executor PUT it at a REAL node's LXD,
   where no instance of that name exists → `HTTP 404 Instance not found`.
2. **Nothing turned a reported `vm_id` into a real LXD instance name.** The node
   agent never reported `lxd_instances[]` at all, and the guardrail had no
   resolution source other than a hand-maintained `TERMINI_LXD_INSTANCE_NAMES`
   map that nothing filled on this deployment.

## 2. Live evidence taken before the fleet was stopped

### 2.1 The guard now refuses a fabricated target — and never issues the 404 write

Live controller, deployed commit `8432f0d`, raw `remediation_audits` rows
(`target_missing` replaces the bare `HTTP 404`) — one row verbatim:

```
2026-10-15T16:05:24.350Z | 01M4NZK474BVARFBXYQBMVA8GV | oom-guard-01 | instance.restart | error
  GET | /1.0/instances/01M4N827979BD9D2762EFE24A0 | https://10.10.0.11:8443
  target_missing: refused PUT /1.0/instances/01M4N827979BD9D2762EFE24A0/state
                  — no LXD instance '01M4N827979BD9D2762EFE24A0' on https://10.10.0.11:8443
                  (probe GET /1.0/instances/01M4N827979BD9D2762EFE24A0 -> not found).
                  The decision's target does not exist on the node this executor dials,
                  so no LXD state change was issued; ...
```

The CLIENT-facing line (`operator_logs.message`, what the AI-operator panel
renders) carries the same reason instead of a raw 404:

```
2026-10-15T16:05:26.584Z | 01M4NZK474BVARFBXYQBMVA8GV | warn
oom-guard-01: instance.restart 01M4N827979BD9D2762EFE24A0 -> error
  (target_missing: refused PUT /1.0/instances/01M4N827979BD9D2762EFE24A0/state
   — no LXD instance '01M4N827979BD9D2762EFE24A0' on https://10.10.0.11:8443
   (probe GET /1.0/instances/01M4N827979BD9D2762EFE24A0 -> not found))
  (threshold: >= 95% of ram_total_bytes for 2 consecutive samples)
```

The decision rows beside them are unchanged model decisions
(`decision_source=model`, `confidence=0.9`, `fallback_reason=None`): the model is
still consulted and still right — only the *execution* of an impossible target
changed shape, from "attempted and 404'd" to "refused, with the reason".

Whole-history result counts on that database at 16:05Z: `error 152`, `ok 75`,
`already_applied 2` (the `error` count includes the historic 404 rows; every row
with the new detail text starts with `target_missing`).

### 2.2 The node-side defect found while taking this evidence

`termini-node-a` reported NO `lxd_instances[]` at all, one journal line per
second:

```
Oct 15 16:14:05 termini-node-a ... termini-bootstrap[9831]:
  2026-10-15T16:14:05Z [termini-bootstrap] lxd inventory unavailable:
  lxc list exit 1: cannot change profile for the next exec call: Operation not permitted
```

Cause: the agent's inventory probe ran the `lxc` CLI, `/usr/bin/lxc` is the snap
wrapper, and the unit ships `NoNewPrivileges=true` (correctly). So the only
transport the agent had was dead on every deployed node — silently, because an
absent `lxd_instances[]` has nothing to resolve a target against. Fixed in
`1e5258d` by reading `GET /1.0/instances?recursion=2` over LXD's own unix socket
(no privilege needed), CLI kept as a fallback. Measured on node-a:

```
### /1.0/instances?recursion=2 -> 200 count: 3
    demo-workload Running state_keys=['cpu','disk','memory','network','pid','processes']
        mem=339972096/8319737856 cpu=26630126000
    termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 Running ... mem=350142464/1073741824
    termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 Running ... mem=347893760/1073741824
### /1.0/instances?recursion=1 -> 200 count: 3     (no state at this depth)
```

After installing the agent and restarting the unit, node-a's next sample carried
its REAL inventory (live row, 2026-10-15T16:21:32Z):

```
node 01M4TGD31WXH97JAA5S6A4R10D (ba1f1faeacb4501aed12cf8ea1e5713b, synthetic=0)
  pct=12.14  instances=[
    {"vm_id": "NNB9287WRHGFDPEZ995TKCXDNW", "state": "running",
     "ram_used_bytes": 0, "ram_limit_bytes": 0, "disk_used_bytes": 0},
    {"vm_id": "7NM4JV8BSD1P8KYBYP8B7BT7CY", ...}, {"vm_id": "...", ...}]
```

Those derived `vm_id`s are exactly what the controller re-derives from the names
it reads back from node-a (`vmids.vm_id_for_instance`), which is what makes a
model-chosen id resolvable to a real instance. The three zeroed numbers in that
row are the depth-1 artefact the `recursion=2` fix removes: the build installed
on node-a at 16:19 was the depth-1 one
(`sha256 1935b24297ee2394cf76afe080ce6d9ec82720fd3475e7b1b063191353f098d9`), and
the depth-2 build on this branch
(`sha256 59c47a6b8e25323d6a99d2f80f6e5b36700d7709b8b72114a93d536df20a9e3d`) had
not been re-installed when the fleet was stopped — the install command is in §4.

### 2.3 The seeder now advertises a real target

`termini-seeder.service` on the live controller after deploying the branch:

```
ExecStart=/usr/bin/python3 /opt/termini/console/seed_demo.py \
  --controller http://127.0.0.1:8080 \
  --token demo-project-token \
  --bootstrap-mint 2 \
  --lxd-instance demo-workload \
  --duration 0
```

`demo-workload` is a real container on termini-node-a (the instance the
controller's own `TERMINI_LXD_DEFAULT_INSTANCE_NAME` names), started for this
run. One more trap was found and fixed here: `setup-seeder-vm.sh` used
`systemctl enable --now`, which is a no-op on an already-active unit, so writing
the new `--lxd-instance` left the OLD argv running while `is-active` said
`active` — a green-looking deploy that had not happened. The script now always
restarts and prints the live argv.

## 3. Hermetic acceptance (raw output in this directory)

`hermetic-acceptance.txt` is the unedited stdout of

```
python3 infra/gcp/evidence/tools/solvable-faults-hermetic.py
```

It runs the REAL operator loop, REAL guardrail and REAL executor with the model's
answer REPLAYED from the live decision row above (action=`instance.restart`,
confidence=0.9) against a strict LXD double (`tests/stub_lxd.py`, which answers
404 for an instance it does not have — a stub that always answers 200 would hide
exactly this defect), with NO `TERMINI_LXD_INSTANCE_NAMES` configured:

* **Case A (the fix):** fault visible in telemetry (99.0% of `ram_total_bytes`,
  3 consecutive samples) → decision `source=model confidence=0.9` →
  `instance_name` resolved from the node-reported `vm_id`
  `NNB9287WRHGFDPEZ995TKCXDNW` to the real instance `demo-workload` → audit
  `result=ok`, `PUT /1.0/instances/demo-workload/state {'action': 'restart',
  'force': True, 'timeout': 30}` preceded by the existence probe
  `GET /1.0/instances/demo-workload` → LXD records 1 restart → the fault clears
  and `trigger_state oom-guard-01 -> armed`.
* **Case B (negative control):** the same fault, the invented id
  `01M4N827979BD9D2762EFE24A0` → audit `result=error`, detail starts with
  `target_missing`, **zero** state-changing requests issued, and the operator log
  line names the target instead of a raw `HTTP 404`.

## 4. What is NOT yet proven live, and why

The card's acceptance (a) also asks for the **node-side** observable on a live
node: the instance's real uptime resetting after the model's restart, and the
real node's telemetry recovering from the injected fault. The fleet was stopped
mid-run by the operator's cost guard (2026-10-15, all four VMs TERMINATED) before
that run could be taken, and this worker is not permitted to raise it.

The remaining live steps, with the exact commands (see the card's comment thread;
they need an approved window):

```bash
# 1. raise the fleet (approved window), then deploy this branch
infra/gcp/tools/deploy-termini-tree.sh --ref <merge_sha>
gcloud compute scp --tunnel-through-iap --zone=asia-south1-a \
  infra/bootstrap/node/termini-bootstrap.py termini-node-a:/tmp/termini-bootstrap.py
gcloud compute ssh termini-node-a --tunnel-through-iap --zone=asia-south1-a \
  --command='sudo install -o root -g root -m0755 /tmp/termini-bootstrap.py \
             /usr/local/bin/termini-bootstrap && sudo systemctl restart termini-bootstrap'

# 2. a REAL fault inside a REAL instance, on the real node
gcloud compute ssh termini-node-a --tunnel-through-iap --zone=asia-south1-a --command='
  sudo lxc config set demo-workload limits.memory 7GB
  sudo lxc file push /tmp/hog.py demo-workload/tmp/hog.py
  sudo lxc exec demo-workload -- bash -c "nohup python3 /tmp/hog.py --mb 6900 --hold 300 &"'

# 3. the acceptance rows
#    node-a's ram crosses 95% -> oom-guard-01 -> model decision ->
#    audit result=ok with lxd_path=/1.0/instances/demo-workload/state,
#    and `sudo lxc exec demo-workload -- cat /proc/uptime` before/after the
#    decision showing the uptime reset, then node-a's telemetry back under 90%.
```

Until that window, treat acceptance (a)'s node-side observation as **open**; §2.1
(fail-closed refusal, no 404, reason on the client surface) and §3 (the action
lands on a real target with `result=ok`) are verified.

## 5. How to re-run everything that does not need the fleet

```bash
python3 -m unittest discover -s tests          # 721 tests
python3 infra/gcp/evidence/tools/solvable-faults-hermetic.py
```
