# Node C serves Gemma — pinned GGUF, raw decision latency, and the live proof (2026-10-13)

Verification run in project `termini-510609`, zone `asia-south1-a`.
Question: can Node C (`termini-node-c`, `n2-standard-8`, CPU-only) serve **Gemma 3 4B IT** —
Google's own open-weights model — as the operator's decision model, and what does a real
decision cost?

**Answer: yes, and it fits.** Gemma is now the pinned, served model on Node C (alias `gemma`);
a real 20-sample decision is **402 s (6.7 min)** — ≈1.8× faster than the DeepSeek-R1 distill
it replaced (720.6 s), and its 12,748-token prompt **fits** the shipped `--ctx-size 16384`
where the old 8,973-token request did not fit `--ctx-size 8192`.

## 1. The pinned GGUF, and how the digest was verified

| | |
|---|---|
| alias | `gemma` |
| artifact | `unsloth/gemma-3-4b-it-GGUF` / `gemma-3-4b-it-Q4_K_M.gguf` (Gemma 3 4B IT, Q4_K_M) |
| size | **2,489,894,016 bytes (2.32 GiB)** |
| sha256 | `04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19` |
| pinned in | `image/models.lock.json` (`aliases.gemma`) |

The digest was verified three independent ways, and the loop was closed on the artifact the
node actually loads:

1. **Publisher API** — `huggingface.co/api/models/unsloth/gemma-3-4b-it-GGUF?blobs=true`
   reports `lfs.sha256 = 04a43a22…a7d19` for that file.
2. **Local hash of the download** — `curl` fetched the file (2,489,894,016 bytes, matching the
   API's size exactly) and `sha256sum` returned `04a43a22…a7d19`.
3. **The bytes the node serves** — the GGUF was uploaded to the project's own bucket and then
   **re-downloaded from `https://storage.googleapis.com/termini-510609-models/gemma-3-4b-it-gguf/…`**
   and hashed: `04a43a22…a7d19`, size 2,489,894,016. Node C then fetched the same URL and its
   own `sha256sum` matched (`/var/lib/termini/models/gemma-3-4b-it-Q4_K_M.gguf.sha256`).

No digest was pinned until step 3 matched — pinning before confirming the served artifact is
exactly the corruption the pin exists to catch.

## 2. What is baked vs fetched (D3)

* **Baked (image pipeline):** `image/models.lock.json` gains the `gemma` pin; the step
  `image/pipeline/provision/40-models.sh` stages it to disk at *bake* time from the lock's
  record (a build-time fetch; a bake is not a first boot).
* **Fetched (live Node C first boot):** the shipped cloud-init script
  `infra/gcp/cloud-init/llama-serve.sh` (rendered by Terraform from `variables.tf`) downloads
  the pinned GGUF **from the project's own bucket** and aborts if the sha256 differs. It never
  touches `huggingface.co`. The bucket object was populated by a one-time admin upload of the
  verified artifact; the bucket is project-owned (`gs://termini-510609-models/`).
* The deployed Node C is Terraform-provisioned from stock Ubuntu 24.04 + cloud-init (it does
  **not** use the `termini-node-os` bake), so the first-boot fetch from the bucket is the
  D3-relevant path for this node and it is the path that was exercised.

## 3. The server

| | |
|---|---|
| node | `termini-node-c`, `n2-standard-8` (8 vCPU / 32 GiB), no accelerator |
| unit | `termini-llama-server.service` → `/opt/termini/llama.cpp/bin/llama-server` |
| ExecStart | `--model /var/lib/termini/models/gemma-3-4b-it-Q4_K_M.gguf --alias gemma --host 0.0.0.0 --port 8080 --ctx-size 16384 --parallel 1 --threads 8 --reasoning-format none` |
| health | `curl http://127.0.0.1:8080/health` → **200** |

`/v1/models` (identity — see §5): `id=gemma`, `n_params=3,880,263,168`, `n_vocab=262,208`,
`n_ctx_train=131,072`, `ftype=Q4_K - Medium`, `n_ctx=16384`.

## 4. Raw decision latency (the point of the task)

The request is the **real decision call**: built with the repo's own
`termini.decision.prompt.build_messages` + the controller's `build_decision_input`, over the
telemetry shape `console/seed_demo.py` streams, with the controller's real evidence window
(`samples_asc[-EVIDENCE_SAMPLE_CAP:]`, cap 20). Both runs were **cold** (`cache_n=0`) on a
freshly restarted server, against `--ctx-size 16384 --parallel 1 --threads 8`.

| request | prompt tokens | generation | prefill | gen | wall clock |
|---|---|---|---|---|---|
| decision, **1-sample** evidence window | 2,553 | 102 tok | 34.6 tok/s | 10.3 tok/s | **83.7 s** |
| decision, **real 20-sample** evidence window | **12,748** | 142 tok | 33.2 tok/s | 8.0 tok/s | **402.1 s (6.7 min)** |

Raw `timings` from llama.cpp for the 20-sample call:

```
prompt_n=12748  prompt_ms=384511.0  prompt_per_second=33.15
predicted_n=142 predicted_ms=17521.5 predicted_per_second=8.05
finish_reason=stop
```

Both completions were well-shaped, guardrail-acceptable decisions. The 20-sample one:

```json
{"schema_version":1,"request_id":"01M4TD38FYBV4BQ8DAQRWAJ8HV","action_id":"instance.restart",
 "params":{"instance_name":"heaviest","force":true,"timeout_s":30},
 "confidence":0.87,"rationale":"Host RAM at 94.7% of total with one instance holding 29%; restarting the heaviest instance reclaims its memory."}
```

**Tokenizer note (why 12,748 here vs 8,973 for the DeepSeek run).** The prompt *text* is the
same 27,292 characters; Gemma's tokenizer (vocab 262,208) simply emits ≈1.42× more tokens than
the DeepSeek distill's (vocab 128,256) — the same ratio holds for the 1-sample prompt
(2,553 vs 1,983). So this is a tokenizer difference, not a different request. The real prompt
now **fits** (`--ctx-size 16384`, 3,636 tokens of headroom); under the old
`--ctx-size 8192 --parallel 2` it would be rejected with HTTP 400.

Compared with the DeepSeek-R1 distill it replaced (same prompt, same harness):
prefill 33.2 vs 13.6 tok/s (**2.4×**), generation 8.0 vs 2.0 tok/s (**3.9×**), wall clock
402 s vs 720.6 s (**1.8×**).

## 5. Proof Gemma is the model actually serving

Correlated as tightly as the artifacts allow; each link is a different, independent surface:

1. **Server model list** — `/v1/models` reports `id=gemma`, `n_params=3,880,263,168`,
   `n_vocab=262,208`, `n_ctx_train=131,072`, `ftype=Q4_K - Medium`. Gemma 3 4B's real
   parameter count (3.88 B) and vocabulary (262 k) — not the 8 B / 128 k of the DeepSeek
   distill, so this cannot be the old model.
2. **Response envelope** — the chat-completion body carries `"model": "gemma"` and
   `system_fingerprint: b1-7fe450e19` (llama.cpp built at the pinned commit).
3. **Server journal** — `load_model: loading model '/var/lib/termini/models/gemma-3-4b-it-Q4_K_M.gguf'`
   at unit start, `n_threads = 8`, `n_slots = 1`, `n_ctx_slot = 16384`.
4. **On-node digest** — `sha256sum /var/lib/termini/models/gemma-3-4b-it-Q4_K_M.gguf` =
   the pin `04a43a22…a7d19`.

**The controller's decision path produced a `decision_source=model` row.** The operator loop
fired `disk-degrade-01` on a synthetic node and the controller called Node C:

```
remediation_decisions: rule_id=disk-degrade-01  decision_source=model  confidence=0.85
                       action_kind=instance.stop  decided_at=2026-10-13T11:03:51.258Z
```

Correlation:
* the controller's own log: `operator.loop node … rule disk-degrade-01 FIRED: … fired=2026-10-13T11:03:51.258Z`
  — the same timestamp as the decision row's `decided_at`;
* Node C's journal: `slot launch_slot_: id 0 | task 259 | processing task` at **11:03:51**,
  ending `prompt eval … 1950 tokens (33.78 tok/s)` / `eval … 136 tokens (10.13 tok/s)` /
  `total time = 71051.84 ms`;
* the request arrived on the underlay from the controller's address:
  `10.10.0.13:8080 ← 10.10.0.2:45472`.

So: the decision row's timestamp, the controller's fire log, and Node C's request record all
land in the same second, and the only controller in the VPC is `10.10.0.2`. The model that
answered is the one that `/v1/models` reports as `gemma`, loaded from the sha256-pinned Gemma
GGUF (items 1–4). This is a same-second + unique-source-IP correlation, not a single joined
request id — stated plainly.

The `externalModelCall()` C0-only guard remains in the shipped decision path
(`controller/app/operator/decision.py::ModelDecisionProvider._call_model` →
`controller/app/operator/external_call.py`). It is proven by the unit suite
(`tests/test_external_model_call.py`, 22 cases) and by the live outcome: the call returned
`source=model` (conf 0.85), which can only happen if the payload passed the guard — a violation
raises *before* `send()` and yields `source=fallback / schema_violation`.

## 6. Cost and teardown (proven)

```
termini-controller  asia-south1-a  n2-standard-2  TERMINATED
termini-node-a      asia-south1-a  n2-standard-2  TERMINATED
termini-node-b      asia-south1-a  n2-standard-2  TERMINATED
termini-node-c      asia-south1-a  n2-standard-8  TERMINATED
https://34-93-190-64.sslip.io/v1/health -> 000
http://34.93.190.64/ -> 000
```

Cost of this verification window ≈ **$0.51** (`node-c` ≈54 min @ `$0.4664/h` ≈ $0.42 +
`termini-controller` ≈44 min @ `$0.1166/h` ≈ $0.09). Everything raised is `TERMINATED`; the
controller URL returns `000`.

## 7. Honest caveats

1. **The `decision_source=model` row came from a second, fresh-DB instance of the same
   controller code**, not the long-lived `termini-controller`. The deployed controller *is*
   pointed at Gemma — `systemctl show termini-controller` →
   `TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080`, `TERMINI_INFERENCE_MODEL=gemma`,
   `TERMINI_OPERATOR_DECISION_MODE=model` — but it could not be re-driven, because all of its
   single-use bootstrap tokens were already spent and minting a fresh one needs a config change
   I did not make. The fresh instance ran the identical decision path against the same Node C.
2. **The deployed Node C reused its existing boot disk.** `llama-serve.sh` was run on it with
   the Gemma substitutions, so the Gemma GGUF was fetched from the project bucket and
   sha256-verified exactly as a first boot would — but a *from-scratch* boot of a brand-new node
   with the updated `variables.tf` was not separately exercised. (The script's other steps
   — build, unit install, enable — did run end-to-end.)
3. **`gs://termini-510609-models` is now bucket-level public-read** (uniform bucket-level access
   has object ACLs disabled, so a single-object grant is impossible). That is how Node C fetches
   its weights (D3 "project's own bucket"). The bucket holds only open-weight Gemma artifacts.
   Reverting to private read would need the node's service account to gain a storage scope.
4. **The live decision that produced the row used a small evidence window** (~1,950 prompt
   tokens — the window when the rule first fired), so its 71 s wall clock is *not* the
   worst-case. The 12,748-token 20-sample figure in §4 is the real ceiling, measured with the
   same harness.
5. **`--ctx-size 16384` is sufficient but not roomy**: the real Gemma-tokenized prompt is
   12,748 tokens, leaving 3,636 tokens of headroom. If the evidence window ever grew past its
   hard cap of 20 samples this would need raising.
6. The DeepSeek catalog pins were left in `image/models.lock.json` (the product treats model
   choice as a config knob); only the shipped *resident* alias changed to `gemma`.
