# t_d1aa35c8 — AI assistant chat + per-element "Ask AI" evidence

Everything here is a real capture from a running dashboard. No pixels were
composited and no answer text was hand-written into a screenshot.

## Screenshots

| file | what it shows |
| --- | --- |
| `t_d1aa35c8-chat-seeded-incident.png` | The docked panel opened from an incident card's own **Ask AI**: the chip reads `incident · oom on the node · decision 01M5404J6W…0008`, the incident card stays visible beside it (the panel shifts the page, it does not cover the panel on a laptop), and the three per-element suggested follow-ups are shown. No answer text is present. |
| `t_d1aa35c8-chat-answer-citations.png` | The same incident chip still attached across a follow-up question, with a grounded answer and its citations as buttons (`incident · oom-guard-01 on ram_used_bytes`, `decision · oom-guard-01 → instance.restart`), plus `decided_by assistant · model mock-assistant · 12 ms`. |
| `t_d1aa35c8-chat-executed-audit-id.png` | A request that asked the assistant to act. `EXECUTED — WHAT ACTUALLY RAN` renders `instance.restart`, its `command_id 01M5400X0W…0008`, its `audit_id 01M5400X0Y…000C` and an **open audit row** link; the proposal beside it is labelled `PROPOSED BY THE MODEL — NOT EXECUTED`. |
| `t_d1aa35c8-chat-error-503.png` | A typed `503 · MODEL_UNAVAILABLE` state: *"the model did not answer — no answer was invented"*, the controller's reason (`ConnectionError: inference endpoint refused the request`), the request id and a Retry. No answer bubble. |
| `t_d1aa35c8-chat-offline-fixture.png` | `?offline=1` (projector / no controller): the header badge reads `FIXTURE DATA · OFFLINE DEMO`, the answer carries `fixture data — offline demo answer — not a model answer, no controller behind it`, and the citations still jump. |
| `t_d1aa35c8-chat-live-controller-probe.png` | The browser talking to the **deployed** controller (`https://34-93-190-64.sslip.io/v1`, project `01J0DEM0WARM000000000000AB`): a node chip is attached, the answer is grounded in that node's record with a citation, `model gemma`, and no fixture badge. |
| `t_d1aa35c8-live-probe-response.json` | The raw 200 body behind the live probe (same route the browser used), kept so the screenshot's text can be checked against the wire. |

The mock screenshots were served by `python3 dashboard/mock/server.py` (the
contract-faithful mock controller); the live one by
`python3 -m http.server 8098 --directory dashboard` with `apiBase` pointing at the
deployed controller and the credential in that tab's `sessionStorage` (never a
URL).

## Live controller probe

```
POST https://34-93-190-64.sslip.io/v1/projects/01J0DEM0WARM000000000000AB/assistant/ask
Authorization: Bearer <demo project token>
{"messages":[{"role":"user","content":"Is this node healthy right now? Cite the record."}],
 "context":{"kind":"node","id":"01M4TGD31WXH97JAA5S6A4R10D"}}
-> 200 {"answer":"Yes, the node is healthy. Its status is \"online\" and its disks are \"ok\".",
        "citations":[{"kind":"node","id":"01M4TGD31WXH97JAA5S6A4R10D","label":""}],
        "executed":[],"model":"gemma","decided_by":"assistant","elapsed_ms":835,...}
```

In the browser the same click-and-ask path returned a citation button for the
attached node; the panel's citation button is wired to `jumpToCitation`, which
selects that node and scrolls its card/chart into view.

## Automated checks

```
cd dashboard && npm test          # 102 tests, including assistant.test.mjs (15) and served-page.test.mjs (1)
```

`tests/assistant.test.mjs` covers context seeding / chip lifecycle, the pending
and typed-error states, `executed[]` attribution, the offline responder's
contract shape and fixture labelling, and the incident-card click binding the
incident id + decision id. `tests/served-page.test.mjs` boots the mock over HTTP
and asserts the **served** page carries the panel and seeding surfaces, the
served modules are byte-identical to the ones the tests import, and the served
assistant route answers an incident context with the §4.10 shape and an
`incident` citation for the id the chip attaches.
