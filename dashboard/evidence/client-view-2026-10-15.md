# Client-visible node registration + health view — evidence (2026-10-15)

Task `t_5649d117` ("Add client-visible node registration and health view to the
dashboard"), built under the frozen enrollment contract written on the parent
decision card `t_074b1a14` (**option (a): provider-issued single-use tokens**).

Everything below was produced by
`dashboard/evidence/run-client-view-evidence.sh` on this host, with **no GCP and
no stub**: a real controller process, the documented customer one-liner, the real
node daemon feeding real telemetry, and the dashboard bundle served on a second
origin. Nothing on the client path uses the project token.

## What the scope note changed

The card's literal REQUIRED WORK 1 and 4 (a client-authenticated **node list**
plus cross-client isolation in the UI) presuppose the self-service option (b)
that the user ruled out: under (a) a client has **no** credential that can list
nodes. The scoped deliverables actually built are:

1. the `token=<project_token>` query-parameter anti-pattern removed from the
   dashboard and from the published URL (primary gap);
2. a **single-node** client view — `GET /v1/nodes/{node_id}/telemetry/recent`
   with the node's own `node_token` in the `Authorization` header, never a fleet
   list, never a query string;
3. the instructive empty state (REQUIRED WORK 5), unchanged and still required.

Client isolation is a property of the credential, not of UI code: the node token
reads exactly one node and the controller refuses a cross-node id with 403, so
there is no isolation code to get wrong and no client identity store was added.

## Run transcript (verbatim)

```
=== controller: real process on http://127.0.0.1:47489 ===
controller ready

=== 1. provider handover (loopback mint, single-use credential) ===
credential minted (not printed)

=== 2. the documented customer one-liner ===
installer wrote the node config + its own credential (0600)

=== 3. real node daemon (telemetry the controller really ingests) ===
the node registered (customer path: termini-node-status --wait)
telemetry samples stored for the node: 3

=== 4. dashboard served on a second origin: http://127.0.0.1:47301 ===
env written: <workdir>/env.json (0600)

=== ready — point a browser at: http://127.0.0.1:47301/?apiBase=http://127.0.0.1:47489/v1&projectId=01J0DEM0WARM000000000000AB ===
node id      : 01M509WE2CS3HBP715P2C14YDT
```

## Evidence items

### (iii) Empty state — `screenshots/01-empty-state.png`

A client with nothing connected sees: what this view is (one node, your own
credential), the five steps (ask the provider for a one-time credential → run the
documented one-liner → `YOUR NODE IS REGISTERED` → `termini-node-status --watch`
→ paste `node_id` + `node_token` from
`/var/lib/termini/identity/node.json`), and what they will see afterwards. The
one-liner is rendered with the **configured controller** substituted in:

```
curl -fsSL http://127.0.0.1:47489/install/termini-node.sh | sudo sh -s -- --controller http://127.0.0.1:47489 --token <CREDENTIAL>
```

### (i) The freshly enrolled node shown as registered — `screenshots/02-registered-live.png`

The node was enrolled minutes earlier by the one-liner + the real daemon. Read
back with that node's own credential only (panel values, verbatim):

```
registered · online — last sample 1s ago
registration  registered
last seen     1s ago
reading       http://127.0.0.1:47489/v1/nodes/01M509WE2CS3HBP715P2C14YDT/telemetry/recent

HEARTBEAT     seq 75   2026-10-15T17:32:48Z
CPU           32.2%    8 cores
MEMORY        86.4%    18092965888 / 20948180992 bytes
UPTIME        284h 48m
LOAD (1M)     3.65
NODE RUNTIME  Ubuntu 24.04   kernel 6.8.0-142-generic
SERVICES      3/3 running    termini-bootstrap:running · lxd:running · sshd:running
```

The telemetry is real: the controller had already ingested samples from the node
daemon (`telemetry samples stored for the node: 3`) before the browser loaded,
and the per-node endpoint returns stored samples, not fixtures.

### (ii) The live health stream — `screenshots/03-registered-live-9s-later.png`

Same document, no navigation, 9 s later:

```
before: HEARTBEAT | seq 75 | 2026-10-15T17:32:48Z
after : HEARTBEAT | seq 78 | 2026-10-15T17:32:52Z   | navigations: 1
```

`navigations: 1` is the initial page load: the panel re-reads the node every 5 s
and re-renders in place, so the heartbeat, CPU and memory advance without a page
reload.

### (iv) No project/demo token on the client path — `network-capture.json`

Captured in the page after the node credential was entered — 48 requests,
including the client-path calls:

| Check | Result |
|---|---|
| request URLs containing `?token=` / `&token=` | **[] (none)** |
| request URLs containing the project/demo token | **[] (none)** |
| the node-credential request | `http://127.0.0.1:47489/v1/nodes/01M509WE2CS3HBP715P2C14YDT/telemetry/recent?limit=5` — no credential in the URL |
| where the node credential is held | `sessionStorage['termini.dashboard.node']` (this tab only) |
| `localStorage` | **empty** (the credential never leaves the tab) |
| page URL | `http://127.0.0.1:47301/?apiBase=…&projectId=01J0DEM0WARM000000000000AB` — no credential |

The credential itself is sent as `Authorization: Bearer …` (the same header the
CLI uses); the URL carries only `?limit=5`.

### (v) A legacy `?token=…` link — `screenshots/04-legacy-token-link-refused.png`

Loading `…&token=demo-project-token` produces, in the live page:

```
URL            : http://127.0.0.1:47301/?apiBase=http%3A%2F%2F127.0.0.1%3A47489%2Fv1&projectId=01J0DEM0WARM000000000000AB
has token=     : False            <- stripped from the address bar and the history entry
toast          : Credential in the URL refused — A credential in the URL (?token=…) is not
                 accepted and has been removed from the address bar. Open Settings and sign
                 in — from then on it is sent in the Authorization header only.
sessionStorage : ['termini.dashboard.node']   localStorage: []   <- nothing was adopted from the URL
```

The parameter is not honoured, not stored, and no longer present in the URL.

## Enforcement (tests, not prose)

`node --test dashboard/tests/*.test.mjs` → **48 tests, 0 failures**, 20 of them
new:

* `tests/credentials.test.mjs`
  * `resolveConfig` never resolves a credential from the query string, and flags
    the legacy link as `urlTokenIgnored`;
  * `apiUrl()` **throws** rather than build a URL carrying a credential —
    including when the credential is smuggled in through `apiBase`;
  * `stripKeysFromQuery()` scrubs only the credential and keeps the rest;
  * the **real** `LogStream` is pointed at a real HTTP server and the assertions
    are on what the server received: `Authorization: Bearer …` present, no
    `token=` anywhere in the request URL, the `?since=` cursor intact.
* `tests/client-node.test.mjs`
  * the empty state contains the documented one-liner, the `--token <CREDENTIAL>`
    shape, `YOUR NODE IS REGISTERED`, `termini-node-status --watch`, the
    `node.json` path — and neither the project token nor the demo token;
  * the registration state machine (`no credential` / `pending` / `registered` /
    `degraded` / `not reporting` / `credential rejected` / `controller
    unreachable`) against contract §1.3's 15 s / 45 s thresholds, with distinct,
    actionable messages for a rejected credential (403) and an unreachable
    controller;
  * first telemetry → cards: heartbeat `seq`, CPU, memory, uptime, load, node
    runtime, services (and dashes, never `NaN`, when a field is missing).

The card's old `?token=` assertion in `tests/core.test.mjs` now asserts the
opposite (the builder throws), so the anti-pattern cannot come back unnoticed.

## Reproduce

```bash
# 1. the whole client path, no GCP, no stub (controller + one-liner + daemon + dashboard)
dashboard/evidence/run-client-view-evidence.sh /tmp/termini-client-view
#    it prints the dashboard URL; open it and watch the empty state, then paste the
#    two values from <workdir>/env.json into the "Your node" panel.

# 2. the tests
node --test dashboard/tests/*.test.mjs
```

## Known limits / follow-ups (out of this card)

* **"Daemon version" is not in the frozen telemetry contract** — no `Sample`
  field and no registration field carries it, so it is not invented here; the
  panel shows the runtime facts the contract does carry (`os_name`, `os_version`,
  `kernel_version`, plus `schema_version` when present). Adding a daemon version
  is a contract change, not a UI one.
* The health view is a 5 s poll of `telemetry/recent`; the contract has no
  per-node SSE endpoint (the CLI's `--watch` is the same poll).
* The health numbers above are this host's (8 cores, 20 GiB), which is why they
  are not the node image's — the plumbing is what the evidence is about.
* `dashboard/mock/server.py` still injects its local mock token into the
  generated `config.js` for loopback development; no client-facing surface does
  that, and the mock's new per-node route refuses a query-string credential
  outright.
