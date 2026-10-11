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
6. `setup-lxd-stub.sh` — give the operator a reachable LXD target so
   remediations actually execute. **Without this step the demo decides
   correctly and then fails at the last hop.** See below.
7. `setup-seeder-vm.sh` — run the demo seeder beside the controller so the demo
   does not depend on an operator's workstation staying up.

## The operator needs a reachable LXD target

Every step of the loop worked while nothing was actually remediating:

```
remediation_audits: result='error'
                    detail='URLError: <urlopen error [Errno 111] Connection refused>'
```

The rule fired, the decision was made, the guardrail validated it, and the
executor tried to `PUT /1.0/instances/<name>/state` — against an endpoint that
did not exist. On a real cluster the nodes serve LXD on `10.10.0.11-13:8443`;
`setup-lxd-stub.sh` points the operator at the repo's own `chaos/lxd_stub.py`,
which records each call as JSONL so the demo has evidence of what was asked:

```
PUT /1.0/instances/01M4N827979BD9D2762EFE24A0/state
body: {"action": "restart", "force": true, "timeout": 30}
```

Two things bite here:

- **Ship the whole `chaos/` package, not just `lxd_stub.py`.** The stub imports
  `from chaos import common`; a lone copy dies with
  `ModuleNotFoundError: No module named 'chaos'` and the unit restarts forever
  without ever binding its port (a `systemctl is-active` of `activating` with a
  refused probe is the signature).
- Point `TERMINI_LXD_BASE_URL` at the stub **and restart the controller**; the
  loop reads it at startup.

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

## Model decisions: partial adherence, by design

The model-driven path is live and **it succeeds**. Early on it did not, and the
cause is worth recording because it was silent.

Guardrail **G5** requires `params.instance_name` to resolve against
`target_resource.instances[]` or the literal `"heaviest"`. `console/seed_demo.py`
originally emitted **no `lxd_instances`**, so no target could ever resolve and
*every* decision recorded `decision_source=fallback`,
`fallback_reason=invalid_target` — the model was consulted and then always
discarded, while the audit trail still looked plausible. Fixed by emitting the
field (a contract field at `node-telemetry.md` §4.6 that was simply missing).

Observed on the deployed controller after the fix:

```
by decision_source:  fallback 3   model 1
src=model     action=instance.stop  conf=0.85  fb=None
        reason='Disk write latency is elevated and the disk is not...'
src=fallback  fb=invalid_target     (guardrail rejected, deterministic fallback)
```

A model-chosen action passed the guardrail with confidence 0.85.

**Adherence is not universal at 4B, and that is left visible rather than tuned
away.** In the same run two of four decisions were still rejected as
`invalid_target` and fell back. That is the designed behaviour — an unresolvable
model target is rejected and the deterministic provider takes over — and showing
both halves demonstrates the capability *and* the safety envelope. Expect a
mixed ratio; it is not a regression.

## Running the seeder on the controller VM

`setup-seeder-vm.sh` installs `console/seed_demo.py` as a systemd service beside
the controller.

This matters: when the seeder ran from an operator's workstation the demo died
whenever that process was reaped, and the dashboard showed both nodes
**OFFLINE**. Hosting it on the VM makes the stack self-contained — nothing
outside GCP is needed for the demo to stay live — and it talks to the controller
over loopback, so no public round-trip is involved.
