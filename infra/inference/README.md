# Termini inference endpoint (`infra/inference`)

Owner: kanban task **`t_95528ae8`** — *Expose host llama.cpp on GPU Node C over
HTTP to Node A* (supersedes the duplicate W4 card `t_4ccb8ea7`). Workstream W3.

This directory **owns the live inference endpoint**: proving that the
host-exposed `llama.cpp` on **Node C** answers a request from **Node A** over the
frozen underlay route, and reporting GPU device visibility honestly.

## Acceptance (verbatim)

> a request from Node A to Node C returns a completion; streaming works.

Both are proven against the live 3-node GCP footprint in
`evidence/acceptance-*.txt`.

## Contract

| | |
|---|---|
| Frozen decisions | `docs/contracts/infra-decisions.md` **D2** (underlay routing, no overlay), **D5** (host-exposed `llama.cpp`, OpenAI-compatible) |
| Interface | `docs/contracts/agent-runtime.md` **AR-4** (OpenAI-compatible router on `:8080`) / **AR-6** (alias `deepseek-flash` resident) |
| Endpoint | `http://10.10.0.13:8080` — Node C's **underlay** IP, bound `0.0.0.0` on the host |
| Reachability | GCP firewall allows `tcp/8080` from `10.10.0.0/24` **only** (never public) |

## What is running on Node C

| Aspect | Value |
|---|---|
| Engine | `llama-server` built from pinned commit `7fe450e19305b828c199d602c23a8337aaa1f03b` (`image/models.lock.json`), CPU-only (`GGML_CUDA=OFF`) |
| Model | pinned **`deepseek-flash`** = `bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF` / `Q4_K_M` / 4,920,736,608 B / sha256 `87bcba20…896f5d` (from `image/models.lock.json`, t_ab18d582) |
| Alias | `deepseek-flash` (a single-model deployment collapses the AR-4 router to a transparent passthrough — the documented case) |
| Serve flags | `--host 0.0.0.0 --port 8080 --ctx-size 8192 --parallel 2 --reasoning-format none` |
| Unit | `termini-llama-server.service` (enabled; `/var/log/termini-llama-serve.log`) |
| Measured (n2-standard-8, CPU) | generation ≈ **6.2 tok/s**, prompt ≈ 12 tok/s, first SSE token ≈ **0.9 s** |

`--reasoning-format none` keeps the R1-distill ` thinking` block inline in the
assistant `content` instead of a separate `reasoning_content`, so plain OpenAI
clients (`termini-agent`, the ADK demo) always receive non-empty `content` and
incremental SSE deltas. This is deliberate and is a deviation from llama.cpp's
default (which splits reasoning out and can return an empty `content` when a
short `max_tokens` is exhausted while the model is still reasoning).

## GPU device visibility on Node C (reported honestly)

Node C has **no GPU device**: `nvidia-smi` absent, `lspci` shows no NVIDIA
function, `/dev/nvidia*` absent, no `nvidia*` modules. This is a **billing**
constraint, not a wiring one: the demo project sits on a Google Cloud free-tier
account that rejects every non-TPU accelerator with HTTP 400, so `t_f2173236`
re-provisioned Node C as a CPU-only `n2-standard-8`. Host-exposed `llama.cpp` on
CPU is the sanctioned **D5 fallback** ("GPU passthrough is a day-2 path only"),
and the parent card `t_5783da13` explicitly allows "host-exposed llama.cpp on
Node C as the demo fallback". Re-enabling the L4 is a billing upgrade + setting
`gpu_accelerator_count = 1` / `inference_machine_type = "g2-standard-4"` (see
`infra/gcp/README.md`).

## Verify

```bash
infra/inference/verify-inference.sh          # derives IPs from the shared inventory
```

It checks, from **Node A**: `/health` → 200, `/v1/models` advertises
`deepseek-flash`, a non-streaming completion returns non-empty `content`, and a
streaming request yields ≥2 incremental SSE `delta` chunks terminated by
`[DONE]` (with first-token latency). It also prints Node C's GPU visibility. Exit
0 = PASS. Override with `INVENTORY`, `NODE_A_PUB`, `NODE_C_PUB`, `NODE_C_IP`,
`PORT`, `MODEL_ALIAS`, `SSH_KEY`.

Maps to the epic acceptance independently re-run by the verifier task
`t_2e7ebf9d`.

## (Re-)activate the endpoint on an existing node

First boot provisions the endpoint from
`infra/gcp/cloud-init/llama-serve.sh` (Terraform) or `infra/gcp/provision.sh`
(gcloud fallback). To activate/repair it on a **running** node without
re-provisioning:

```bash
# on Node C (files must already be installed by first boot)
sudo infra/inference/activate-endpoint.sh          # writes the unit, restarts, waits for health
```

The script is idempotent and uses the pinned binary + pinned `deepseek-flash`
model already under `/var/lib/termini/models`.

## Evidence

| File | Shows |
|---|---|
| `evidence/acceptance-<ts>.txt` | full `verify-inference.sh` run: Node A → Node C `/health` 200, alias `deepseek-flash`, non-stream completion with content, 40 streaming delta chunks + `[DONE]`, first token 0.9 s, and the GPU-visibility report. `RESULT: PASS` |
| `evidence/activation-<ts>.txt` | Node C host facts after activation: pinned model sha256, `llama-server` version/commit, the running unit `ExecStart`, the `0.0.0.0:8080` listener, and `/v1/models` |
| `evidence/node-c-model-cache-<ts>.txt` | pre-cached model + runtime on Node C: on-node GGUF + `llama-server` sha256 (GGUF sha256 matches the `image/models.lock.json` pin), `lsof` showing the process mmaps the local GGUF, `/health {"status":"ok"}`, and a fresh second server starting from the same local file. Paths + checksums documented in [`NODE-C-MODEL-CACHE.md`](NODE-C-MODEL-CACHE.md) |

## Honest limitations

- The endpoint runs the **CPU** variant because this footprint has no GPU
  (free-tier billing); the `gpu` image variant and NVIDIA driver stack from
  `t_4ccb8ea7` are baked but unexercised here.
- The pinned binary is **built on Node C at first boot** by cloud-init, not
  streamed from the pre-baked LXD image (`image/pipeline`) — the pre-baked image
  bake itself was never executed in this environment (no KVM/Packer on the build
  host, no GCP image publish), so node provisioning falls back to a pinned
  source build. The *commit* and the *weights* are the pinned artifacts from
  `t_ab18d582`; the packaging path is not.
- The endpoint is not authenticated (`Authorization: Bearer …`, `agent-runtime.md`
  §10.4) — it is underlay-firewalled to `10.10.0.0/24` only, which is the demo
  posture, not a production one.
