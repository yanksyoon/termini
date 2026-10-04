# Termini agent VM image (`agent-vm/<version>`)

- Owner: kanban task **`t_d1960fd6`** (W4 — agentic OS image).
- Status: **built + offline-verified** (see §5; evidence in `evidence/`).
- Consumers: `t_7c40f9a3` (template + place the alias on Nodes A/B), `t_fd8eee6c`
  (cross-node agent→llama.cpp ADK call path), `t_cca07879` (W4 root).
- Contract sources: `docs/contracts/agent-runtime.md` §3 (image contents),
  §4.4 (`/etc/termini/agent.yaml`), §5 (in-VM runtime), §19.1 (addresses).
  Requirements: `termini/project.md` §4 ("Guest VM OS: Python 3.12, Node.js,
  pip packages LangChain, AutoGen, CrewAI…", agent runtime/ADK).

This directory builds the **GUEST image** the controller launches as agent VMs —
distinct from the node/host image in `image/pipeline` (`agentic-os/w3`,
`t_ab18d582`). First boot installs nothing and downloads nothing.

---

## 1. Published identity

| Field | Value |
|-------|-------|
| **Alias** | `agent-vm/2026.10.0` |
| **Immutable tag** | `agent-vm-2026.10.0+g<12-hex-gitsha>` |
| **Digest** | `sha256:<12-hex>` over the four pip locks + the npm lock (recorded in `/etc/termini/agent-image.digest`) |
| **Format** | LXD image tarball (`lxc image export agent-vm/2026.10.0 <prefix>` → `<prefix>.tar.gz`) |
| **Type** | container image (the acceptance target); the same provisioning also applies to a VM install (`--vm`) |

The running VM exposes its identity at `/etc/termini/agent-image`:
`agent-vm-2026.10.0+g<gitsha> sha256:<digest>` (agent-runtime.md §3.1).

## 2. What is baked in

| Layer | Contents |
|-------|----------|
| Base | Ubuntu 24.04 LTS minimal, `systemd`, `chrony`, `nftables`, `openssh-server`, `python3.12`, `ca-certificates`, `jq` |
| Node.js | **20.20.2**, unpacked from the pinned upstream tarball (sha256-verified) into `/opt/termini/node` |
| Python runtime venv | `/opt/termini/venv` — `openai`, `httpx`, `pydantic`, `PyYAML`, `tenacity`, `structlog`, `langchain-core`, `langgraph`, **`google-adk`** (the adopted agent runtime), `setuptools` |
| Framework venvs (isolated) | `/opt/termini/venvs/langchain`, `/opt/termini/venvs/crewai`, `/opt/termini/venvs/autogen` |
| Node libs | `/opt/termini/nodejs` — `openai`, `pino`, `undici` (`npm ci` from the committed lock) |
| Runtime | `termini-agent` (console script in the runtime venv) + `termini-agent.service` (enabled, unprivileged `agent`) + local status socket `/run/termini/agent.sock` and `127.0.0.1:9101/status` |
| Config | `/etc/termini/agent.yaml` (baked default; cloud-init renders the authoritative copy at launch from `/etc/termini/agent.yaml.example`) |
| Offline proof | `/var/lib/termini/.baked-offline` sentinel + `/etc/termini/agent-image{,.digest}` + `termini-firstboot-preflight.service` |

**Never installed at boot:** any `apt`/`npm`/`pip` fetch. `unattended-upgrades` is
disabled and `Acquire::Retries 0` is set; the first-boot preflight fails the boot
loudly if anything in the bake is missing (`termini-agent.service` has
`Requires=termini-firstboot-preflight.service`).

### Why four Python venvs

`crewai`, `langchain` and `google-adk` pin conflicting versions of
`openai`/`opentelemetry`/`pydantic`; installing them into one interpreter makes
`pip check` fail and lets a framework upgrade silently break the runtime. Each
framework therefore gets its own venv, every one passes `pip check`, and the
runtime venv carries only what the agent needs.

> **Cross-doc note (W0 research).** `docs/research/02-…` marks upstream **AutoGen**
> as maintenance-mode (successor: Microsoft Agent Framework) and **CrewAI** as
> optional. Both are still baked because `termini/project.md` §4 and the W4 card
> name them explicitly; the committed runtime wire-up (t_fd8eee6c) uses **ADK** +
> the OpenAI-compatible client, not AutoGen.

## 3. Build (reproducible)

```bash
# recipe validation only — works on any checkout, no LXD:
image/agent-vm/build-agent-image.sh --check

# real bake (needs LXD 5.21+, network at BUILD time only):
image/agent-vm/build-agent-image.sh --build            # launches ubuntu:24.04, provisions, publishes, exports
```

Reproducibility is enforced by **pins**, not "latest":

| Input | Pinned where |
|-------|--------------|
| Base image | `lxc launch ubuntu:24.04` (LXD image server) |
| Python deps | `requirements/{runtime,langchain,crewai,autogen}.lock` — exact versions + `--hash` |
| Node.js | `NODE_VERSION`/`NODE_SHA256` in `provision/10-runtime.sh` |
| Node libs | `node/package-lock.json` |
| Locks | regenerated with `uv pip compile --generate-hashes <x>.in` |

The builder emits `image-manifest.json` (schema:
`manifests/agent-image-manifest.schema.json`) with the digest, every pin, and the
artifact's `size_bytes` + `sha256`.

## 4. First boot: proving no network download happens

`first-boot-preflight.sh` runs before `termini-agent.service` and, without the
network, asserts:

1. the bake sentinel + image digest exist;
2. the runtime venv, all three framework venvs, and the Node runtime are present;
3. every pinned requirements lock is on disk;
4. **`termini-agent selfcheck` imports every agent dependency offline and validates `/etc/termini/agent.yaml`** against the on-prem inference allow-list.

It writes `/var/log/termini/first-boot.json` (`verdict`, per-module import status,
`network_downloads_at_boot: 0`) and exits non-zero on any failure.

## 5. Verification (the card's acceptance)

One command builds the image, boots it **with no network interface** and asserts
the acceptance criteria inside:

```bash
image/agent-vm/verify-agent-image.sh            # recipe check + build + offline boot + assertions
```

It launches the image with the `termini-agent-offline` profile (root disk, **no
NIC**), waits for `termini-agent` to become active, runs
`tools/offline-acceptance.sh` inside, and copies the evidence out. Checks:

- `no_network_interface` — only `lo` exists (so any boot-time fetch is impossible);
- `agent_selfcheck` + per-module `dep` lines — runtime + 3 framework venvs + config all import offline;
- `preflight_verdict` / `network_downloads_at_boot` — `pass` / `0`;
- `agent_service_active`, `status_surface`, `governance_egress_deny`;
- `node_libs_importable` — `openai`+`pino`+`undici`;
- `no_route_to_controller` — the VM cannot reach the GCP controller.

Committed evidence lives in `evidence/` (`offline-acceptance.json`,
`first-boot.json`, `selfcheck.json`, `status.json`, `boot-journal.txt`).

## 6. Honest scope

- The image was **built and booted for real on this host's LXD 5.21.8** (container
  variant). The **VM (`--vm`) variant was not built here** (no nested-virt KVM in
  the build container); the provisioning is identical and `image/pipeline`
  (`t_ab18d582`) owns the host/VM-side artifacts.
- **GPU/inference is not part of this image.** The agent reaches Node C's
  router at `http://10.10.0.13:8080`; the server is W3 `t_95528ae8`.
- Registry distribution to Nodes A/B is `t_7c40f9a3`; this card produces the
  alias + exportable tarball + launch profile.
- Cross-node streaming against the real endpoint is `t_fd8eee6c`.

## 7. Layout

```
image/agent-vm/
  README.md                         this file
  build-agent-image.sh              builder (+ --check) via LXD
  verify-agent-image.sh             acceptance: build + offline boot + assertions
  first-boot-preflight.sh           no-network boot assertion + boot log
  requirements/*.in                 top-level deps per venv
  requirements/*.lock               hashed, fully-pinned locks (uv pip compile --generate-hashes)
  node/{package.json,package-lock.json}
  config/agent.yaml.template        baked default + cloud-init example
  provision/00-base.sh              apt base layer
  provision/10-runtime.sh           Node.js 20 + python venvs
  provision/20-agent-libs.sh        install the four hashed locks + npm ci
  provision/30-termini-agent.sh    user, runtime package, identity, units, sentinel
  provision/35-agent-selftest.sh    bake-time unit tests (quality gate)
  systemd/*.service                 agent + first-boot preflight units
  agent/                            the termini-agent python package (pip-installable)
  tools/offline-acceptance.sh       in-VM acceptance checks
  tools/validate_manifest.py        manifest schema/structural validation
  manifests/agent-image-manifest.schema.json
  evidence/                         committed run evidence
```
