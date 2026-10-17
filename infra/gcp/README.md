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

## Node C CPU llama.cpp serving — BEHIND the inference front door

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
4. starts the dedicated intent-extraction instance — `termini-llama-intent.service`
   on **`0.0.0.0:8090`** (`--ctx-size 4096 --parallel 1 --threads 3`) — so
   `POST /intents` extraction never queues behind the operator's ~12,000-token,
   ~400 s-on-CPU decision prompt (controller-api.md §5.2). The controller reaches
   it with `TERMINI_INTENT_INFERENCE_BASE_URL`, and
5. **installs the inference FRONT DOOR in the same boot** by CALLING
   `infra/inference/install-inference-proxy.sh` (staged by cloud-init, byte-exact
   — task `t_359ed606`). A node created from the image family therefore serves
   the model with **no manual step**: `termini-inference-proxy.service`
   answers on `0.0.0.0:8080`, and llama.cpp becomes the **loopback worker** on
   `127.0.0.1:8081` (`--ctx-size 32768 --parallel 2 --threads 6`,
   `--reasoning-format none`, alias `gemma`).

**What a fresh node serves is explicit and one variable away.**
`inference_backend` (default **`gemini-proxy`**) is substituted into the first
boot as `TERMINI_INFERENCE_BACKEND`. `gemini-proxy` is the shipped configuration
since the pivot (**kanban `t_957956e5` / `t_3fcee90b`**, which rolls back the
inference split of D9, `t_aa7a70f9`): `0.0.0.0:8080` is the **agent-facing**
surface and it is answered by the forwarded managed model (Google AI, through the
proxy's `generateContent` translation), labelled in-band so a client can always
tell where its tokens came from. The reason is measured — the live-GPU path is
blocked by the L4 quota and this node's own CPU weights measured **402 s** on the
operator's prompt — so the proxy is the path that works today. The **on-prem**
configuration, `inference_backend = "local"`, is still supported and is now the
**one-variable fallback**: it serves that node's own weights through the front
door and forwards nothing, one command away on a running node
(`termini-inference-switch local`); the pre-proxy host-exposed server is
`termini-inference-switch legacy`, and `status` reports what is actually serving
(`/run/termini/inference-backend.json`). The controller keeps its **provider-only
diagnosis listener** (`127.0.0.1:8085`, `gemini-proxy` →
`aiplatform.googleapis.com`), installed by
`infra/inference/setup-inference-surfaces.sh`; see `infra/inference/`.

The underlay firewall rules allow `10.10.0.0/24 → node-c:8080` (the front door,
i.e. the frozen inference address) and `10.10.0.0/24 → node-c:8090` (the
intent-extraction model) only, so both endpoints are reachable from the underlay
but never public. The worker on `127.0.0.1:8081` is loopback-only by design (AR-4).
Health probes: `curl http://10.10.0.13:8080/health` and
`curl http://10.10.0.13:8090/health`. The first build takes ~10–20 min; the
whole block is idempotent across reboots (`/var/log/termini-llama-serve.log`), and
`/run/termini` is recreated for `termini-infer` by a `tmpfiles.d` rule the
installer writes, so a rebooted node still records its active backend.
The pinned Gemma 3 4B Q4_K_M runs at ≈33 tok/s prompt and ≈8–10 tok/s generation
on `n2-standard-8` CPU.

Acceptance for the endpoint itself (Node A → Node C completion + streaming) is
verified by `infra/inference/verify-proxy.sh` (run from a PEER) and
`infra/inference/verify-inference.sh`; see `infra/inference/`.

Tunables: `serve_inference`, `inference_backend`, `inference_port`,
`inference_model_url`, `inference_model_file`, `inference_model_sha256`,
`inference_model_alias`, `llamacpp_commit`.

The first-boot block (the serve script + the front-door payload) is rendered by
**one** shared emitter for the gcloud paths
(`cloud-init/node-c-serve-block.sh`) and natively by the `userdata` module for
Terraform; `verify-cloud-init.sh` decodes both renders and asserts the payload is
byte-identical to `infra/inference/`, and `tests/test_node_c_first_boot.py` pins
the same property on every PR.

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
every node, the llama.cpp block on Node C only, valid YAML, that the same
template still renders through envsubst — and (task `t_359ed606`) that Node C's
first boot carries the inference **front door** with a byte-exact payload, that
the non-inference nodes carry none of it, and that the first-boot backend is
**pinned** to the shipped `gemini-proxy` (the managed-model proxy, `t_3fcee90b`)
— a `local` expectation is rejected, so the check proves the render pins a value
rather than inheriting a default.

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

## Controller LXD trust on every node (part of the raise path)

LXD authenticates the controller per node, against that node's own trust store
(`lxc config trust list`). An unsupervised node answers `GET /1.0` with 200 and
`GET /1.0/instances` with **403**, so a plan that resolves to it applies to
`state: failed` and creates nothing — proven live on node-c
(`infra/gcp/evidence/node-c-lxd-trust-2026-10-17.md`, task `t_d31b8e02`).

A recreated instance replaces its boot disk, so its trust store comes back
**empty**. The install therefore belongs to the raise path, not to a one-off:

```bash
# done for you by tools/raise-standing-fleet.sh --raise (default; --no-trust-install skips)
infra/gcp/tools/install-lxd-trust.sh --node a --node c

# node-b is TERMINATED today (12/12 CPUS_ALL_REGIONS); when it is raised again
# it needs the same step, with no code change:
infra/gcp/tools/install-lxd-trust.sh --node b

# no mutation, prints what it would do; --all also skips an instance that does
# not exist (node-b), --timeout bounds the wait for a freshly booted node
infra/gcp/tools/install-lxd-trust.sh --all --dry-run
```

The helper probes first (an **authenticated** `GET /1.0/instances`), so a node
that is already trusted is a no-op; otherwise it mints a one-time
certificate-add trust token on the node with the node's own
`termini-bootstrap mint-trust-token` and consumes it on the controller with
`python3 -m app.lxdtrust trust --node … --token …`. It exits non-zero if the
node is not proven trusted afterwards. The token is single-use, is never
printed, and needs no secret from the operator. Verify at any time with:

```bash
# on the controller
cd /opt/termini/controller && PYTHONPATH=.:/opt/termini TERMINI_DB_PATH=/data/controller.db \
  python3 -m app.lxdtrust verify --node https://$NODE_IP:8443   # TRUSTED — … -> 200
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

The **egress + identity** probe (task `t_5a4a0f73`) is separate, because the
nodes have no external IP and can only reach Google APIs through Private Google
Access (`private_ip_google_access = true` on `termini-underlay`, encoded in
`main.tf`); Cloud NAT was deliberately **not** used — it costs money and Google
API traffic reached over PGA never touches the NAT meter:

```bash
./tools/verify-node-c-egress.sh    # PGA flag, least-privilege SA + cloud-platform
                                   # scope, no egress deny rules, then a real
                                   # metadata-token Vertex AI call FROM the node
```

It adds a temporary ephemeral external IP only to open the SSH transport (the
node is `--no-address`) and removes it again on exit.

Offline (no cloud) checks that need no credentials:

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
bash -n provision.sh verify.sh teardown.sh verify-cloud-init.sh cloud-init/llama-serve.sh cloud-init/node-c-serve-block.sh tools/cleanup-verify.sh tools/install-lxd-trust.sh tools/raise-standing-fleet.sh && shellcheck -S warning *.sh cloud-init/*.sh tools/*.sh
jq -e . inventory.schema.json examples/inventory.example.json
./verify-cloud-init.sh                          # renders + asserts the unified cloud-init
```

## Teardown — `--pause` vs `--full` (`teardown.sh`)

`teardown.sh` has **two explicit modes and no default mode**, and it refuses to
run without an explicit `--project/--region/--zone`. (The previous version
defaulted to `us-central1`/`us-central1-a` while the footprint lives in
`asia-south1`/`asia-south1-a`: every `describe` failed, every branch printed
`<name> absent`, and it ended with `teardown complete` **having deleted
nothing**.)

### "Instances terminated" is NOT "no cost" — the measured idle floor

Measured on the live project `termini-510609` on **2026-10-15**: all four GCE
instances (`termini-controller`, `termini-node-a/b/c`) were TERMINATED and
Cloud Run `termini-gemma` had scaled to zero — and the project still billed
**$37.77/mo**:

| idle resource | size / count | rate | $/mo |
|---|---|---|---|
| 4 `pd-balanced` disks (30+60+60+60) | 210 GiB | $0.120/GiB·mo | $25.20 |
| image storage `termini-node-os-1/3/5` | 60 GiB | $0.060/GiB·mo | $3.60 |
| static IP `termini-controller-ip` `34.93.190.64` | 1 | $0.012/h × 730 h | $8.76 |
| `gs://termini-510609-models` (Singapore) | 10.37 GiB | $0.020/GiB·mo | $0.21 |
| **idle floor while "stopped"** | | | **$37.77** |

A TERMINATED instance keeps billing every disk and image attached to it, and
Google bills an external static IP as IN USE while it is associated with a VM
**whether that instance is running or stopped** — which is why
`termini-controller-ip` bills while the controller is down.

Retention freeze through **6 Nov 2026** (user decision, kanban `t_59831eb3`):
the superseded images `termini-node-os-1` and `-3` were deleted (the family now
resolves to `termini-node-os-5`), so the idle floor today is **$35.37/mo** —
disks $25.20 + image 20 GiB $1.20 + static IP $8.76 + GCS $0.21. The static IP
*is* the published demo URL (`https://34-93-190-64.sslip.io/...`), so the demo
set stays until that date; `--full` refuses to touch it before then.

### Modes

```bash
cd infra/gcp
# PAUSE — stop what bills by the hour, keep everything expensive to rebuild
./teardown.sh --pause --project termini-510609 --region asia-south1 --zone asia-south1-a

# FULL — delete every billable resource (refuses while the demo freeze is active)
./teardown.sh --full  --project termini-510609 --region asia-south1 --zone asia-south1-a

# See every decision first; changes nothing:
./teardown.sh --full --dry-run --project ... --region ... --zone ...
```

**`--pause`** stops RUNNING instances (an already-TERMINATED one is left alone —
no mutation) and sets Cloud Run `min-instances=0` only when it is not already
zero. It deletes nothing: disks, images, the bucket and the reserved IP stay, so
the fleet is re-raisable without re-baking or re-fetching. Verified after a
`--pause`: the family `termini-node-os-5` still resolves, the model bucket still
holds its 17 objects, `tools/raise-standing-fleet.sh` still renders its
user-data, and a paused node restarts from its retained boot disk in 15 s.

**`--full`** deletes instances (including `termini-controller`) and their disks,
regional + global addresses and forwarding rules, images (every version of every
family, family pointer included), snapshots, machine images, reservations,
instance templates, the Cloud Run service and its revisions, the Artifact
Registry repository (drained first when it holds images), GCS buckets with their
objects, and non-Google-managed log buckets. It then re-runs the enumerator and
exits **2** if anything it intended to delete survived — a cleanup whose success
path cannot fail is not evidence.

Fail-loud semantics (`--full` **and** `--pause`):

* a `describe` that fails for any reason other than `NOT_FOUND` (403, disabled
  API, quota, malformed zone) **aborts the run** — it is never read as "absent";
* a declared zone/region that holds none of the project's instances/disks aborts
  the run and names where they actually live (`--allow-empty-scope` overrides);
* `--full` refuses to delete the protected demo set while the freeze is active
  (`--allow-protected-demo`, only with recorded user approval);
* there is no `|| true`, and every deletion is followed by a `describe` that must
  confirm the resource is gone.

Flags whose default is the conservative one: `--name-filter ERE` (scope a run,
e.g. `^termini-verify-`, safely), `--dry-run`, `--allow-empty-scope`,
`--allow-protected-demo`, `--delete-quota-preferences`, `--include-network`,
`--remove-inventory`, `--services-region`.

Known, API-bound limits — reported explicitly, never silently dropped:

* `_Default` and `_Required` log buckets are Google-managed and cannot be
  deleted;
* a log bucket with retained logs moves to `DELETE_REQUESTED` and is reclaimed
  when its retention window closes; no API lever removes it sooner, and it
  accepts no further logs;
* quota preference records are not billable, and `termini-cpus-all-regions`
  carries the granted `CPUS-ALL-REGIONS=12` the fleet depends on, so they go only
  with `--delete-quota-preferences`;
* VPC / subnet / firewall rules are free and are kept unless `--include-network`.

### The enumerator — pre-check AND post-check

`tools/cleanup-verify.sh` prints every billable-class resource in the project
with its class, region, size and whether it bills while idle, and exits:

| exit | meaning |
|---|---|
| 0 | nothing in the classes `--full` removes is present (within the filter scope) |
| 1 | such a resource is still present — the cleanup is NOT complete |
| 2 | an instrument failed: the answer is UNKNOWN, and never reported as clean |
| 3 | 0 in-remit, but a billable resource **outside** `--full`'s remit exists (Cloud SQL, GKE, Filestore, Redis, Dataproc, Vertex endpoints) |

```bash
cd infra/gcp
./tools/cleanup-verify.sh --project termini-510609 --region asia-south1 --zone asia-south1-a
./tools/cleanup-verify.sh ... --name-filter '^termini-verify-'   # scope a verification
```

It is also the positive control: run it against a project where resources exist
and it must report them (a zero from an enumerator that was never seen to report
a hit proves nothing). Cloud Logging buckets expose **no size instrument**, so
the tool prints `size=unknown` for them rather than a fabricated `0`.

Raw output of every run recorded for this work (throwaway end-to-end, the
`DELETE_REQUESTED` log-bucket case, the falsification battery, the real-project
dry run and the post-run footprint check) is in
`evidence/cleanup-2026-10-15.md`.

The Terraform path is unchanged and complementary: `terraform destroy` removes
only the three VMs (and their boot disks) that `*.tf` owns. It knows nothing
about the node images, `gs://…-models`, the Cloud Run service, the Artifact
Registry repository or the reserved IP — which is exactly the gap
`teardown.sh --full` closes.

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

`teardown.sh --full` and `tools/cleanup-verify.sh` need the same credentials
plus read/delete on the services they enumerate: `compute.*`,
`storage.buckets.*` + `storage.objects.*`, `run.services.*`, `artifactregistry.repositories.*`,
`logging.buckets.*` (+ `logging.buckets.delete`) and, for
`--delete-quota-preferences`, `cloudquotas.quotas.update`/`delete`. A missing
permission makes the run **abort** (exit 1/2) — it is never reported as "already
clean".

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
