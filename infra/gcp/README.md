# Termini — GCP node footprint (Nodes A, B, C)

Reproducible IaC that creates the three-node Termini footprint on Google Cloud:
**two compute nodes (A, B)** and **one real GPU node (C)**, on a single shared
underlay network, with the frozen firewall/port plan and an inventory file other
workstreams consume.

- Task: kanban `t_f2173236` (infra)
- Decisions this implements: `docs/contracts/infra-decisions.md` (D2 networking,
  D5 GPU, D6 control topology) and `docs/contracts/node-bootstrap.md` §3.
- Two interchangeable paths, both create the **exact same footprint**:
  - **Terraform** (`*.tf`) — idempotent, plan/apply.
  - **gcloud script** (`provision.sh`) — one-shot fallback when Terraform is unavailable.

## Chosen machine types and CIDR (the documented values)

| Node | Role | Machine type | Accelerator | Underlay IP | Guest bridge |
|------|------|--------------|-------------|-------------|--------------|
| `node-a` | compute (LXD seed) | `n2-standard-4` | — | `10.10.0.11/24` | `10.20.1.0/24` |
| `node-b` | compute | `n2-standard-4` | — | `10.10.0.12/24` | `10.20.2.0/24` |
| `node-c` | inference (GPU) | `g2-standard-4` | `nvidia-l4` × 1 | `10.10.0.13/24` | `10.20.3.0/24` |

- **Underlay CIDR: `10.10.0.0/24`** (single VPC + single subnetwork shared by all
  three nodes), gateway `10.10.0.1`. This is the D2 addressing plan, frozen.
- **Compute nodes** use `n2-standard-4` (4 vCPU / 16 GiB) — compute-optimized,
  overridable via `compute_machine_type`.
- **GPU node** uses `g2-standard-4`, which ships exactly **one NVIDIA L4** (24 GB,
  AD102). The accelerator is attached through the `guest_accelerator` block and
  scheduling is pinned to `on_host_maintenance = TERMINATE` (GPU instances cannot
  live-migrate). Override with `gpu_machine_type` / `gpu_accelerator_type`.
- **Boot disk:** 60 GiB `pd-balanced` per node by default (the D7 loop-backed ZFS
  pool lives here).

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

## Inventory (downstream contract)

Both paths write a JSON inventory to the **well-known path
`/home/ubuntu/.termini/inventory.json`** (override with `inventory_path` /
`--inventory`). Its shape is frozen by `inventory.schema.json`; an example lives
at `examples/inventory.example.json`.

```json
{
  "schema_version": "1.0",
  "nodes": [
    { "name": "node-a", "role": "compute",   "machine_type": "n2-standard-4", "private_ip": "10.10.0.11", "public_ip": "...", "accelerator": null },
    { "name": "node-c", "role": "inference", "machine_type": "g2-standard-4", "private_ip": "10.10.0.13", "public_ip": "...", "accelerator": { "type": "nvidia-l4", "count": 1 } }
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
./verify.sh                                     # inventory + SSH + nvidia-smi + A->C inference
```

`verify.sh` checks: inventory valid & 3 nodes; every node SSH-reachable;
`nvidia-smi -L` works on Node C; and (optionally) Node A can reach
`http://10.10.0.13:8080/health` on Node C over the underlay.

Offline (no cloud) checks that need no credentials:

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
bash -n provision.sh verify.sh teardown.sh && shellcheck -S warning *.sh
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
- **GPU quota**: `NVIDIA_L4_GPUS` (or `GPUS_ALL_REGIONS`) ≥ 1 in the chosen region,
  and a zone that offers L4 (`us-central1-a/b/c/f`, `us-east1-*`, …);
- **paid billing**: GPU/accelerator attach is rejected on a Google Cloud *free-tier*
  billing account (`Error 400: Your billing account is currently in the free tier
  where non-TPU accelerators are not available`). Node C requires an **upgraded
  (paid) billing account**; Nodes A/B do not.

> No credentials are stored in this repo. Pass them via the standard Google auth
> chain (ADC file, `GOOGLE_APPLICATION_CREDENTIALS`, or an attached service account).

## Live deployment record (task `t_f2173236`)

First real `terraform apply` against the project supplied for the Termini demo:

| Item | Value |
|------|-------|
| Project | `termini-510609` |
| Region / zone | **`asia-south1` / `asia-south1-a`** |
| Underlay CIDR | `10.10.0.0/24` (VPC `termini-vpc`, subnet `termini-underlay`) |
| Node A | `n2-standard-4` @ `10.10.0.11` — **created, SSH verified** |
| Node B | `n2-standard-4` @ `10.10.0.12` — **created, SSH verified** |
| Node C | `g2-standard-4` + `nvidia-l4` @ `10.10.0.13` — **BLOCKED** |

**Blocker on Node C (and why 2 of 3 nodes exist).** The supplied project is on a
Google Cloud *free-tier* billing account, so the Compute API refuses to attach any
non-TPU accelerator:

```
Error 400: Your billing account is currently in the free tier where non-TPU
accelerators are not available. Please upgrade to a paid billing account …
```

This is account-wide, not zonal: `NVIDIA_L4_GPUS` quota is 1 in both
`asia-south1` and `us-central1`, and a **SPOT** `g2-standard-4` attempt failed with
the identical error — no provisioning model or zone works around it. Node C
therefore cannot be created until the billing account is upgraded.

**State after the partial apply** (Terraform local state, 13/14 resources):
VPC, subnetwork, all three static internal addresses, the five firewall rules and
Nodes A/B are live. Node C and the inventory `local_file` are the only resources
missing, and the inventory is gated on all three instances.

**To finish once billing is upgraded** — the same plan resumes, creating only the
GPU node and the inventory:

```bash
cd infra/gcp
terraform apply                 # creates node-c + /home/ubuntu/.termini/inventory.json
./verify.sh                     # 3 nodes, SSH, nvidia-smi on node-c
```

**To tear the partial footprint down:**

```bash
cd infra/gcp
terraform destroy
```

## Status / scope note

This module is the provisioning artifact. The exact acceptance sequence is
`terraform apply` → `./verify.sh` (or `provision.sh` → `./verify.sh`). Both paths
were authored to be run against a real project; see the task card for the
verification evidence actually captured on this host.

