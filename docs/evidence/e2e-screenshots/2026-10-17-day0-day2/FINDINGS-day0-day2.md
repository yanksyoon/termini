# Day-0 → day-2 on the deployed fleet — findings from the rebuild run (2026-10-17)

Run scope the owner asked for: (1) rebake the node image with the current daemon,
(2) land the product fix that makes the controller's LXD trust install at
registration, (3) accept the workload-launch beat, then delete the rehearsal node
and re-run the whole day-0 → day-2 test with the findings documented here.

**Status: (1) done, (2) landed but INCOMPLETE on the node side, (3) accepted, re-run
partially executed.** Day-2 on a node booted from the current image still does not
execute. The blocker is item F2 below and it is a code gap, not a config knob.

Everything in this file is measured on the live fleet; the raw transcripts of the
re-run are in `raw/` next to this file.

## What was shipped

| # | Change | Verified |
|---|---|---|
| 1 | Node image **`termini-node-os-6`** baked from the current tree (`image/pipeline/tools/bake-golden-node-image.sh`), family now resolves to it | bake checks all PASS: `api_8443_http_code`, `storage_pool_default`, `guest_bridge_lxdbr0`, `default_profile_root_disk`, `alias_agent-vm/2026.10.0`, `lxc_image_list_has_agent-os/w4`, `bootstrap_unit_enabled`, `baked_offline_sentinel`; `BAKE_OK … alias=agent-vm/2026.10.0` |
| 2 | Controller **`POST /v1/nodes/{nid}/lxd-trust`** + node-side `install_lxd_trust()` (PR #252, merge `4bca67b`, deployed to `/opt/termini` at 22:22Z) | 12 new unit tests green (6 controller, 6 node); CI green; route answers `403 invalid node token` to a bogus token (exists, not 404) and `200` for a real one |
| 3 | Fleet cleanup: rehearsal VM `termini-day0` deleted, reserved IP `termini-day0-ip` released, stale registration `01M55WPSX…` decommissioned | fleet back to the two standing nodes before the re-run |

## What the re-run proved (node booted from `termini-node-os-6`)

Raw: `raw/01-image.txt` … `raw/09-audit.txt`.

- **Day-0 registration works on the new image** — created 22:37:44Z, unit start
  22:38:32Z, `REGISTERED node_id=01M5609C8CQHS2EMZT2V9VZZA6 role=compute
  register_ms=24786 boot_to_registered_ms=48210`, `state=ONLINE`, telemetry `seq`
  climbing (`raw/03-registered.txt`).
- **The controller sees it as a real (non-synthetic) node** (`raw/04-fleet.txt`).
- **It carries the workload alias** — `lxc launch agent-vm/2026.10.0 demo-workload`
  succeeds from the baked image store (`raw/06-workload.txt`).
- **Day-2 was attempted on that node and the decision was right while the action
  was refused.** Fault injected on the new node at 22:59:55 (`raw/07-inject.txt`);
  the model chose `oom-guard-01 -> instance.restart`, `decision_source=model`,
  `confidence=0.9`, and it *resolved the target to `demo-workload`*
  (`raw/08-decision.txt`); the audit row then reads

  ```
  2026-10-17T22:59:57.907Z oom-guard-01 instance.restart result=error
    ep=https://10.10.0.14:8443 path=/1.0/instances/NNB9287WRHGFDPEZ995TKCXDNW/state
    detail: HTTP 403: {"error_code":403,"error":"Forbidden"}
  ```

  while the **same rule and action on node-a in the same window reads
  `result=ok`** (`ep=https://10.10.0.11:8443 … action=restart; state Running ->
  Running`). The pair is the finding: the day-0 node is reachable, enrolled and
  correctly targeted, and still not *authorised* — F2 in one line. On the node the
  workload is `RUNNING, PID 2745, up 13 min` (`raw/09-audit.txt`): it was **not**
  restarted, because the call never landed.

## Findings

**F1 — `register_ms` regressed from 245 ms to 24,786 ms on the new image.** Same
fleet, same controller, three hours apart: 245 ms on `termini-node-os-5`
(21:36Z), **24,786 ms** on `termini-node-os-6` (22:38Z) — 5× over the 5 s budget
the deck claims. `boot_to_registered_ms` moved 19.1 s → 48.2 s. The likely cause is
the daemon's pre-registration `ensure_lxd_api()` on a cold first boot (it probes and
may repair the LXD listener before registration, and `register_ms` counts that
work); it is **not yet established** — this needs a timing breakdown before the
number goes on stage. Do not quote 150/245 ms for `os-6` until it is.

**F2 — the node cannot mint the trust token under its own unit sandbox
(`NoNewPrivileges=true`).** The daemon logged
`cannot mint LXD trust token: cannot change profile for the next exec call: Operation not permitted`,
then `LXD_TRUST skipped: could not mint a trust token`
(`raw/03-registered.txt`). The snap `lxc` CLI cannot exec under `NoNewPrivileges`,
which is exactly why the *inventory* reader in the same daemon talks to LXD's unix
socket instead of the CLI. Consequence: the controller route landed in PR #252 is
never called on a real node, so **the operator's first remediation against a
day-0 node would still answer `403 Forbidden`** — the defect PR #252 was meant to
remove is still present for nodes deployed today.

*Fix (next):* mint the certificate-add token over `/var/snap/lxd/common/lxd/unix.socket`
(`POST /1.0/certificates {"type":"client","token":true}`), assemble the
`CertificateAddToken` exactly as `lxc config trust add` does, and post that through
the existing route. The route, the idempotent `ensure_trusted`, the endpoint check
and the tests all stay; only the minting transport changes.

**F3 — the evidence capture script had three defects of its own, all found by
running it and all fixed in this pack's tooling** (they are why `raw/07-inject.txt`
is a refusal in the first pass): it never re-rendered `/tmp/day0-cloud-init.yaml`,
so the VM booted with the *previous* run's burned token and failed
`401 bootstrap token already used`; it looked the node up by `registration_id ==
"node-d"` while the API returns a 32-hex hostname hash, so `node_id` came back
empty; and its step gating was inverted, skipping steps. The transcripts of those
failures are kept rather than deleted — `raw/07-inject.txt` (pass 1) is the
`INVALID_ARGUMENT: node_id is required` refusal, not a fabricated success.

**F4 — a deleted VM leaves a live `offline` node row.** The rehearsal VM deleted at
22:16Z kept its registration (`01M55WPSX…`, `status=offline`), so the dashboard
read `3 nodes` with one dead. Retired with
`POST /v1/projects/{pid}/nodes/{nid}/decommission`. Any demo teardown must
decommission, not just delete the instance, or the fleet count contradicts the
story.

**F5 — the whole local unit suite cannot run on this host as-is.** `1144 tests`
→ `17 errors`, every one in `test_agent_tasks` with
`ModuleNotFoundError: No module named 'yaml'` (the agent-vm package needs PyYAML;
this host's Python 3.14 has no pip). CI is green on the same commit; the local
errors are environmental, not a regression — state that rather than quoting the
local run as a failure.

## Not captured / not claimed

- **No successful day-2 beat on a day-0 node** — the action is refused (F2). What
  IS captured is the refusal and the working control side by side (see above); do
  not present the `result=ok` rows from node-a as the new node healing itself.
- **No dashboard frame yet** for this run: the browser capture follows once F2 is
  fixed, because a frame showing the new node "online" without a remediation behind
  it would advertise exactly the thing that does not work.
- The earlier `termini-node-os-5` measurements (245 ms / 19.06 s) remain valid for
  that image, and the archived 150 ms run remains valid for its image; neither
  describes `os-6`.

## Re-run (after F2)

```bash
infra/gcp/tools/live-window.sh take --owner t-day0-trust --ttl 45m --purpose 'day-0 -> day-2 evidence'
bash docs/evidence/e2e-screenshots/2026-10-17-day0-day2/tools/capture-day0-day2.sh 01
python3 docs/evidence/e2e-screenshots/2026-10-17-day0-day2/tools/make-index.py \
  docs/evidence/e2e-screenshots/2026-10-17-day0-day2
```

A rebake is required to ship the F2 fix in the image (the daemon is baked); the
in-place daemon upgrade path exists but is not the demo story.

## Artefact index (generated by tools/make-index.py from the files themselves)

### Raw transcripts (the train of truth)

| # | file | what it is | bytes | lines | sha256 |
|---|---|---|---|---|---|
| 01 | `raw/01-image.txt` | $ gcloud compute images list --no-standard-images --filter='family=termini-node-os' --format='table(name,famil | 383 | 6 | `9c59010ef99c84adbe23ca8484545bcaa5a311116bede110513918e6516f1ebc` |
| 02 | `raw/02-create.txt` | $ date -u +%Y-%m-%dT%H:%M:%SZ; gcloud compute instances create termini-day0 --zone=asia-south1-a --machine-typ | 816 | 9 | `842ea6e3b9db5e9c305378b290aab861e8bae7b57cfa50b3e884fd2e5fb98362` |
| 03 | `raw/03-registered.txt` | $ for i in $(seq 1 24); do S=$(gcloud compute ssh termini-day0 --zone=asia-south1-a --quiet --command 'sudo te | 1829 | 30 | `1076ade0774f9d067364dc8d45aed70c135c4d0c83d35cb227436d95eec93287` |
| 04 | `raw/04-fleet.txt` | $ curl -sS -H 'Authorization: Bearer demo-project-token' 'https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WA | 573 | 6 | `8a9b6184f0f92bef12222d67d70976dd3f7742de16e1f7e29a00bc313137d2ae` |
| 05 | `raw/05-trust.txt` | $ gcloud compute ssh termini-controller --zone=asia-south1-a --quiet --command="sudo journalctl -u termini-con | 208 | 3 | `e58b0bf5a70a93f270170463eef63d1be58e10f5c5443dff18d514d88dce3d4a` |
| 06 | `raw/06-workload.txt` | $ gcloud compute ssh termini-day0 --zone=asia-south1-a --quiet --command="date -u +%H:%M:%SZ; sudo lxc launch  | 652 | 14 | `8dc2d95431cb0d13e01936d3e0bb0f7af12216efd7c30af6f988f3558a92b8b7` |
| 07 | `raw/07-inject.txt` | $ echo node_id=01M5609C8CQHS2EMZT2V9VZZA6; curl -sS -X POST 'https://34-93-190-64.sslip.io/v1/projects/01J0DEM | 965 | 5 | `ab10460c9023f9d7ad8fb4bdfc6410f1589041c40e2a38d2a390fd12344c1aa8` |
| 08 | `raw/08-decision.txt` | $ sleep 12; curl -sS -H 'Authorization: Bearer demo-project-token' 'https://34-93-190-64.sslip.io/v1/operator/ | 1479 | 9 | `7c128e2d794f02f4e5c34c3a946f5c50340db3c309812d61e2deae66ba94e851` |
| 09 | `raw/09-audit.txt` | $ curl -sS -H 'Authorization: Bearer demo-project-token' 'https://34-93-190-64.sslip.io/v1/operator/audits?lim | 2512 | 20 | `c778ca4bf946215bd1497658afbd643b19279592d26e15b5947611c78cd1f8b2` |
