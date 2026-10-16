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

> **Correction, 2026-10-16 (same day, post-pivot).** This verdict was written against
> the inference split of D9 (`t_aa7a70f9`) and merged at 19:25 UTC. At **19:08 UTC the
> owner's pivot landed** (`#177` / `t_3fcee90b`), which supersedes D9 and flips the
> SHIPPED `TERMINI_INFERENCE_BACKEND` on Node C's agent-facing front door to
> **`gemini-proxy`** — `infra/inference/README.md`: *"the agent port offers the
> forwarded backend too … a request to `10.10.0.13:8080` is answered by the managed
> model unless the node was explicitly configured with the `local` fallback"*, with
> egress `external`. Two claims below relied on the split's on-prem agent path and are
> **wrong as written for the shipped configuration**; they are corrected in place
> (§1, §2), the bottom line is re-scoped to the diagnosis path (§5), and the policy
> ruling this raises is recorded as OPEN rather than decided (§6).

## 1. The framing (decided, recorded)

The shipped configuration is a **split**, not a single decider (the user's Q1b
decision of 2026-10-15, already landed in the docs):

- **Diagnosis (the operator's decision step)** — Gemini on Vertex AI, provider-only,
  on the controller. This is the one model call that leaves the premises.
- **Agents + intent extraction** — Node C's agent-facing front door (`:8080`, underlay
  `10.10.0.13`). **Since the pivot this port ships `gemini-proxy`**, so this traffic is
  FORWARDED to `aiplatform.googleapis.com` by default; the on-prem path (Gemma 3 4B IT
  under `llama.cpp`, CPU-only, alias `gemma`) is the *one-variable fallback*
  (`termini-inference-switch local`), **not the shipped value**. The split's premise
  that "that port offers no forwarded backend" no longer holds.

This is framing (a) from the card — the decider is a managed model service reached
from the customer's side of the boundary — **scoped**: the premises-boundary claim
("no C2 egresses") is what actually holds; the diagnosis prompt itself is C0 and is
permitted to leave (the user's Q1b).

## 2. What leaves the premises — quantified

| Item | Class | Content | Destination |
|---|---|---|---|
| Diagnosis prompt | **C0 only** | rule/metric context (the firing rule, the metric, its value and threshold) + the instance inventory (`target_resource.instances[]`) — **no workload payloads, no logs, no prompts, no credentials** | `aiplatform.googleapis.com` |
| Diagnosis response | model output | `gemini-2.5-flash` tokens (an action choice from the allowlist + rationale) | returns to the controller |
| Agent / intent traffic | **C2 (customer payloads)** | forwarded to the managed model on Node C `:8080` while the shipped `gemini-proxy` backend is selected (it carries whatever the agent asks); on-prem **only** under the `local` fallback | **`aiplatform.googleapis.com` in the shipped configuration** — `never leaves` holds **only** under `local` |

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
customer data reaches Google on this (diagnosis) path.** Note the scope: this claim is
about the OPERATOR's diagnosis path. The **agent-facing** path is a different surface
and, since the pivot, is forwarded by default — see §1/§2, and §6 for the ruling it
needs. That claim is verified at the
application layer (2026-10-15 evidence + this session's test re-run), and the
enforcement is code, not prose (`externalModelCall()` + `egress.py`). The raw
byte-for-byte wire capture and a fresh live adversarial re-run remain **unverified**
pending an approved live-fleet window (items in §4).

## 6. Open owner ruling (raised 2026-10-16, post-pivot)

The pivot traded an on-prem guarantee for a working demo: Node C's agent-facing front
door now ships `gemini-proxy`, so **agent traffic — which can carry customer payloads
— leaves for `aiplatform.googleapis.com` unless the `local` fallback is selected**.
The governance sentence this repo has asserted in several places ("your data never
leaves") therefore now depends on a configuration choice, not on the architecture.

What is verified is limited and stated as such:
- the DIAGNOSIS path's C0-only substitution is enforced in code (this document, §2/§5);
- the agent path's destination is `aiplatform.googleapis.com` when `gemini-proxy` is
  selected — that is a property of the shipped configuration, and it is the opposite
  of what §1/§2 said before this correction.

Open questions for the owner (not decided here, and no worker should decide them):
1. Is agent-facing egress of C2 payloads acceptable for the demo footprint, or must the
   demo run Node C with `local` (on-prem weights, slower) and keep `gemini-proxy` for
   the provider-only diagnosis surface?
2. If egress is acceptable, which sentence in the deck / narrative / spec answers is
   restated, and how, so the on-prem claim is not read as covering agent traffic?

Until (1) is answered, the honest reading is: **the demo's diagnostic loop keeps its
C0-only property; the agent surface does not have an on-prem guarantee in the shipped
configuration.**
