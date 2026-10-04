# 02 — Model Serving, GPU Inference & Agent Runtimes

Research stream 2 of 4 for W0. Owner: research. Feeds W1 contracts: **serving API**,
**agent image**, **GPU requirements**. Status: research complete, decisions proposed.
Date: 2026-10-04. Cloud target: GCP. Node C is a **real GPU VM** (L4 or T4).

Companion inputs already frozen and honored here:
- `docs/contracts/controller-api.md` (§5 guardrails; `agent-runtime.md` is the serving hand-off).
- `docs/contracts/governance.md` §7.1 (on-prem inference = llama.cpp on Node C; external model = GCP, C0 telemetry only).

> **Read the constraints section (§7) first if short on time.** Two hard constraints
> change the design: (1) a single L4/T4 cannot host the DeepSeek V4 models named in
> `governance.md`, and (2) GPU passthrough into a *nested* LXD VM on a GCP GPU VM is the
> single riskiest infra assumption in this stream.

---

## 0. Executive summary — the shortlist

| Area | Commit to | Why |
|------|-----------|-----|
| Inference engine | **llama.cpp `llama-server`** (MIT) | Only engine that is trivial to pre-bake, runs well on a single small GPU, ships an OpenAI-compatible API with streaming/tools/JSON/metrics, no Python/CUDA compile-version roulette. |
| Alternate engine | **vLLM** (Apache-2.0) | Keep as drop-in *if* we need multi-session throughput on the L4; same OpenAI API shape. Do **not** adopt SGLang/TGI for the demo. |
| On-prem model (demo-substitutable) | **DeepSeek-R1-Distill-Qwen-14B Q4_K_M** (~9–12 GB) on L4; **8B Q4_K_M** (~5–8 GB) on T4 | Fits a single 16–24 GB GPU with KV headroom; DeepSeek-family so naming stays coherent with `governance.md`. 32B Q4 (~21 GB) is the stretch goal on L4 only. |
| Quantization | **GGUF Q4_K_M** default; Q5_K_M/Q8_0 only as quality upgrades if VRAM allows | Community-proven default; ~60% smaller than FP16 at negligible quality loss; native to llama.cpp. |
| GPU for Node C | **GCP G2 (`g2-standard-4`, 1× L4 24 GB)** preferred; N1+T4 (16 GB) only if cost-forced | L4 = 24 GB (fits 14B/32B Q4), newer Ada arch, ~2–4× T4 perf; T4 16 GB caps us at ~8B comfortably. |
| GPU presentation | **llama.cpp on Node C host, or in an LXD *container* with a `gpu` device** — not a nested LXD *VM* with vfio | LXD passes a physical GPU to containers cleanly; PCI passthrough *inside* a cloud guest to a nested VM is unsupported/high-risk. |
| Controller agent runtime | **Google ADK** (Apache-2.0) as primary; **LangGraph** (MIT) as fallback | ADK is Gemini-native and matches the GCP controller; both are permissive and well-documented. |
| On-prem agent image | **Ubuntu 24.04 + Python 3.12 + pre-pulled GGUF + llama.cpp client**, built as a golden **LXD image via cloud-init/Packer**; no cold downloads at demo time | Reproducible, offline-capable, fits the 5-day window. |
| Avoid for the demo | **TGI** (archived/maintenance), **AutoGen** (maintenance → Microsoft Agent Framework), **SGLang** (heavy for one small GPU) | Correct long-term tools, wrong risk profile for a 5-day demo. |

---

## 1. Method & scope

Survey of model-serving/inference engines (llama.cpp, vLLM, Ollama, TGI, SGLang),
GCP GPU options and passthrough, quantization, and agent runtimes/agentic-OS
packaging (ADK, LangChain/LangGraph, AutoGen, CrewAI, OpenHands; Packer/cloud-init;
image-based OS). Each area gets: what exists · maturity · license/cost · fit ·
**adopt-vs-build verdict** · integration risk · citation.

---

## 2. Model serving / inference engines

### 2.1 Comparison table

| Engine | License | Maturity (signals) | GPU story | Best for | Demo verdict |
|--------|---------|--------------------|-----------|----------|--------------|
| **llama.cpp** (`llama-server`) | MIT | ~126k★, very active; broadest hardware support (CUDA/HIP/Vulkan/WebGPU) | CUDA on L4/T4; GGUF quants; CPU fallback via `-ngl` offload | Single-GPU, low-concurrency, portable, easy to pre-bake | **ADOPT** |
| **vLLM** | Apache-2.0 | ~92k★, 2000+ contributors; PagedAttention + continuous batching | NVIDIA SM ≥ 7.5 (covers T4 and L4); AWQ/GPTQ/FP8/INT8/GGUF | High-concurrency multi-user serving | **ADOPT as secondary** (only if multi-session throughput needed) |
| **Ollama** | MIT | ~180k★; wraps llama.cpp; 1-line model pull | CUDA; auto-quant selection | Fastest "just run it" path | **ADOPT for dev, not as the demo serving path** (less control over quant/offload/`--api-key`) |
| **SGLang** | Apache-2.0 | ~20–36k★, LMSYS; RadixAttention, best-in-class low-concurrency TTFT; DeepSeek-optimized | Recommends Hopper/Blackwell; single small-GPU path is heavy to operate | High-throughput/large-cluster, DeepSeek native weights | **REJECT for demo** (install/load friction, oversized for 1 GPU) |
| **TGI** (HF) | Apache-2.0 | ~archived Mar 2026, **maintenance mode**; HF recommends vLLM/SGLang/llama.cpp | Rust/gRPC server, continuous batching | Legacy HF serving | **REJECT** (upstream itself points elsewhere) |

### 2.2 Per-engine notes

**llama.cpp — ADOPT (primary).** Single C/C++ binary, MIT, GPU via CUDA (`-DGGML_CUDA=ON`).
`llama-server` exposes an **OpenAI-compatible** surface: `POST /v1/chat/completions`
(SSE streaming via `"stream": true`), `/v1/completions`, `/v1/embeddings`,
`/v1/models`, plus **native** `/health`, `/props`, `/slots`, `/metrics`
(Prometheus, `--metrics`). Structured output is enforced by a **GBNF grammar**
(`response_format` `json_schema`), and OpenAI-style **tool calling** works for models
whose chat template supports it. Auth via `--api-key` (Bearer). Key flags:
`-m`, `-c/--ctx-size`, `-ngl/--n-gpu-layers`, `--host`, `--port` (default 8080),
`--parallel`, `--alias`, `--jinja`.
Real T4 datapoint from the project's own CUDA scoreboard: Llama-2-7B Q4_0 ≈ **44–46 tok/s**
generation on a Tesla T4 (16 GB), ~46 t/s; L4/Ada-class parts are materially faster.
- Why: minimal dependencies, fast cold start, no Python/CUDA wheel mismatch, trivially
  pre-baked into an image, and *the same process serves the OpenAI API that agents expect*.
- Risk: high-concurrency → TTFT degrades (queueing; throughput is near-flat vs vLLM).
  Mitigation: this demo is **single-user, low-concurrency**; set `--parallel 2–4` and
  short `--ctx-size` (4k–8k).
- Citations: <https://github.com/ggml-org/llama.cpp> · license MIT
  <https://github.com/ggml-org/llama.cpp/blob/master/LICENSE> ·
  server API <https://llama.app/docs/api> ·
  T4 CUDA scoreboard <https://github.com/ggml-org/llama.cpp/discussions/15013>.

**vLLM — ADOPT as secondary / fallback.** Apache-2.0, from UC Berkeley Sky Lab, ~92k★.
Needs NVIDIA **compute capability ≥ 7.5** (docs list T4 and L4 explicitly). PagedAttention +
continuous batching give multi-× throughput vs llama.cpp at concurrency > 4; at
concurrency 1 they are comparable. Heavier install (CUDA wheels / large Docker image),
so it is a fallback, not the default. Red Hat's benchmark: at peak load vLLM delivered
~35× the RPS and ~44× TPS of a tuned llama.cpp — but only under high concurrency; at
concurrency 1 llama.cpp is right there.
- Citations: <https://github.com/vllm-project/vllm> (Apache-2.0) ·
  GPU requirements (SM ≥ 7.5, T4/L4) <https://docs.vllm.ai/en/stable/getting_started/installation/gpu/> ·
  benchmark <https://developers.redhat.com/articles/2025/09/30/vllm-or-llamacpp-choosing-right-llm-inference-engine-your-use-case>.

**Ollama — ADOPT for development only.** MIT license
(<https://github.com/ollama/ollama/blob/main/LICENSE>). Wraps llama.cpp, model pull in one
command, OpenAI-compatible at `http://localhost:11434/v1` (with `/v1/responses` non-stateful
added in v0.13.3). Excellent for iterating locally; because it hides quant/offload choices and
local auth is a no-op, prefer raw `llama-server` for the demo serving contract.
- Citations: <https://docs.ollama.com/api/openai-compatibility> · LICENSE MIT.

**SGLang — REJECT for the demo.** Apache-2.0, LMSYS-hosted, powers 400k+ GPUs, day-1
DeepSeek V3/R1 support, and the best low-concurrency TTFT in third-party benchmarks
(3.7× faster than vLLM at c=1). But its own docs target H100/A100/GB200-class boxes,
install is heavier, and a single-L4 deployment is over-engineering for a 5-day demo.
Revisit post-hackathon if we move to real DeepSeek MoE weights.
- Citations: <https://github.com/sgl-project/sglang> (Apache-2.0) ·
  SGLang-vs-vLLM benchmark <https://rawlinson.ca/articles/vllm-vs-sglang-performance-benchmark-h100>.

**TGI — REJECT.** Apache-2.0 again since Apr 2024, but the repo is **archived (Mar 2026)**
and in maintenance mode; Hugging Face's own README now recommends vLLM, SGLang, llama.cpp.
- Citations: <https://github.com/huggingface/text-generation-inference> (archived) ·
  license history <https://aiwiki.ai/wiki/huggingface_tgi>.

---

## 3. GCP GPU: which card, passthrough, cost

| GPU | VRAM | Attaches to | On-demand (approx) | Fit |
|-----|------|-------------|--------------------|-----|
| **NVIDIA L4** | 24 GB GDDR6, 300 GB/s | **G2 series only** (`g2-standard-*`; 1 L4 per `g2-standard-4/8/12/16/32`) | ~$0.45–$1.00/GPU-hr (reserved→on-demand); `g2-standard-24` ≈ $2.00/hr for 2 L4 | **Preferred** — 24 GB fits 14B/32B Q4 |
| **NVIDIA T4** | 16 GB | **N1 general-purpose** (`--accelerator type=nvidia-tesla-t4`) | ~$0.35–$0.40/hr | Fallback — caps us at ~8B Q4 comfortably |

- L4 is the successor NVIDIA positions for cost-optimized inference; GCP's same-card
  comparison reports **2–4×** the performance of T4 when switching T4→L4.
- **Passthrough reality:** GCP attaches the GPU to the instance in passthrough mode, so the
  guest (Node C) sees the real GPU. LXD's `gpu` device supports `gputype=physical` for
  **both containers and VMs** (one GPU per device for VMs), plus `mdev`/`sriov` (VM-only) and
  `mig` (container-only). **But** passing a GPU *through a cloud guest into a nested LXD VM*
  (what `project.md` calls the "Inference VM") requires IOMMU/vfio inside the guest and is
  **not a supported GCP configuration**. See §7.2.
- Citations: GPU machine types <https://docs.cloud.google.com/compute/docs/gpus> ·
  G2/L4 launch + T4→L4 2–4× <https://cloud.google.com/blog/products/compute/introducing-g2-vms-with-nvidia-l4-gpus> ·
  L4/T4 pricing <https://cloud.google.com/compute/vm-instance-pricing> ·
  LXD GPU device <https://canonical.com/lxd/docs/latest/reference/devices_gpu/>.

---

## 4. Quantization choices

| Format | Where it runs | Size vs FP16 | Verdict |
|--------|---------------|--------------|---------|
| **GGUF Q4_K_M** | llama.cpp / Ollama | ~25–30% | **Default** — "always prefer Q4_K_M over Q4_0"; negligible quality loss |
| GGUF Q5_K_M | llama.cpp | ~35% | Quality upgrade if VRAM allows |
| GGUF Q8_0 | llama.cpp | ~50% | Near-lossless; only for 7B/8B with headroom |
| GGUF Q3_K/Q2_K/IQ* | llama.cpp | smaller | Fallback only; hurts reasoning quality |
| AWQ / GPTQ (INT4) | vLLM | ~25–30% | vLLM path; Turing (T4) AWQ ✓ / Marlin ✗; Ada (L4) both ✓ |
| FP8 (W8A8) | vLLM | ~50% | **Ada (L4) only**, not Turing (T4) |
| INT8 (W8A8) | vLLM | ~50% | T4 and L4 supported |

Rule of thumb: **leave 20–30% of VRAM free for the KV cache**; add ~1–3 GB for KV at
typical context. Pick the **largest model that fits at Q4**, not a smaller one at Q8.
- Citations: Q4_K_M default <https://dexity.com/intel/run-deepseek-locally-2026> ·
  quant levels <https://praveentechworld.com/blog/deepseek-r1-quantization-fp8-q4-local-vram-guide> ·
  vLLM quantization×hardware matrix <https://docs.vllm.ai/en/v0.8.5/features/quantization/supported_hardware.html> ·
  GGUF format <https://huggingface.co/docs/hub/en/gguf>.

---

## 5. Agent runtimes

### 5.1 Comparison table

| Runtime | License | Maturity (signals) | Fit for Termini | Demo verdict |
|---------|---------|--------------------|-----------------|--------------|
| **Google ADK** | Apache-2.0 | GA-class; `pip install google-adk`; ADK 2.0; Gemini-native; multi-agent, tools, eval | The **controller AI operator** is Gemini-based on GCP — ADK is the native fit | **ADOPT** (primary) |
| **LangChain / LangGraph** | MIT (LangGraph) | LangGraph ~41k★; stateful graph orchestration; used by Klarna/Uber/Replit | Best non-Google fallback; deterministic graph control matches the guardrail loop | **ADOPT as fallback** |
| **AutoGen** | MIT | Repo flags **maintenance mode**; successor = **Microsoft Agent Framework** (MIT) | Not worth adopting a maintenance-mode dep for 5 days | **REJECT** (use MAF only if MS stack required) |
| **CrewAI** | MIT | ~45–59k★; Crews + Flows; independent of LangChain | Fast role-based prototyping, but opinionated abstractions add little over LangGraph here | **OPTIONAL** (nice-to-have, not core) |
| **OpenHands** | MIT | Formerly OpenDevin; v1.x; Docker-sandbox controller+runtime; LiteLLM-backed | Great **prior art** for a sandboxed agent VM, but its controller loop is overkill for the ops operator | **REFERENCE ONLY** |

### 5.2 Notes

- **ADK** is an open-source (Apache-2.0) agent framework introduced at Google Cloud NEXT 2025;
  it targets Gemini, supports multi-agent composition, tool use, and deployment. Since the
  controller runs on GCP and the AI operator calls Gemini over C0 telemetry only
  (`governance.md` §7), ADK minimizes glue code.
  Citations: <https://github.com/google/adk-python> (Apache-2.0) · <https://adk.dev/> ·
  <https://developers.googleblog.com/en/agent-development-kit-easy-to-build-multi-agent-applications/>.
- **LangGraph** is an MIT-licensed low-level orchestration runtime (stateful graphs,
  deterministic + agentic steps in one graph) — the cleaner fit if we want the remediation
  loop to be a hand-authored state machine rather than an LLM-driven planner. Same OpenAI
  client works against `llama-server`.
  Citations: MIT <https://github.com/langchain-ai/langgraph/blob/main/LICENSE> · overview
  <https://docs.langchain.com/oss/python/langgraph/overview>.
- **AutoGen** is MIT but in maintenance mode; Microsoft Agent Framework is the successor.
  Citation: <https://github.com/microsoft/autogen>.
- **CrewAI** is MIT (<https://github.com/crewAIInc/crewAI>).
- **OpenHands** is MIT and architecturally instructive: a controller spawns one **Docker
  sandbox per task**, agent executes shell/file/patch inside it, controller↔sandbox over a
  socket. That is exactly the isolation model we want for on-prem Agent VMs.
  Citations: <https://github.com/OpenHands/openhands> (MIT) ·
  sandboxes <https://docs.openhands.dev/openhands/usage/sandboxes/overview>.

---

## 6. Packaging a reproducible agent image

**What "agentic OS" prior art exists (2026):**
- **Fedora `bootc` image-mode OS**: define the whole OS in a Containerfile, emit qcow2/AMI/GCP
  image; updates are transactional and roll back like git commits; read-only `/usr`.
- **AgentOS (Ubuntu-based)**: pre-configured OVA/QCOW2 appliance, `cloud-init` support,
  headless "Server" edition with `.raw.gz` cloud image.
- **Firecracker microVM per-session sandbox**: disposable Linux machine per agent session,
  pre-snapshotted for near-instant boot (older but the isolation gold standard).

**Recommended packaging for the 5-day demo (adopt, don't build):**
1. **Base:** Ubuntu 24.04 LTS cloud image.
2. **Golden image build:** **HashiCorp Packer + cloud-init `autoinstall`**, then `cloud-init
   clean` / wipe SSH host keys / clear machine-id before templating. This is the mainstream,
   reproducible path (Packer handles post-install provisioning; cloud-init handles unattended
   install). Build takes **~20–40 min once**, offline from the demo.
3. **Bake-in (no cold downloads at demo):** Python 3.12, Node.js, `pip` deps (agent
   frameworks), the **llama.cpp CUDA binary**, and the **GGUF weights already on disk**.
4. **Serve to customers:** register → controller issues the project SSH public key →
   inject into the image's first-boot (`cloud-init`) → expose `ssh root@<node-ip>` + web
   terminal (xterm.js). Mirrors `project.md` §4 and `controller-api.md` §4.3.
5. Lighter alternative if Packer is too slow: build the guest rootfs as a **Docker image**,
   then `docker save | lxc image import` as an LXD image (single artifact, versioned by tag).
   Keep it as plan B.

- Citations: Packer+cloud-init golden images
  <https://oneuptime.com/blog/post/2026-03-02-how-to-use-cloud-init-with-packer-for-automated-builds-on-ubuntu/view> ·
  Packer template cleaning <https://oneuptime.com/blog/post/2026-03-02-how-to-use-packer-to-build-ubuntu-vm-templates/view> ·
  bootc agentic OS <https://www.redhat.com/en/blog/building-hardened-image-based-foundation-ai-agents> ·
  AgentOS <https://github.com/SecureAgentOS/agentos> ·
  Firecracker agent sandbox <https://github.com/vivek1504/agent-sandbox>.

---

## 7. Constraints to flag (setup time & VRAM) — read before W1

### 7.1 VRAM: the DeepSeek V4 names in `governance.md` do NOT fit Node C

`governance.md` §7.1 wires **"DeepSeek flash"** and **"deepseek-v4-pro"** to on-prem
inference on Node C. The actual V4 family is **MoE**:

| Model | Total / active params | Smallest viable on-disk/VRAM | Fits L4 24 GB? | Fits T4 16 GB? |
|-------|----------------------|------------------------------|----------------|----------------|
| DeepSeek-V4-Flash | 284B / 13B active | native FP4+FP8 ≈ **146 GB** (needs B200); FP8 ≈ **284 GB** (needs H100) | **No** | **No** |
| DeepSeek-V4-Pro | 1.6T / 49B active | hundreds of GB | **No** | **No** |

**Consequence:** the literal V4-Flash/V4-Pro models can only be self-hosted on a multi-GPU
cluster (2×H100+/B200). On a single L4/T4 we must either:
- **(Recommended)** realize "DeepSeek flash on-prem" as a **DeepSeek-R1-Distill** model —
  `DeepSeek-R1-Distill-Qwen-14B` Q4_K_M (~9–12 GB, comfortable on L4) or
  `DeepSeek-R1-Distill-Llama-8B` Q4_K_M (~5–8 GB, fits T4). Keep the *name* "on-prem
  DeepSeek" in contracts but pin the exact GGUF + quant here so no one assumes the 284B model.
- Or call the hosted DeepSeek API for V4 — but that **violates** the on-prem boundary for
  customer payloads (`governance.md` §3/§7) and is rejected.

→ **Flag to architect (owner of `governance.md`):** pin the on-prem model to a named distill
+ quant. Blocking for the serving contract, not for the rest of the build.

Citations: V4-Flash specs <https://lambda.ai/inference-models/deepseek-ai/deepseek-v4-flash> ·
V4 series model card <https://modelscope.cn/models/deepseek-ai/DeepSeek-V4-Pro> ·
distill VRAM table <https://praveentechworld.com/blog/deepseek-r1-quantization-fp8-q4-local-vram-guide>.

### 7.2 GPU passthrough into a nested LXD VM is the riskiest assumption

`project.md` describes an "Inference VM (llama.cpp on Node C)" using physical GPU
passthrough. On GCP, the GPU is already passed through **to Node C**. LXD can pass a
physical GPU to a **container** easily (`lxc config device add c1 gpu0 gpu gputype=physical`),
but passing it **again** into a nested VM needs IOMMU/vfio bound inside the guest, which GCP
does not support/expose.
→ **Recommendation:** run `llama-server` **directly on the Node C host**, or in an **LXD
container** with a `gpu` device — not a nested VM. This preserves the LXD/OVN story for the
compute nodes while removing the demo's biggest failure mode. If a VM boundary is mandatory,
budget fallback time and pre-test on real GCP hardware before demo day.
Citations: LXD GPU device types <https://canonical.com/lxd/docs/latest/reference/devices_gpu/> ·
GCP GPU attach/passthrough <https://docs.cloud.google.com/compute/docs/gpus>.

### 7.3 Setup-time budget (all pre-baked, none live)

| Step | Time | Do it when |
|------|------|-----------|
| Packer golden agent image build | 20–40 min | once, day 0 |
| llama.cpp CUDA build from source | ~5–15 min | once, baked into image (prefer official Docker/prebuilt) |
| vLLM install / Docker pull | several min; image multi-GB | only if adopting vLLM |
| GGUF download: 8B ≈ 5 GB, 14B ≈ 9 GB, 32B ≈ 20 GB | minutes–tens of min | **pre-pull onto Node C disk**, never during demo |
| Model load into VRAM (llama.cpp) | ~seconds (GGUF) | at boot; keep `llama-server` warm |
| LXD cluster form | ~5–10 min | once |

This directly implements `project.md` adversarial advisory #2: the "installer" must merely
**activate an already-installed, pre-warmed** daemon and register in <5 s.

### 7.4 Throughput / sizing notes
- T4 datapoint: Llama-2-7B Q4_0 ≈ 44–46 tok/s. A 14B Q4 on L4 will be roughly comparable or
  better; a 32B Q4 on L4 will be slow-but-usable for single-user. Keep default `--ctx-size`
  4k–8k and `--parallel` 2–4 for the demo.
- vLLM on T4 (SM 7.5) blocks Marlin and FP8 kernels; AWQ/GPTQ/INT8 work. On L4 (SM 8.9) all
  quant paths work.

---

## 8. Verdict table (build vs adopt, per area)

| Area | Verdict | Choice | Citation |
|------|---------|--------|----------|
| Inference engine (primary) | **ADOPT** | llama.cpp `llama-server` (MIT) | github.com/ggml-org/llama.cpp |
| Inference engine (secondary) | **ADOPT** | vLLM (Apache-2.0) | github.com/vllm-project/vllm |
| Local dev runner | **ADOPT** | Ollama (MIT) | docs.ollama.com |
| High-throughput engine | **REJECT (demo)** | SGLang | github.com/sgl-project/sglang |
| Legacy HF server | **REJECT** | TGI (archived) | github.com/huggingface/text-generation-inference |
| Quantization | **ADOPT** | GGUF Q4_K_M default | huggingface.co/docs/hub/en/gguf |
| GPU | **ADOPT** | GCP G2 / L4 24 GB (T4 fallback) | docs.cloud.google.com/compute/docs/gpus |
| GPU presentation | **CHANGE plan** | host or LXD container, not nested VM | canonical.com/lxd/docs/latest/reference/devices_gpu/ |
| Controller agent runtime | **ADOPT** | Google ADK (Apache-2.0) | github.com/google/adk-python |
| Agent runtime fallback | **ADOPT** | LangGraph (MIT) | github.com/langchain-ai/langgraph |
| Multi-agent framework | **REJECT** | AutoGen (maintenance) | github.com/microsoft/autogen |
| Role-based framework | **OPTIONAL** | CrewAI (MIT) | github.com/crewAIInc/crewAI |
| Sandboxed agent loop prior art | **REFERENCE** | OpenHands (MIT) | github.com/OpenHands/openhands |
| Agent image packaging | **ADOPT** | Packer + cloud-init → golden LXD image | oneuptime.com (Packer+cloud-init) |
| Image-based OS (future) | **REFERENCE** | Fedora `bootc` / AgentOS | redhat.com/agentic-OS |

---

## 9. Concrete shortlist for W1 contracts

**Serving API contract (`agent-runtime.md`):**
- Endpoint: `POST http://<node-c-ip>:8080/v1/chat/completions`, OpenAI schema, SSE streaming
  (`"stream": true`, terminated by `data: [DONE]`).
- Auth: `Authorization: Bearer <key>` via `llama-server --api-key`.
- Health: `GET /:8080/health` (200 ready / 503 loading); Metrics: `GET /metrics` (Prometheus).
- Structured output: `response_format: {"type":"json_schema", ...}` (GBNF-enforced) for intent
  extraction → feeds `controller-api.md` §4.2.
- Models: `--alias deepseek-flash` (14B distill Q4_K_M) and, on L4 only, a 32B variant
  `--alias deepseek-pro-lite`; document that these are **distills**, not V4 284B/1.6T.
- Routing rule (from `governance.md`): agent/prompt traffic stays on this on-prem endpoint;
  GCP external model is used only for the operator loop over C0 telemetry.

**Agent image contract:**
- Base Ubuntu 24.04 LTS (matches `controller-api.md` §4.1 `os` enum).
- Pre-installed: Python 3.12, Node.js, LangGraph/ADK client libs, `llama.cpp` binary, GGUFs.
- Built with Packer + cloud-init `autoinstall`; cleaned (`cloud-init clean`, ssh host keys
  removed, machine-id truncated) before templating → fresh identity per clone.
- SSH: project ed25519 key injected at first boot by the bootstrap daemon (per
  `controller-api.md` §4.3).

**GPU requirements:**
- Node C: **1× NVIDIA L4, 24 GB** (`g2-standard-4`) preferred; T4 16 GB acceptable.
- Serve from **host or LXD container** with a `gpu` device; avoid nested-VM passthrough.
- VRAM budget: model Q4_K_M weights + 20–30% headroom for KV cache + ~1 GB runtime.

---

## 10. Open questions for the synthesizer / architect

1. **[blocking, owner: architect]** Pin the on-prem model to a **named DeepSeek distill + GGUF
   quant** (e.g. `DeepSeek-R1-Distill-Qwen-14B-Q4_K_M`). `governance.md` §7.1 currently names
   V4-Flash/V4-Pro, which cannot run on a single L4/T4. (§7.1)
2. **[blocking, owner: infra]** Confirm Node C will run `llama-server` on the **host or in an
   LXD container**, not a nested LXD VM; if a VM is required, validate passthrough on real GCP
   hardware before demo day. (§7.2)
3. **[decision, owner: research/infra]** L4 vs T4 — L4 recommended (24 GB, Ada) if budget and
   G2 quota allow; T4 caps the model at ~8B comfortably. (§3)
4. **[decision, owner: agents]** ADK vs LangGraph for the controller operator loop — both
   keep the OpenAI client ABI, so this is low-risk to defer to W2.

---

## 11. Citations (consolidated)

Engines: <https://github.com/ggml-org/llama.cpp> · <https://llama.app/docs/api> ·
<https://github.com/vllm-project/vllm> ·
<https://docs.vllm.ai/en/stable/getting_started/installation/gpu/> ·
<https://docs.vllm.ai/en/v0.8.5/features/quantization/supported_hardware.html> ·
<https://github.com/ollama/ollama/blob/main/LICENSE> · <https://docs.ollama.com/api/openai-compatibility> ·
<https://github.com/sgl-project/sglang> · <https://github.com/huggingface/text-generation-inference> ·
<https://developers.redhat.com/articles/2025/09/30/vllm-or-llamacpp-choosing-right-llm-inference-engine-your-use-case> ·
<https://rawlinson.ca/articles/vllm-vs-sglang-performance-benchmark-h100> ·
<https://github.com/ggml-org/llama.cpp/discussions/15013>

GPU/quant: <https://docs.cloud.google.com/compute/docs/gpus> ·
<https://cloud.google.com/blog/products/compute/introducing-g2-vms-with-nvidia-l4-gpus> ·
<https://cloud.google.com/compute/vm-instance-pricing> ·
<https://canonical.com/lxd/docs/latest/reference/devices_gpu/> ·
<https://huggingface.co/docs/hub/en/gguf> ·
<https://praveentechworld.com/blog/deepseek-r1-quantization-fp8-q4-local-vram-guide> ·
<https://dexity.com/intel/run-deepseek-locally-2026> ·
<https://lambda.ai/inference-models/deepseek-ai/deepseek-v4-flash> ·
<https://modelscope.cn/models/deepseek-ai/DeepSeek-V4-Pro>

Agents/packaging: <https://github.com/google/adk-python> · <https://adk.dev/> ·
<https://developers.googleblog.com/en/agent-development-kit-easy-to-build-multi-agent-applications/> ·
<https://github.com/langchain-ai/langgraph> · <https://docs.langchain.com/oss/python/langgraph/overview> ·
<https://github.com/microsoft/autogen> · <https://github.com/crewAIInc/crewAI> ·
<https://github.com/OpenHands/openhands> · <https://docs.openhands.dev/openhands/usage/sandboxes/overview> ·
<https://oneuptime.com/blog/post/2026-03-02-how-to-use-cloud-init-with-packer-for-automated-builds-on-ubuntu/view> ·
<https://oneuptime.com/blog/post/2026-03-02-how-to-use-packer-to-build-ubuntu-vm-templates/view> ·
<https://www.redhat.com/en/blog/building-hardened-image-based-foundation-ai-agents> ·
<https://github.com/SecureAgentOS/agentos> · <https://github.com/vivek1504/agent-sandbox>
