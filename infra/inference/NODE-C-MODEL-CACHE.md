# Node C model + runtime cache (pre-downloaded, zero-download demo)

Owner: kanban task **`t_5968666d`** — *Pre-download and cache the quantized model
+ llama.cpp binary on Node C*. Workstream W6 (demo reliability); parent of the
end-to-end dry-run card `t_3bb56d97`.

The live demo must never pull a multi-GB model. **On Node C the weights and the
`llama.cpp` runtime are already resident on local disk**, and the inference
server boots straight from those local files. This document records the exact
paths and checksums as measured on the live node, and the proof that a server
starts from the local file with `/health` returning `{"status":"ok"}`.

Evidence transcript: `evidence/node-c-model-cache-20261009T051042Z.txt`.

---

## 1. What is on Node C

Node C = `termini-node-c` (`n2-standard-8`, 8 vCPU, 31 GiB RAM, asia-south1-a),
the D5 inference node (`infra/inference/`). The root filesystem is the node's
**local** `/dev/root` disk (no network filesystem), so "resident on disk" here
means "resident on the node", not on a mounted model volume.

| Artifact | Path on Node C | Size (bytes) | sha256 |
|---|---|---|---|
| **GGUF weights** (`deepseek-flash`) | `/var/lib/termini/models/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf` | 4,920,736,608 | `87bcba20b4846d8dadf753d3ff48f9285d131fc95e3e0e7e934d4f20bc896f5d` |
| **`llama-server` binary** | `/opt/termini/llama.cpp/bin/llama-server` | 18,000,096 | `e3772536442b02e74cdaa0835754402995c7bfb2c3a018b46866ad994cd303e5` |
| weights sha256 sidecar | `/var/lib/termini/models/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf.sha256` | 107 | (contains the same sha256) |

The weights sha256 is **identical to the pin** in `image/models.lock.json`
(`aliases.deepseek-flash.sha256`, owner `t_ab18d582`) and to the on-node sidecar
file — three independent records of the same digest. The file is a valid GGUF
(first four bytes `47 47 55 46` = `GGUF`).

`/var/lib/termini/models/` additionally holds a **test** model,
`qwen2.5-1.5b-instruct-q4_k_m.gguf` (1,117,320,736 B, sha256
`6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`); it is not a
product alias and is not served by the `:8080` endpoint.

### `llama-server` identity

```
version: 0.5.0-dev (build 1, commit 7fe450e19)
```

`7fe450e19…` is the pinned commit in `image/models.lock.json`
(`llamacpp.commit = 7fe450e19305b828c199d602c23a8337aaa1f03b`, release `v0.5.0`).
CPU-only build (`GGML_CUDA=OFF`) — this footprint has no GPU; see
`infra/inference/README.md` for the honest GPU story.

---

## 2. Proof: the server runs, and *starts*, from the local file

The production unit `termini-llama-server.service` (installed by
`infra/inference/activate-endpoint.sh`) is `enabled` + `active` and its
`ExecStart` references only on-node paths:

```
/opt/termini/llama.cpp/bin/llama-server \
  --model /var/lib/termini/models/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf \
  --alias deepseek-flash --host 0.0.0.0 --port 8080 \
  --ctx-size 8192 --parallel 2 --reasoning-format none
```

`lsof` on the running PID shows the process holds **both local files** open —
the binary as `txt` and the GGUF `mmap`ed as `mem`:

```
llama-ser 10930 termini-infer  txt  REG  8,1   18000096  /opt/termini/llama.cpp/bin/llama-server
llama-ser 10930 termini-infer  mem  REG  8,1 4920736608  /var/lib/termini/models/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf
```

`http://127.0.0.1:8080/health` → `{"status":"ok"}` and
`/v1/models` advertises the `deepseek-flash` alias.

**Fresh-start proof.** To show the local files are *bootable* (not just
currently mapped), a second `llama-server` was launched from the *same* local
GGUF on a throwaway port `:18080` — the production `:8080` listener was never
touched:

```
$ /opt/termini/llama.cpp/bin/llama-server --model /var/lib/termini/models/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf \
    --alias deepseek-flash --host 127.0.0.1 --port 18080 --ctx-size 2048 --parallel 1 --reasoning-format none
fresh instance /health ok after 7s
health={"status":"ok"}
0.05.664.455 I srv  llama_server: model loaded
0.05.664.464 I srv  llama_server: listening on http://127.0.0.1:18080
```

It reached `/health` `{"status":"ok"}` in 7 s and the production endpoint stayed
healthy throughout.

### Reproduce

```bash
# from the build host (SSH key: ~/.ssh/id_ed25519)
NODE_C=34.100.181.44
ssh -i ~/.ssh/id_ed25519 ubuntu@$NODE_C 'sha256sum \
  /var/lib/termini/models/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf \
  /opt/termini/llama.cpp/bin/llama-server'
ssh -i ~/.ssh/id_ed25519 ubuntu@$NODE_C 'curl -s http://127.0.0.1:8080/health'   # {"status":"ok"}
```

---

## 3. Where the cache comes from (why the demo never downloads)

- The weights URL + sha256 are pinned in `image/models.lock.json`
  (`hf.co/bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF`, quant `Q4_K_M`), and the
  image bake (`image/pipeline/provision/40-models.sh`) aborts on any digest
  mismatch.
- On this footprint the node was provisioned from
  `infra/gcp/cloud-init/llama-serve.sh` (Terraform) / `infra/gcp/provision.sh`
  (gcloud fallback): **first boot** fetches the pinned GGUF once (verifying the
  sha256) and builds `llama-server` once at the pinned commit; everything after
  that is offline. `infra/inference/activate-endpoint.sh` only re-wires the unit
  around the already-present local files.

**Honest scope.** The *one-time* fetch/build happens at node first boot; the
claim this card establishes is that at **demo time** neither the weights nor the
runtime are fetched — they are already on Node C's local disk at the paths and
digests above. (The pre-baked LXD image path from `image/pipeline` that would
make first boot itself offline was never executed in this environment — no
KVM/Packer on the build host — so this node's first boot used the pinned
cloud-init download; see `infra/inference/README.md` §Honest limitations. The
pre-warm-on-disk card is `t_f0017269`.)

## 4. Files

```
infra/inference/NODE-C-MODEL-CACHE.md                          this document
infra/inference/evidence/node-c-model-cache-<ts>.txt           raw captured transcript
infra/inference/README.md                                      endpoint contract (cross-linked)
image/models.lock.json                                         the sha256 pin this verifies against
```
