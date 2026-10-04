# examples/ — rehearsed pipeline evidence

These files are produced by `pipeline/tools/rehearse_bake.py` and
`pipeline/tools/rehearse_gpu.py` (run via `pipeline/verify-pipeline.sh
--write-examples`). They are **rehearsal artifacts**: they demonstrate that the
first-boot preflight, the manifest validator and the Node C GPU verifier behave
correctly, using a stub image/fixture root — they are **not** a production image
release and **not** a GPU run.

| File | Produced by | What it shows |
|------|-------------|---------------|
| `boot-log.rehearsal.json` | `first-boot-preflight.sh` run against a stub root | the boot log shape; both aliases resolved from local disk; `network_downloads_at_boot: 0` with `network.measured: false` (a staged root is not a live boot); `verdict: pass` |
| `image-manifest.rehearsal.json` | `rehearse_bake.py` | the exact manifest the builder emits, validated against `manifests/image-manifest.schema.json` |
| `gpu-check.rehearsal.json` | `gpu_check.py` run against a stub fixture (fake `nvidia-smi` + `/dev` nodes + stub `libcudart`) | the `termini.gpu_check` report shape; `verdict: pass` with `gpu_reported`, `device_nodes`, `device_openable`, `cuda_runtime` all true |
| `gpu-check.absent.json` | same verifier, no `nvidia-smi` on the host | the failure shape: `verdict: fail`, `reason: nvidia_smi_absent` |

The rehearsal also asserts the **negative** cases. Delete one model file and the
preflight must fail (`model_files_present`, `model_sizes_match`,
`model_sha256_sidecars` all flip to `false`). Point the GPU verifier at a missing
`nvidia-smi`, a driver with no GPU, or an absent `/dev/nvidia*` and it must fail
with the matching `reason` (`nvidia_smi_absent`, `no_gpu_reported`,
`device_nodes_missing`, `cuda_runtime_unusable`). That is why the checks are
trustworthy rather than decorative.

A real `agentic-os/w3` release is emitted at build time by
`pipeline/build-image.sh` with **real** artifact checksums; it is not committed
here (the artifact is multi-GB and the model weights are not stored in git).
