# Agents panel — the demo row is explained, not offered (t_df3120a8)

Captured 2026-10-18 06:37 UTC against the **live** controller
(`https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB`) in a
real Chromium, with the branch's bundle served locally
(`python3 -m http.server 18099` from `dashboard/`) because the hosting deploy is
the orchestrator's step, not this card's (`firebase deploy` was NOT run).

Page: `http://127.0.0.1:18099/index.html?synthetic=1&dest=fleet&apiBase=https://34-93-190-64.sslip.io/v1&projectId=01J0DEM0WARM000000000000AB`
Token: pasted into the app the way a viewer does it
(`sessionStorage['termini.dashboard.token']`, the Settings field writes the same
key). No credential in the URL.

## What the fleet reported at capture time

`GET /v1/projects/{pid}/nodes?include=synthetic` + each node's
`metrics_latest.lxd_instances[]`:

| node | synthetic | reported instances |
|---|---|---|
| `demo-node-a` (`01M4NZK46WXG5ZEFAGAAV5JRXM`) | **true** | `NNB9287WRHGFDPEZ995TKCXDNW` |
| `demo-node-c` (`01M4NZK474BVARFBXYQBMVA8GV`) | **true** | `NNB9287WRHGFDPEZ995TKCXDNW` |
| `9a0225f0…` (`01M5609C8CQHS2EMZT2V9VZZA6`) | false | `NNB9287WRHGFDPEZ995TKCXDNW` |
| `ba1f1fae…` (`01M4TGD31WXH97JAA5S6A4R10D`) | false | `J2DFSZ2VRKD2EVF2ZAJFCE2H8E`, `T6K2AZ0PQ08AGKMZTKGRM7QGB0` |

This is the owner-reported id from the card, and it is exactly the ambiguous
case: the same id is attested by two seeder-fabricated nodes *and* by a real
one. The panel's decision is per ROW, from the node's own `synthetic` flag —
never from the id's shape.

## 01-demo-row-explained-disabled.png

`demo-node-a`'s group: `demo` badge, the group-level explanation ("these rows
have no agent runtime behind them"), the row `NNB9287W…`, and
**Start / Stop / Restart / Give a task… all disabled** with the explicit line
beneath them:

> demo row: seeder-fabricated instance, no agent runtime behind it

The pill reads `agents 1 · 2 demo · 1 node no inventory`: the demo rows are
named, never folded into the client's agent count.

## 02-real-node-row-box-works.png

The SAME id on the real node `9a0225f0…`: "Give a task…" opens the box, the
draft types, **Send task** is enabled. Nothing else changed for a real row.

## "No request is attempted" — measured, not assumed

With `window.fetch` instrumented, every control on the demo row was activated:

```
demo_buttons_clicked:            ["Start","Stop","Restart","Give a task…"]
requests_after_demo_clicks:      []
requests_after_real_open:        []
```

(Synthetic `dispatchEvent('click')` reaches a disabled button's listener where a
real click would not — so this also exercised the second gate in `main.js`: the
`refusedLocally()` path answered with a "No action sent" toast instead of
calling the API.)

## The draft survives the poll

Box opened on the real row, draft typed, then two 5 s poll cycles:

```
open_after_two_polls: true
draft:                "ping"
send_enabled:         true
```

## The refusal the live controller answers today

`POST /v1/projects/…/agents/00000000000000000000000000/tasks` → **404**:

```json
{"error": {"code": "AGENT_NOT_FOUND",
           "message": "no agent 00000000000000000000000000 on this project's nodes",
           "details": [{"field": "vm_id", "reason": "unknown",
                        "known_vm_ids": ["termini-01m55jewvas5whw0qjnckpvj-0-0"],
                        "known_agent_ids": ["T6K2AZ0PQ08AGKMZTKGRM7QGB0"]}],
           "request_id": "01M56V8NBS2R1W1TZKRFN09Y34"}}
```

The deployed controller predates `t_a1fa0567`: it still sends `known_vm_ids`
(internal instance NAMES). The panel renders `reason` and `known_agent_ids`
and **drops `known_vm_ids`** rather than re-leaking what the contract stopped
sending — pinned by the test `the refusal the DEPLOYED controller answers today
renders, without re-leaking instance names`.

## What was NOT done, and why

- **No task was dispatched at the live fleet.** The card's parent probe
  deliberately kept the production fleet untouched while the owner is demoing;
  a real `POST …/tasks` writes a task record, an audit command and (if the id
  resolves live) runs an `exec` inside the instance. The refusal *rendering* is
  proven by the unit + DOM tests (including the live 404 body above and a 503
  body carrying `details[0].unscanned_nodes`); the live browser check covers
  what only a browser can show — the disabled, explained row and the working
  box.
- **No `firebase deploy`** — the orchestrator deploys hosting from merged
  `main` and re-probes the live URL.
