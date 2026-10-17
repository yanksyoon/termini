# Forwarded decider — data-governance red-team verdict (re-run)

Date: 2026-10-16 (UTC), live re-run 2026-10-17 · Owner: `architect` (t_8e907685) ·
Re-runs: t_23a76b8a (the original governance red team) against the **forwarded**
diagnosis path.

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
>
> **Live re-run, 2026-10-17.** The standing fleet was raised by the operator
> (controller 34.93.190.64, node-a, node-c running; node-b terminated). The two
> §4 items that were "unverified pending an approved live window" are now run and
> recorded (§3 live blocks): a byte-for-byte wire capture of the forwarded
> diagnosis call, and a fresh adversarial C2-injection re-run against the
> **deployed** guard. What remains unverified is named in §4.

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
| Diagnosis prompt | **C0 only** | rule/metric context (the firing rule, the metric, its value and threshold) + the evidence window (20 telemetry samples) + the instance inventory (`target_resource.instances[]`) — **no workload payloads, no logs, no prompts, no credentials** | `aiplatform.googleapis.com` |
| Diagnosis response | model output | `gemini-2.5-flash` tokens (an action choice from the allowlist + rationale) | returns to the controller |
| Agent / intent traffic | **C2 (customer payloads)** | forwarded to the managed model on Node C `:8080` while the shipped `gemini-proxy` backend is selected (it carries whatever the agent asks); on-prem **only** under the `local` fallback | **`aiplatform.googleapis.com` in the shipped configuration** — `never leaves` holds **only** under `local` |

The diagnosis request is the **same 20-sample prompt shape** measured at 12,748
tokens under Gemma's tokenizer — but here it carries only C0 substitutions (the
`externalModelCall()` guard refuses anything else before `send()`). Raw workload
data (C2) does **not** reach Google on this path.

On the live run the diagnosis request body (the OpenAI `chat/completions` call the
decision loop hands to the provider listener on loopback `:8085`) measured
**76,062 bytes**, of which Gemini counted **28,527 prompt tokens** and returned
**424 completion tokens** in **2.31 s** (a node-c `oom-guard-01`, §3). The response
body returned to the controller measured **2,158 bytes**.

## 3. Verified (application-layer + live wire capture)

**Application layer (2026-10-15 window, `t_f466824c`).**

- The audit row for a forwarded decision reads
  `egress=external destination=aiplatform.googleapis.com`, `answered_by=forwarded-managed-model`,
  `model=gemini-2.5-flash` — from `infra/inference/evidence/simulated-gpu-node-2026-10-15.md`
  (15 verbatim capture blocks).
- The forwarded response is labelled **in-band** — `X-Termini-Backend: gemini-proxy`,
  `X-Termini-External-Destination: aiplatform.googleapis.com`, `X-Termini-Egress: external`,
  `X-Termini-Answered-By: forwarded-managed-model`, and the `termini_backend` body block —
  so a forwarded answer cannot be read as a local one.
- `externalModelCall()` (`controller/app/operator/external_call.py`) asserts the frozen
  template version and the C-class of every substituted field **before** `send()`; a
  violation raises `ExternalCallViolation` and the loop falls back deterministically
  (`schema_violation`), fail-closed. Destination-agnostic — it applies on the forwarded
  path exactly as on the local one.
- `controller/app/operator/egress.py` classifies the destination (`external`) and the
  audit row records it. A forced Vertex failure returns HTTP 503 and the loop records
  `decision_source=fallback` (`reason=inference_unavailable`) — verified 2026-10-15.
- Re-run this session: `tests.test_external_model_call` → **22 OK**;
  `tests.test_operator_egress` → **21 OK**.

**Live wire capture (2026-10-17, raised fleet).** A fresh oom injection on node-c
(injection `01M55MKDWKYFB036G2T6X71870`) fired `oom-guard-01`, the loop made its
diagnosis call, and two `tcpdump`s captured it:

- `lo` port 8085 (plaintext, the decision loop → provider listener): the full
  diagnosis request and response, byte for byte. Request `Content-Length: 76062`
  (the C0-only prompt below, request id `01M55MKPTXXEFCYPKM1N19ZJGH`); response
  `Content-Length: 2158` (the model's JSON decision).
- `ens4` (the controller's uplink, all `tcp/443`): the TLS connection that carries
  the forwarded call, identified by its ClientHello SNI. The only
  `*.googleapis.com` SNI in the window is `aiplatform.googleapis.com`, to a Google
  `1e100.net` endpoint:

  ```
  19:14:45.995343 IP termini-controller…42752 > lcbomp-in-f95.1e100.net.https:
      … ClientHello SNI = aiplatform.googleapis.com … length 517
  ```

**What the captured prompt actually contains (C0, verified by inspection of the
bytes).** The user turn of the captured request is the frozen decision input —
`trigger` (metric/value/threshold/fired_at), `state` and 20 `evidence` samples
(CPU/RAM/disk/net/pressure/oom counters, service and process lists), `target_resource`
(one node id + its instances), `allowed_action_space`, `derived` (window deltas). A
signature scan over the captured prompt bytes found **no** email addresses, **no**
private-key material, **no** raw MAC addresses, and **no** raw host IPs; the C1
identifiers are already transformed (`hostname`/`mac_address` are 16-hex truncated
SHA-256, `ip_local` is `10.10.0.0/24`-form). This is the same content the
`externalModelCall()` audit row records leaving.

**The enforcement trail for that one call (verbatim):**

```
operator_logs: externalModelCall before endpoint=http://127.0.0.1:8085
  backend=gemini-proxy egress=external model=gemini-2.5-flash
  destination=aiplatform.googleapis.com answered_by=forwarded-managed-model
  outcome=ok request_id=01M55MKPTXXEFCYPKM1N19ZJGH
operator_logs: externalModelCall after  … outcome=returned request_id=01M55MKPTXXEFCYPKM1N19ZJGH
remediation_decisions: {"id":"01M55MKS6GX1FZG1YBMCA07W86","node_id":"01M4ZQ7Q2J80ATY3K32PY3PPJH",
  "rule_id":"oom-guard-01","decision_source":"model","fallback_reason":null,
  "decided_at":"2026-10-17T19:14:53.905Z"}
termini-inference-provider: {"ts":"2026-10-17T19:14:56Z","event":"inference_request",
  "backend":"gemini-proxy","model":"gemini-2.5-flash","answered_by":"forwarded-managed-model",
  "egress":"external","destination":"aiplatform.googleapis.com","path":"/v1/chat/completions",
  "status":200,"request_id":"01M55MKPTXXEFCYPKM1N19ZJGH","latency_ms":2306.07,"forwarded":true,
  "prompt_tokens":28527,"completion_tokens":424,"finish_reason":"stop"}
```

Captures: `lo8085.pcap` sha256 `530c564ad766b55bcf39bfb4ebedcd4c5a7b5ca0ac53d64c3ddb5b251f47bcc9`,
`uplink.pcap` sha256 `f8ffdcc348dbf9b1d86e40c3f8955bdc8145f4425a81aa15576211a310616c88`
(held with the task; the ASCII dumps `lo8085.txt` / `uplink.txt` are attached).

**Fresh adversarial C2-injection re-run (2026-10-17, against the DEPLOYED guard).**
The deployed `external_model_call` / `assert_allowed_classes` (imported from
`/opt/termini/controller` on the live controller) was run against one clean and six
C2/C1-injected decision payloads. A `send()` recorder proves whether transport was
reached:

| case | violations | transport | outcome |
|---|---|---|---|
| clean (C0/C1-transformed) | `[]` | called | **sent** |
| email in a C0 field | `os_name: email signature` | not called | **refused** |
| C2 field name (`ssh_private_key`) | `not on the C0/C1 allowlist` | not called | **refused** |
| raw hostname | `hostname: expected SHA-256 truncated hash` | not called | **refused** |
| raw MAC | `mac_address: expected SHA-256 hash` | not called | **refused** |
| private-key material | `kernel_version: private key material` | not called | **refused** |
| raw local IP | `ip_local: expected transformed IP` | not called | **refused** |

So on the live, deployed controller the guard **sends the clean payload and refuses
all six C2/C1 injections before any transport**, fail-closed. This closes work item 3
and the §4 item-3 gap.

## 4. Still unverified (named, not dropped)

1. **The TLS payload is not decrypted.** The uplink capture proves the destination
   (SNI `aiplatform.googleapis.com`, a Google `1e100.net` endpoint) and the encrypted
   byte stream, but not the literal Vertex `generateContent` JSON, because the link is
   TLS. This does not weaken the C0 claim: the plaintext source (the loopback
   `chat/completions` request) was captured before the proxy's deterministic
   OpenAI→Vertex translation (`proxy.py` `to_contents`/`to_generation_config`), and
   that translation strips/reformats but never adds content. The literal re-serialised
   Vertex bytes are still named here rather than asserted.
2. **The agent-facing path was not re-captured.** This verdict's live evidence is for
   the operator's **diagnosis** path only. The agent-facing `:8080` front door ships
   `gemini-proxy` (§1) and forwards C2 customer payloads by default; that path's wire
   capture is a separate red-team surface and is out of scope here (§6 table).

## 5. Bottom line

On the forwarded diagnosis path, **what leaves the premises is a C0-telemetry-only
prompt** to Gemini on Vertex AI — now proven at three layers at once: the plaintext
prompt captured on the wire (C0, 76,062 bytes), the TLS destination captured on the
uplink (SNI `aiplatform.googleapis.com`), and the audit row (`egress=external`,
`destination=aiplatform.googleapis.com`, `answered_by=forwarded-managed-model`).
**No C2 customer data reaches Google on this (diagnosis) path.** The enforcement is
code, not prose: the live guard refuses every C2/C1 injection before `send()`, and
the in-band response labels make a forwarded answer unreadable as local.

Scope caveat, restated: this is the OPERATOR's diagnosis path. The **agent-facing**
path is a different surface and, since the pivot, is forwarded by default — see
§1/§2 and §6.

## 6. Owner ruling (2026-10-16) — what the product does, and what tonight's demo does instead

**The ruling, recorded verbatim in intent:** *"the real idea is for node C to have a local
model deployed, so data never leaves. however, in this demo, since we cannot have access to
gpu nodes for node C, we have to proxy it."*

So there are two different, separately-true statements, and every claim in the deck, the
narrative, the spec answers and the demo script must be read as one of them:

| | **The product (what Termini is)** | **Tonight's demo (what is running)** |
|---|---|---|
| Node C's model | **local weights on the node** (Gemma 3 4B IT, `llama.cpp`, `backend=local`) | **`gemini-proxy`** — forwarded to `aiplatform.googleapis.com` |
| Why | that is the design: the customer's hardware runs the model, so **data never leaves** | **no GPU node is available for Node C** (L4 quota 0 in `asia-south1`), so the identical call is proxied to the managed model |
| Status of the on-prem guarantee | the product's posture | a **disclosed stand-in for the missing GPU**, labelled in-band on every response (`X-Termini-Backend`, `X-Termini-External-Destination`, `X-Termini-Egress`) |

**What follows for claims.** "Your data never leaves" is a statement about the product,
and it is the reason the local path exists. It must not be presented as a property of
tonight's footprint while that footprint forwards the call — the substitution is what
changes, not the design, and the demo now says so out loud (see `docs/demo-script.md`,
`docs/spec-answers.md`, `SUBMISSION.md`).

**The intent path is NOT forwarded — corrected.** An earlier revision of this section said
the extraction "follows the front door and is therefore forwarded"; that was wrong. The
deployment **pins** `TERMINI_INTENT_INFERENCE_BASE_URL` at Node C's own dedicated
small-context `llama.cpp` instance on `:8090` (`infra/cloudrun/setup-operator-node-c.sh` sets
`http://${NODE_C}:8090`; `infra/inference/setup-inference-surfaces.sh` sets the same;
`tests/test_inference_surfaces.py` asserts it), and that instance is **deliberately not
fronted by the proxy**. So the user-facing intent prompt is answered by the node's own
weights and does **not** leave the premises — the code default is empty, but the deployed
configuration is pinned, and it is the deployed configuration that runs.

What tonight's demo actually forwards, precisely:

| Surface | Tonight | Leaves the premises? |
|---|---|---|
| **Intent extraction** (controller → Node C `:8090`, pinned) | the node's own `llama.cpp`, not fronted by the proxy | **no** |
| **Agent traffic** (agent VMs → Node C `:8080` front door) | `gemini-proxy` → `aiplatform.googleapis.com` | **yes** — the disclosed stand-in |
| **Diagnosis** (controller → `127.0.0.1:8085`, provider-only) | `gemini-2.5-flash` on Vertex AI, C0-only substituted prompt | **yes**, by design (owner's Q1b) |

## 7. Findings named, not smoothed (2026-10-17 live run)

1. **The synthetic seeder's fake GPU telemetry egresses to Gemini (guardrail #5).**
   During the capture window the synthetic node `demo-node-a` (a seeder-fed series,
   not one of the two real nodes) reported a `gpu` sample — `{"model":"NVIDIA L4",
   "util_pct":38.0,"vram_total_bytes":"25,769,803,776","vram_used_bytes":"12,884,901,888",…}` —
   and that field is **C0-classified** (`fields.py` `GPU_FIELDS`), so it is passed
   through `externalModelCall()` and sent to Gemini in the diagnosis prompt. The real
   nodes report `gpus: []`. This is a live honesty risk for guardrail #5 ("no
   screenshot may imply local weights are running"): a dashboard rendering that sample
   would show "NVIDIA L4, 38% utilised" on a node that has no GPU. It is not a wording
   defect in the restatement (the restatement already forbids calling it GPU inference),
   and it is not this card's surface to fix — but it is named here so it is not lost.
2. **Concurrent activity on the shared fleet.** The same window recorded decisions for
   a second synthetic node (`disk-degrade-01` → `model`, and recurring
   `oom-guard-01` → `fallback/schema_violation`). The `schema_violation` rows are the
   guard refusing a non-conforming payload before send — independent live evidence of
   the fail-closed path — but they are not attributable to this card's injection and
   are noted only as context.
