# A node now comes up with its OWN LXD API, and registration fails closed without one

Task `t_0139006c`. All outputs below are real, pasted from the runs on
2026-10-17 (UTC) against the **standing GCP fleet**: controller `termini-controller`
(`10.10.0.2`, `34.93.190.64`), `termini-node-a` (`10.10.0.11`), `termini-node-c`
(`10.10.0.13`).

## 1. What was actually wrong on node-a (not just a config typo)

`snap.lxd.daemon` was **crash-looping**, nothing listened on `:8443` and the API
property was empty:

```
$ sudo systemctl show snap.lxd.daemon -p NRestarts,SubState,MainPID
NRestarts=110
SubState=running
MainPID=856829
$ sudo ss -ltn | grep 8443            # (no row)
$ sudo lxc config get core.https_address   # hangs: the daemon is not serving
$ sudo journalctl -u snap.lxd.daemon -n 4
lxd.daemon[856256]: level=warning msg="Failed to rollback transaction after error
  (failed to check if schema table is there: schema table query returned no rows)"
lxd.daemon[856256]: level=error msg="Failed starting daemon" err="Error creating
  database: failed to check if schema table is there: schema table query returned no rows"
systemd[1]: snap.lxd.daemon.service: Scheduled restart job, restart counter is at 110.
```

The global dqlite directory carried un-finalised `open-2/3/4` WAL segments from the
12:38Z crash plus an OOM kill (`dmesg`: `Out of memory: Killed process … (systemd)`).
From the controller the endpoint was dead, exactly as the card measured:

```
$ curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 --insecure https://10.10.0.11:8443/1.0
000
```

## 2. Restore (measured, from the controller)

A boot-time repair script (installed as `startup-script` metadata, DB backed up to
`/var/backups/lxd-db-t_0139006c-*`, no deletion) stopped the loop, quarantined the
un-finalised WAL segments, started the daemon and asserted `core.https_address`.
The instance was reset, and the node came back whole — **all 5 instances intact,
including `demo-workload` RUNNING**, and the controller's client certificate still
in its trust store:

```
=== repair log ===
listener already present; ensuring core.https_address
core.https_address=:8443
listeners:  LISTEN 0 4096 *:8443 *:*      (lxd)
instances:  demo-workload RUNNING 10.174.247.148
            termini-01m4wjhwqr3b8pcav2tvqbgg-0-0 RUNNING
            termini-01m4wjhwqr3b8pcav2tvqbgg-0-1 RUNNING
            termini-01m537g26sm8qgkmztn8stbt-0-0 STOPPED
            termini-01m537km0trb189a6e1gz546-0-0 RUNNING
trust:      client termini-controller a27fd4eb94cb (expires Oct 9 2036)
NRestarts=0  SubState=running  ActiveEnter=Sat 2026-10-17 16:32:16 UTC
```

From the controller, the acceptance probe:

```
2026-10-17T16:32:53Z node-a:8443 -> 200
```

## 3. A real operator action produces a real LXD operation id

`remediation_audits` rows written **after** the repair (node `01M4TGD31…` resolves to
`https://10.10.0.11:8443`):

| executed_at | result | detail | lxd_endpoint |
|---|---|---|---|
| 16:33:36.873Z | ok | `operation Success: /1.0/operations/4ef77f4b-3325-47f2-9b23-c0341fa641e9; action=start; state Stopped -> Running` | `https://10.10.0.11:8443` |
| 16:33:42.670Z | ok | `operation Success: /1.0/operations/84392ccd-3ddb-45d4-84ea-5dae4a2fb9be; action=restart; state Running -> Running` | `https://10.10.0.11:8443` |
| 16:33:54.333Z | ok | `operation Success: /1.0/operations/2a46e426-66ad-4dc1-8029-517c1a2726af` | `https://10.10.0.11:8443` |
| 16:34:00.642Z | ok | `operation Success: /1.0/operations/499c560f-ca46-4828-b71e-f6dfd29bd152` | `https://10.10.0.11:8443` |

Before the repair, the same path read (16:31:18Z / 16:31:46Z):

```
URLError: <urlopen error [Errno 111] Connection refused>
```

## 4. Registration now fails closed, with a distinct status

Run against the **live controller** with the branch's daemon and a port that cannot
serve TLS (`:8599` squatted by a plain HTTP listener, so LXD cannot bind it):

```
lxd api repair: PUT /1.0 core.https_address=:8599 -> HTTP 500
registration retry 1/1 in 1s: own LXD API is not listening on 10.10.0.2:8599
  (repair_attempted=true): SSLError: [SSL: WRONG_VERSION_NUMBER] wrong version number
LXD_API_UNAVAILABLE (terminal, kind=lxd_api_unreachable, attempts=2): own LXD API is
  not listening on 10.10.0.2:8599 (repair_attempted=true): SSLError: …
daemon_exit=1

state.json: {"state": "LXD_API_UNAVAILABLE", "failure_kind": "lxd_api_unreachable",
             "registration_attempts": 2, "terminal": true}
identity dir: (absent — no registration request was ever made)
```

The registration POST is never attempted, so the single-use enrollment token is not
spent and the node never appears in `nodes` — it is refused **at registration**, not
discovered at remediation time. (A first probe run on the controller proved the
repair half instead: the daemon set `core.https_address=:8599` through the daemon's
unix socket, LXD bound it and `GET /1.0` answered `200` — the controller's own LXD
config was restored to empty afterwards.)

## 5. What changed

* `infra/bootstrap/node/termini-bootstrap.py` — `probe_lxd_api()`,
  `ensure_lxd_api()` (binds `core.https_address` over the daemon's unix socket,
  starts the unit when the socket is dead, bounded wait), `registration_body()`
  advertises the endpoint **only** when proven, `register()` refuses to run without
  it (kind `lxd_api_unreachable`), `_park_registration_failed()` parks in the
  distinct `LXD_API_UNAVAILABLE`, and the telemetry loop now sends a REST heartbeat
  carrying `lxd_endpoint` (+ `lxd_api.listening`) with the endpoint omitted when the
  listener is down.
* `infra/bootstrap/node/termini-bootstrap.service` — ordered after
  `snap.lxd.daemon.service` (Wants=, not Requisite=: the daemon repairs the listener
  itself and fails closed).
* `controller/app/registration.py` — `endpoint_from_heartbeat()` (same validation as
  registration; malformed is a 422, never a silent fallback).
* `controller/app/store.py` — `set_node_lxd_endpoint()`.
* `controller/app/server.py` — `_heartbeat` records the reported endpoint and logs a
  warning when a node reports its API is down.
* `docs/contracts/node-bootstrap.md` — §2 (`LXD_API_UNAVAILABLE`), §5.2 (endpoint is
  advertised only when proven; registration fails closed), §9 row 14, §13
  (`heartbeat_interval_s`).
* Tests: `tests/test_bootstrap_lxd_api.py` (18),
  `tests/test_controller_heartbeat_lxd_endpoint.py` (7); `tests/test_bootstrap_registration.py`
  stubs the precondition so HTTP-status classification stays a unit test.
* `tests/test_client_installer.py` — the customer-enrollment end-to-end test now
  presents the listener instead of assuming one: a real TLS stub answering
  `GET /1.0` on an ephemeral port, selected through the documented `lxd_port` config
  key, and it asserts the daemon actually probed it. CI caught this the honest way —
  the first run of this branch was RED because the test had only ever passed on a
  host that happened to run LXD at `:8443`; it no longer depends on the host.
* `infra/bootstrap/node/termini-bootstrap.py` — `TERMINI_LXD_UNIX_SOCKETS` overrides
  the daemon's unix-socket list. It exists so a suite run on a developer machine
  cannot repair that machine's REAL LXD: this task's first test run wrote
  `core.https_address` through the dev host's socket. The e2e test sets it to a path
  that does not exist, and `tests/test_bootstrap_lxd_api.py` pins both the override
  and the default.
* `infra/gcp/tools/verify-node-lxd-api.py` — re-runnable acceptance: probes every
  live node's own endpoint from the controller and exits non-zero on `000`
  (§5a). It reads the controller's node table, so it asserts the endpoint the
  executor would really dial, not a hand-typed URL.

Test run: `python3 -m unittest tests.test_bootstrap_lxd_api tests.test_bootstrap_registration
tests.test_bootstrap_telemetry tests.test_controller_heartbeat_lxd_endpoint
tests.test_fleet_listing_default_view tests.test_operator_loop
tests.test_operator_remediation_effect tests.test_lxdtrust tests.test_raise_lxd_trust`
→ **129 tests, OK**.

## 5a. Re-verified live at 17:12Z (after the branch was rebased onto main)

The listener is a live thing, so the acceptance was re-measured, not carried over.
New re-runnable tool — `infra/gcp/tools/verify-node-lxd-api.py` — run **on the
controller** against the controller's own node table. It probes the endpoint the
controller would actually dial (the node-reported `nodes.lxd_endpoint`), and says
so loudly when it has to fall back to the global URL:

```
$ TERMINI_DB_PATH=/data/controller.db python3 infra/gcp/tools/verify-node-lxd-api.py
2026-10-17T17:12:33Z
ok   01M4TGD31WXH97JAA5S6A4R10D  ba1f1fae…5713b 10.10.0.11:8443  [reported] HTTP 200
ok   01M4ZQ7Q2J80ATY3K32PY3PPJH  33678f54…2486e6 10.10.0.13:8443  [reported] HTTP 200

2/2 nodes answer their own LXD API (0 resolved via the global fallback)
exit=0
```

`HTTP 200` (not `000`) is the acceptance: a non-zero status from the controller.
Decommissioned/offline rows are excluded by default — they are history, not the
fleet; `--include-offline` probes them too (it reported 7 stale rows from earlier
demos, which is why the default is scoped).

Real operator actions in the same window (controller API `GET /v1/operator/audits`),
each one a successful `PUT /1.0/instances/demo-workload/state` on node-a:

```
17:12:19.211Z result=ok  lxd_endpoint=https://10.10.0.11:8443
  operation Success: /1.0/operations/ad5c8394-159a-4287-8663-d4d54836b0b3; action=start
17:12:11.047Z result=ok  lxd_endpoint=https://10.10.0.11:8443
  operation Success: /1.0/operations/720669b4-1011-43b1-928b-0d4d11f0cec0; action=stop
17:11:49.698Z result=ok  lxd_endpoint=https://10.10.0.11:8443
  operation Success: /1.0/operations/a7834ac7-7a3f-4cf2-a1b0-3aec6bb2d040; action=restart
```

Full suite on the rebased branch: `python3 -m unittest discover -s tests` →
**1050 tests, OK** (295 s).

## 6. Security posture (unchanged)

No trust password, no proxy, no tunnel: the API is reachable only from
`10.10.0.0/24` (`termini-allow-lxd-cluster`, tcp/8443), and the controller
authenticates with its mTLS client certificate in the node's trust store
(`controller/app/lxdtrust.py`) — verified present on node-a after the repair. The
new probe disables TLS *verification only for the reachability assertion*; it never
adds trust.

## Caveat

The listener itself is still only as durable as the daemon's own database (that is
what drifted on node-a). The bootstrap now re-asserts and re-proves it on **every**
boot and re-reports it on every heartbeat, so a drifted node either comes back by
itself or refuses to register. A node whose daemon cannot start at all is a
different fault and is visible as `lxd` `stopped` in its telemetry `services[]`.

The **running** node-a/node-c still execute the baked copy at
`/usr/local/bin/termini-bootstrap` from their current boot disk: the fail-closed
daemon in this PR ships with the next node image build, while the listener on a
freshly raised node is already guaranteed by the image itself
(`image/pipeline/provision/10-node-os.sh` sets `core.https_address=:8443` and
`bake-node-os.sh` fails the bake if `GET /1.0` does not answer — that is what
`--raise` boots from). Restarting the enrollment unit on the demo node while the
owner is evaluating it would risk the demo for no extra proof: the fail-closed
path is already proven live against the real controller (§4), and the
re-assert/heartbeat paths are covered by the suite.
