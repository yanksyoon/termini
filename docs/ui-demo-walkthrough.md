# UI demo walkthrough — Fleet

What this is: the per-element companion to `docs/demo-script.md` (the timed narration)
and `docs/ui-hig.md` (the design conformance). It answers, for every element on the
**Fleet** destination: what is it, what do I click, why is the number there, and where
else does the same fact show up. Bullets, not prose; written for the presenter to skim
backstage, not to read aloud.

Captured against `origin/main` @ `4928eea` (includes `cbea13d`, the raw-sample truncation
fix), served locally at 1440×900 and pointed at the live controller
(`https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB`, token
`demo-project-token`, `?synthetic=1&dest=fleet`). Screenshots in
`dashboard/evidence/ui-demo/` (numbered below). See "Not verified" for what is
pinned to code rather than to a live screen.

**Deployed since (2026-10-18, orchestrator):** Firebase Hosting was redeployed from `origin/main`
@ `e523f62` after this capture. The served assets now match `main` — `assets/styles.css`
`2ea989a7…`, `proto/variants/hig.js` `e04a7ba6…`, `proto/variants/hig.css` `6504cacafaf4…` —
and the legibility claim was re-measured on the **deployed** page (Chromium, cache disabled,
`?synthetic=1&dest=fleet`): **0 clipped sample cells of 66** at 1280×900 and at 1440×900, with
`documentElement.scrollWidth === innerWidth` (no horizontal overflow) at both widths. Labels
render in full (`CPU stall (some)`, `I/O stall (some)`, `cumulative` under the value), and the
card titles wrap (`PSI pressure (latest sample)`, `Disk saturation / inodes (latest sample)`).
So the fix is live, not only on a local bundle; the counts above are the presenter's floor —
re-measure if the fleet or the build changes.

The standing fleet as observed: **four** nodes at capture (five at the 2026-10-18 re-check —
registrations come and go, so count them live rather than off this page) — two
seeder-fabricated (`demo-node-a` compute, `demo-node-c` inference) and two **real** registered
nodes (`ba1f1fae…` 2-core compute, `33678f54…` 8-core compute, the latter running
`llama-cpp`). `?synthetic=1` reveals the two `demo` rows; the default listing hides
them while real nodes are registered (`docs/contracts/dashboard.md` §1.1).

Each element below: **Shows** / **Do** / **Why** / **From → reappears**.

---

## 1. Fleet node rows

**Shows** — one row per node: name, a registration short id, a role chip
(`compute`/`inference`), a `demo` chip (synthetic rows only), a status dot with the
word beside it (`online`/`degraded`/`offline`/`fenced`), and three compact figures
CPU / RAM / Disk. The row's only control is a `•••` overflow menu
(`dashboard/js/ui/overview.js`, `proto/variants/hig.js` `decorateCard`).

**Do** — click the row (or `Enter` on it) to open the node detail pane on the right;
`↑`/`↓` move between rows. Click `•••` to open the menu: **Charts**, **Ask AI**,
**Restart**, **Drain** (destructive, red). Read the status word aloud, not just the
dot colour.

**Why** — the row is the answer to "is the fleet healthy". The `demo` chip is the
honesty surface: a `synthetic: true` row is fabricated by the demo seeder
(`console/seed_demo.py`), registered through the same endpoint as real hardware and
labelled so a viewer can tell them apart — **do not** present `demo-node-a`/`demo-node-c`
as real registrations. On this fleet the real rows are the two hash-named compute nodes.

**From → reappears**
- `seen … ago` vs `up …` live in the node card's foot, which the HIG layout moves into
  the detail pane (not the row). `seen` = heartbeat age (`last_seen_at` vs now);
  `up` = `uptime_sec` from the latest telemetry sample (`node-telemetry.md` §4.1).
  Same two numbers feed status: `offline` at >45 s, `degraded` at >15 s.
- **RAM% is displayed but never changes status.** Status derives only from
  disk / gpu / inference / service / heartbeat — `controller/app/health.py`
  `derive_status` + `dashboard.md` §1.1/§1.3. A node at 96% RAM is still `online`
  (see `demo-node-c`); it becomes `degraded`/`fenced`/`offline` for disk, GPU,
  inference, a failed service, or a stale heartbeat — never for RAM alone.
- `gpu n/a` / `inference n/a` on a `compute` row mean **not reported / not applicable**
  (the flags are `null` for that role), **not zero**. `demo-node-c` (`inference`) shows
  `gpu ok` (reports `NVIDIA L4`) but still `inference n/a` (no `inference.server_up` on
  its sample).

---

## 2. Resource utilization — line charts

**Shows** — per-chart canvas cards: CPU utilization, Memory, Disk write latency, Network
throughput (plus GPU utilization on the inference node). Each title carries a chip
(`derived` or `raw`), the display unit, and an **Ask AI** link; the caption names the
aggregation and, when a rule fired in the window, the decision + threshold line
(`dashboard/js/ui/metrics.js`, `dashboard/js/core/series.js`).

**Do** — use the `node` selector to pick a node; the `5m / 15m / 1h / 6h` pills change
the window (5m and 15m resolve to 5 s buckets, 1h to 1 m, 6h to 5 m —
`dashboard/js/core/series.js` `windowQuery`); `all metrics` reveals the
non-primary charts (GPU VRAM, disk capacity, network latency, load, temp, inference);
`demo nodes` is the `?include=synthetic` reveal — leave it on for the demo so the
`demo`-labelled rows show. Click a point on a chart to **select that sample**, then
**Ask AI** attaches the point's timestamp and value to the chat.

**Why** — the charts are the "what is it doing" layer under the status dot. The
`derived` chip exists because the controller aggregates: the frozen wire telemetry is
**nested** (`disk[]`, `net[]`, `gpu[]`), and the contract defines **no per-tick flat
aggregate** — so the controller derives the flat series by the §1.4 nested→flat mapping
(first element of `disk[0]`/`net[0]`/`gpu[0]`) and downsamples to a bucket mean. The
chart says `derived` rather than presenting `disk[0]` as "the node's disk"
(`dashboard.md` §1.4; `series.js` `DERIVED_FROM`). `disk_write_latency_ms` is additionally
the collector's own write probe, not a kernel latency — worth saying out loud.

**From → reappears** — series come from `GET /nodes/{id}/metrics`
(`dashboard.md` §1.4); the same 19-metric allowlist is `controller/app/store.py METRICS`.
A decision marker on a chart is the same event the Incidents destination lists, and a
chart click → Ask AI seeds the assistant with `{kind: metric, node_id}` (`controller-api.md` §4.10).

---

## 3. Resource utilization — raw-sample / indicator cards

Six cards, each titled "(latest sample)" with a `raw sample` chip and a `rank N` chip.
They read the **newest raw telemetry sample**, not `/metrics` — they are per-sample
facts, not time series (`node-telemetry.md` §4.12–§4.14). A field the node did not
report renders **"not reported"**, never zero; a `cumulative` flag means "count since
boot", never a rate; `derived:` marks a ratio the panel computed (never a client-side
threshold). This is the part the owner asks about — read the metric, its unit, and its
cumulative/instant nature aloud, and know which rule each feeds.

| Card (rank) | Metrics (unit) | cumulative / instant | "leading indicator for" |
|---|---|---|---|
| PSI pressure (1) | `pressure.cpu_some_avg10`, `io_some_avg10`, `memory_some_avg10`, `memory_full_avg10` (%) | instant (kernel 10 s average, `/proc/pressure`) | `oom-guard-01 · disk-degrade-01 · latency-01` |
| Memory pressure (2) | `oom_kill_count` (#), `pgmajfault_count` (#), `mem_available_bytes` (GiB) | `oom_kill_count`/`pgmajfault_count` cumulative; `mem_available_bytes` instant | `oom-guard-01` |
| Disk saturation / inodes (4) | `disk[].io_ticks_ms` (ms), `io_in_flight` (#), `inode_used` (#), `inode_total` (#), plus derived `inode_used/inode_total` (%) | `io_ticks_ms` cumulative; the rest instant; the ratio derived | `disk-degrade-01` |
| Network errors / drops (6) | `net[].rx_errors`, `tx_errors`, `rx_dropped`, `tx_dropped` (#) | cumulative (all four) | `latency-01` |
| Transport / conntrack (7) | `tcp_retransmit_segs` (#), `conntrack_count` (#), `conntrack_max` (#), plus derived `conntrack_count/conntrack_max` (%) | `tcp_retransmit_segs` cumulative; conntrack counts instant; ratio derived | `latency-01` |
| Per-instance OOM / faults (9) | `lxd_instances[].oom_kill_count` (#), `pgmajfault_count` (#) | cumulative (both) | `oom-guard-01` (instance attribution) |

The `where` column (`disk 0`, `net 0`, `instance NNB928…XDNW`) names the **entry's real
index / id** in the sample, so a disk whose extension fields were unreadable can never
shift the next disk's numbers. On the live fleet only the synthetic nodes report
§4.12–§4.14, so only `demo-node-a`/`demo-node-c` fill these cards; the two real nodes
show "not reported" (their samples carry no §4.12–§4.14 fields).

**Do** — point at `demo-node-c` and read the pressure numbers (memory stall 72.4% some /
41.3% full) as "where the stall is, not just that RAM is high", then the OOM-kill
counter as cumulative, then the per-instance row as the *named* instance the node-level
OOM is attributed to.

**Why** — the card's "leading indicator for `<rule>`" line is the **dashboard's own
diagnostic framing** (`series.js` `CHART_GROUPS[].rule`). The frozen detector does
**not** threshold any §4.12–§4.14 field — `controller/app/operator/detect.py`
`RULE_IDS = (oom-guard-01, disk-fail-01, disk-degrade-01, latency-01)` fires on the
original fields: `ram_used_bytes` ≥95% ×2 (`oom-guard-01`), `disk[].mount_ro` true
(`disk-fail-01`), `disk[].write_latency_ms` ≥50 ×5 or free ≤5% ×3 (`disk-degrade-01`),
`net[].latency_ms` ≥250 ×3 (`latency-01`) — `node-telemetry.md` §5. Say it exactly that
way: these indicators *explain* the fault; no §4.12–§4.14 metric is a rule threshold in
this build. The one place the counters are consumed by the controller is the decision
step's evidence prompt as per-window **deltas**
(`controller/app/operator/decision.py` `_CUMULATIVE_COUNTERS`,
`external_call.py` `_DERIVED_KEYS`), not as a trigger.

**From → reappears** — the same latest sample (`GET /nodes/{id}` → `metrics_latest`)
feeds the node card's health flags, the Agents panel, and these cards
(`dashboard.md` §1.2). `lxd_instances[].oom_kill_count` is the per-instance twin of the
node-level `oom_kill_count`.

---

## 4. Agents panel

**Shows** — one row per instance a **real** node reports in `lxd_instances[]`, grouped by
node. Each row: the `vm_id` (26-char Crockford id), state badge, and facts — CPU, RAM
(`used / limit`), disk used, state age, restarts — then the fields the contract does not
carry, each labelled **"not reported"**: `image alias`, `IP`, `vCPU`
(`dashboard/js/ui/agents.js`, `core/agents.js`, `dashboard.md` §4.6).

**Do** — read the id, not an instance name: `vm_id` is a one-way hash
(`base32(sha256("termini-vm-id:" + name))`, `node-telemetry.md` §4.6); the LXD instance
name, IP and image alias are **not on the wire**, so the panel shows "not reported" and
never guesses. Actions (Start / Stop / Restart / Give a task…) are human-issued commands
audited exactly like a node Restart/Drain.

**Why** — there is **no second inventory**: the panel is each node's `lxd_instances[]`
from telemetry. On the live fleet that is the real node `ba1f1fae…`, which reports six
running instances; `33678f54…` reports an empty array ("zero running"); the two `demo`
rows are excluded (synthetic). Note the honesty cases: an **omitted** array renders
"inventory not reported" (never "no agents"), and a node you stop keeps its row labelled
"stopped by you".

**From → reappears** — same `lxd_instances[]` as the per-instance OOM/faults card (§3);
the plan you `Apply` under Scale intent lands here as `provisioning` rows until the node
reports them (hashed client-side with the §4.6 rule), so the plan's outcome appears in
this panel, not just a toast (`dashboard.md` §4.3/§4.6).

---

## 5. Manual intervention — Node actions and Scale intent

**Shows** — two human-issued controls against the selected node, plus the intent flow.
**Node actions**: Restart node → `POST …/actions/reboot` (`node.reboot`, approval
`required`), Drain node → `POST …/actions/drain` (`node.drain`, approval `required`,
reason `disk_degradation | disk_failure | maintenance | manual`). **Scale intent**:
prompt (or structured spec) → `Validate intent` → guardrail-validated plan → `Apply plan`.

**Do** — pick the node (the detail pane's controls follow the selection), then Restart or
Drain (both behind a confirm dialog naming the node and the consequence). For Scale
intent, type a plain-English request (e.g. "Deploy 3 worker agents and 1 fast LLM"),
`Validate intent`, read the plan's per-class `clamped`/`rejected` lines, then `Apply plan`.

**Why** — every one of these is **human-issued and audited the same way as the operator's
own actions**: `actor: human` in `guardrail_audit`, `Idempotency-Key`, the standard error
envelope (`dashboard.md` §4). The guardrail validator (`dashboard.md` §4.3
"`guardrailClamp()`"; the real code is `controller/app/health.py` `clamp_to_node_capacity`
+ `termini/intent/validator.py` `clamp_validate`) clamps each agent-vm field to the
node's allocatable **per-node capacity** — RAM → vCPU → disk, in that order, against
`ram_free_gb`/`cpu_free`/`disk_free_gb` — and rejects a class whose clamped value falls
below the type floor. So a `128 vCPU / 512 GB` worker comes back clamped, not crashed.

**From → reappears** — a Restart/Drain lands in the operator log stream (§7) and the
decision surface as a `human` command. An applied plan's instances appear in the Agents
panel (§4); the clamp's `from → to` decisions are in the audit record.

---

## 6. Pending AI remediation

**Shows** — the approval queue for **operator/playbook-initiated** commands whose
`approval` is `required` (`node.drain`, `node.reboot`). In the HIG layout it is not a
permanent queue: a count badge on the **Incidents** destination, and a banner there
holding the shipped Approve / Deny controls. Each item carries a **300 s** clock —
`pending_approval` auto-denies 300 s after issue (`state: denied`, `reason:
approval_timeout`; `dashboard.md` §4.4; `proto/variants/hig.js` `syncApprovals`).

**Do** — Approve releases the command (`approved → queued → acked → running →
succeeded|failed`); Deny kills it and the operator records the denial (no re-issue of the
same command for the same `sample_seq` within 60 s). The countdown is honest about its
clock: it says "counted from when this tab first saw the request" because only the
controller knows the true issue time.

**Why** — the contrast to say out loud: **assistant-initiated** actions (issued through
`Ask AI` → `POST /v1/projects/{pid}/assistant/ask`) execute **on guardrail admission**,
never entering `pending_approval`; they are recorded `initiated_by: assistant` and land
on the decision surface instead of this queue (`dashboard.md` §4.4, `controller-api.md`
§4.10). Approval is keyed to the **action**, not a fixed rule→action table.

**From → reappears** — the same commands appear in the operator log stream (the `status:
pending_approval` line is what populates the queue, `main.js` `trackRemediation`). Live
capture note: the queue was empty ("No pending approvals") during capture — the demo's
`oom-guard-01 → instance.restart` and `disk-*` remediations are `auto`, so no
`pending_approval` item was in flight; the mechanism is verified from code + contract,
not from a live pending item.

---

## 7. Operator log sheet / Ask AI / Settings — where they are

- **Operator log** — a right-edge sheet opened from the Fleet bar's `Operator log`
  button (or Settings → Diagnostics). It is the shipped raw stream ("Live AI-operator
  log") moved into a `<dialog>`: severity/node/actor filters, pause, clear, the
  `truncated` resume marker (`dashboard.md` §2).
- **Ask AI** — the `✦ Ask AI` button in the bar opens the docked assistant panel; every
  element that has records behind it (node, chart, pending remediation, decision, audit)
  carries its own Ask AI affordance that seeds the chat with `{kind, id}`
  (`controller-api.md` §4.10).
- **Settings** — a sidebar destination. Appearance (System / Light / Dark), Connection
  (the controller URL + project id + token dialog, and the `Web terminal` link when a
  terminal base is injected), Enroll a node (client view), Diagnostics (operator log
  sheet).

---

## Screenshots (all 1440×900)

1. `dashboard/evidence/ui-demo/01-fleet-landing.png` — sidebar (Fleet/Incidents/Settings),
   the four node rows (two `demo`-chipped, two real), and the empty detail pane. Look at:
   the `demo` chip vs the hash-named real rows, and the status word beside each dot.
2. `02-node-detail-charts.png` — `demo-node-c` selected; detail pane with the status line
   and the line charts (`derived` chips + units + Ask AI). Look at: the `derived` chip and
   the `mean over 5 s buckets` caption.
3. `03-raw-sample-cards.png` — the six §4.12–§4.14 indicator cards for `demo-node-c`:
   `raw sample` / `rank N` chips, `cumulative`/`derived:` flags, the `disk 0` / `net 0` /
   `instance NNB928…XDNW` `where` column. Look at: the three-line rows and the
   "leading indicator for" captions (0 clipped cells measured at this width).
4. `04-agents-panel.png` — one row per `lxd_instances[]` entry (the real node
   `ba1f1fae…`'s six instances), `not reported` for image alias / IP / vCPU.
5. `05-manual-intervention.png` — Node actions (Restart/Drain) + Scale intent
   (Validate intent / Apply plan) + Pending AI remediation ("No pending approvals").
6. `06-operator-log-sheet.png` — the operator log sheet slid over the page.
7. `07-settings.png` — the Settings destination (Appearance / Connection / Enroll / Diagnostics).

## Not verified

- **The screenshots predate the hosting deploy.** They were taken from a local
  `origin/main` bundle served against the live controller, not from the deployed page —
  at that time Firebase still served the old five-track `.sample-row` (`assets/styles.css`
  sha256 `2ba74819…`). That has since been corrected: hosting was redeployed from
  `e523f62` (see the note at the top) and the same measurement now passes **on the
  deployed page** (0 clipped of 66 cells at 1280×900 and 1440×900). The screenshots still
  show the fixed layout, so they remain representative; the numbers, not the images, are
  what pin legibility, and both now agree.
- **§4.12–§4.14 only render on the synthetic nodes.** The two real nodes report none of
  those fields, so their indicator cards read "not reported"; the filled cards shown in
  screenshot 3 are `demo-node-c`'s. Say this if a real node is selected on stage.
- **No live `pending_approval` item** was in flight during capture (queue empty); the
  approval banner and 300 s auto-deny are described from `dashboard.md` §4.4 + the HIG
  `syncApprovals` code, not from a rendered banner.
- **The node-side LXD path is real only on the real nodes.** `ba1f1fae…` reports six
  running `lxd_instances[]`; the two `demo` nodes are seeder-fabricated (`synthetic: true`).
  Do not claim the demo nodes' LXD inventory is real hardware.
- The deployed page's title is `🐴 Termini — Operations Dashboard` (a deploy-time
  decoration not present in any git commit); `origin/main` is
  `Termini — Operations Dashboard`. Cosmetic only.

## Contradictions found

None between `docs/demo-script.md`, `docs/ui-hig.md` and the shipped Fleet UI at
`4928eea`. One framing caveat recorded above (§3): the raw-sample cards' "leading
indicator for `<rule>`" is a dashboard-side diagnostic label — the frozen detector
thresholds the original fields, not the §4.12–§4.14 metrics.
