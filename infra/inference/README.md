# Termini inference endpoint (`infra/inference`)

Owner: kanban task **`t_95528ae8`** — *Expose host llama.cpp on GPU Node C over
HTTP to Node A* (supersedes the duplicate W4 card `t_4ccb8ea7`). Workstream W3.

**2026-10-15:** the endpoint is now fronted by
[`termini-inference-proxy`](#termini-inference-proxy--node-cs-front-door-t_f466824c)
(owner: `t_f466824c`) — the same address, the same contract, one extra hop. See
that section before changing anything on Node C.

This directory **owns the live inference endpoint**: proving that the
host-exposed `llama.cpp` on **Node C** answers a request from **Node A** over the
frozen underlay route, and reporting GPU device visibility honestly.

## Termini-inference-proxy — Node C's front door (`t_f466824c`, first-boot `t_359ed606`)

| | |
|---|---|
| Service | `termini-inference-proxy.service` — `0.0.0.0:8080`, the address nodes A/B already call |
| Code | [`proxy.py`](proxy.py) (stdlib only — the node needs no new packages) |
| Installer | [`install-inference-proxy.sh`](install-inference-proxy.sh) — **the single source of truth for the front door's units and env files**, called by Node C's FIRST BOOT and runnable by hand on a running node |
| Switch | [`termini-inference-switch.sh`](termini-inference-switch.sh) `gemini-proxy\|local\|legacy\|status` (installed by the installer at `/usr/local/sbin/termini-inference-switch`) |
| Verify | [`verify-proxy.sh`](verify-proxy.sh) (from **Node A** — a peer, not localhost) |
| Backend config | `/etc/termini/inference-proxy.env` → `TERMINI_INFERENCE_BACKEND=local\|gemini-proxy` |

**A fresh node comes up on the front door — there is no manual installer step.**
Node C's first boot (`infra/gcp/cloud-init/llama-serve.sh`) builds llama.cpp,
fetches the pinned weights, starts the intent instance, and then CALLS this
directory's `install-inference-proxy.sh` on the payload cloud-init staged
(byte-identical to the committed file; rendered by
`infra/gcp/cloud-init/node-c-serve-block.sh` for the gcloud paths and by the
`infra/gcp/userdata` module for Terraform). What a fresh node serves is explicit:
**`TERMINI_INFERENCE_BACKEND=local`** — this node's own CPU weights, because
`:8080` is the **agent-facing** surface of the inference split (D9, below).
Change it for the whole footprint with the `inference_backend` Terraform variable
/ `INFERENCE_BACKEND` env in `provision.sh` and `tools/raise-standing-fleet.sh`,
or on one running node with `termini-inference-switch gemini-proxy` (one command,
no rebuild, no reinstall; `legacy` restores the pre-proxy host-exposed server).

## Two inference surfaces (D9, `t_aa7a70f9`)

The footprint runs the same proxy code twice, on purpose, with **different
guarantees** — and neither listener may stand in for the other:

| | **Agent-facing** (this directory's front door) | **Provider-only** |
|---|---|---|
| Address | `http://10.10.0.13:8080` — Node C, underlay | `http://127.0.0.1:8085` — the CONTROLLER, loopback |
| Backend | **`local`** (shipped) — worker on `127.0.0.1:8081` | **`gemini-proxy`** → `aiplatform.googleapis.com` |
| Answers from | this node's own weights (alias `gemma`) | `gemini-2.5-flash`, a forwarded managed model |
| Egress | `in-vpc` — nothing leaves the premises | `external` — the request leaves for Google |
| Who calls it | agent VMs, the controller's intent extraction, any OpenAI client on the underlay | the operator's **diagnosis**, and nothing else |
| Unit | `termini-inference-proxy.service` | `termini-inference-provider.service` |
| Env | `/etc/termini/inference-proxy.env` | `/etc/termini/inference-proxy.env` (on the controller — a different host) |
| Installer | [`install-inference-proxy.sh`](install-inference-proxy.sh) (first boot calls it) | [`setup-inference-surfaces.sh`](setup-inference-surfaces.sh) |

The agent port **offers no forwarded backend**: a request to `10.10.0.13:8080`
can only be answered by that node's weights. The provider listener **offers no
local backend** — there is no worker on the controller, and `/health` says so by
**measuring** it (`local_path.available: false`) rather than asserting a local
path exists. Each surface has its own env file, unit and address, so flipping one
leaves the other's `/health` byte-identical; `tests/test_inference_surfaces.py`
asserts exactly that, offline.

The installer also writes `/usr/lib/tmpfiles.d/termini-inference.conf` so
`/run/termini` (the active-backend state file) is recreated for `termini-infer`
after a reboot, since `/run` is a tmpfs.

Two backends, chosen by configuration, no rebuild:

* **`local`** — transparent passthrough to the llama.cpp **worker**, which moves to
  loopback `127.0.0.1:8081` (this is also the frozen AR-4 shape: front door on
  `:8080`, workers on loopback). The bytes that come back are llama.cpp's own.
  **This is the shipped value on Node C**, because this listener is the
  agent-facing surface.
* **`gemini-proxy`** — the request is translated to Vertex AI `generateContent`
  (`gemini-2.5-flash`) and the answer is returned in the SAME OpenAI envelope,
  including `usage`/`timings`, honouring `max_tokens` and `temperature`. **This is
  the provider listener's value** on the controller (`127.0.0.1:8085`), and the
  operator's diagnosis is the traffic it exists for.

**A forwarded answer says so in-band — it is not GPU inference, not on-prem
inference, and not Gemma.** Every forwarded response states it (headers
`X-Termini-Backend: gemini-proxy`,
`X-Termini-External-Destination: aiplatform.googleapis.com`,
`X-Termini-Egress: external`, `X-Termini-Answered-By: forwarded-managed-model`;
body `termini_backend`: `backend=gemini-proxy`, `answered_by=forwarded-managed-model`,
`egress=external`, `destination=aiplatform.googleapis.com`), and so does every log
line. The controller's `externalModelCall()` audit row records the same destination
(`controller/app/operator/egress.py`), so a forwarded answer cannot be read as a
local one — and on the agent-facing port there is no forwarded answer to read.
On the **local** surface the labelling moves to the headers plus the JSON log line
(the body is llama.cpp's own bytes, relayed verbatim).

Auth is the **host's own identity**: an OAuth token minted per request from the
GCE metadata server (`roles/aiplatform.user`, `cloud-platform` scope). On Node C
that is the node's least-privilege identity (the egress/identity card
`t_5a4a0f73`); on the controller it is the controller's dedicated provider
service account. **There is no service-account key file on either host, and none
may be added** — the metadata identity is the only credential.

### Cost note for the agent path (stated, not hidden)

Node C is **CPU-only** (free-tier billing rejects the L4). The local model is
**slow**: generation ≈8–10 tok/s, prefill ≈33 tok/s, and the operator's 20-sample
decision prompt measured **402 s** on `n2-standard-8`
(`infra/cloudrun/evidence/node-c-gemma-decision-latency-2026-10-13.md`). The same
prompt on the provider surface takes **≈1.8 s**.

So: **agent tasks served locally are not interactive until the GPU quota lands** —
short turns are seconds-to-minutes. That is the price of the agent path being
genuinely on-prem, and it is the reason the diagnosis runs on the provider surface
instead. Neither number is a surprise; both are in this file on purpose.

Failure semantics: Vertex AI slow / rate-limited / unreachable / misconfigured
produces a **distinct, loud** error (`upstream_failure` log at `error` severity
with a `distinct` code such as `vertex_timeout`, `vertex_rate_limited`,
`vertex_rejected`, `vertex_unreachable`, `metadata_token`) and a bounded HTTP
status (`429 INFERENCE_BUSY` with `Retry-After`, `503 MODEL_UNAVAILABLE`,
`504 INFERENCE_TIMEOUT`). The controller's decision path turns any non-2xx into
`inference_unavailable` and falls back to the deterministic fast-path — it never
hangs and never silently serves nothing.

Reversibility: `termini-inference-switch legacy` restores the pre-proxy
host-exposed `llama.cpp` on `0.0.0.0:8080` in one command (`local` serves the same
CPU path through the front door instead). The active backend is recorded in
`/run/termini/inference-backend.json` and reported by `/health` and
`termini-inference-switch status` — including `"is_gpu_inference": false`, because
a node that emulates a GPU host must not claim to be one.

> WIRED AS OF `t_359ed606`: Node C's **first boot** now installs the front door
> itself — cloud-init stages the four files this directory owns and
> `infra/gcp/cloud-init/llama-serve.sh` calls `install-inference-proxy.sh`, so a
> node created from the image family serves `local` on `:8080` with no
> manual step, and llama.cpp comes up as the loopback worker on `127.0.0.1:8081`.
> The first-boot default is a variable (`inference_backend` / `INFERENCE_BACKEND`),
> and on a running node it is `termini-inference-switch local|gemini-proxy`.

> WIRED AS OF `t_aa7a70f9` (the inference split, **D9**): the shipped default is
> `local` (above), and the forwarded backend moved to the controller's
> provider-only listener — `setup-inference-surfaces.sh` installs it on
> `127.0.0.1:8085` and writes the controller drop-in that points the diagnosis
> there. Evidence and the live acceptance commands:
> [`evidence/split-surfaces-2026-10-15.md`](evidence/split-surfaces-2026-10-15.md).

## Acceptance (verbatim)

> a request from Node A to Node C returns a completion; streaming works.

Both are proven against the live 3-node GCP footprint in
`evidence/acceptance-*.txt`.

## Contract

| | |
|---|---|
| Frozen decisions | `docs/contracts/infra-decisions.md` **D2** (underlay routing, no overlay), **D5** (host-exposed `llama.cpp`, OpenAI-compatible), **D9** (two inference surfaces: agents local on Node C, diagnosis provider-only on the controller) |
| Interface | `docs/contracts/agent-runtime.md` **AR-4** (OpenAI-compatible router on `:8080`, local-only) / **AR-6** (alias `gemma` resident, shipped) |
| Endpoint | `http://10.10.0.13:8080` — Node C's **underlay** IP, bound `0.0.0.0` on the host. **Agent-facing**, `backend=local` |
| Provider endpoint | `http://127.0.0.1:8085` — the **controller's** loopback listener, `backend=gemini-proxy`. Never a node address, never handed to an agent |
| Reachability | GCP firewall allows `tcp/8080` from `10.10.0.0/24` **only** (never public); the provider listener is loopback and has no firewall rule because the fleet has no route to it |

## What is running on Node C

| Aspect | Value |
|---|---|
| Engine | `llama-server` built from pinned commit `7fe450e19305b828c199d602c23a8337aaa1f03b` (`image/models.lock.json`), CPU-only (`GGML_CUDA=OFF`) |
| Model | shipped resident **`gemma`** = **Gemma 3 4B IT** (`unsloth/gemma-3-4b-it-GGUF`) / `Q4_K_M` / 2,489,894,016 B / sha256 `04a43a22…a7d19` (from `image/models.lock.json`, delivered from `gs://termini-510609-models/gemma-3-4b-it-gguf/`). The `deepseek-flash` / `deepseek-v4-pro` catalog pins remain (t_ab18d582) |
| Alias | `gemma` (a single-model deployment collapses the AR-4 router to a transparent passthrough — the documented case) |
| Serve flags | `--host 0.0.0.0 --port 8080 --ctx-size 16384 --parallel 1 --threads 8 --reasoning-format none` (with the front door installed, the worker moves to loopback `:8081` — see the proxy section) |
| Unit | `termini-llama-server.service` (enabled; `/var/log/termini-llama-serve.log`) |
| Measured (n2-standard-8, CPU) | Gemma 3 4B: generation ≈ **8–10 tok/s**, prompt ≈ 33 tok/s (the prior 8B distill measured ≈ 6.2 tok/s / 12 tok/s) |

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
