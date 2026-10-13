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

The vCPU total must fit the project's `CPUS_ALL_REGIONS` quota. This demo project
is capped at **12**, and the increase was **denied** — a quota preference for 24
sits at `stateDetail: Quota request denied` with `grantedValue: 12`.

The ideal `4 + 4 + 8 = 16` node layout does **not** fit, so the demo tfvars sets
`compute_machine_type = n2-standard-2`, giving the nodes `2 + 2 + 8 = 12`.

**Since Phase 3 the controller VM consumes quota too**, which is why `node-b` is
`TERMINATED`:

| resource | vCPU | state |
|---|---|---|
| `node-a` | 2 | RUNNING (`n2-standard-2`) |
| `node-c` | 8 | RUNNING (`n2-standard-8`) |
| `termini-controller` | 2 | RUNNING (`n2-standard-2`) |
| `node-b` | 2 | **TERMINATED** — including it would make 14 > 12 |
| **total** | **12 / 12** | |

`node-b` was stopped purely to free its 2 vCPU for the controller. The demo does
not need it: `console/seed_demo.py` fabricates `demo-node-a`/`demo-node-c`, and
neither it nor the operator loop touches node-b — its LXD hosted zero containers
when it was stopped.

To run all four again, free 2 vCPU. **`node-c`'s 8 vCPUs are in use**: they host the
`llama.cpp` inference service (`termini-llama-server`, `:8080`) that the operator's
decision calls reach over the underlay, so downsizing `node-c` would remove the model
host, not reclaim idle headroom. The routes are: keep `node-c` at `n2-standard-8` and
leave `node-b` terminated (`2 + 8 + 2 = 12`), or raise `CPUS_ALL_REGIONS` — which has
already been denied once. (An off-prem decision model on Cloud Run GPU would free the
headroom, but the project's free-tier billing account rejects every non-TPU accelerator,
so that route is not available here.)


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

## Node C CPU llama.cpp serving

Nodes A/B are strictly activation-only (D3 cloud-init: config file + bootstrap
service, no installs, no downloads). Node C is the one exception: when
`serve_inference = true` (default) its cloud-init also runs
`cloud-init/llama-serve.sh`, which on first boot:

1. installs `build-essential cmake git curl`,
2. clones and builds **CPU-only, static** `llama-server` at the pinned commit
   (`llamacpp_commit`, default `7fe450e1…`, matching `image/models.lock.json`),
3. downloads the pinned **`gemma`** GGUF (**Gemma 3 4B IT**, `unsloth/gemma-3-4b-it-GGUF`,
   2.49 GB, sha256-pinned in `image/models.lock.json`) to `/var/lib/termini/models/` **from
   the project's own bucket** (`gs://termini-510609-models/gemma-3-4b-it-gguf/`, D3 — never a
   public registry at first boot) and **aborts the boot if the sha256 does not match**,
4. runs it as `termini-llama-server.service` on **`0.0.0.0:8080`** as an
   OpenAI-compatible single-model deployment aliased **`gemma`**
   (`agent-runtime.md` AR-4/AR-6; `--ctx-size 16384 --parallel 1 --threads 8`, and
   `--reasoning-format none` so any inline reasoning block stays in `content`).

The underlay firewall rule allows `10.10.0.0/24 → node-c:8080` only, so the
endpoint is reachable from Node A but never public. Health probe:
`curl http://10.10.0.13:8080/health`. The first build takes ~10–20 min; the
service is idempotent across reboots (`/var/log/termini-llama-serve.log`).
The pinned Gemma 3 4B Q4_K_M runs at ≈33 tok/s prompt and ≈8–10 tok/s generation
on `n2-standard-8` CPU.

Acceptance for the endpoint itself (Node A → Node C completion + streaming) is
verified by `infra/inference/verify-inference.sh`; see `infra/inference/`.

Tunables: `serve_inference`, `inference_port`, `inference_model_url`,
`inference_model_file`, `inference_model_sha256`, `inference_model_alias`,
`llamacpp_commit`.

## Cloud-init: unified with the node bootstrap (task `t_5e9c9d96`)

`cloud-init/node.yaml.tftpl` is a **symlink** to the canonical node bootstrap
template `infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl`, so the GCP VMs
and the LXD node flow render **one** cloud-init (no drift). Every node gets the
same SSH custody as the LXD flow: the team SSH public key injected into
`authorized_keys` (idempotent), `ssh_pwauth: false` + key-only root (`sshd`
policy `PasswordAuthentication no`, `PermitRootLogin prohibit-password`,
`TrustedUserCAKeys`), `/etc/termini/config.yaml`, the single-use
`enrollment_token`, and a start of `termini-bootstrap.service`.

Inputs: `team_ssh_public_key` (falls back to `ssh_public_key`;
empty = activation-only), `enrollment_token`, `controller_ssh_ca_pubkey`,
`node_image_version`. Because `envsubst` has no conditionals, the optional YAML
is built by the **caller** and handed to the template as plain
`ssh_authorized_keys_block` / `extra_write_files` / `extra_runcmd` variables:

* **GCP Terraform** — `main.tf` renders it through the provider-free
  `module "userdata"` (`infra/gcp/userdata/`). The Node C `llama-serve.sh` block
  arrives as the optional `extra_write_files` + `extra_runcmd` (the bootstrap
  template hardcodes no Node C behaviour).
* **GCP gcloud fallback** — `provision.sh` renders it through the same
  `render-cloud-init.sh` (`--render-only` emits it with no gcloud calls).
* **LXD flow** — `infra/bootstrap/render-cloud-init.sh` (envsubst).

Verify the render offline (no project, no credentials, no state):

```bash
./verify-cloud-init.sh
```

It renders all three nodes through the module and asserts: symlink identity, no
`placeholder`/unresolved token, team key + sshd hardening + CA + config/token on
every node, the llama.cpp block on Node C only, valid YAML, and that the same
template still renders through envsubst.

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

## Adopting the baked node image (GCE)

A GCE node image now exists that carries the bootstrap daemon, so a node can boot
**pre-baked** and register with **no installs at boot** (D3):

| | |
|---|---|
| image | `termini-node-os-1`, family `termini-node-os`, 20 GB |
| contains | `/usr/local/bin/termini-bootstrap`, `termini-bootstrap.service` (enabled), `/var/lib/termini/.baked-offline` |
| hypervisor | LXD (snap `5.21/stable`, revision held), **initialised** — `lxd init --preseed` ran at bake time, so a `default` dir pool + `lxdbr0` bridge + `default` profile exist and the API listens on `:8443` (the port the node contract and the GCP firewall rule expect). Without this a booted node has the `lxc` binary but no daemon and no pool |
| workload image | the guest the controller launches (`agent-vm/2026.10.0`, per-node `agent-os/w4`) is preloaded into the image's **local LXD image store** under both aliases (`image/pipeline`, D3) — so the operator's `instance.create` has a launch target at boot |

> The LXD init and the workload-image preload are the two things that make a
> booted node actually *usable* rather than merely *registered*. Both are wired
> in `image/pipeline` and asserted by `build-image.sh --check`; confirming them on
> a real node (`lxc image list` shows the workload image) needs a fresh bake.

**Why this matters:** the deploy still defaults to stock `ubuntu-2404-lts-amd64`, on
which the activation-only cloud-init finds no `termini-bootstrap.service` and the
node **never registers**. It does say so, in the cloud-init runcmd output:

> `termini-bootstrap.service not installed on this image; node will not register
> (bake the daemon into the node image)`

so the failure is diagnosable, but it is silent to anyone not reading that log.

Measured on a node created from the baked family, from the daemon's own timing line:

```
[termini-bootstrap] REGISTERED role=compute register_ms=150 boot_to_registered_ms=22840
```

`register_ms` is the contract's `< 5 s` budget (daemon start → controller ack), met
with ~17× margin. `boot_to_registered_ms` is from VM power-on and is dominated by
firmware/OS/cloud-init.

To adopt it, in `terraform.tfvars`:

```hcl
image_project = "termini-510609"     # default is the generic ubuntu-os-cloud
image_family  = "termini-node-os"
```

**This recreates the node VMs.** `image_family` is a boot-disk property, so
`terraform plan` will show `termini-node-a/b/c` being **replaced** — schedule it as a
migration, not as a side effect of an unrelated apply.

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
# TEAM_SSH_PUBKEY_FILE + ENROLLMENT_TOKEN are required so each node gets the same
# SSH custody + registration inputs as the Terraform path (task t_5e9c9d96).
TEAM_SSH_PUBKEY_FILE=~/.ssh/team.pub ENROLLMENT_TOKEN=... \
  PROJECT=my-project ./provision.sh             # creates exactly 3 VMs + inventory

# Render the cloud-init for node-a/b/c only (no gcloud, no credentials):
TEAM_SSH_PUBKEY_FILE=~/.ssh/team.pub ENROLLMENT_TOKEN=... \
  ./provision.sh --render-only --render-out /tmp/cloud-init
```

## Verification (acceptance)

```bash
cd infra/gcp
./verify.sh                                     # inventory + SSH + no-accelerator + A->C inference
```

`verify.sh` checks: inventory valid & 3 nodes; **no node declares an
accelerator**; every node SSH-reachable; Node C has no accelerator device
(`lspci`); and Node A can reach `http://10.10.0.13:8080/health` on Node C over
the underlay. `--wait SECONDS` (default 1800) waits for the first-boot CPU build.

Offline (no cloud) checks that need no credentials:

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
bash -n provision.sh verify.sh teardown.sh verify-cloud-init.sh cloud-init/llama-serve.sh && shellcheck -S warning *.sh cloud-init/llama-serve.sh
jq -e . inventory.schema.json examples/inventory.example.json
./verify-cloud-init.sh                          # renders + asserts the unified cloud-init
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
| llama.cpp | Node C `:8080` (pinned `gemma` = Gemma 3 4B IT Q4_K_M, alias `gemma`), reachable from Node A over the underlay |
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

**Inference endpoint activation (`t_95528ae8`, W3; re-pointed to Gemma 2026-10-13).** On the
live footprint Node C was first switched from the stopgap Qwen2.5-1.5B weight to the pinned
**`deepseek-flash`** GGUF, then (2026-10-13) to the pinned **`gemma`** GGUF — **Gemma 3 4B IT**,
Google's own open weights — served with `--alias gemma` so `/v1/models` and completions report
the deployed alias. The GGUF is fetched from the project's own bucket (D3) and sha256-verified
before the unit starts. GPU device
visibility was re-confirmed absent (`nvidia-smi` absent, `lspci` no NVIDIA device,
no `/dev/nvidia*`) — the free-tier billing account rejects non-TPU accelerators, so
host-exposed CPU llama.cpp is the sanctioned D5 path. Acceptance ("a request from
Node A to Node C returns a completion; streaming works") is captured in
`infra/inference/evidence/` and reproducible with `infra/inference/verify-inference.sh`.

## Status / scope note

This module is the provisioning artifact. The exact acceptance sequence is
`terraform apply` → `./verify.sh` (or `provision.sh` → `./verify.sh`). See the
task card for the verification evidence captured against the live footprint.
