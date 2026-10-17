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

# 2) or point at a real controller: edit dashboard/config.js, or use Settings, or
#    pass the NON-SECRET connection parameters as query params and sign in once:
#    http://<host>/dashboard/?apiBase=https://10.10.0.10/v1&projectId=<ULID>
#
#    The credential is never a query parameter. Open Settings, paste the
#    project_token, and it is sent in the Authorization header from then on,
#    kept for that browser tab only (sessionStorage) — see "Credential handling".
```

Live controller, no mock:

```bash
python3 -m http.server 8099 --directory dashboard
# then open .../?apiBase=https://<controller>/v1&projectId=<ULID>, open Settings and
# sign in. A `?token=…` link is refused and scrubbed from the address bar.
```

### Configuration precedence

`?query` → `localStorage` (Settings dialog, non-secret settings) →
`dashboard/config.js` → defaults.

**The credential is the one exception:** it is read only from this tab's
`sessionStorage` (what Settings writes) or from `dashboard/config.js` when a
deployment injects it server-side. It is *never* read from the query string, and
`apiUrl()` throws rather than build any request URL that would carry it.

| Key | Meaning |
|-----|---------|
| `apiBase` | REST root incl. `/v1`. `""` = same origin. |
| `projectId` | project ULID (required unless `offline`) |
| `token` | `project_token` — Bearer header on every request; **never** a query parameter |
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
| Operator decisions + the telemetry that fired them + the incident record + the audit rows | `GET /operator/{decisions,events,audits}` (poll 3 s, joined client-side) | operator-decision.md §4.2/§11 |
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
2. **AI operator — incident view** — one incident card per decision (title,
   explanation, the evidence fields quoted, the actions *decided* and the audit
   result *executed*), with the record's provenance labelled on every card (see
   below).
3. **Live AI-operator log (secondary)** — scrolling, timestamped,
   severity-coloured feed with auto-scroll (+ pause/resume), severity/node/actor
   filters, a client ring buffer, `log_id` de-duplication, `truncated` marker
   handling, and inline Approve/Deny for `pending_approval` remediations. It is
   collapsed by default: the incident card is the primary surface.
4. **Resource utilization** — dependency-free Canvas time-series charts (CPU,
   memory, disk latency, network, GPU/VRAM, load, temp, inference…), a selectable
   window (5 m / 15 m / 1 h / 6 h) mapped to contract resolutions, labelled axes
   with units, legends and hover crosshair; bytes are displayed in GiB/MiB while
   the wire stays in bytes.
5. **Metric expansion (latest sample)** — the §4.12–§4.14 groups as cards read from
   the **newest raw telemetry sample** (not from `/metrics`: they are per-sample
   facts, not time series): PSI pressure, memory pressure (`oom_kill_count`,
   `pgmajfault_count`, `mem_available_bytes`), disk saturation + inode counts,
   NIC error/drop counters, transport (`tcp_retransmit_segs`, conntrack) and
   per-instance OOM/fault counts. Every value states what it is: `cumulative` for
   a count since boot (never presented as a rate), `derived` for anything the
   panel computed (an inode or conntrack ratio), and the **frozen rule the group
   is a leading indicator for** in the card's caption — the contract defines no
   threshold for these fields, so no client-side flag is invented. A field the
   node did not report renders as **not reported**, never as a zero, and no
   threshold-shaped value (`pressure` is a fixed 4-key object) is fabricated.
   The panel bounds what it draws (`disk[]` 8, `net[]` 1, `lxd_instances[]` 10)
   and says what it cut. Only the frozen §1.4 allowlist is ever named in the
   `?metrics=` query; the new groups are read from the sample instead.
6. **Manual intervention** — Restart / Drain behind confirm dialogs with a reason
   selector, a scale-intent form (prompt → reviewed plan with clamps as warnings
   and rejects as errors → apply), and approve/deny of pending AI remediation.

The inline Approve/Deny surface is for **operator-initiated** `pending_approval`
remediations. The assistant endpoint (`POST /v1/projects/{pid}/assistant/ask`,
`controller-api.md` §4.10) is a separate card (frontend) that consumes a
different contract: an assistant-initiated action executes on guardrail
admission — never entering `pending_approval` — and is recorded with
`initiated_by: assistant` so it appears on the decision surface, not in this
approval list.

### AI assistant chat (`Ask AI`)

The topbar's **Ask AI** button opens a docked panel (it shifts the page instead
of covering the incident view on a laptop screen) for grounded questions about
any incident, node, decision or action Termini took. The panel is reachable from
every view and keeps a session message history.

* **Seeding an element.** Every element that has records behind it carries its
  own **Ask AI** affordance: an incident card, a decision row (including its
  thinking log), a node card in Node health, a chart (and its selected point), a
  pending remediation, and an audit row. Clicking one attaches that element's
  structured context — `{kind, id}` from the frozen §4.10 enum — as a removable
  chip (`incident · oom on the node · decision 01M5…0008`). The id travels as
  DATA in the request body, never baked into the question prose, and the chip
  survives follow-up questions until the reader removes it or attaches another
  element. Suggested follow-ups are per element, not one generic wall.
* **Citations.** Each citation the controller returns is a button that jumps to
  the row, node or audit row it names; a citation pointing at a row outside the
  fetched window says so rather than failing silently.
* **Honest states.** A pending question renders as a status line with no answer
  text; a `503 MODEL_UNAVAILABLE` or `422` renders as a typed error carrying the
  controller's reason (and a Retry) — never a fabricated answer. When the
  response carries `executed[]`, the panel renders what actually ran with its
  `command_id` and `audit_id` and an **open audit row** link; an answer that
  claims an execution without those ids is flagged as an unverified claim, and
  an execution row missing either id is flagged as unattributed. Actions the
  model proposed but did not run are labelled **proposed — not executed**.
* **Offline demo (`?offline=1`).** `js/offline.js` implements the same contract
  shape from bundled fixtures, so the projector demo shows the chat answering
  with citations and no controller. Every offline answer is labelled **fixture
  data — not a model answer, no controller behind it**, and offline questions
  that ask the assistant to act say there is no executor instead of inventing a
  `command_id`.
* The panel does **not** soften the dashboard's honesty surfaces: the
  degraded-backend banner, absent-not-zero values, trace provenance warnings and
  synthetic/demo labels all render as before, and a chat that cannot reach the
  controller is an error state, not the loudest thing on the page.

Evidence for this surface (screenshots plus the live-controller probe) is under
`dashboard/evidence/screenshots/t_d1aa35c8-*` and
`dashboard/evidence/t_d1aa35c8-assistant-chat.md`.

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
  `controller/app/store.py METRICS`). The mock's own `Sample` does **not** carry
  the §4.12–§4.14 groups yet, so the metric-expansion cards read "not reported"
  against it — inventing pressure/OOM numbers here would be fabricating
  telemetry. The committed real payload from the collector card
  (`docs/evidence/2026-10-16-metric-expansion/node-payload.json`) is the input
  those cards are built and evidenced against,
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
* **SSE auth.** `EventSource` cannot set `Authorization`, which is what forced
  the old `?token=` query parameter on `/logs`. The dashboard therefore reads the
  stream with `fetch()` + a `ReadableStream` reader and sends the credential in
  the `Authorization` header (`js/stream.js`); the URL carries only `?since=`.
  Reconnect/backoff is explicit (the browser's automatic EventSource reconnect
  no longer applies).
* **Terminal.** Out of scope here; see `t_cd585251` (`/terminal/v1/{node_id}`).

## AI panel: incident cards (what happened → why → evidence → decided vs executed)

The primary AI surface is one **incident card per decision**. The controller
persists the §4.2 incident record in the decision row itself
(`incident_title`/`incident_explanation`/`incident_evidence`/`incident_actions`/
`incident_generated`, migration 0016) and returns it verbatim from
`GET /v1/operator/decisions`, so the card needs no second endpoint: `GET
/v1/operator/{decisions,events,audits}` are three separate lists joined
client-side (`js/core/decisions.js`, pure and unit-tested).

Each card carries, in this order:

* a **provenance badge** — `AI-GENERATED INCIDENT` when `incident_generated` is
  `"model"`, or `CONTROLLER-GENERATED · MODEL UNAVAILABLE` when it is
  `"fallback"` (§8.1) — plus the raw `incident_generated` value, so the label is
  checkable against the row rather than taken on trust;
* the **title** (what happened) and the **explanation** (why the evidence implies
  it), both the model's own words when the record is model-generated;
* the **evidence list** — the telemetry fields the record quotes, rendered from
  `{field, value, seq?, ts?}` and nothing else; a value's unit comes from the
  field's own frozen name (`ram_used_bytes`, `*_ms`, `*_pct`), never from a
  guess, and an unknown field name is printed raw;
* **decided** — the actions the record names, presented as what was *decided*,
  not as an outcome;
* **executed** — the audit row for that decision (`remediation_audits`, joined on
  `decision_id`): result, endpoint, LXD call, `detail`, `idempotency_key` and
  the audit id. When there is no audit row the card says so and claims nothing.
  The two halves are never merged: the record must not imply an outcome the
  executor did not produce.

Three states are deliberately distinct, and none of them is a blank card:

| state | how it renders |
|-------|----------------|
| record with `generated: "model"` | `AI-GENERATED INCIDENT`, accented card |
| record with `generated: "fallback"` (§8.1) | `CONTROLLER-GENERATED · MODEL UNAVAILABLE`, amber card, distinct badge/label |
| no record (deterministic mode, §8.1) | `no incident record (deterministic mode)` |
| record with no `incident_generated` | `RECORD WITH NO PROVENANCE` in alarm styling — a contract violation is reported, not smoothed over |

Below the card the row still carries the scannable summary (timestamp, node,
rule, chosen action, execution result), the breaching metric + trigger value +
threshold text, and the rationale — with links to the audit record and to the
telemetry sample that fired the decision.

Two things the panel deliberately does **not** do:

* it never presents a `fallback` decision like a model decision — a fallback is
  the deterministic playbook stepping in because the model did not answer
  (operator-decision.md §8), so the row is styled differently and says so (and
  the §8.1 fallback *record* is labelled the same way);
* it never claims the model ran locally. The decider label is built from the
  egress record the controller writes to the operator log stream
  (`externalModelCall … backend=… model=… destination=… answered_by=…`), and an
  unobserved decider reads "decider unknown" rather than "local GPU".

`show on chart` plots the decision's own node (revealing `?include=synthetic`
seeder rows, labelled `demo`) with a decision marker coloured by who decided and
the threshold line resolved from the rule's own threshold text, so the fault
rising and the action landing are readable off one axis.

### The log stream is the secondary view

The raw AI-operator log panel is a collapsed `<details>` section below the
incident cards (a native disclosure, so no JS state has to be kept in sync with
the stream). It keeps everything it had — severity/node/actor filters, auto-scroll
with pause, ring buffer, `log_id` de-duplication, the `truncated` control line,
and inline Approve/Deny for `pending_approval` remediations — but it is no longer
the surface an operator reads first: the incident card answers the four questions
the log stream cannot.

### Log stream: the empty panel

The controller's SSE replays only when the client supplies a cursor
(`?since=` / `Last-Event-ID`, dashboard.md §2.2/§2.3). A cold page load has no
cursor, so the panel rendered "0 lines — Waiting for operator events…" until the
next live event, even though the stream itself was healthy. The client now opens
the first connection with the §2.3 out-of-buffer cursor (a 26-zero ULID) so the
server takes its documented truncation path and replays the retained tail, then
resumes from the last real `log_id`. The truncation control line reuses the
oldest retained line's `log_id`, so it is never entered into the client de-dupe
set (that collision used to swallow the first replayed line).

### Contract discrepancies found while building

1. **ULID example length.** `docs/contracts/dashboard.md` (§1.1, §2.1, §3.2)
   prints 30-character example IDs such as `01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C1E`,
   which violates the document's own frozen invariant — a 26-char Crockford
   base32 ULID (§1.3, `governance.md` §4). The fixtures use canonical 26-char
   ULIDs; the doc examples are illustrative and should be shortened.
2. **`controller-api.md` §7.2/§7.3** still describe the thin `{ts,level,text}` log
   shape and the VM-level terminal; `dashboard.md` §7 (R-D1/R-D3) already
   supersedes both for this surface — the dashboard follows R-D1/R-D3.

---

## Credential handling (no credential in a URL)

The published dashboard URL used to carry `?token=<project_token>`. A credential
in a URL is shareable by construction — address bar, browser history, the
`Referer` header, every proxy log, and any link a viewer copies — so the pattern
is now refused at three depths, each covered by a test
(`tests/credentials.test.mjs`):

1. `resolveConfig()` never reads a credential from the query string. A legacy
   `?token=…` link sets `urlTokenIgnored`, the app strips the parameter out of the
   address bar (`history.replaceState`) and tells the viewer to sign in;
2. `apiUrl()` — used by every REST call *and* the SSE stream — throws rather than
   build a URL carrying one, including when `apiBase` itself was pasted out of a
   tokenized link;
3. the SSE reader (`fetch()` + a `ReadableStream` reader) sends the credential in
   the `Authorization` header, which is why `EventSource` (which cannot set one)
   is gone.

Where state is held: non-secret connection settings (`apiBase`, `projectId`,
`offline`) live in `localStorage`; **credentials** — the project token from
Settings, and the client view's node credential — live in `sessionStorage`, i.e.
one tab, gone when it closes, so a copied link never carries them.

## Your node (client view)

`GET /v1/nodes/{node_id}/telemetry/recent`, authenticated with the node's
**own** credential: the per-node `node_token` the installer left at
`/var/lib/termini/identity/node.json` (0600) when the node registered. The panel
shows the registration state (`pending` / `registered` / `degraded` /
`not reporting`, derived with contract §1.3's 15 s/45 s thresholds), when the node
was last seen, and the first telemetry: heartbeat `seq` + timestamp, CPU, memory,
uptime, load, node runtime facts and service status — refreshing in place every
5 s with no page reload, and never using the project token.

It is deliberately **one node, not a fleet**. Under the frozen enrollment contract
(task `t_074b1a14`, option (a) provider-issued single-use tokens) a client's only
read credential is that node token, which reads exactly one node (the controller
refuses a cross-node id with 403). Client isolation therefore comes from the
credential itself: there is no client identity store and no client-authenticated
list, by decision — building one would reintroduce the ruled-out option (b).

A client with nothing enrolled yet gets the instructive empty state: the
documented one-liner, the `termini-node-status --watch` confirmation, and exactly
what they will see once their node registers.

`dashboard/evidence/run-client-view-evidence.sh` runs the whole client path with
no GCP and no stub (real controller process, the documented one-liner, the real
node daemon, the dashboard on a second origin). The screenshots and the network
capture under `dashboard/evidence/` come from that run.
