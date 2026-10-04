# Termini — Customer Operations Dashboard

Customer-facing operations dashboard for W5: node health overview, live
AI-operator log stream, resource-utilization charts, and manual-intervention
controls. Built to the frozen contract in
[`docs/contracts/dashboard.md`](../docs/contracts/dashboard.md) — no divergent
API was invented.

Zero build step, zero third-party runtime dependencies (plain ES modules + Canvas
+ `EventSource`), matching the controller's stdlib-only philosophy. Any static
file server can host it.

---

## Run it

```bash
# 1) contract-faithful mock controller + static host (stdlib only)
python3 dashboard/mock/server.py --port 8099
#    -> http://localhost:8099/   (config.js is generated, so no query args needed)

# 2) or point at a real controller: edit dashboard/config.js, or use Settings,
#    or pass query params:
#    http://<host>/dashboard/?apiBase=https://10.10.0.10/v1&projectId=<ULID>&token=<project_token>
```

Live controller, no mock:

```bash
python3 -m http.server 8099 --directory dashboard
# then open .../?apiBase=https://<controller>/v1&projectId=<ULID>&token=<project_token>
```

### Configuration precedence

`?query` → `localStorage` (Settings dialog) → `dashboard/config.js` → defaults.

| Key | Meaning |
|-----|---------|
| `apiBase` | REST root incl. `/v1`. `""` = same origin. |
| `projectId` | project ULID (required unless `offline`) |
| `token` | `project_token` (Bearer for REST; `?token=` for SSE) |
| `pollMs` | REST poll cadence (default 5000, contract §1.5) |
| `defaultWindow` | initial chart window (`5m`/`15m`/`1h`/`6h`) |
| `logBufferMax` | client-side log ring buffer (default 2000) |
| `offline` | `?offline=1` — bundled in-browser fixtures, no network at all |

---

## What the dashboard consumes

| Surface | Endpoint / transport | Contract |
|---------|----------------------|----------|
| Node status + health | `GET /projects/{pid}/nodes` (poll 5 s) | dashboard §1.1/§1.3 |
| Node detail + latest sample | `GET /projects/{pid}/nodes/{nid}` | dashboard §1.2 |
| Resource series | `GET /projects/{pid}/nodes/{nid}/metrics?start&end&resolution&metrics` | dashboard §1.4 |
| AI-operator log | `GET /projects/{pid}/logs` (SSE, `Last-Event-ID`/`since` resume) | dashboard §2.2/§2.3 |
| Restart | `POST /projects/{pid}/nodes/{nid}/actions/reboot` | dashboard §4.1 |
| Drain | `POST /projects/{pid}/nodes/{nid}/actions/drain` | dashboard §4.2 |
| Scale intent | `POST /projects/{pid}/intents` → `POST /projects/{pid}/vms/apply` | dashboard §4.3 |
| Approve / deny AI remediation | `POST /projects/{pid}/remediations/{cid}/approve\|deny` | dashboard §4.4 |

Every state-changing POST carries an `Idempotency-Key` (contract §1.5) and renders
the standard error envelope (contract §3) with its `code`/`message`/`request_id`.

The terminal panel (xterm.js WebSocket + SSH string) is a separate card
(`t_cd585251`) and is deliberately **not** part of this bundle.

### Panels

1. **Overview grid** — per-node card: role/status badges, CPU %, RAM %, disk
   state, health flags (`disk_ok`/`gpu_ok`/`inference_ok`), allowlisted services,
   `degraded_reasons`, heartbeat age + uptime, and quick Restart/Drain buttons.
2. **Live AI-operator log** — scrolling, timestamped, severity-coloured feed with
   auto-scroll (+ pause/resume), severity/node/actor filters, a client ring
   buffer, `log_id` de-duplication, `truncated` marker handling, and inline
   Approve/Deny for `pending_approval` remediations.
3. **Resource utilization** — dependency-free Canvas time-series charts (CPU,
   memory, disk latency, network, GPU/VRAM, load, temp, inference…), a selectable
   window (5 m / 15 m / 1 h / 6 h) mapped to contract resolutions, labelled axes
   with units, legends and hover crosshair; bytes are displayed in GiB/MiB while
   the wire stays in bytes.
4. **Manual intervention** — Restart / Drain behind confirm dialogs with a reason
   selector, a scale-intent form (prompt → reviewed plan with clamps as warnings
   and rejects as errors → apply), and approve/deny of pending AI remediation.

### Empty / loading / error behaviour

* Loading → skeleton cards; no data → explicit empty states ("No nodes
  registered yet", "no data for this window").
* Backend failure → non-blocking **degraded-backend banner** with the error code
  and a Retry button; last-known data stays on screen; every action surfaces a
  toast + inline result on success or failure.
* Invalid/unreachable states never leave a blank or hung panel.

---

## Verify

```bash
# pure core logic (format / health / series / logs / config)
node --test dashboard/tests/core.test.mjs

# contract conformance of the mock fixtures (also runs in CI)
python3 -m unittest tests.test_dashboard_contract -v

# full UI: 5-minute continuous run, zero console errors, live transitions
python3 dashboard/mock/server.py --port 8099 &
node dashboard/tests/run5min.mjs http://127.0.0.1:8099/ 300
```

`dashboard/tests/run5min.mjs` launches headless Chromium over the DevTools
Protocol and records every console error/warning, uncaught exception and browser
log entry for the duration, while sampling the rendered DOM (node cards, log
rows, charts, banner, statuses) and writing a final screenshot + `summary.json`.
It needs no npm packages (Node ≥ 22 for the built-in `WebSocket`).

---

## Mock controller

`dashboard/mock/server.py` serves the static bundle **and** implements the
contract endpoints for the four W5 surfaces, with:

* evolving 1 s telemetry and 19-metric series (same names/units as
  `controller/app/store.py METRICS`),
* live status transitions (`online → degraded → offline → online`) via heartbeat
  stalls,
* a streaming SSE operator log incl. periodic `pending_approval` remediations,
* the intervention endpoints, with `409 CONFLICT` for a disallowed approve/deny
  and `503 NODE_OFFLINE` for rebooting an offline node,
* `GET /nodes/{id}/ssh` and `GET /telemetry/recent` (as-built fallback shape).

It is a dev/test double, not a second implementation of the controller — the real
backend is owned by `t_3468e5f0`.

---

## Integration notes for the real controller

* **Same-origin serving.** The dashboard is a static bundle; serve it from the
  controller (or a reverse proxy) so `apiBase` can stay `""` and no CORS is
  needed. The mock allows `*` for local development only.
* **Health on the list endpoint.** `GET /nodes` per contract §1.1 includes
  `health`; the as-built core (`server.py _node_view`) does not, so the dashboard
  falls back to deriving contract §1.3 flags from a latest sample
  (`GET /nodes/{id}` → `metrics_latest`) when `health` is absent.
* **SSE auth.** `EventSource` cannot set `Authorization`; the token rides in
  `?token=` exactly as the contract specifies for `/logs`.
* **Terminal.** Out of scope here; see `t_cd585251` (`/terminal/v1/{node_id}`).

### Contract discrepancies found while building

1. **ULID example length.** `docs/contracts/dashboard.md` (§1.1, §2.1, §3.2)
   prints 30-character example IDs such as `01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E`,
   which violates the document's own frozen invariant — a 26-char Crockford
   base32 ULID (§1.3, `governance.md` §4). The fixtures use canonical 26-char
   ULIDs; the doc examples are illustrative and should be shortened.
2. **`controller-api.md` §7.2/§7.3** still describe the thin `{ts,level,text}` log
   shape and the VM-level terminal; `dashboard.md` §7 (R-D1/R-D3) already
   supersedes both for this surface — the dashboard follows R-D1/R-D3.
