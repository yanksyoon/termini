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
| `serve.py` | Static compose + runtime-config injector. Serves `dashboard/` at `/` and `frontend/terminal/` at `/terminal/`, injecting `apiBase`/`projectId`/`token` into the dashboard's `config.js` and the terminal's page defaults. Stdlib-only; it does **not** proxy the controller. |
| `seed_demo.py` | Registers a compute + an inference node and streams telemetry (with an out-of-phase fault pattern) so health is live and the AI operator keeps emitting log lines. |
| `run-demo.sh` | Brings up SSH CA + node sshd + controller + seed + console with one command; generates the CA, the operator key and the break-glass key + inventory. |

Environment knobs: `CONTROLLER_PORT`, `CONSOLE_PORT`, `PROJECT_ID`,
`PROJECT_TOKEN`, `BOOTSTRAP`, `SSH_USER`, `SSH_PORT`, `PUBLIC_IP`,
`TERMINI_TERMINAL_BACKEND` (`ssh` — the node path — is the default; `local` is a
hermetic host-shell fallback for tests), `TERMINI_NODE_SSHD` (`auto`|`off`),
`TERMINI_SSH_KEY` (break-glass private key, default
`~/.ssh/termini_breakglass`), `TERMINI_DEMO_STATE`.

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
