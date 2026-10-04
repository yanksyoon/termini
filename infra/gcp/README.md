# Termini — GCP node footprint (Nodes A, B, C)

Reproducible IaC that creates the three-node Termini footprint on Google Cloud:
**two compute nodes (A, B)** and **one CPU-only inference node (C)**, on a single
shared underlay network, with the frozen firewall/port plan and an inventory file
other workstreams consume.

- Task: kanban `t_f2173236` (infra)
- Decisions this implements: `docs/contracts/infra-decisions.md` (D2 networking,
  D5 inference, D6 control topology) and `docs/contracts/node-bootstrap.md` §3.
- **Node C is CPU-only.** The GPU (L4) was dropped by user decision: the demo
  project sits on a Google Cloud *free-tier* billing account, which rejects every
  non-TPU accelerator with HTTP 400. Node C therefore runs `llama.cpp` on CPU
  with a small quantized model. The accelerator variables are kept so the GPU can
  be re-enabled after a billing upgrade (see below).
- Two interchangeable paths, both create the **exact same footprint**:
  - **Terraform** (`*.tf`) — idempotent, plan/apply.
  - **gcloud script** (`provision.sh`) — one-shot fallback when Terraform is unavailable.

## Chosen machine types and CIDR (the documented values)

| Node | Role | Machine type | Accelerator | Underlay IP | Guest bridge |
|------|------|--------------|-------------|-------------|--------------|
| `node-a` | compute (LXD seed) | `n2-standard-4` | — | `10.10.0.11/24` | `10.20.1.0/24` |
| `node-b` | compute | `n2-standard-4` | — | `10.10.0.12/24` | `10.20.2.0/24` |
| `node-c` | inference (CPU) | `n2-standard-8` | — (CPU-only) | `10.10.0.13/24` | `10.20.3.0/24` |

- **Underlay CIDR: `10.10.0.0/24`** (single VPC + single subnetwork shared by all
  three nodes), gateway `10.10.0.1`. This is the D2 addressing plan, frozen.
- **Nodes A/B** default to `n2-standard-4` (compute-optimized), overridable via
  `compute_machine_type`.
- **Node C** is CPU-heavy so `llama.cpp` has cores to run a small model:
  `inference_machine_type = n2-standard-8` (8 vCPU / 32 GiB) by default. It has
  **no `guest_accelerator` block** while `gpu_accelerator_count = 0`.
- **Boot disk:** 60 GiB `pd-balanced` per node by default (the D7 loop-backed ZFS
  pool lives here).

### vCPU budget vs `CPUS_ALL_REGIONS`

The vCPU total must fit the project's `CPUS_ALL_REGIONS` quota (this demo project
is capped at **12** vCPU, and a quota-increase request was denied). The ideal
`4 + 4 + 8 = 16` layout does **not** fit; the demo tfvars sets
`compute_machine_type = n2-standard-2` so that `2 + 2 + 8 = 12` fits while keeping
Node C at `n2-standard-8`. Restore A/B to `n2-standard-4` (and/or raise the quota)
when the cap allows.

## Ports opened (D2 port map)

| Port | Proto | Target | Source |
|------|-------|--------|--------|
| 22 | TCP | all nodes | `ssh_source_ranges` (demo default `0.0.0.0/0` — **tighten this**) |
| 443 | TCP | all nodes | underlay `10.10.0.0/24` (controller registration/API) |
| 8443 | TCP | all nodes | underlay `10.10.0.0/24` (LXD cluster API / OVN) |
| 4789 | UDP | all nodes | underlay `10.10.0.0/24` (VXLAN transport) |
| 8080 | TCP | **node-c only** | underlay `10.10.0.0/24` (llama.cpp, never public) |

Every resource carries `labels = {project, managed-by, workstream, task}` and
network tags `termini-node` (+ per-node `termini-node-a|b|c`) so the footprint
tears down cleanly (`deletion_protection = false`).

## Node cloud-init (shared bootstrap template)

Every node's `user-data` is rendered at plan/apply time from the **shared bootstrap
template** `infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl` via Terraform
`templatefile()` (task `t_5e9c9d96`). `infra/gcp/cloud-init/node.yaml.tftpl` is a
**symlink** to that file, so the GCP footprint and the LXD bootstrap flow
(`infra/bootstrap`) boot from byte-identical cloud-init — there is no second copy
to drift, and no placeholder CA line.

Per node it:

* installs the **team SSH public key** (`team_ssh_pubkey`, REQUIRED) into the
  default user's and root's `authorized_keys` (idempotent);
* writes the **controller SSH CA public key** (`controller_ssh_ca_pubkey`) as
  `/etc/ssh/termini_ca.pub` + `TrustedUserCAKeys`, and hardens sshd
  (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`);
* writes `/etc/termini/config.yaml` + the per-node `enrollment_tokens[<node>]`,
  then starts `termini-bootstrap.service`, which registers with the controller
  (`node-bootstrap.md` §3.2/§5/§7).

The gcloud fallback (`provision.sh`) renders the **same template** through
`infra/gcp/render-cloud-init.sh` (envsubst), so both IaC paths emit the same
user-data. Roles that need more than the base flow pass **extra cloud-config
fragments** (the `extra_write_files` / `extra_runcmd` template variables); the
template itself hardcodes no role-specific block — Node C uses this for its
llama.cpp server below.

`verify-cloud-init.sh` checks the rendering offline and, with `--host USER@IP`,
proves a real GCP node accepts the injected team key and registers; evidence is in
`evidence/cloud-init-verify.txt`.

## Node C CPU llama.cpp serving

Nodes A/B are strictly activation-only (D3 cloud-init: config file + bootstrap
service, no installs, no downloads). Node C is the one exception: when
`serve_inference = true` (default), `main.tf` injects `cloud-init/llama-serve.sh`
into its user-data as the `extra_write_files`/`extra_runcmd` fragments (the shared
template appends them; it hardcodes no inference block). That script on first boot:

1. installs `build-essential cmake git curl`,
2. clones and builds **CPU-only, static** `llama-server` at the pinned commit
   (`llamacpp_commit`, default `7fe450e1…`, matching `image/models.lock.json`),
3. downloads the pinned **`deepseek-flash`** GGUF
   (`bartowski/DeepSeek-R1-Distill-Llama-8B-Q4_K_M`, 4.92 GB, sha256-pinned in
   `image/models.lock.json`) to `/var/lib/termini/models/` and **aborts the boot
   if the sha256 does not match**,
4. runs it as `termini-llama-server.service` on **`0.0.0.0:8080`** as an
   OpenAI-compatible single-model deployment aliased **`deepseek-flash`**
   (`agent-runtime.md` AR-4/AR-6; `--reasoning-format none` so the R1 ` thinking`
   block stays inline in `content` and plain clients always see text).

The underlay firewall rule allows `10.10.0.0/24 → node-c:8080` only, so the
endpoint is reachable from Node A but never public. Health probe:
`curl http://10.10.0.13:8080/health`. The first build takes ~10–20 min; the
service is idempotent across reboots (`/var/log/termini-llama-serve.log`).
The pinned 8B Q4_K_M runs at ~6 tok/s (prompt ~12 tok/s) on `n2-standard-8` CPU.

Acceptance for the endpoint itself (Node A → Node C completion + streaming) is
verified by `infra/inference/verify-inference.sh`; see `infra/inference/`.

Tunables: `serve_inference`, `inference_port`, `inference_model_url`,
`inference_model_file`, `inference_model_sha256`, `inference_model_alias`,
`llamacpp_commit`.

## Re-enabling the GPU (after a billing upgrade)

The accelerator variables are preserved, defaulted off:

```hcl
inference_machine_type = "g2-standard-4"   # ships one NVIDIA L4
gpu_accelerator_count  = 1                 # 0 = CPU-only (default)
gpu_accelerator_type   = "nvidia-l4"
```

With `gpu_accelerator_count = 1` the instance template emits the
`guest_accelerator` block again, pins `on_host_maintenance = TERMINATE` (GPU
instances cannot live-migrate), and the inventory records
`accelerator = { type, count }` on Node C. `inference_machine_type` must then be
a GPU machine type. Requires a **paid** billing account: free-tier rejects
non-TPU accelerators (`Error 400: … free tier where non-TPU accelerators are not
available`). Also ensure `serve_inference` builds the GPU variant or swap the
binary for the pre-baked image (`image/pipeline`, D3).

## Inventory (downstream contract)

Both paths write a JSON inventory to the **well-known path
`/home/ubuntu/.termini/inventory.json`** (override with `inventory_path` /
`--inventory`). Its shape is frozen by `inventory.schema.json`; an example lives
at `examples/inventory.example.json`.

```json
{
  "schema_version": "1.0",
  "nodes": [
    { "name": "node-a", "role": "compute",   "machine_type": "n2-standard-2", "private_ip": "10.10.0.11", "public_ip": "...", "accelerator": null },
    { "name": "node-c", "role": "inference", "machine_type": "n2-standard-8", "private_ip": "10.10.0.13", "public_ip": "...", "accelerator": null }
  ]
}
```

Read it with `jq`, e.g. `jq -r '.nodes[] | "\(.name) \(.public_ip)"' ~/.termini/inventory.json`.

## Usage — Terraform

```bash
cd infra/gcp
gcloud auth application-default login          # or set GOOGLE_APPLICATION_CREDENTIALS
cp terraform.tfvars.example terraform.tfvars   # set `project`
terraform init
terraform apply                                 # creates exactly 3 VMs
terraform output nodes
```

## Usage — gcloud fallback (no Terraform)

```bash
cd infra/gcp
PROJECT=my-project ./provision.sh               # creates exactly 3 VMs + inventory
```

## Verification (acceptance)

```bash
cd infra/gcp
./verify.sh                                     # inventory + SSH + no-accelerator + A->C inference
./verify-cloud-init.sh                          # shared cloud-init renders clean (offline)
./verify-cloud-init.sh --host ubuntu@<node-ip>  # + live: injected team key SSH + registration
```

`verify.sh` checks: inventory valid & 3 nodes; **no node declares an
accelerator**; every node SSH-reachable; Node C has no accelerator device
(`lspci`); and Node A can reach `http://10.10.0.13:8080/health` on Node C over
the underlay. `--wait SECONDS` (default 1800) waits for the first-boot CPU build.

Offline (no cloud) checks that need no credentials:

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
bash -n provision.sh verify.sh teardown.sh cloud-init/llama-serve.sh && shellcheck -S warning *.sh cloud-init/llama-serve.sh
jq -e . inventory.schema.json examples/inventory.example.json
```

## Teardown

```bash
cd infra/gcp
terraform destroy                               # or: PROJECT=my-project ./teardown.sh --remove-inventory
```

## Prerequisites (out of band)

`apply`/`provision.sh` need a live GCP project with:
- **Compute API enabled** and **billing attached**;
- credentials with `compute.instances.*`, `compute.networks.*`, `compute.firewalls.*`,
  `compute.addresses.*`;
- **vCPU headroom**: the footprint must fit `CPUS_ALL_REGIONS` (16 vCPU for the
  default A/B=`n2-standard-4` + C=`n2-standard-8`; 12 with A/B=`n2-standard-2`);
- **no GPU quota is required** for the CPU-only default. Re-enabling the GPU needs
  `NVIDIA_L4_GPUS` (or `GPUS_ALL_REGIONS`) ≥ 1 in the region **and a paid billing
  account**.

> No credentials are stored in this repo. Pass them via the standard Google auth
> chain (ADC file, `GOOGLE_APPLICATION_CREDENTIALS`, or an attached service account).

## Live deployment record (task `t_f2173236`, CPU-only re-provision)

Re-provisioned from a clean project on the supplied free-tier billing account:

| Item | Value |
|------|-------|
| Project | `termini-510609` |
| Region / zone | **`asia-south1` / `asia-south1-a`** |
| Underlay CIDR | `10.10.0.0/24` (VPC `termini-vpc`, subnet `termini-underlay`) |
| Node A | `n2-standard-2` @ `10.10.0.11` — RUNNING, no accelerator, SSH OK |
| Node B | `n2-standard-2` @ `10.10.0.12` — RUNNING, no accelerator, SSH OK |
| Node C | `n2-standard-8` @ `10.10.0.13` — RUNNING, no accelerator, SSH OK |
| llama.cpp | Node C `:8080` (pinned `deepseek-flash` = DeepSeek-R1-Distill-Llama-8B Q4_K_M, alias `deepseek-flash`), reachable from Node A over the underlay |
| Inventory | `/home/ubuntu/.termini/inventory.json` (valid vs `inventory.schema.json`) |

**Why A/B are `n2-standard-2` here.** The project's `CPUS_ALL_REGIONS` quota is
12 and a Cloud Quotas increase request (`CPUS-ALL-REGIONS-per-project` → 24) was
auto-denied (`stateDetail: "Quota request denied"`). `4 + 4 + 8 = 16` exceeds the
cap; `2 + 2 + 8 = 12` fits and keeps Node C CPU-heavy. Restore A/B to
`n2-standard-4` once the quota is raised.

**GPU history.** The prior run of this card created Nodes A/B but Node C
(`g2-standard-4` + `nvidia-l4`) was rejected by free-tier billing. Nodes A/B, the
VPC, firewall rules and static IPs were then torn down; this run starts clean and
re-provisions all three with a CPU-only Node C.

**Inference endpoint activation (`t_95528ae8`, W3).** On the live footprint Node C
was switched from the stopgap Qwen2.5-1.5B weight to the pinned **`deepseek-flash`**
GGUF from `image/models.lock.json` (t_ab18d582), served with `--alias deepseek-flash`
so `/v1/models` and completions report the frozen contract alias. GPU device
visibility was re-confirmed absent (`nvidia-smi` absent, `lspci` no NVIDIA device,
no `/dev/nvidia*`) — the free-tier billing account rejects non-TPU accelerators, so
host-exposed CPU llama.cpp is the sanctioned D5 path. Acceptance ("a request from
Node A to Node C returns a completion; streaming works") is captured in
`infra/inference/evidence/` and reproducible with `infra/inference/verify-inference.sh`.

## Status / scope note

This module is the provisioning artifact. The exact acceptance sequence is
`terraform apply` → `./verify.sh` (or `provision.sh` → `./verify.sh`). See the
task card for the verification evidence captured against the live footprint.
