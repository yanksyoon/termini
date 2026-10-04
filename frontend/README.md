# Termini web terminal + SSH access panel

The customer-facing terminal surface for the Termini dashboard: an embedded
[xterm.js](https://xtermjs.org/) terminal that opens an interactive shell on a
selected node over WebSocket, plus the copyable project SSH access string.

Built against the frozen contract in
[`docs/contracts/dashboard.md`](../docs/contracts/dashboard.md) §3 — the terminal
WebSocket framing is the source of truth; the backend bridge itself is owned by
task `t_3468e5f0` and lives in `controller/`.

## Layout

```
frontend/
├── terminal/                     # the static UI (no build step)
│   ├── index.html                # page: node selector, terminal, SSH panel
│   ├── terminal.css
│   ├── js/
│   │   ├── framing.js            # wire codec: base64 input/output, resize, ping (§3.1)
│   │   ├── ssh-command.js        # sanitize host/user/key + build the ssh command (§3.2)
│   │   ├── terminal-client.js    # DOM-free session state machine + reconnect/backoff
│   │   ├── ssh-panel.js          # SSH panel component (textContent-only render, copy)
│   │   └── app.js                # xterm.js + FitAddon wiring, node selector, states
│   └── vendor/                   # vendored xterm.js 6.0.0 + @xterm/addon-fit 0.11.0 (MIT)
├── server/
│   └── terminal_bridge.py        # DEV MOCK of the controller endpoints (stdlib only)
├── test/                         # node --test unit suite (framing, ssh-command, client)
└── package.json                  # {"type":"module"} so the js/ modules resolve as ESM
```

## Run the demo locally

The mock bridge serves the static UI **and** a real PTY-backed shell over the
frozen WebSocket framing — no dependencies beyond the Python standard library:

```bash
python3 frontend/server/terminal_bridge.py          # http://127.0.0.1:8091
# open the printed URL, or:
#   http://127.0.0.1:8091/?api=http://127.0.0.1:8091&token=demo-project-token
```

Page config comes from the query string:

| param     | default              | meaning                                        |
|-----------|----------------------|------------------------------------------------|
| `api`     | page origin          | controller base URL (http/https)               |
| `project` | `demo`               | project id for the node/ssh endpoints          |
| `token`   | `demo-project-token` | `project_token` (sent as `?token=` on the WS)  |
| `key`     | `~/.ssh/termini_breakglass` | operator's local private-key path       |

Pointing at the **real** controller is a config change only — the client speaks
the frozen protocol and is agnostic to the bridge mechanism:

```
http://<dashboard>/?api=https://<controller-host>&project=<pid>&token=<project_token>
```

## Contract conformance (§3)

* Terminal WS: `ws(s)://<host>/terminal/v1/{node_id}?token=<project_token>`.
  Client frames `input` (base64), `resize`, `ping`; server frames `ready`,
  `output` (base64), `exit`, `error`, `pong`.
* Auth failure → server close code `4401`; the client renders an error and does
  **not** retry.
* `NODE_OFFLINE`/`SESSION_REPLACED`/`exit` are terminal: surfaced clearly, never
  a hung terminal, no auto-reconnect.
* An unexpected socket drop → exponential-backoff + jitter reconnect
  (500 ms → 10 s, 8 attempts).
* SSH string: `ssh -i <key> [-p <port>] <user>@<public_ip>`, built from
  `GET /v1/projects/{pid}/nodes/{nid}/ssh`.

## Security notes

* `ssh-command.js` validates `user`/`host`/`key path` against strict allowlists
  and **rebuilds** the command from the individual fields — the server-computed
  `command` field is never trusted or rendered. Anything with shell
  metacharacters is rejected, not escaped.
* The SSH panel writes every dynamic value with `textContent` (never
  `innerHTML`), so a hostile inventory value cannot inject markup.
* Key material never reaches the browser: only the command string and the key
  fingerprint are displayed.

## Tests

```bash
node --test frontend/test/*.test.mjs         # 36 unit tests: codec, sanitizer, session FSM
python3 -m unittest tests.test_web_terminal   # integration against the mock bridge (real PTY)
```

The Python integration test boots the bridge and drives the real WebSocket
protocol: ready handshake, `whoami`/`ls` actually running in a PTY, resize,
auth rejection (4401), `NODE_OFFLINE`, and single-session replacement. It also
runs the JS suite via `node --test` (skipped if node is absent).

## Note on `server/`

`frontend/server/terminal_bridge.py` is a **development mock**. It shells out to
a local `bash` on the machine running it and is meant for local/CI testing while
the real controller endpoints land. Do not deploy it.
