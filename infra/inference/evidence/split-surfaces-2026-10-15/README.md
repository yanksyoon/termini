# Inference surface split — evidence (`t_aa7a70f9`, D9)

Owner: kanban **`t_aa7a70f9`** (infra). Freezes the user's decisions of 2026-10-15:
agent/client inference is served by **LOCAL weights on Node C**; the operator's
**diagnosis** is served by **Gemini from the controller only**.

| | Agent-facing | Provider-only |
|---|---|---|
| Address | `http://10.10.0.13:8080` (Node C, underlay) | `http://127.0.0.1:8085` (controller, loopback) |
| Backend | `local` (shipped) — worker `127.0.0.1:8081` | `gemini-proxy` → `aiplatform.googleapis.com` |
| Egress | `in-vpc` | `external` |
| Credential | node metadata identity, no key file | controller metadata identity, no key file |

## What is in this PR

| File | Why |
|---|---|
| `infra/inference/setup-inference-surfaces.sh` | installs the provider-only listener on the controller and writes the controller drop-in (diagnosis → `127.0.0.1:8085`; Node C named for placement) |
| `infra/inference/termini-inference-provider.service` | the provider unit: loopback, `gemini-proxy`, `EnvironmentFile=/etc/termini/inference-proxy.env`, no key file |
| `infra/inference/proxy.py` | `/health` now **measures** `local_path.available` instead of asserting it (a provider listener has no worker and must not claim one); docstring states which surface the process is |
| `infra/inference/install-inference-proxy.sh`, `infra/gcp/{variables,userdata/variables,userdata/verify/main}.tf`, `infra/gcp/provision.sh`, `infra/gcp/tools/raise-standing-fleet.sh`, `infra/gcp/cloud-init/{node-c-serve-block.sh,llama-serve.sh,node_c_front_door_check.py}`, `infra/gcp/verify-cloud-init.sh`, `infra/gcp/terraform.tfvars.example` | the shipped Node C default becomes **`local`**: a rebuilt node cannot hand agent traffic to a forwarded managed model |
| `infra/cloudrun/setup-operator-node-c.sh` | the pre-split wiring now **refuses** without `--allow-unsplit` (it would move the diagnosis back onto the agent surface) |
| `docs/contracts/infra-decisions.md` | **D9** — the decision of record (table, rationale, rejected alternatives, cost note, consequences) |
| `docs/contracts/agent-runtime.md` (AR-4, §8), `controller-api.md` (§5.2 step 5), `operator-decision.md` (§6.1, §12), `governance.md` (§7.1) | the two surfaces as contracts: the agent endpoint is local-only; the diagnosis egress is labelled and C0-only; the placement exclusion must now name the model host explicitly |
| `docs/architecture.md`, `README.md`, `SUBMISSION.md`, `infra/gcp/README.md`, `infra/cloudrun/README.md`, `infra/inference/README.md` | the same two surfaces, told consistently, with the **cost note** (402 s local vs ≈1.8 s provider) stated rather than implied |
| `controller/app/config.py` | comment only: `inference_base_url` no longer identifies the model host |
| `tests/test_inference_surfaces.py`, `tests/test_node_c_first_boot.py` | the split asserted offline (see below) |

## Offline verification (raw output: `offline-verified.txt`)

Reproduce with `bash infra/inference/evidence/split-surfaces-2026-10-15/capture-offline.sh`.

* `python3 -m unittest tests.test_inference_surfaces -v` → **Ran 16 tests … OK**
  — renders the provider surface with `--render-dir` (no root, no systemd),
  asserts loopback-only + `gemini-proxy` + no key material, asserts the drop-in
  targets the loopback provider and names Node C for placement, asserts the
  pre-split script refuses, and asserts the two surfaces' `/health` are
  independent (16 tests include two live in-process listeners with faked
  upstreams: one `local` with a worker, one `gemini-proxy` whose worker is dead).
* `python3 -m unittest discover -s tests` → **Ran 742 tests … OK**.
* `scripts/check-doc-ulids.py` → pass; `scripts/check-readme-links.py` → 58 targets resolve.
* `shellcheck -S warning` over every changed shell script → clean (rc=0).
* `cmp infra/inference/proxy.py <rendered>` → identical
  (`sha256 6aebb74ee02f8923c365d14ab2d545dec8c024ea47ba0691afaf82b0cd7eade1`).

## Live acceptance — **PENDING an approved fleet window**

The fleet is **stopped by the operator's cost guard** (controller/node-a/node-b/
node-c TERMINATED; `CPUS` / `NVIDIA_L4_GPUS` / `CPUS_ALL_REGIONS` usage = 0.0).
Nothing below was run, and no output below is claimed. The commands are recorded
here so an approved window can execute them verbatim and paste the raw output
back into this file.

```bash
# 0. raise the fleet (approved window only) — infra/gcp/tools/raise-standing-fleet.sh

# (a) Node C's agent surface is LOCAL, and it really serves weights
ssh ubuntu@10.10.0.13 \
  'sudo /usr/local/sbin/termini-inference-switch local && \
   curl -s localhost:8080/health && echo && \
   curl -s localhost:8080/v1/models'
ssh ubuntu@10.10.0.13 \
  'curl -s -X POST localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
     -d "{\"model\":\"gemma\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":16}"'
#   expect: backend=local, egress=in-vpc, served_model=gemma, alias gemma resolves,
#           non-empty content relayed from llama.cpp (watch the clock: CPU-slow)

# (b) an AGENT-path call leaves nothing: only Node C's log line, and NO line
#     for that request id on the controller's provider listener
ssh ubuntu@10.10.0.13 'sudo tail -3 /var/log/termini-inference-proxy.jsonl'
ssh ubuntu@34.93.190.64 'sudo tail -3 /var/log/termini-inference-provider.jsonl'
#   expect: Node C line backend=local egress=in-vpc; provider log unchanged

# (c) a diagnosis on a real injected fault goes out from the CONTROLLER
ssh ubuntu@34.93.190.64 \
  'sudo tail -40 /var/log/termini-inference-provider.jsonl; \
   systemctl show termini-controller -p Environment | tr " " "\n" | grep INFERENCE; \
   sqlite3 /data/controller.db "select ts,message from operator_logs order by ts desc limit 6"'
#   expect: provider log line backend=gemini-proxy egress=external
#           destination=aiplatform.googleapis.com; audit row
#           externalModelCall … egress=external model=gemini-2.5-flash
#           destination=aiplatform.googleapis.com answered_by=forwarded-managed-model

# (d) the surfaces are independent: flipping Node C does not move the provider
ssh ubuntu@10.10.0.13 'sudo /usr/local/sbin/termini-inference-switch gemini-proxy'
ssh ubuntu@34.93.190.64 'curl -s localhost:8085/health'      # unchanged
ssh ubuntu@10.10.0.13 'sudo /usr/local/sbin/termini-inference-switch local'  # restore
#   expect: the provider /health is byte-identical before and after the flip

# (e) the demo still decides in seconds, and the fleet re-registers
ssh ubuntu@34.93.190.64 'sqlite3 /data/controller.db \
  "select decision_source, round(latency_ms) from decisions order by ts desc limit 5"; \
  curl -s localhost:8080/v1/projects/01J0DEM0WARM000000000000AB/nodes'
#   expect: decision_source=model with a seconds-scale latency; node rows present
```

Still to execute **before** any of the above, once a window is approved — these
three steps are the deployment, and they are the ones that touch the fleet:

```bash
# 1. provider SA with the right roles + cloud-platform scope on the controller
gcloud iam service-accounts create termini-provider --display-name="Termini provider diagnosis"
for r in roles/aiplatform.user roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding termini-510609 \
    --member="serviceAccount:termini-provider@termini-510609.iam.gserviceaccount.com" --role="$r"
done
# 2. ANNOUNCED downtime: stop → set-service-account → start
gcloud compute instances stop termini-controller --zone=asia-south1-a
gcloud compute instances set-service-account termini-controller --zone=asia-south1-a \
  --service-account=termini-provider@termini-510609.iam.gserviceaccount.com --scopes=cloud-platform
gcloud compute instances start termini-controller --zone=asia-south1-a
# 3. stage the payload and install the provider surface on the controller
scp infra/inference/{proxy.py,termini-inference-provider.service,setup-inference-surfaces.sh} \
  ubuntu@34.93.190.64:/usr/local/src/termini-inference/
ssh ubuntu@34.93.190.64 'sudo /usr/local/src/termini-inference/setup-inference-surfaces.sh'
# 4. and pin Node C's agent surface to local
ssh ubuntu@10.10.0.13 'sudo /usr/local/sbin/termini-inference-switch local'
```

No service-account key file is created by any of this: the controller's metadata
identity is the credential (the same rule `t_5a4a0f73` established for Node C).
