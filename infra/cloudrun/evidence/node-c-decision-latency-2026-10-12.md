# Node C hosts the decision model — raw latency measurement (2026-10-12)

Verification run in project `termini-510609`, zone `asia-south1-a`.
Question: can the CPU-only inference node (`node-c`, `n2-standard-8`) serve the
operator's real decision call fast enough for an interactive demo?

**Answer: no. A decision is minutes, not seconds — and the real request does not even
fit the shipped server context.** The wiring is real and the calls do reach Node C; the
interactive on-screen moment is carried by the deterministic fast-path (`R24`).

## The model and the server

| | |
|---|---|
| node | `termini-node-c`, `n2-standard-8` (8 vCPU / 32 GiB), CPU-only (`gpu_accelerator_count = 0`, no `/dev/nvidia*`, no `lspci` NVIDIA device) |
| server | `termini-llama-server.service` — `/opt/termini/llama.cpp/bin/llama-server`, built on the node at first boot (the deployed `termini-node-os` family member does **not** carry the inference stack; the golden-VM bake runs only `10-node-os.sh` + `60-node-bootstrap.sh`) |
| weights | `deepseek-flash` = `DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf`, 4,920,736,608 bytes, fetched at first boot (sha256-pinned; the same discipline D3 requires of a registry, applied to the weight) |
| shipped flags | `--ctx-size 8192 --parallel 2` ⇒ **4,096 tokens/slot** |
| measured flags | `--ctx-size 16384 --parallel 1 --threads 8` (to fit the real request at all) |

## The request

Built with the repo's own prompt builder (`termini.decision.prompt.build_messages`) and
`build_decision_input`, from the telemetry shape `console/seed_demo.py` streams. The
controller's real evidence window is `samples_asc[-EVIDENCE_SAMPLE_CAP:]` with
`EVIDENCE_SAMPLE_CAP = 20` (`controller/app/operator/store.py`).

| request | prompt tokens | generation | wall clock |
|---|---|---|---|
| tiny probe (prefill speed) | 10 | 8 tok @ 5.75 tok/s | 2.1 s |
| decision, **1-sample** evidence window | 1,983 | 125 tok @ 4.11 tok/s | **156.2 s** |
| decision, **real 20-sample** evidence window | **8,973** | 123 tok @ 2.04 tok/s | **720.6 s (12.0 min)** |

Raw `timings` from llama.cpp for the 20-sample call:

```
prompt_n=8973   prompt_ms=660736.4  prompt_per_second=13.58
predicted_n=123 predicted_ms=59873.0 predicted_per_second=2.04
```

The model's completion was a well-shaped decision (the guardrail would accept it):

```json
{"schema_version":1,"request_id":"…","action_id":"instance.restart",
 "params":{"instance_name":"heaviest","force":true,"timeout_s":30},
 "confidence":0.87,"rationale":"host RAM at 94.7% of total with one instance holding 29%; …"}
```

## The shipped configuration rejects the real request

With the deployed flags (`--ctx-size 8192 --parallel 2`), the real 20-sample request is
refused before a single token is generated:

```
{"error":{"code":400,"message":"request (8973 tokens) exceeds the available context size
 (4096 tokens), try increasing it","type":"exceed_context_size_error",
 "n_prompt_tokens":8973,"n_ctx":4096}}
```

## The wiring was exercised on the deployed stack

- The deployed controller was rewired and shows it: `systemctl show termini-controller`
  → `TERMINI_INFERENCE_BASE_URL=http://10.10.0.13:8080`,
  `TERMINI_INFERENCE_MODEL=deepseek-flash`, `TERMINI_OPERATOR_DECISION_MODE=model`.
- Over the underlay, from the controller VM: `curl http://10.10.0.13:8080/health` → `200`
  and `/v1/models` → `deepseek-flash`.
- Node C observed the controller's decision call: an `ESTABLISHED` socket
  `10.10.0.13:8080 ← 10.10.0.2:52526` (10.10.0.2 = the controller's underlay address), and
  served a decision-shaped request of 1,671 prompt tokens / 110 generated tokens.
- The controller's decision table gained a `decision_source=model` row
  (`disk-degrade-01`, confidence 0.71) while pointed at Node C; a later fire that raced a
  Node C restart recorded `decision_source=fallback / inference_unavailable` — the
  mandatory deterministic fallback, fail-closed.

## What this implies

1. **The demo's interactive moment must not be gated on the model.** It already is not:
   `oom-guard-01` has an explicit deterministic fast-path (threshold → `instance.restart`,
   no model call) — `architecture.md` R24. The model-driven path is real, wired, and
   disclosed as minutes-per-decision.
2. **The operator loop holds its SQLite write transaction for the whole call.** A
   multi-minute CPU decision therefore stalls telemetry ingest; the seeder's ticker
   threads hit their 15 s request timeout and die. Observed twice in this run. Any
   decision path slower than the ingest timeout, on CPU, has this failure mode.
3. **A fast, external decision model would free Node C's 8 vCPUs** — but this project's
   free-tier billing account rejects every non-TPU accelerator, so the GPU route
   (Cloud Run L4) is unavailable here. That is why the honest resolution is the
   deterministic fast-path, not a faster model.

## Cost

My footprint (`node-c` 41 min at `$0.4664/h` + `termini-controller` 32 min at
`$0.1166/h`) ≈ **$0.38**. Everything I raised is `TERMINATED` and the controller URL
returns `000`.
