# Seeder self-provisioning — VERIFICATION (2026-10-15, repo-side, zero GCP spend)

Card: `t_b62d1afd` (*"Demo resume burns 2 bootstrap tokens per seeder start"*), re-scoped by
the operator to **verify-and-close**: the fix already landed, the fleet stays down until the
approved raise on Fri 16 Oct, and this card must not raise it. Everything below was run on
this host against a throwaway worktree of `origin/main` plus the committed deployment
evidence `deploy-seeder-placement-2026-10-14.md` §FIX 2.

Verdict: **verified.** The mechanism is in the tree, it is a valid systemd unit, and the
reported failure and its fix were both reproduced end to end with a real controller and a real
seeder process — with no hand-minted token on the fixed path.

| | |
|---|---|
| Verified tree | `origin/main` @ `6ac1372` (worktree `verify-main-135`, detached) |
| Fix commit | `0f64d7b` — `t_demo_deploy fix(controller): seeder self-provisions, …` (PR #148) |
| Deployed commit in the evidence file | `57b0ed4` (the `wt/deploy` tip of that same work) |
| Controller VM / fleet | **not touched** — the deployed fleet was left down |

---

## 1. The fix is present, on the commit under test

```
$ grep -rn -- "--bootstrap-mint" console/seed_demo.py infra/cloudrun/setup-seeder-vm.sh
console/seed_demo.py:305:    ap.add_argument("--bootstrap-mint", type=int, default=0, metavar="N",
infra/cloudrun/setup-seeder-vm.sh:58:  --bootstrap-mint ${SEEDER_TOKENS} \\
```

`console/seed_demo.py:314-315` extends its token list from `mint_bootstrap_tokens()`
(`:127-149`), which calls `POST /v1/bootstrap-tokens` with the **project token**.
`controller/app/server.py:232` routes that to `_mint_bootstrap_tokens` (`:597`), which
requires the project token, requires `_is_loopback_client()` (loopback peer **and** no
proxy-forwarding header), clamps `count` to `1..32`, and inserts ordinary **unused**
single-use rows via `store.seed_bootstrap_tokens`. `setup-seeder-vm.sh:55-61` installs
`ExecStart=… seed_demo.py --controller … --token … --bootstrap-mint ${SEEDER_TOKENS}` with
`SEEDER_TOKENS=2` (`:29`) and `Restart=always` (`:60`).

## 2. The installed unit is valid, and carries no hand-minted token

Rendered out of the script exactly as bash would (unquoted heredoc: `\\` → `\`, `${VAR}`
expanded), then validated by systemd itself:

```
[Unit]
Description=Termini demo seeder (synthetic nodes + telemetry heartbeat)
After=network-online.target termini-controller.service
Wants=network-online.target

[Service]
User=ubuntu
ExecStart=/usr/bin/python3 /opt/termini/console/seed_demo.py \
  --controller http://127.0.0.1:8080 \
  --token demo-project-token \
  --bootstrap-mint 2 \
  --duration 0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
contains '--bootstrap-mint 2': True
contains a hand-minted '--bootstrap ' token: False
Restart=always: True
$ systemd-analyze verify /tmp/seeder.service ; echo rc=$?
rc=0
$ systemd-analyze verify /tmp/broken.service   # negative control, must have teeth
broken.service:2: Failed to resolve unit specifiers in %Q: Invalid slot
broken.service: Unit configuration has fatal error, unit will not be started.
rc=1
```

## 3. End to end: real controller + real seeder, four starts

Real `python3 -m app.main` on `127.0.0.1:18585` with a temp DB
(`TERMINI_BOOTSTRAP_TOKEN=boot-a,boot-c` — the only hand-minted tokens anywhere in this run),
driven by real `console/seed_demo.py` processes.

```
### controller up: 404
2026-10-15 09:02:32,086 INFO termini.controller controller listening on 127.0.0.1:18585 (db=…/controller.db)

=== RUN 1 (baseline, hand-minted --bootstrap): seed_demo.py --bootstrap boot-a,boot-c ===
[seed] node A (compute)   = 01M4ZCS0C7VZBS7N3C9R6D4HX9
[seed] node C (inference) = 01M4ZCS0CJEFQF0YY0XAM8QGZS
--- exit: 0

=== RUN 2 (same hand-minted tokens, NO mint flag -> the original defect) ===
register demo-node-a failed: 401 {'error': {'code': 'UNAUTHENTICATED',
  'message': 'bootstrap token already used', …}}
--- exit: 1

=== RUN 3 (--bootstrap-mint 2, no hand-minted token) ===
[seed] node A (compute)   = 01M4ZCS0C7VZBS7N3C9R6D4HX9
[seed] node C (inference) = 01M4ZCS0CJEFQF0YY0XAM8QGZS
--- exit: 0

=== RUN 4 (--bootstrap-mint 2, restart again) ===
[seed] node A (compute)   = 01M4ZCS0C7VZBS7N3C9R6D4HX9
[seed] node C (inference) = 01M4ZCS0CJEFQF0YY0XAM8QGZS
--- exit: 0

=== proxy-header mint attempt (what the host's Caddy sends for a public caller) ===
HTTP 403
{"error": {"code": "PERMISSION_DENIED", "message": "bootstrap-token minting is restricted to the controller loopback", …}}
```

* **RUN 2 reproduces the card's landmine** on the same tree: a bare restart with the
  pre-existing single-use tokens dies `401 bootstrap token already used`.
* **RUNS 3-4 are the fix**: with `--bootstrap-mint 2` and no hand-minted token, a start and a
  further restart both succeed.
* Registration is idempotent on `machine_id`: all three successful starts report the **same**
  two node ids, so no synthetic node is duplicated per start.
* Minting stays **direct-loopback-only**: the same call carrying `X-Forwarded-For` → `403`.

State read-back after those four starts — the accumulated history is *intact*, which is the
point the old workaround destroyed by deleting `/data/controller.db`:

```
nodes                    = 2
bootstrap_tokens rows    = 6
  ... UNUSED             = 0
  ... BURNED             = 6
operator_logs rows       = 4
    {'machine_id': 'demo-node-a', 'id': '01M4ZCS0C7VZBS7N3C9R6D4HX9', 'role': 'compute',   'status': 'online', 'synthetic': 1}
    {'machine_id': 'demo-node-c', 'id': '01M4ZCS0CJEFQF0YY0XAM8QGZS', 'role': 'inference', 'status': 'online', 'synthetic': 1}
operator_logs:
    minted 2 single-use bootstrap token(s) for the demo seeder
    disk-degrade-01: instance.stop 01M4N3EA8290C98F5E1F514570 -> error (…)
    minted 2 single-use bootstrap token(s) for the demo seeder
    oom-guard-01: instance.restart 01M4NDDBB976869DB6F062594B -> error (…)
tables: 18 (bootstrap_tokens, commands, decision_fallback_state, health_events, nodes,
        operator_lease, operator_logs, projects, remediation_audits, remediation_decisions,
        schema_migrations, sqlite_sequence, ssh_certificates, telemetry_batches,
        telemetry_samples, trigger_config, trigger_state, vm_allocations)
bootstrap_tokens (hash prefix, used_at):
    3224e442 used_at= 2026-10-15T09:02:32Z   <- RUN 1 hand-minted
    dd8922d4 used_at= 2026-10-15T09:02:32Z
    1fff906a used_at= 2026-10-15T09:02:38Z   <- RUN 3 minted
    a8427f7a used_at= 2026-10-15T09:02:38Z
    c2563bd9 used_at= 2026-10-15T09:02:44Z   <- RUN 4 minted
    110cb048 used_at= 2026-10-15T09:02:44Z
```

(The two non-mint `operator_logs` rows are the operator loop acting on the synthetic telemetry
in `deterministic` mode and erroring only because `TERMINI_LXD_BASE_URL` points at a dead port
on this host — the decision/audit path ran and wrote history, which is what is being shown.)

## 4. Gates, re-run on the commit

```
$ python3 -m unittest tests.test_seeder_self_provision -v
Ran 14 tests in 9.841s
OK
$ python3 -m unittest discover -s tests -t .
Ran 624 tests in 94.274s
OK
$ python3 scripts/check-doc-ulids.py
all example identifiers are 26-char ULIDs
$ python3 scripts/check-readme-links.py
checked 58 unique link/path target(s)
all README links and repo paths resolve
```

`tests/test_seeder_self_provision.py` covers the mint route (project token required, bad
`count` → 422, rows inserted **unused**), that **single-use survives** (a minted token
registers once, the replay is `401 already used`, and minting more does not resurrect a burned
token), loopback-only including the proxy-header trap, and the seeder minting + registering
twice — i.e. the restart that used to fail.

## 5. Provenance: the DB-deleting workaround really is gone

```
$ git log --oneline -6 -- infra/cloudrun/setup-seeder-vm.sh
0f64d7b t_demo_deploy fix(controller): seeder self-provisions, agents land on the compute node, scripted deploy (#148)
aad3724 t_d12a6a4b docs: correct two claims that no longer match the deployed system (#99)

$ git show aad3724:infra/cloudrun/setup-seeder-vm.sh | grep -n "controller.db\|bootstrap\|rm "
13:TOKENS="demo-boot-a3,demo-boot-c3"   # fresh: bootstrap tokens are single-use
25:sudo rm -f /data/controller.db /data/controller.db-wal /data/controller.db-shm
41:  --bootstrap ${TOKENS} \\

$ git show 0f64d7b:infra/cloudrun/setup-seeder-vm.sh | grep -n "controller.db\|bootstrap\|rm "
44:  sudo rm -rf /etc/systemd/system/termini-seeder.service.d
58:  --bootstrap-mint ${SEEDER_TOKENS} \\
```

Before: hard-coded single-use tokens **and** a `controller.db` wipe (audit history destroyed to
get a start to work). After: neither — the only remaining `rm` is the *drop-in directory*, and
it is backed up first (`seeder-dropins-backup-<ts>`).

## 6. Cross-check against the committed live evidence

`deploy-seeder-placement-2026-10-14.md` §FIX 2 records the live before/after (bare
`systemctl start` → `401 bootstrap token already used` under a stale drop-in, then
`is-active: active` with `--bootstrap-mint`, a live replay refused `401`, a public mint
refused `403`). Its mechanism matches the tree. Content check between the deployed commit and
today's `origin/main`:

```
$ git diff --stat 57b0ed4 origin/main -- console/seed_demo.py infra/cloudrun/setup-seeder-vm.sh controller/app/server.py
controller/app/server.py | 172 ++++++++++++++++++++++++++++++++++++++-
 1 file changed, 170 insertions(+), 2 deletions(-)
```

`console/seed_demo.py` and `infra/cloudrun/setup-seeder-vm.sh` are **byte-identical** between
the deployed tree and `origin/main`; only `controller/app/server.py` has moved on with later
PRs, and the mint path in today's `server.py` is what §3 exercised (201 direct, 403 with
`X-Forwarded-For`).

## Still wrong / unverified (disclosed)

1. **Two tokens are still burned per seeder start** (6 burned after 4 starts above), because
   that is what the chosen mechanism does: mint fresh single-use rows on demand. The *harm* on
   the card — a finite pool, hand-minting before every take, or wiping `controller.db` — is
   gone; the supply is now self-service, project-token-authenticated and loopback-only.
   Consequence: `bootstrap_tokens` grows 2 rows per start with no retention policy, plus one
   audit row per mint. Inert at the documented restart rate, but it is a real (accepted)
   trade-off, not something this verification removed.
2. **`console/run-demo.sh` (the local dev runner) still passes a fixed pair** —
   `--bootstrap "$BOOTSTRAP"` at `:177` with `BOOTSTRAP=demo-boot-a,demo-boot-c`
   (`:25`). It survives only because `STATE` defaults to a fresh `mktemp -d` and it provisions
   the controller it talks to; with a persistent `TERMINI_DEMO_STATE` its second run hits
   exactly RUN 2 above. Out of scope for this card (it is not the deployed seeder) and filed
   as a follow-up task.
3. **No live control-plane check was run, and none is needed for this card.** The failure and
   its fix were both reproduced locally through the same code path the unit invokes, and the
   unit itself was checked by `systemd-analyze verify`. Any live re-check belongs to the raise
   window owned by `t_ece9d14f`.

## Reproduction

```
git worktree add --detach /tmp/verify-afd origin/main
cd /tmp/verify-afd
python3 -m unittest tests.test_seeder_self_provision -v
# then, with the two helper scripts from this card:
#   v-seeder-local.sh        -> the four-start controller+seeder end-to-end (§3)
#   v-db-readback.py <db>    -> the state read-back (§3)
#   v-unit-render.py . /tmp/seeder.service && systemd-analyze verify /tmp/seeder.service   (§2)
```
