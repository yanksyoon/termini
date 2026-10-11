# Phase 3 — deploying Termini to Google Cloud

The three deploy targets, and why each sits where it does.

| component | host | why there |
|---|---|---|
| **dashboard** | Firebase Hosting | static site; `firebase deploy --only hosting` |
| **model (Gemma)** | Cloud Run GPU, `asia-southeast1` | Google's own open-weights model on a named GCP product, via a Model Garden image; scale-to-zero |
| **controller** | Compute Engine VM, `asia-south1-a` | it persists to **SQLite** — Cloud Run's filesystem is ephemeral (issue #96) |

All three are GCP, so "deployed on Cloud Run/GCP/Firebase" holds. The controller
is on Compute Engine deliberately: moving it to Cloud Run forces a
Firestore/Cloud SQL refactor of the lease and idempotency semantics, for no
functional gain, because the single-owner lease means it is single-instance by
design.

## Scripts, in order

1. `stage-weights.sh` — download Gemma weights once and upload to GCS.
   **Do not skip this.** See below.
2. `deploy-gemma.sh` — deploy Gemma on Cloud Run GPU with the bucket mounted.
3. `setup-controller-vm.sh` — install the controller as a systemd unit on a VM
   with a persistent `/data` disk.
4. `setup-tls.sh` — Caddy + Let's Encrypt in front of the controller.
5. `setup-operator-model.sh` — `inference-proxy.py` plus a systemd drop-in that
   flips the operator to model-driven decisions.

## Why weights are staged instead of pulled at startup

The first deploy attempt failed. vLLM started correctly but could not finish
downloading the model inside the startup probe budget, and it says why:

```
Warning: You are sending unauthenticated requests to the HF Hub.
Please set a HF_TOKEN to enable higher rate limits and faster downloads.
```

Unauthenticated HF downloads are rate-limited. Baking the download into the
cold-start path also means paying it again on every scale-from-zero. Staging to
GCS once turns a ~14-minute timeout into ~2.6 minutes (measured: 78 s per
safetensors shard).

Weights come from `unsloth/gemma-3-4b-it`, an **ungated** mirror of
`google/gemma-3-4b-it`. The `google/*` repos are `gated=manual` on HF and need
the Gemma licence accepted plus a token; the mirror does not, so no interactive
step is required.

## Why TLS is mandatory

The dashboard is served over HTTPS from Firebase Hosting. A browser refuses to
call a plain-HTTP API from an HTTPS page (mixed content), so
`?api=http://<ip>:8080/v1` cannot work. This is the one thing Cloud Run would
have provided for free, and it is the entire cost of keeping the controller off
it.

`setup-tls.sh` uses a `sslip.io` hostname that resolves to the VM's **static**
external IP, so Let's Encrypt can issue a real certificate without buying a
domain. Pin the IP first — an ephemeral IP would invalidate the certificate's
hostname on restart.

## Why the inference proxy exists

The Cloud Run service is deployed authenticated
(`--no-allow-unauthenticated`), so callers need an OIDC identity token. But
Termini's decision adapter takes a **static** bearer string from
`TERMINI_INFERENCE_TOKEN`, and Cloud Run identity tokens expire in about an
hour — pasting one into an environment variable yields a service that works and
then mysteriously stops.

`inference-proxy.py` listens on loopback and mints a fresh token per request
from the GCE metadata server. The controller points at
`http://127.0.0.1:8090` with no static secret, and the model service stays
private. No controller or adapter change is needed.

## Gotchas that cost time

- **`CPUS_ALL_REGIONS` is 12 and fully consumed** by the three nodes. A new VM
  needs the quota freed first. (The regional `CPUS` quota reads 100 and is *not*
  the binding constraint — don't be misled by it.)
- **SSH is tag-scoped.** `termini-allow-ssh` targets the `termini-node` tag, so a
  new VM with a different tag has no SSH until its own rule allows tcp:22.
- **`/data` must be owned by the service user.** Root-owned `/data` gives
  `sqlite3.OperationalError: unable to open database file`.
- **Don't redeploy while a revision is still provisioning.** Two revisions
  holding GPU + 32 Gi memory at once exceeds the regional memory quota and the
  second dies with `Quota exceeded for total allowable memory per project per
  region`.
- Google's published recipe targets `--gpu-type nvidia-rtx-pro-6000` at 20 CPU /
  80 Gi. L4 (24 Gi) is used here: it is what the project has quota for, and it
  is sufficient for a 4B bf16 model.

## Known gap: model decisions currently always fall back

The model-driven path is wired and running, but in the synthetic demo every
decision falls back deterministically. Cause is precise and not a code defect:

- Guardrail **G5** requires `params.instance_name` to resolve against
  `target_resource.instances[]` / the evidence window.
- `console/seed_demo.py` emits **no `lxd_instances`**, so that list is empty.
- The prompt (`termini/decision/prompt.py`) therefore permits exactly one valid
  target literal, `"heaviest"`, and Gemma 3 4B does not reliably emit it.
- Result: `decision_source=fallback`, `fallback_reason=invalid_target`.

Observed on the deployed controller:

```
by decision_source:  fallback  1
src=fallback  action=instance.restart  conf=None  fb=invalid_target
```

To demonstrate a *successful* model-chosen remediation, either seed telemetry
that includes resolvable `lxd_instances[]`, or strengthen the prompt's
insistence on the `heaviest` literal. The guardrail behaving this way is the
designed behaviour: a model output that cannot be resolved is rejected and the
deterministic provider takes over.
