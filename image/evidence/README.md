# evidence/ — what was actually executed

This workstream ran on a plain CI/dev host (no KVM, no GPU, no LXD, no Packer),
so the *image* could not be baked here. What **was** executed is recorded below,
so the reviewer can separate "specified" from "proven".

| Evidence | Command (as run) | Proves |
|----------|------------------|--------|
| `offline-load-proof.txt` | `sudo unshare -n bash offline-proof-server.sh` | the **pinned** llama.cpp build loads a **pre-warmed GGUF from local disk** and serves an OpenAI-compatible completion with **no network** (netns has only loopback; DNS fails) |
| `../examples/boot-log.rehearsal.json` | `first-boot-preflight.sh --root <stub>` | the first-boot assertion passes on a complete root; `network_downloads_at_boot: 0` with `network.measured: false` (a staged root is not a live boot — the field is measured from `/proc/net/dev` on a real boot, never hardcoded) |
| `../examples/image-manifest.rehearsal.json` | `rehearse_bake.py` | the builder's manifest validates against `manifests/image-manifest.schema.json` |
| `../examples/gpu-check.rehearsal.json` | `gpu_check.py --smi <stub> --dev-dir <stub> --cudart <stub.so>` | the Node C GPU verifier's positive path: a stub `nvidia-smi` reports an L4, the stub `/dev/nvidia*` nodes open, and a real `libcudart` call (stub `.so`, `cudaGetDeviceCount`/`cudaMemGetInfo`/`cudaStreamCreate` via `ctypes`) succeeds — `verdict: pass` |
| `../examples/gpu-check.absent.json` | `gpu_check.py --smi <missing>` | the verifier fails closed on a host with no `nvidia-smi` — `verdict: fail`, `reason: nvidia_smi_absent` |

## `offline-load-proof.txt`

`llama.cpp` was built from the **pinned commit** `7fe450e19305b828c199d602c23a8337aaa1f03b`
(tag `v0.5.0`) with `-DBUILD_SHARED_LIBS=OFF` — the same flags `provision/30-llamacpp.sh`
uses — then run inside `unshare -n` (a network namespace that contains only a
loopback interface, no route off-host). The model file is
`Qwen/Qwen2.5-0.5B-Instruct-GGUF` `q4_k_m` (469 MiB), used **only** as a fast CPU
smoke model; the product weights are the two DeepSeek distills in
`models.lock.json` and are not downloaded here (multi-GB).

Key lines the reviewer should look for:

- `ip -brief addr show` → only `lo 127.0.0.1/8` (no uplink);
- `no DNS -> no network (expected)`;
- `load_model: loading model '/…/qwen2.5-0.5b-instruct-q4_k_m.gguf'` → `model loaded`;
- `POST /v1/chat/completions` → `"content":"prewarmed model online."`;
- the response `system_fingerprint` `b1-7fe450e19` echoes the pinned commit.

Caveat: this is a **CPU smoke model** on a dev host — the 65 tok/s figure is not a
performance claim. Contract §11.4 throughput targets are for the GPU L4, not this host.

**Not proven here (needs a build host / GPU):** the gpu variant's NVIDIA driver
bake (`15-nvidia-driver.sh`) and CUDA build, the live `verify-gpu.sh` run against
the Node C L4, the real multi-GB bake, booting a GCP VM, and publishing the alias
to a registry.
