# Agents panel — live acceptance on the standing fleet (2026-10-17)

Task `t_44a2b87b` ("Client dashboard: Agents panel"). Every frame below was taken
through the **published client URL** `https://termini-510609.web.app/` against the
**live controller** `https://34-93-190-64.sslip.io` (project
`01J0DEM0WARM000000000000AB`), with the controller redeployed at the merged commit
and hosting deployed from `main`. No GCP compute was started or stopped.

| | | |
|---|---|---|
| Controller tree | `/opt/termini` = `f1c947a` (PR #231), then `cf02343` (PR #232) for hosting only — `DEPLOYED_COMMIT` read back on the VM |
| Hosting | `firebase deploy --only hosting` from `cf02343` |
| Fleet | `termini-controller`, `termini-node-a`, `termini-node-c` RUNNING (already up; untouched); `termini-node-b` TERMINATED |
| Window | 2026-10-17 18:17 → 18:37 UTC |

## (a) N agents RUNNING, matching `lxc list`

`01-agents-panel-live.png` — the panel lists **5** agents, all `running`, grouped by
node: node `ba1f1fae…` reports 5, node `33678f54…` reports **0** with an explicit
"the node reports zero running agent instances" line (not a blank panel).

`01-lxc-list-node-a.txt` — `sudo lxc list` on node-a at 18:24:17Z, the same moment:
5 RUNNING instances (4 × `termini-…` + `demo-workload`) and 1 STOPPED, which
telemetry correctly does not list (node-telemetry §4.6 reports only running).

The panel's ids are the §4.6 derived hash of those names; verified locally with the
same rule (`c4-…`, below) so the mapping is exact, not asserted:

```
7NM4JV8BSD1P8KYBYP8B7BT7CY  termini-01m4wjhwqr3b8pcav2tvqbgg-0-0
VW4XZD24288MCXGT4557CDYFF9  termini-01m4wjhwqr3b8pcav2tvqbgg-0-1
R3P229VXA4EM2QSJS38WB411C8  termini-01m55dz5fzksstj3fkf91n5hae-0-0
J2DFSZ2VRKD2EVF2ZAJFCE2H8E  termini-01m537km0trb189a6e1gz546-0-0
NNB9287WRHGFDPEZ995TKCXDNW  demo-workload
```

## (b) Deploy via Scale intent, watch the agent appear

`02a-plan-applied-provisioning.png` — the Scale intent panel with the prompt
`deploy 1 worker agent for coding tasks` applied; plan `01M55JEWVA…` accepted,
`#0 agent_vm×1 → accepted node 01M4TGD3…`.

`02b-new-agent-running.png` — the same panel after the node's next telemetry
samples: a **6th** row, `T6K2AZ0P…`, joined node `ba1f1fae…` as `running`, and the
header pill moved from `agents 5` to `agents 6` with no reload.

Caveat, stated rather than glossed: the just-applied *provisioning placeholder* rows
(`expectAgentsFromJob`) did **not** render in this run — the new row appeared only
once telemetry reported it (~50 s). The placeholder path reads
`job.allocations[].vm_ids`; the live apply answered with the instance created (the
row appeared), so the placeholder is a display refinement that did not fire, not a
data-path failure. Flagged as follow-up, not claimed as verified.

## (c) Give one agent a task, answer with the agent identified

`03-agent-task-answer-provenance.png` — row `R3P229VX…` (the only instance whose
baked runtime implements the task surface; the others answer HTTP 501 and the panel
renders that as `AGENT_REJECTED`, see below), prompt sent from the browser,
`done · 804 ms · task 01M55J…`, answer rendered under the row that produced it.

The provenance label is the endpoint's own, not a hardcoded string:

```
answered by: gemma · forwarded managed model (gemini-proxy)
answered by forwarded-managed-model at aiplatform.googleapis.com — this is NOT
local GPU inference on your node (egress external)
```

Panel-level header pill after the answer: `inference: gemma · forwarded managed
model (gemini-proxy)`.

## (d) Restart one agent from the panel, with the human-audited record

`04a-restart-confirm.png` — the confirmation dialog ("Restart agent VW4XZD24…?")
before anything was issued.

`04b-restart-result.png` — after confirming: toast
`VW4XZD24… · vm.restart · succeeded · command 01M55J5TFQ…`, and the row is
`running` again with RAM back to 52.3 MiB (fresh boot; 150 MiB before).

`04d-audit-record.txt` — the controller's own rows, read from `/data/controller.db`:

```
command 01M55J5TFQSYSG6W9YQAPY4E56  type vm.restart  state succeeded  issued_by human
payload {"vm_id": "VW4XZD24288MCXGT4557CDYFF9", "action": "restart"}
result  {"result": "ok", "detail": "operation Success … action=restart; state Running -> Running", "latency_ms": 2109}
log     "- human - vm.restart VW4XZD24288MCXGT4557CDYFF9 -> ok (2109 ms)"
log     "- human - vm.restart issued on node 01M4TGD31WXH97JAA5S6A4R10D"
```

Same envelope as a node action: a `commands` row (`queued → running → succeeded`),
one content-free operator-log line, the client-known `vm_id` only — no instance
name on the wire or in the record.

## (e) Honest label and explicit empty state

Honest label: frame 03 above (forwarded managed model, named as forwarded).

Explicit empty states captured live: node `33678f54…` renders "the node reports zero
running agent instances" (frames 01, 02b) and a node that omits `lxd_instances` is
rendered as "inventory not reported", never as zero. The **panel-level** "No agents
yet — Deploy one with Scale intent" state was **NOT CAPTURED live**: the standing
fleet has agents on node-a in this window. It is covered by
`dashboard/tests/agents.test.mjs` (omitted-vs-empty, zero-vs-unknown) and by the
mock's `--agents none` mode, but not by a screenshot from this run.

A second real edge state was observed live and is captured in text only: agents
whose runtime predates the task surface answer `HTTP 501`, and the controller
turns that into `503 AGENT_REJECTED` naming the vm_id — the panel renders that as a
task `error`, not as a stuck `queued`. No screenshot of that frame.

## Anything else not verified

- The restart/stop/start **stop** path was not exercised live (a stopped agent
  disappears from telemetry by contract; the retained-row labelling is unit-tested
  only).
- `lxc list` was captured on node-a only; node-c reports zero instances and its
  `lxc list` was not captured.
- The apply in (b) created one instance and was **not** released, so node-a
  carries it until the fleet is torn down or capacity is released.
