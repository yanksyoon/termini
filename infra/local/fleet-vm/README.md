# Termini fleet on LXD VMs (one host)

The GCE footprint (`infra/gcp/`) is four billable `e2` instances. This directory
runs the same three-member fleet — **controller, node-a, node-c** — as real KVM
guests on a single LXD host, so the demo costs nothing on Compute Engine.

```
                host (LXD, nested virt)
   ┌──────────────────────────────────────────────────────────────┐
   │  tvm-controller  10.14.100.10   :8080  controller (stdlib py) │
   │      ▲  registration + telemetry                             │
   │      │                                                       │
   │  tvm-node-a      10.14.100.20   :8443  own LXD API           │
   │  tvm-node-c      10.14.100.30   :8443  own LXD API + :8080   │
   │                                        inference front door  │
   └──────────────────────────────────────────────────────────────┘
        host :8099  dashboard gateway (static dashboard + /v1 proxy)
```

## Why VMs, not containers

A container shares the host kernel and cannot be handed to a controller as a
machine. Each guest therefore runs **its own LXD daemon** and advertises
`https://<guest>:8443` at registration — the same contract the GCE nodes satisfy
(`infra/gcp/tools/verify-node-lxd-api.py`). The guests boot with `/dev/kvm` and
`vmx` exposed, so a node's agent workloads can themselves be VMs rather than
containers (nested virtualisation, `kvm_intel.nested=Y` on this host).

## What runs where

| Piece | GCE today | Here |
|---|---|---|
| controller | `termini-controller`, `e2-standard-2` | `tvm-controller` (2 vCPU/2 GiB) running `python3 -m app.main` as a systemd unit |
| compute nodes | `termini-node-a`, `termini-node-c`, `node-d` | `tvm-node-a`, `tvm-node-c` (2 vCPU/3 GiB), enrolled through the **documented customer installer** (`/install/termini-node.sh`) |
| inference | node-c `:8080`, `gemini-proxy` → Vertex AI | same front door on `tvm-node-c`, same backend |
| dashboard | Firebase Hosting + public controller | host gateway `:8099`, same-origin `/v1` proxy |

Roles match production: **every node registers as `compute`** since the
CPU/proxy pivot (no node can hold `role=inference`); the inference front door is
addressed by `TERMINI_INFERENCE_BASE_URL`, not by role.

## Usage

```bash
./fleet-vm.sh create                 # create + boot the three VMs (caches the image once)
./fleet-vm.sh status                 # name / state / address / guest kernel

./provision-fleet.sh --sa-key ~/.config/gcloud/application_default_credentials.json
./verify-fleet.sh                    # structural + live checks, exit 0 = fleet good
./verify-fleet.sh --with-fault       # + one oom injection driven to a model decision

./fleet-vm.sh stop | start           # park the fleet, keep the disks
./fleet-vm.sh destroy                # delete the VMs
```

`provision-fleet.sh` is idempotent: re-running it re-stages the controller tree,
restarts the units, and re-joins only nodes that have no identity yet.

## The one credential that is not in GCP

The shipped `gemini-proxy` backend mints its OAuth token from the **GCE metadata
server**, which a local VM does not have. `install-node-c-proxy.sh` therefore also
installs `termini-vertex-token-refresh.timer`, which signs a JWT with
`/etc/termini/vertex-sa.json` (mode 0600, **never committed**) and rewrites
`TERMINI_GEMINI_ACCESS_TOKEN` every 40 minutes. Same identity as production
(`roles/aiplatform.user`), different token source.

Without a key the front door still starts but every completion returns
`503 MODEL_UNAVAILABLE / metadata_token` — it fails loudly, it does not fake a
local answer. `termini-inference-switch local` is **not** available on these VMs:
the CPU worker and the 2.5 GiB GGUF are not installed here (that is what
`infra/gcp/cloud-init/llama-serve.sh` builds on a GCE node's first boot).

## Cost

`cost-report.py` reads live inventory and prices it from the public Cloud Billing
Catalog API (the deployment SA cannot read the billing account, so this is a
list-price estimate of provable inventory, not an invoice):

```
$ python3 cost-report.py
TOTAL while RUNNING      265.62 $/mo   (245.25 of it Compute Engine)
AFTER VMs STOPPED         20.37 $/mo   (disks + image, billed while they exist)
```

Stopping the instances is what removes the compute charge; the disks
(`$18.96/mo`), the node image (`$1.20/mo`), the model bucket (`$0.21/mo`) and any
reserved-but-unattached static IP (`$0.012/h` ≈ `$8.76/mo`) keep billing until
they are deleted or released.

**Current state (t_14b567d4, after retirement): `0.00 $/mo`.** Every GCP resource
the fleet owned — four instances and their disks, four addresses, the node image,
the Cloud Run service, the Artifact Registry repo and the model bucket — has been
deleted; `evidence/gcp-before-removal.txt` and `evidence/gcp-after-removal.txt`
record both sides of that removal. Only Vertex AI token usage remains, and it is
charged per call, not per month. Vertex AI tokens are usage-based and continue as
long as the inference front door answers.

## Not replicated here

* **node-d** — the fourth GCE instance, registered to the standing fleet but not
  part of the two-node demo shape; it is stopped with the rest and not recreated.
* **Public HTTPS / sslip.io** — the GCE controller is reachable at
  `https://34-93-190-64.sslip.io`; the local controller is plain HTTP on the LXD
  bridge, published to the tailnet only through the gateway on `:8099`.
* **Cloud Logging / Monitoring agents** — guest logs stay in the guest journals.
* **Firebase Hosting** — the dashboard is served by the gateway instead.
