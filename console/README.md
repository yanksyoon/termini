# console/ — composed W5 customer operations console

One origin, one command, for the two customer-facing W5 surfaces: the operations
dashboard (`dashboard/`, task `t_d9de3c25`) and the xterm.js web terminal
(`frontend/terminal/`, task `t_cd585251`), both pointed at the same controller.

This directory is the *integration* layer added by the W5 root task
(`t_e3aa4c5e`): it does not reimplement either UI — it composes them, reconciles
the terminal route with the frozen contract, and seeds a live demo.

## Run the whole thing

```bash
console/run-demo.sh                      # dashboard http://127.0.0.1:8090/
CONTROLLER_PORT=18085 CONSOLE_PORT=18090 console/run-demo.sh
```

It starts, in order:

1. the SSH CA + a demo **node sshd** on `SSH_PORT` (via `sudo`) that trusts the
   CA (`TrustedUserCAKeys`) and the break-glass key — so the one-click terminal
   has a real node to shell into (dashboard.md §3.1). Set `TERMINI_NODE_SSHD=off`
   to use an externally-run node sshd instead;
2. the controller from this checkout (`python3 -m app.main`) on `CONTROLLER_PORT`;
3. two seeded demo nodes with a 1 s telemetry heartbeat (`seed_demo.py`) — live
   node health and a steady AI-operator log stream for the demo;
4. the console (`serve.py`) in the foreground, serving the dashboard at `/` and
   the web terminal at `/terminal/`.

Ctrl-C tears everything down.

The terminal bridge reaches the node over SSH with a short-lived, CA-signed
operator certificate (dashboard.md §3.1/§3.2, D4): `run-demo.sh` generates the
SSH CA, pre-signs the controller-local operator key, and hands the controller
`TERMINI_SSH_CA_KEY_PATH` / `TERMINI_SSH_OPERATOR_KEY_PATH` with
`TERMINI_TERMINAL_BACKEND=ssh`. Without this wiring the terminal would fall back
to a shell on the controller host, which is **not** the contract.

## Pieces

| File | Role |
|------|------|
| `serve.py` | Static compose + runtime-config injector. Serves `dashboard/` at `/`, `frontend/terminal/` at `/terminal/`, and the operator fault console at `/console/`, injecting `apiBase`/`projectId`/`token` into the dashboard's `config.js` and the terminal's page defaults. The fault console gets `apiBase`/`projectId` only — **never** the token. Stdlib-only; it does **not** proxy the controller. |
| `seed_demo.py` | Registers a compute + an inference node and streams telemetry (with an out-of-phase fault pattern) so health is live and the AI operator keeps emitting log lines. Retries a failed telemetry POST and can never lose its ticker threads — see *Seeder resilience* below. |
| `run-demo.sh` | Brings up SSH CA + node sshd + controller + seed + console with one command; generates the CA, the operator key and the break-glass key + inventory. |

Environment knobs: `CONTROLLER_PORT`, `CONSOLE_PORT`, `PROJECT_ID`,
`PROJECT_TOKEN`, `BOOTSTRAP`, `SSH_USER`, `SSH_PORT`, `PUBLIC_IP`,
`TERMINI_TERMINAL_BACKEND` (`ssh` — the node path — is the default; `local` is a
hermetic host-shell fallback for tests), `TERMINI_NODE_SSHD` (`auto`|`off`),
`TERMINI_SSH_KEY` (break-glass private key, default
`~/.ssh/termini_breakglass`), `TERMINI_DEMO_STATE`.

## Seeder resilience (telemetry timeouts)

The seeder is the only thing keeping the synthetic nodes ONLINE, so it is built to
survive a controller that stops answering:

- a telemetry POST that **times out** (or a 5xx — the controller failing to store a
  valid sample) is **retried inside the ticker** with backoff, and can never kill
  the ticker thread. A 4xx is not retried: the sample or the token is wrong;
- anything unexpected in the tick loop is caught and logged, because an exception
  that escapes `NodeTicker.run` ends the thread silently;
- `main` runs a watchdog: a ticker that died anyway is respawned in-process (same
  node id, token and sample `seq` — no re-registration), and a ticker that is alive
  but has not landed a sample for `--stall-warn-seconds` (default 20 s) is reported
  loudly on stderr, at most once per window per node.

Why this is load-bearing: a dead ticker is invisible from outside the process —
`main` keeps sleeping, `systemctl is-active termini-seeder` keeps reporting
**ACTIVE**, and the only symptom is both nodes flipping OFFLINE ~45 s later
(`DEGRADED_AFTER_S=15` / `OFFLINE_AFTER_S=45`), with no error anywhere. That
"sounds healthy, is frozen" state was observed live when the controller held its
single SQLite writer across the operator's model call (controller side fixed in
PR #145).

Knobs: `--telemetry-timeout S` (default 15; the retry deadline for one POST) and
`--stall-warn-seconds S` (default 20; the silence that gets reported). The run
also ends with a per-node total, `N sample(s) accepted, M attempt(s) failed` — the
line to read in the journal when the dashboard looks quiet.

Evidence: `tests/test_seeder_ticker_resilience.py` — a real hanging HTTP server
reproduces the timeout, and asserts the ticker survives it, that an unguarded call
really does kill a thread, and that the watchdog respawns a dead ticker and reports
a silent one.

## Demo fault page (`/demo/`)

`console/fault_demo/` is the one-button demo surface: one card per fault, each stating **what you will see**,
**how it is injected**, and **the expected resolution**, with a duration choice and a single *Inject fault* button.
It reads the same operator-only endpoints as the console (`GET/POST/DELETE /v1/projects/{pid}/demo/faults`), keeps the
project token in the tab's `sessionStorage`, and is served without a token by `console/serve.py`.
The node picker defaults to a real node; choosing a synthetic one is labelled as such and the `target` field is sent to
match the node's provenance (the controller refuses a mismatch). While an injection is open the card shows the samples
written and a *Clear now* button; a cleared injection reads `recovering` until the rule's healthy samples re-arm it.

```bash
python3 console/serve.py --controller https://your-controller --project <project_id> --port 8090
# demo faults: http://127.0.0.1:8090/demo/   (open it on the tailnet/host the audience can reach)
```

## Operator fault console (`/console/`)

`console/fault_console/` is the **operator-only** control surface for demo faults
(contract `docs/contracts/controller-api.md` §4.7): it lists the two demo scenarios
(`oom`, `disk-fail`), lets the operator pick a node from the fleet listing, a
scenario, a TTL and whether the node is `real` or `synthetic`, and shows the
currently-active injections with a **Clear** button. The `disk-degrade` and
`latency` detector rules remain implemented in the controller (a real customer
node can still produce those signals), but the demo can no longer inject them.
Before it, fault injection existed only as `seed_demo.py`'s oscillation —
there was no way to trigger a chosen fault on a chosen node from the UI, and no way
to clear one that was running.

It is deliberately **not** part of the client-facing dashboard: it is not linked
from the dashboard's navigation, it is not given the project token by `serve.py`,
and every call is authenticated with the project token the operator pastes in
(kept in that tab's `sessionStorage`). Without a token the page loads and can do
nothing — the controller answers `401`/`403`. Guardrails enforced by the controller,
not the page: one open injection per `(node, rule)`, refusal while a remediation for
that `(node, rule)` is in flight, a fault is always clearable, and nothing outlives
its TTL silently (see §4.7).

```bash
CONTROLLER_PORT=8085 CONSOLE_PORT=8090 console/run-demo.sh
# dashboard http://127.0.0.1:8090/ · operator fault console http://127.0.0.1:8090/console/
```

The seeder is **not** required for a demo with the console: it stays usable (and is
still the fixture that gives a seeder-only demo a fleet to fault), but the faults
themselves are driven by the console's injections.

## Configuration injection

Both frontends already accept connection settings over the URL; the console
fills them in server-side so the served URL stays clean:

- dashboard → appended to `window.TERMINI_CONFIG` in `config.js`
  (`apiBase`, `projectId`, `token`, plus `terminalUrl` to reveal the header
  *Web terminal* link);
- terminal → `window.__TERMINI_TERMINAL_DEFAULTS__` (read by
  `frontend/terminal/js/app.js`), with `?node=<id>` preselect.

Cross-origin is not required: the console serves both apps and the controller
enables CORS for REST/SSE and validates the terminal WS token from the query
string (contract §3.1).

## Acceptance

Point the W7 harness at the running controller:

```bash
TERMINI_CONTROLLER_URL=http://127.0.0.1:18085 \
TERMINI_PROJECT_TOKEN=demo-project-token \
TERMINI_PROJECT_ID=01J0DEM0WARM000000000000AB \
python3 w7/acceptance/run.py
```

`W5.1` (live health + AI-log stream), `W5.2` (terminal shell), and `W5.3`
(SSH string + break-glass login) all pass against this stack.
