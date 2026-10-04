# examples/ — rehearsed pipeline evidence

These two files are produced by `pipeline/tools/rehearse_bake.py` (run via
`pipeline/verify-pipeline.sh --write-examples`). They are **rehearsal artifacts**:
they demonstrate that the first-boot preflight and the manifest validator behave
correctly, using a stub image root — they are **not** a production image release.

| File | Produced by | What it shows |
|------|-------------|---------------|
| `boot-log.rehearsal.json` | `first-boot-preflight.sh` run against a stub root | the boot log shape; both aliases resolved from local disk; `network_downloads_at_boot: 0`; `verdict: pass` |
| `image-manifest.rehearsal.json` | `rehearse_bake.py` | the exact manifest the builder emits, validated against `manifests/image-manifest.schema.json` |

The rehearsal also asserts the **negative** case: delete one model file and the
preflight must fail (`model_files_present`, `model_sizes_match`,
`model_sha256_sidecars` all flip to `false`). That is why the checks are
trustworthy rather than decorative.

A real `agentic-os/w3` release is emitted at build time by
`pipeline/build-image.sh` with **real** artifact checksums; it is not committed
here (the artifact is multi-GB and the model weights are not stored in git).
