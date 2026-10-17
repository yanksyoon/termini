# One live window: the lease that stops parallel workers clearing each other (t_9c1362f6)

**Card:** t_9c1362f6 — *Live fleet has no window lock: parallel infra workers clear
each other's injections and redeploy under each other.*
**Measured:** 2026-10-17T21:54:29–22:11:18Z on the standing fleet (controller +
node-a + node-c; node-b stays TERMINATED under the CPU budget).
**Window taken:** `2026-10-17T21:54:55.504Z` by `t_9c1362f6`, TTL 45 m, released
`2026-10-17T22:10:30.821Z` (see §2; the lock is the deliverable, and this run is
the first live acceptance to take it).
**Deployed trees:** `98ed279c8116cf44b9b2c655f64d5cabc87c26fd` (PR #251, the lease
+ `X-Termini-Actor` + boot-sweep attribution) and `2adc15e648b3a9ab1706cd89fea430ce5d54cf8f`
(PR #253, the driver's own lines). Both read back from
`/opt/termini/DEPLOYED_COMMIT` during the run.
**Nodes:** `termini-node-c` / `01M4ZQ7Q2J80ATY3K32PY3PPJH` and `termini-node-a` /
`01M4TGD31WXH97JAA5S6A4R10D`, both real (`synthetic=0`), both online.

Every block below is raw output from the live VM, the controller's own API or its
database. Commands are in [Reproduce](#reproduce).

**Result:** two concurrent takers cannot both hold the fleet (the loser exits 3
naming the holder, purpose and expiry); a deploy by a non-holder is refused with
**nothing written** to `/opt/termini` (byte-identical hashes, unchanged mtimes); and
a clear, a supersede, a recovery-complete line and a controller restart all name the
card that caused them instead of "the operator".

## 1. What went wrong

Three `infra` workers ran live experiments on the same standing fleet on
2026-10-17 between 19:30Z and 19:50Z (card body, and
`demo-fault-clear-telemetry-2026-10-17.md` §6):

* `t_c37aa0dc`'s first acceptance attempt injected at 19:40:56Z and was cleared by
  **another actor** 31 s in. Every project-token caller used to be recorded as
  `cleared_by=human`, so a peer's clear-all was indistinguishable from the operator
  pressing clear.
* Two different trees were deployed to `/opt/termini` within ~4 minutes (a peer's
  unmerged branch, then merged `main`). Each deploy restarts the controller, and
  `expire_stale_on_boot` kills any injection another process was driving.
* A schema migration pauses controller startup ~71 s on the 1.4 GB DB — exactly when
  a peer is most likely to deploy on top of you.

Nothing in the system said "this fleet is busy".

## 2. The lease, and the window this run held

`infra/gcp/tools/live-window.py` is a lease file on the controller VM
(`/var/lib/termini/live-window/live-window.json`) guarded by an `flock`-serialised
read-check-write, so two takers cannot both win. It lives on the VM, not in the
controller DB, so it survives controller restarts and is readable while the
controller is down (a migration pause). `live-window.sh` runs it over SSH.

```
=== take window for t_9c1362f6 ===
taken: t_9c1362f6 holds the live fleet
  owner=t_9c1362f6 purpose='t_9c1362f6 live acceptance: window concurrency, deploy gate, actor attribution' taken=2026-10-17T21:54:55.504Z expires=2026-10-17T22:39:55.504Z (expires in 2700s) host=termini-controller.asia-south1-a.c.termini-510609.internal
  acquired: True
take_rc=0
```

```
=== release the live window for t_9c1362f6 at 2026-10-17T22:10:30.821Z ===
  owner=t_9c1362f6 purpose='…' taken=2026-10-17T21:54:55.504Z expires=2026-10-17T22:39:55.504Z
  released: True
released
release_rc=0
=== status after release ===
  held: False
  expired: False
free — no live window is claimed
```

The window was held across every inject, restart and deploy in this document and
released before the "no window claimed" refusal in §3.3.

## 3. Acceptance (a): two concurrent workers cannot both hold it

### 3.1 Two takers released at the same instant — exactly one wins

Both `take` processes were started from one shell with no delay, on a free lease.
The loser is refused **loudly** with exit 3 and the holder's identity:

```
=== started: 2026-10-17T21:54:29.329Z ===
=== before: status ===
  held: False
  expired: False
free — no live window is claimed
launched at 2026-10-17T21:54:29.462Z pids A=330636 B=330637
=== taker A (owner t_probe_a) rc=3 ===
REFUSED: the live fleet is already claimed.
  owner=t_probe_b purpose='t_9c1362f6 concurrency probe B' taken=2026-10-17T21:54:29.557Z expires=2026-10-17T21:59:29.557Z (expires in 300s)
  You are t_probe_a. Do not inject, deploy or restart until this window is released or expires; if it is abandoned, the holder releases it (`live-window.sh release --owner t_probe_b`).
=== taker B (owner t_probe_b) rc=0 ===
taken: t_probe_b holds the live fleet
  owner=t_probe_b purpose='t_9c1362f6 concurrency probe B' taken=2026-10-17T21:54:29.557Z expires=2026-10-17T21:59:29.557Z (expires in 300s) host=termini-controller.asia-south1-a.c.termini-510609.internal
  acquired: True
  renewed: False
=== after: status ===
  owner=t_probe_b … held: True
HELD by t_probe_b — do not inject/deploy/restart
=== finished: 2026-10-17T21:54:29.673Z rc_a=3 rc_b=0 ===
```

Exactly one taker acquired (`rc_b=0`); the other was refused (`rc_a=3`) and its
message names the holder, purpose and expiry. The probe lease was then released
(`release --owner t_probe_b` → `released: True`) before `t_9c1362f6` took the window.

`tests/test_live_window.py` pins the stronger case in CI: six concurrent takers,
one winner.

### 3.2 A deploy by a non-holder is refused (exit 3) and writes nothing

A peer (`t_peer_probe`) takes the window at `2026-10-17T22:12:19.867Z`; the same
deploy that succeeds inside a window is refused while the peer holds it (the
`tarball=` on the local side is the tree being staged for transfer, not a write to
the fleet):

```
[deploy] ref=2adc15e commit=2adc15e648b3a9ab1706cd89fea430ce5d54cf8f tarball=787380 bytes
  owner=t_peer_probe purpose="t_9c1362f6 acceptance (a): peer-held window must refuse another worker's deploy" taken=2026-10-17T22:12:19.867Z expires=2026-10-17T22:17:19.867Z (expires in 288s) …
  ok: False
  reason: another owner holds the window
REFUSED: another owner holds the live window — wait for its release
[remote] ABORTED (exit 3): another owner holds the live window.
[remote]   Nothing was written to /opt/termini. Wait for the release, or ask
[remote]   the holder on the board (infra/gcp/tools/live-window.sh status).
ACTUAL_RC=3
```

Note there is no `[remote] backup ->` and no `[remote] deployed …` line: the gate
runs before the extract. The fingerprint below is the measurement.

### 3.3 A deploy with no window at all is refused (exit 4) and writes nothing

After the release, same ref, same actor, no window claimed:

```
[deploy] ref=2adc15e commit=2adc15e648b3a9ab1706cd89fea430ce5d54cf8f tarball=787380 bytes
  ok: False
  reason: no live window is claimed
REFUSED: no live window is claimed for t_9c1362f6 — take one first
[remote] ABORTED (exit 4): no live window is claimed for t_9c1362f6.
[remote]   Take one first:
[remote]     infra/gcp/tools/live-window.sh take --owner t_9c1362f6 --ttl 45m --purpose '...'
[remote]   (--no-window exists for repo bootstrap only.)
ACTUAL_RC=4
```

"Nothing written" is measured, not asserted — `/opt/termini` fingerprints are
byte-identical before and after both refusals (baseline `22:12:19.720Z`, after the
exit-3 refusal `22:12:44.603Z`; baseline `22:10:30.821Z`, after the exit-4 refusal
`22:11:18.644Z`):

| file | before the refusals | after the refused deploys |
|---|---|---|
| `DEPLOYED_COMMIT` | `584d0840f815d55864e74bc046215f54db0cd0179bab6f866f8f62d5fc45e60f` | same |
| `DEPLOY_ACTOR` | `c332dff989f7504ff1524f58af4c73f9dba7cc790f6d0a3f6144c12805d3d494` | same |
| `DEPLOYED_FILES` | `b784f5c78cfa8ab9662279d821fbce70805148d3ad4c5e736600516a28a77dd0` | same |
| `DEPLOYED_COMMIT` mtime/size | `1792274781` / 41 B | `1792274781` / 41 B |

### 3.4 Inside the window, the deploy proceeds and records who did it

```
  owner=t_9c1362f6 purpose='…' taken=2026-10-17T21:54:55.504Z expires=2026-10-17T22:39:55.504Z
  ok: True
  reason: window owned by caller
[remote] live window: held by t_9c1362f6 — deploying inside it
[remote] backup -> /opt/termini.bak-20261017T220610Z
[remote] deployed by t_9c1362f6 (window: check)
[remote] deployed 161 files from 2adc15e648b3a9ab1706cd89fea430ce5d54cf8f
[deploy] restarting termini-controller
active
[deploy] done: termini-controller /opt/termini now carries 2adc15e648b3a9ab1706cd89fea430ce5d54cf8f
```

and the durable record next to `DEPLOYED_COMMIT`:

```json
{"schema_version":"1.0","actor":"t_9c1362f6","ref":"2adc15e","commit":"2adc15e648b3a9ab1706cd89fea430ce5d54cf8f","deployed_at":"20261017T220610Z","window":"check"}
```

## 4. Acceptance (b): the audit line names the actor, never "the operator"

`X-Termini-Actor` on the §4.7 routes is written to `demo_faults.cleared_by` and to
the operator log; absent, it reads `unattributed_project_token`.

### 4.1 A clear by `t_9c1362f6` (build `98ed279`)

```
POST /demo/faults -> HTTP 201 injection_id: 01M55XTYKAH1H27V99MX9KYZ0X
DELETE (X-Termini-Actor: t_9c1362f6) -> HTTP 200
  state: recovering cleared_by: t_9c1362f6 clear_reason: cleared_by_operator
  demo_faults row: ('01M55XTYKAH1H27V99MX9KYZ0X', 'oom', 'cleared', 't_9c1362f6', 'cleared_by_operator', '2026-10-17T21:56:15.061Z')
```

### 4.2 A clear with no header is unattributed — not `human`

```
DELETE /demo/faults/01M55XWSE8CBDNXCBNMTT6111M (no X-Termini-Actor) -> HTTP 200
  "cleared_by": "unattributed_project_token"
2026-10-17T21:57:12.714Z | unattributed_project_token | fault cleared by an un-attributed project-token caller (no X-Termini-Actor header): node ba1f1f…
```

### 4.3 A controller restart names the deploy actor and commit

Injection open (5 samples written) when the controller was restarted inside the
window:

```
before restart: [{'state': 'active', 'cleared_by': None, 'samples_written': 5}]
restarting termini-controller ...
SWEEP LINE | 2026-10-17T22:06:57.858Z | t_9c1362f6 | fault injection 01M55YEKBT6G4EBV7504GJ6JF4 expired: controller restarted by t_9c1362f6 (deploy 2adc15e648b3 at 20261017T220610Z) while oom was active — no samples are being written for it
after restart: [{'state': 'expired', 'cleared_by': 'controller_restart', 'clear_reason': 'controller restarted by t_9c1362f6 (deploy 2adc15e648b3 at 20261017T220610Z) — the driver that owned this injection is gone'}]
```

Migration 0022 is what lets that line survive: `operator_logs.actor` used to be a
closed CHECK enum (`ai_operator|human|assistant`), so an actor id made the row
vanish. On the live DB (`schema_migrations` 22 applied `2026-10-17T21:46:05Z`) the
column now accepts an id of 1–64 chars.

### 4.4 The driver's own lines carry the actor too (PR #253)

Before PR #253 the *caller* line was right but two controller-emitted lines about the
same injection still defaulted to `human`, so the audit trail still showed an
operator action for a worker's fault:

```
2026-10-17T21:56:12.463Z | t_9c1362f6 | fault cleared by t_9c1362f6: …            (caller line — correct)
2026-10-17T21:56:15.062Z | human      | fault cleared: … back below threshold …    (worker's fault, operator's name)
2026-10-17T21:57:11.428Z | human      | node telemetry superseded by …             (same)
```

After deploying `2adc15e`, one injection produces four lines, all naming the card:

```
2026-10-17T22:09:59.745Z | t_9c1362f6 | fault injected by t_9c1362f6: node 33678f54a5e3e25cc03620b1bd2486e6 scenario=oom rule=oom-guard-01 target=real ttl=120s (injection 01M55YMAE0R1W4GRH5R
2026-10-17T22:10:00.354Z | t_9c1362f6 | node telemetry superseded by active fault injection 01M55YMAE0R1W4GRH5RS50X67B (oom); the injected series is authoritative until it is cleared
2026-10-17T22:10:04.783Z | t_9c1362f6 | fault cleared by t_9c1362f6: node 33678f54a5e3e25cc03620b1bd2486e6 scenario=oom rule=oom-guard-01 — restoring the healthy series, 3 sample(s) to re-ar
2026-10-17T22:10:07.861Z | t_9c1362f6 | fault cleared: node 33678f54a5e3e25cc03620b1bd2486e6 rule oom-guard-01 back below threshold after 3 healthy sample(s) (injection 01M55YMAE0R1W4GRH5RS5

demo_faults row: [{'state': 'cleared', 'cleared_by': 't_9c1362f6', 'clear_reason': 'cleared_by_operator', 'recovery_samples': 3}]
human-attributed fault lines in the whole log (must be 0 for these ids): [{'n': 0}]
```

## 5. Acceptance (c): written where workers read it, and used

* `.hermes.md` §"The live fleet — one window at a time" (merged in #251) is the
  agent working agreement: take the window, deploy with `--actor`, send
  `X-Termini-Actor`, release on **every** outcome and record take/release on the
  card, and no `--force`.
* `infra/gcp/README.md` §"Live fleet — one window at a time" has the commands;
  `docs/contracts/controller-api.md` §4.7 and `docs/contracts/dashboard.md` §2.1
  carry the attribution rules.
* This run **is** the next live acceptance: the window above was taken before the
  fleet was touched, every inject/restart/deploy happened inside it, and it was
  released at 22:10:30.821Z.

## 6. What could have fooled this evidence

* **The two takers were synthetic owners** (`t_probe_a`, `t_probe_b`) — a peer's
  task id would behave identically, but the refusal path does not verify that an
  owner exists on the board. A taker can also invent an owner; what it cannot do is
  take a lease another owner holds.
* **The exit-3 test used `t_peer_probe`, not a real card.** That is the point — it
  simulates the peer. The deploy gate reads the *lease*, not the board.
* **`DEPLOY_ACTOR.window` records `"check"`**, the gate mode, not the owner. The
  owner is `DEPLOY_ACTOR.actor`, and the gate prints
  `held by t_9c1362f6 — deploying inside it`. Do not read the `window` field as the
  lease.
* **A lease can still expire mid-run.** TTL is bounded (60 s–6 h) and there is no
  `--force`: if a worker overruns, the fleet becomes free and the next taker wins.
  The 45 m TTL here had 29 m left at release; a longer acceptance should take a
  longer TTL rather than renew silently.
* **The 21:52:03Z sweep line** (`fault injection 01M55XJS5TWE32HART76RCEFKC expired:
  controller restarted by t_9c1362f6 (deploy 98ed279c8116 …)`) comes from the
  pre-fix build's deploy by the same card earlier that hour; §4.3 is the fresh
  restart on the final build.
* **`/health` was probed with the wrong path** so the script's wait printed `None`;
  liveness after the restart is evidenced by the controller answering
  `POST /demo/faults` with `201` and `DELETE` with `200` at 22:09:59–22:10:04Z and
  `systemctl is-active termini-controller` → `active`.
* **The fleet is shared.** While this run held the window the lease was the only
  thing preventing a peer's clear-all; the evidence above describes one window, not
  a fleet-wide guarantee. A peer that ignores `.hermes.md` and calls the project
  token directly still can — which is exactly why the actor is now recorded, so a
  collision is attributable rather than invisible.

## 7. Final state

`/opt/termini` carries `2adc15e648b3a9ab1706cd89fea430ce5d54cf8f` (deployed by
`t_9c1362f6`, 20261017T220610Z), `systemctl is-active termini-controller` →
`active`, window free, both real test nodes online. No instance was created or
destroyed; node-b remains TERMINATED.

## Reproduce

```bash
export PATH="$PATH:/home/ubuntu/google-cloud-sdk/bin"
cd /home/ubuntu

infra/gcp/tools/live-window.sh status
infra/gcp/tools/live-window.sh take --owner <your-card> --ttl 45m --purpose "<card>: <what it proves>"

# (a) concurrency — from the controller VM, two takers at once:
#   sudo /usr/local/bin/termini-live-window --state-dir /var/lib/termini/live-window \
#     take --owner t_probe_a --ttl 5m --purpose probe &   (x2, different owners)

# (a) gate
infra/gcp/tools/deploy-termini-tree.sh --ref 2adc15e --actor <other-card>   # exit 3
infra/gcp/tools/deploy-termini-tree.sh --ref 2adc15e --actor <your-card>    # exit 0 inside your window

# (b) attribution (on the VM; the controller listens on 127.0.0.1:8080)
#   POST /v1/projects/<project>/demo/faults      X-Termini-Actor: <card>
#   DELETE /v1/projects/<project>/demo/faults/<id> X-Termini-Actor: <card>
#   then: demo_faults.cleared_by, operator_logs.actor
#   and: systemctl restart termini-controller with an injection open -> boot sweep line

infra/gcp/tools/live-window.sh release --owner <your-card>
python3 -m unittest tests.test_live_window tests.test_demo_faults
```
