# Forwarded decider — data-governance red-team verdict (re-run)

Date: 2026-10-16 (UTC) · Owner: `architect` (t_8e907685) · Re-runs: t_23a76b8a
(the original governance red team) against the **forwarded** diagnosis path.

This is a **fresh verdict**, not a reprint of the original. The original
(t_23a76b8a, quoted in `w7/verdict.md` §Bottom line) verified "no C2 leaves the
premises" while the operator loop was **deterministic** — it made no model call
at all. That premise no longer holds: since the inference split (`infra-decisions.md`
D9, kanban `t_aa7a70f9`) the operator's **diagnosis** is a model call to Gemini on
Vertex AI (`aiplatform.googleapis.com`), reached from the controller's provider-only
loopback listener (`127.0.0.1:8085`, `backend=gemini-proxy`). This verdict re-asks the
red-team question against that path: **what leaves the premises, and is it C2?**

## 1. The framing (decided, recorded)

The shipped configuration is a **split**, not a single decider (the user's Q1b
decision of 2026-10-15, already landed in the docs):

- **Diagnosis (the operator's decision step)** — Gemini on Vertex AI, provider-only,
  on the controller. This is the one model call that leaves the premises.
- **Agents + intent extraction** — Gemma 3 4B IT on Node C (`backend=local`, CPU-only),
  on-prem; that port offers no forwarded backend, so nothing it touches leaves.

This is framing (a) from the card — the decider is a managed model service reached
from the customer's side of the boundary — **scoped**: the premises-boundary claim
("no C2 egresses") is what actually holds; the diagnosis prompt itself is C0 and is
permitted to leave (the user's Q1b).

## 2. What leaves the premises — quantified

| Item | Class | Content | Destination |
|---|---|---|---|
| Diagnosis prompt | **C0 only** | rule/metric context (the firing rule, the metric, its value and threshold) + the instance inventory (`target_resource.instances[]`) — **no workload payloads, no logs, no prompts, no credentials** | `aiplatform.googleapis.com` |
| Diagnosis response | model output | `gemini-2.5-flash` tokens (an action choice from the allowlist + rationale) | returns to the controller |
| Agent / intent traffic | C2 (customer payloads) | stays on-prem on Node C `:8080` (`backend=local`) | **never leaves** |

The diagnosis request is the **same 20-sample prompt shape** measured at 12,748
tokens under Gemma's tokenizer — but here it carries only C0 substitutions (the
`externalModelCall()` guard refuses anything else before `send()`). Raw workload
data (C2) does **not** reach Google on this path.

## 3. Verified (application-layer evidence)

- The audit row for a forwarded decision reads
  `egress=external destination=aiplatform.googleapis.com`, `answered_by=forwarded-managed-model`,
  `model=gemini-2.5-flash` — from the 2026-10-15 acceptance window (`t_f466824c`,
  `infra/inference/evidence/simulated-gpu-node-2026-10-15.md`, 15 verbatim capture blocks).
- The forwarded response is labelled **in-band** — `X-Termini-Backend: gemini-proxy`,
  `X-Termini-External-Destination: aiplatform.googleapis.com`, `X-Termini-Egress: external`,
  `X-Termini-Answered-By: forwarded-managed-model`, and the `termini_backend` body block —
  so a forwarded answer cannot be read as a local one.
- `externalModelCall()` (`controller/app/operator/external_call.py`) asserts the frozen
  template version and the C-class of every substituted field **before** `send()`; a
  violation raises `ExternalCallViolation` and the loop falls back deterministically
  (`schema_violation`), fail-closed. This is the enforcement required by the card's
  work item 3, and it is **destination-agnostic** — the class assertion applies on the
  forwarded path exactly as on the local one.
- `controller/app/operator/egress.py` classifies the destination (`external`) and the
  audit row records it. A forced Vertex failure returns HTTP 503 and the loop records
  `decision_source=fallback` (`reason=inference_unavailable`) — verified 2026-10-15.
- Re-run this session: `tests.test_external_model_call` → **22 OK**;
  `tests.test_operator_egress` → **21 OK**.

## 4. Unverified / not re-run (named, not dropped)

1. **A fresh live wire capture has NOT been taken for this verdict.** The standing
   fleet is STOPPED by the user's cost decision (2026-10-15/16); the cost guard forbids
   raising it without an approved window. The evidence above is application-layer
   (audit rows, response headers, body block), captured on 2026-10-15 — **not** a
   fresh capture against the current deployed state today.
2. **No raw packet capture (pcap) of the diagnosis call on the controller's uplink**
   has been committed. The original red team proved "no C2 on the uplink" with a
   full-payload pcap while the loop was deterministic; the equivalent pcap for the
   forwarded path — showing the *exact bytes* that leave for `aiplatform.googleapis.com`
   and nothing else — has not been re-taken. The card's "byte for byte on the wire"
   bar is therefore **partially met**: the application layer is proven, the raw wire
   bytes are not.
3. **A fresh adversarial C2-injection test against the forwarded path** (inject a C2
   field into the diagnosis context and confirm the guard refuses before send) has not
   been re-run live; the unit tests (`test_external_model_call.py`) cover the refusal
   path in-process, but not against the deployed proxy.

Items 1–3 all require a live window (`needs-live-fleet`): start the seeder on the
controller, fire an `oom-guard-01`, and capture the diagnosis call end to end
(controller audit + a `tcpdump` filter on the uplink). ~$0.02–0.05 at the standing
CPU rate for a few minutes.

## 5. Bottom line

On the forwarded path, **what leaves the premises is a C0-telemetry-only diagnosis
prompt** to Gemini on Vertex AI, labelled in-band and in the audit row. **No C2
customer data reaches Google on this path.** That claim is verified at the
application layer (2026-10-15 evidence + this session's test re-run), and the
enforcement is code, not prose (`externalModelCall()` + `egress.py`). The raw
byte-for-byte wire capture and a fresh live adversarial re-run remain **unverified**
pending an approved live-fleet window (items in §4).
