# Termini — Technology Landscape & W1 Decision Document

- Owner: `architect` (kanban task `t_8550b53c`, W0 synthesis)
- Status: synthesis complete — single decision document for W1; feeds W2–W6 planning
- Date: 2026-10-04
- Consumes (four W0 research streams):
  - `docs/research/01-bootstrap-cluster-network.md` (infra, t_d1469c94)
  - `docs/research/02-model-serving-agent-runtime.md` (agents, t_eb6f843c)
  - `docs/research/03-aiops-observability-control-loop.md` (controller, t_59812ed9)
  - `docs/research/04-prior-art-positioning.md` (pitch, t_1b521d47)
- Feeds (W1 contracts, all frozen): `docs/contracts/infra-decisions.md` (D1–D8), `docs/contracts/node-bootstrap.md`, `docs/contracts/agent-runtime.md`, `docs/contracts/controller-api.md`, `docs/contracts/node-telemetry.md`, `docs/contracts/governance.md`, `docs/contracts/vm-allocation.schema.json`.
- Requirements source: `~/termini/project.md`.
- Scope: one **build-vs-adopt verdict per research area**, each with a citation and notes on maturity, license/cost, fit, and integration risk; a concrete 5-day-demo tool shortlist; an explicit mapping onto the six W1 contract surfaces; contradiction resolutions; and anything still unclear flagged as a blocking question with an owner.

---

## 0. How to read the verdicts

Each area carries exactly one primary verdict. The legend is the union of the four streams' vocabularies, normalised:

| Verdict | Meaning |
|---------|---------|
| **ADOPT** | Commit to it now for the 5-day build (use the tool/library/pattern as-is). |
| **ADOPT (optional)** | Cheap, low-risk; use if it saves time, never a critical-path dependency. |
| **ADOPT-LATER** | Real and correct for production/day-2; deliberately out of the demo critical path. |
| **BORROW** | Do not run it; take its design pattern/semantics into our own code. |
| **BUILD** | Implement ourselves; the frozen contracts already assume this. |
| **REFERENCE** | Prior art we study, not integrate. |
| **DO-NOT-ADOPT** | Evaluated and rejected (with reason); we neither adopt it nor hand-roll it in the demo. |
| **DIFFERENTIATE** | Position the product against it; not a component. |
| **MODEL-SUPPLIER** | A component (weights) we may source, not a rival. |

The four streams used slightly different wordings ("REJECT" = DO-NOT-ADOPT; "adopt-as-feature" = ADOPT (optional)). This document is the single reconciled reading.

---

## 1. Master verdict table (one row per research area)

### 1.1 Node bootstrapping & provisioning (stream 1)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| Canonical MAAS | ADOPT-LATER (on-prem) | very mature | AGPL-3.0; self-support free for all machines | none on GCP (no BMC/PXE) | high (own subnet + BMC creds) | https://canonical.com/maas |
| Canonical MicroCloud | ADOPT-LATER (day-2) | mature (v3.x) | free snaps; Ubuntu Pro for prod | too heavy (Ceph+OVN, ≥3 nodes) | high | https://canonical.com/microcloud |
| Foreman | DO-NOT-ADOPT | mature | GPL-3.0; free | duplicates image+cloud-init path | medium | https://github.com/theforeman/foreman |
| Netboot.xyz | ADOPT (optional, on-prem) | mature | Apache-2.0; free | on-prem netboot menu only | low | https://github.com/netbootxyz/netboot.xyz |
| PXE / iPXE | ADOPT (interface only) | mature standard | free | on-prem boot mechanism | medium | https://canonical.com/maas/docs/about-maas-networking |
| cloud-init | ADOPT (activation-only) | mature (24.1/24.3 single-process) | GPL-3.0 / Apache-2.0 dual; free | write config + start service, never install | low if activation-only | https://docs.cloud-init.io/ |
| Ansible | ADOPT (build/runbooks) | very mature | GPL-3.0+; free | image build + CI + day-2 runbooks, not boot path | low | https://github.com/ansible/ansible |
| Terraform / OpenTofu | ADOPT (prefer OpenTofu) | very mature | Terraform BUSL-1.1; OpenTofu MPL-2.0 | GCP VPC/VMs/machine images | low | https://opentofu.org |

### 1.2 VM/container runtime & clustering (stream 1)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| LXD | ADOPT (core) | very mature; 5.21 LTS to Jun 2029 | AGPL-3.0-only; free | hypervisor + 3-member cluster, one API | low–medium (snap-revision match) | https://canonical.com/lxd |
| Incus | ADOPT-LATER (drop-in alt) | mature; 6.0 LTS | Apache-2.0; free | interchangeable with LXD | low (`lxd-to-incus`) | https://linuxcontainers.org/incus/ |
| OpenStack (DevStack/Kolla) | DO-NOT-ADOPT | mature | Apache-2.0; free | days to stand up; solves problems we don't have | very high | https://docs.openstack.org/kolla-ansible/2025.1/user/quickstart.html |
| Kubernetes — k3s | DO-NOT-ADOPT (ADOPT-LATER if k8s) | very mature | Apache-2.0; free | no k8s requirement in demo | medium–high | https://github.com/k3s-io/k3s |
| Kubernetes — Talos | DO-NOT-ADOPT | mature | MPL-2.0; Enterprise $1,000/node·yr | replaces OS, breaks Ubuntu/SSH story | high | https://talos.dev |
| Proxmox VE | DO-NOT-ADOPT | mature | AGPLv3; subs €120–€1,100/socket·yr | LXD already covers containers+VMs | medium | https://www.proxmox.com |
| Live migration / evacuation | ADOPT (evacuation); live-migration optional | mature | included in LXD | disk-failure scenario | medium (`migration.stateful` pre-set) | https://canonical.com/lxd/docs/latest/howto/instances_migrate/ |

### 1.3 Networking overlays (stream 1)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| OVN | ADOPT-LATER (day-2 overlay) | mature | Apache-2.0; free | correct multi-tenant answer, 1–2 days to integrate | high for 5-day build | https://www.ovn.org/ |
| VXLAN | DO-NOT-ADOPT (build nothing) | in-kernel standard | GPL-2.0 | primitive, not a product | medium (MTU) | https://docs.kernel.org/networking/vxlan.html |
| LXD fan networking | DO-NOT-ADOPT | legacy; regressions | free | fragile MTU + 6.14-kernel regression | high (silent drops) | https://canonical.com/lxd/docs/latest/reference/network_bridge/ |
| Tailscale | ADOPT (optional, operator access) | mature | free ≤6 users; $8/user·mo | operator reach, not node↔controller path | low | https://tailscale.com |
| WireGuard | ADOPT (primitive, if tunnel needed) | mature, in-kernel | GPL-2.0 | tunnel only if strictly required | low–medium | https://www.wireguard.com |
| Nebula | DO-NOT-ADOPT | mature | MIT; free | redundant with WireGuard/Tailscale | low–medium | https://github.com/slackhq/nebula |
| Cilium | DO-NOT-ADOPT | mature | Apache-2.0 | Kubernetes-only | N/A | https://github.com/cilium/cilium |

### 1.4 Model serving & inference (stream 2)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| llama.cpp `llama-server` | ADOPT (primary) | ~126k★, very active | MIT; free | single small GPU, OpenAI-compatible, trivially pre-baked | low (concurrency) | https://github.com/ggml-org/llama.cpp |
| vLLM | ADOPT (secondary/fallback) | ~92k★ | Apache-2.0; free | multi-session throughput on L4 (SM ≥ 7.5) | medium (heavier install) | https://github.com/vllm-project/vllm |
| Ollama | ADOPT (dev only) | ~180k★ | MIT; free | fastest local iteration; hides quant/offload | low (not the demo serving path) | https://github.com/ollama/ollama |
| SGLang | DO-NOT-ADOPT (demo) | ~20–36k★ | Apache-2.0; free | oversized for one small GPU | high (install/load friction) | https://github.com/sgl-project/sglang |
| TGI (HuggingFace) | DO-NOT-ADOPT | archived Mar 2026 | Apache-2.0 | upstream points to vLLM/SGLang/llama.cpp | — | https://github.com/huggingface/text-generation-inference |
| Quantization | ADOPT — GGUF Q4_K_M default | proven default | free | ~60% smaller than FP16, negligible loss | low | https://huggingface.co/docs/hub/en/gguf |
| GPU for Node C | ADOPT — GCP G2 / L4 24 GB (T4 fallback) | GA | ~$0.45–$1.00/GPU-hr | L4 fits 14B/32B Q4; T4 caps at ~8B | low | https://docs.cloud.google.com/compute/docs/gpus |
| GPU presentation | ADOPT (host or LXD container, NOT nested VM) | — | — | nested-VM vfio unsupported on GCP | the riskiest removed | https://canonical.com/lxd/docs/latest/reference/devices_gpu/ |

### 1.5 Agent runtimes & packaging (stream 2)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| Google ADK | ADOPT (primary controller agent runtime) | GA-class (ADK 2.0) | Apache-2.0; free | Gemini-native on the GCP controller | low | https://github.com/google/adk-python |
| LangGraph | ADOPT (fallback) | ~41k★ | MIT; free | deterministic graph control matches guardrail loop | low | https://github.com/langchain-ai/langgraph |
| AutoGen | DO-NOT-ADOPT | maintenance mode | MIT | successor = Microsoft Agent Framework | — | https://github.com/microsoft/autogen |
| CrewAI | ADOPT (optional) | ~45–59k★ | MIT; free | fast role-based prototyping | low | https://github.com/crewAIInc/crewAI |
| OpenHands | REFERENCE | v1.x | MIT; free | sandbox-per-task model is the isolation pattern we copy | n/a | https://github.com/OpenHands/openhands |
| Golden agent image (Packer + cloud-init) | ADOPT | mainstream | free | reproducible, offline-capable, ~20–40 min once | low | https://oneuptime.com/blog/post/2026-03-02-how-to-use-cloud-init-with-packer-for-automated-builds-on-ubuntu/view |
| Fedora bootc / AgentOS | REFERENCE | emerging | — | image-mode OS prior art; not adopted | n/a | https://www.redhat.com/en/blog/building-hardened-image-based-foundation-ai-agents |

### 1.6 AIOps / observability / control loop (stream 3)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| Datadog | DO-NOT-ADOPT | very high | closed SaaS; $15–40/host/mo | SaaS = C2 egress + cost | high | https://www.datadoghq.com/pricing/ |
| Netdata agent | DO-NOT-ADOPT (BORROW ML detection) | high | GPLv3+ agent; closed Cloud | redundant agent; C2 collectors | medium | https://github.com/netdata/netdata |
| Prometheus | BORROW semantics (BUILD store) | very high | Apache-2.0; free | pull-model mismatch with push telemetry | medium (full stack) | https://prometheus.io |
| Alertmanager | BORROW (`for:` rule semantics) | very high | Apache-2.0; free | rule shape reused in threshold engine | low | https://github.com/prometheus/alertmanager |
| Grafana | ADOPT (optional, stretch) | very high | AGPL-3.0 | dashboards only; frontend already covers demo | low–medium | https://github.com/grafana/grafana |
| VictoriaMetrics | ADOPT (optional, TSDB fallback) | high | Apache-2.0; free | push-protocol TSDB if ring buffer insufficient | low | https://github.com/VictoriaMetrics/VictoriaMetrics |
| Zabbix | DO-NOT-ADOPT | very high | AGPLv3 (≥7.0) | heavyweight, agent-centric | medium | https://www.zabbix.com/license |
| Ansible / AWX / EDA | DO-NOT-ADOPT (BORROW idempotency + rulebooks) | very high | GPLv3 core / Apache-2.0 AWX | SSH push conflicts with command channel | medium | https://github.com/ansible/awx |
| Rundeck | DO-NOT-ADOPT (BORROW lifecycle/audit) | high | Apache-2.0 CE | JVM service; duplicates command channel | medium | https://github.com/rundeck/rundeck |
| StackStorm | DO-NOT-ADOPT (BORROW pipeline shape) | mature | Apache-2.0 | multi-service; deterministic engine already covered | medium–high | https://github.com/StackStorm/st2 |
| Kubernetes operator / reconcile loop | ADOPT (pattern only) | mature pattern | Apache-2.0 (controller-runtime) | our AI operator = reconcile loop | low | https://kubernetes.io/docs/concepts/extend-kubernetes/operator/ |
| K8sGPT | ADOPT AS REFERENCE (BORROW guardrails) | early (CNCF Sandbox) | Apache-2.0 | guardrail + anonymization design evidence | n/a | https://k8sgpt.ai |
| Telemetry daemon + redactor + ring-buffer TSDB + rules engine + operator + command channel | BUILD (frozen) | ours | — | the moat | n/a | `controller-api.md` §5–§8; `node-telemetry.md` |

### 1.7 Prior art & positioning (stream 4)

| Area | Verdict | Maturity | License / cost | Fit | Integration risk | Citation |
|------|---------|----------|----------------|-----|------------------|----------|
| Datadog AIOps (Watchdog/Bits) | DIFFERENTIATE | very high | SaaS; "AI Credits" ≈$500–600/bundle/mo | day-2 benchmark; telemetry exfiltration | none (don't integrate) | https://docs.datadoghq.com/watchdog/ |
| Samsung × Mistral (Sept 2026) | DIFFERENTIATE (validation) + ADOPT open-weight self-host playbook | very early | bespoke deal | trigger event; mega-corp, not productized | low | https://kommunikasjon.ntb.no/pressemelding/19056888/samsung-and-mistral-ai-announce-strategic-partnership-for-intelligence-driven-semiconductor-infrastructure |
| Mistral models | MODEL-SUPPLIER | high | Large 3 Apache-2.0; Medium 3.5 modified-MIT ($20M/mo carve-out) | pattern proof; not needed for demo | low | https://huggingface.co/mistralai/Mistral-Medium-3.5-128B/blob/main/LICENSE |
| Open WebUI + Ollama | ADOPT (optional, as-feature) | very high | Ollama MIT; Open WebUI custom license (branding clause >50 users) | one feature (private chat UI), not the product | low (keep branding) | https://github.com/open-webui/open-webui/blob/main/LICENSE |
| Canonical AI | ADOPT (substrate) | high | OSS substrate; Ubuntu Pro support | OS + LXD + snaps; not the operator | low | https://canonical.com/solutions/ai |
| Tailscale (remote ops) | DIFFERENTIATE (adjacent) / ADOPT (optional) Tailscale SSH | very high | freemium ≤6 users; closed coordination server | connectivity + SSH UX, nothing else | medium if adopted | https://tailscale.com/docs/features/tailscale-ssh |
| K8sGPT / Robusta (AI-SRE) | DIFFERENTIATE (K8s-scoped) | medium | Apache-2.0 / MIT | fix a pod, not bootstrap bare metal | none | https://github.com/k8sgpt-ai/k8sgpt |

---

## 2. Net guidance — the moat

**Adopt the substrate, the connectivity, and the model. Build only two things: the governance boundary and the operator loop.**

- **Substrate (adopt):** LXD + Ubuntu 24.04 (`D1`), underlay routing (`D2`), immutable pre-baked images (`D3`), OpenTofu/Packer/cloud-init/Ansible for build-time only.
- **Connectivity (adopt):** WireGuard-class underlay + controller SSH CA + short-lived certs (`D4`); Tailscale SSH optional for the operator.
- **Model (adopt):** open-weight self-host inference via llama.cpp on Node C (`D5`), GGUF Q4_K_M.
- **The moat (build):** (1) the on-node PII/redaction + C0/C1/C2 fail-closed boundary (`governance.md`), and (2) the AI operator reconcile loop — telemetry → threshold rule → advisory model output → deterministic `guardrailClamp()` → `commandAllowlist()` → ack/audit (`controller-api.md` §5–§6, `node-telemetry.md` §5). No incumbent (Datadog, Canonical, Tailscale, K8sGPT/Robusta) ships both.

This is the same conclusion the four streams independently reached; no cross-stream contradiction changes it.

---

## 3. Concrete tool shortlist for the 5-day demo

### 3.1 Commit (adopt, critical path)

1. **LXD snap `5.21/stable`, `--cohort="+"`, identical revision per member** — hypervisor + 3-member cluster `termini`; built-in evacuation. (D1/D7)
2. **cloud-init, activation-only** — write `config.yaml` + CA pubkey + enrollment token + `systemctl enable --now termini-bootstrap`; never install packages. (D3)
3. **OpenTofu (MPL-2.0) + `hashicorp/google` provider** — GCP VPC, firewall, the three node VMs, GPU Node C.
4. **Packer + `virt-customize` (Ansible as provisioner)** — produce the immutable "Termini Node OS" GCP machine image and the `agent-vm` LXD image; preloaded on every host.
5. **llama.cpp `llama-server` (MIT)** — primary serving engine on Node C host, OpenAI-compatible `:8080`; **vLLM (Apache-2.0)** as drop-in fallback for multi-session throughput.
6. **GGUF Q4_K_M** — default quantization; leave 20–30% VRAM for KV cache.
7. **Google ADK (Apache-2.0)** — controller AI-operator runtime; **LangGraph (MIT)** fallback.
8. **Underlay routing + per-node NAT bridge** — D2 networking; no overlay. `INFERENCE_BASE_URL=http://10.10.0.13:8080`.

### 3.2 Adopt optionally (cheap, low risk)

9. **Tailscale (free ≤6 users) + Tailscale SSH** — operator access; complements, never replaces, the SSH-CA contract (D4).
10. **netboot.xyz + iPXE** — on-prem bare-metal netboot of the `qcow2` (GCP uses the machine image).
11. **Open WebUI + Ollama** — one-command "private chat" prop (keep Open WebUI branding; on-prem inference only).
12. **Ollama (dev)** — local iteration only, not the demo serving path.

### 3.3 Borrow (patterns, no runtime dependency)

13. **K8sGPT guardrail design** (`anonymized`, `autoRemediation` risk threshold) — reference for our deterministic clamp.
14. **Netdata per-metric edge ML anomaly detection** — post-demo roadmap to replace static thresholds.
15. **Prometheus/Alertmanager metric naming + `for:` N-consecutive-samples rule semantics**.
16. **Kubernetes operator reconcile loop** — the shape of the AI operator.
17. **Rundeck execution-lifecycle + audit shape** (`acked → running → succeeded/failed` + result) — already frozen in `command_id` + ack.

### 3.4 Explicitly do NOT adopt (and do NOT build equivalents)

MAAS (day-2 only), MicroCloud (day-2 only), Foreman, OpenStack, k3s/Talos, Proxmox, OVN in-demo (day-2 only), hand-rolled VXLAN, LXD fan, Nebula, Cilium; TGI (archived), SGLang (demo), AutoGen (maintenance); Datadog, Netdata agent, Zabbix, Ansible/AWX/EDA, Rundeck, StackStorm.

---

## 4. Mapping to the W1 contracts

Each W1 surface named in the task, and exactly what this synthesis locks in for it.

| W1 surface | Contract doc | What the synthesis decides / confirms |
|------------|--------------|---------------------------------------|
| **Node registration** | `node-bootstrap.md` §4–§5, `controller-api.md` §4.1 | Add `machine.provisioning {method, image_version}` to `POST /v1/nodes/register` to distinguish GCP-image vs ipxe-netboot vs manual. Add `machine_id` DMI-UUID fallback for GCP (fall back to `/etc/machine-id` / GCP instance id). Keep upsert-by-`machine_id` and the outbound mTLS (D6) transport. |
| **Telemetry schema** | `node-telemetry.md` | BUILD, unchanged: push model, C0/C1 allowlist, 1–2 s demo cadence. Borrow Prometheus/Alertmanager naming + `for:` rule shape for the four frozen rules (`oom-guard-01`, `disk-fail-01`, `disk-degrade-01`, `gpu-oos-01`). Ring-buffer TSDB with raw→5s/1m/5m downsample; VictoriaMetrics = designated W2 fallback. |
| **Serving API** | `agent-runtime.md` §8–§12 | ADOPT llama.cpp `llama-server`, OpenAI-compatible `POST /v1/chat/completions` (SSE streaming), `GET /health`, `GET /metrics`, `--api-key` bearer. Explicit alias routing `deepseek-flash` (resident) / `deepseek-v4-pro` (lazy), fail-closed to `503`, no silent substitution. |
| **Agent image** | `agent-runtime.md` §3–§4 | ADOPT Ubuntu 24.04 + Python 3.12 + Node 20 + llama.cpp client + pre-pulled GGUF, built with Packer + cloud-init `autoinstall`, preloaded on every host; no cold downloads at demo. LXD VM with `agent` user, no sudo, no GCP route. |
| **Remediation guardrails** | `governance.md` §7.3/§9, `controller-api.md` §5–§6, `vm-allocation.schema.json` | BUILD: advisory model output → `guardrailClamp()` (deterministic, clamp-vs-reject table) → `commandAllowlist()` (`vm.resize|migrate|stop|start|destroy|node.drain|node.reboot|ssh.inject_key`, plus `vm.launch`, `agent.dispatch_task`, `agent.cancel_task` from `agent-runtime.md` §17). K8sGPT is the published reference for this exact shape. |
| **Networking / SSH** | `infra-decisions.md` D2/D4, `node-bootstrap.md` §7 | ADOPT underlay routing (no overlay) + per-node NAT bridges; static `10.10.0.0/24` plan. SSH: controller-held Ed25519 CA + short-lived, principal/source-bound user certs (8 h default); browser-local private keys; no inbound SSH required (D6). |

---

## 5. Contradictions resolved

1. **Model naming vs. GPU reality (RESOLVED in principle — see blocking question BQ-1).** `governance.md` §7.1 wires "DeepSeek flash" and "deepseek-v4-pro" to on-prem inference. Stream 2 shows the literal V4 family is MoE (Flash 284B ≈146 GB, Pro 1.6T) and cannot fit a single L4/T4. `agent-runtime.md` §11.1 has already collapsed the aliases to distill-scale budgets (`deepseek-flash` ≈6 GiB Q4_K_M, `deepseek-v4-pro` ≈18 GiB Q4_K_M) on an L4 24 GB. The aliases are fine; the *exact* HF GGUF artifacts are not yet pinned — BQ-1.

2. **GPU passthrough into a nested VM (RESOLVED).** `project.md` describes an "Inference VM … GPU passthrough." Stream 2 flags nested-VM vfio as unsupported on GCP; `infra-decisions.md` D5 and `agent-runtime.md` §8.1 already freeze host-exposed llama.cpp (`10.10.0.13:8080`) with passthrough as day-2 only. No action needed; the "Inference VM" in `project.md` is a narrative simplification, and the frozen contracts override it.

3. **Live migration with local ZFS (CORRECTION to record — owner: infra, non-blocking).** `infra-decisions.md` D7 states "with per-member local storage, live migration of a running VM is not available." Stream 1 shows upstream LXD **does** support local-pool live migration via **stateful migration** (`migration.stateful=true` + `size.state ≥ limits.memory`), at the cost of copying the root disk. Correct D7's wording to: "live migration is available on a local pool but copies the disk and requires `migration.stateful` + `size.state`; the demo uses resize + evacuation, and shared Ceph (day-2) removes the copy." The demo plan (resize primary, evacuation for disk-failure) is unchanged.

---

## 6. Blocking questions (owner assigned)

| # | Question | Owner | Why blocking | Proposed resolution |
|---|----------|-------|--------------|---------------------|
| BQ-1 | Pin the exact HF GGUF artifacts + quant behind `deepseek-flash` and `deepseek-v4-pro` aliases, and amend `governance.md` §7.1 wording from "DeepSeek flash / deepseek-v4-pro" to state these are **distills**, not the 284B/1.6T V4 MoE. | architect (governance.md) + infra (Node C bake) | The serving contract's `model` enum, VRAM budget, and pre-baked weights all depend on the concrete artifact; without a pin, W2 can't bake the image. | `deepseek-flash` = DeepSeek-R1-Distill-Llama-8B Q4_K_M (~6 GB); `deepseek-v4-pro` = DeepSeek-R1-Distill-Qwen-14B Q4_K_M (~18 GB); L4 24 GB recommended, T4 16 GB → flash-only. |

## 7. Deferred decisions (non-blocking; W2+ with owner)

| Decision | Recommendation now | Day-2 trigger | Owner |
|----------|--------------------|---------------|-------|
| TSDB: in-memory ring buffer vs VictoriaMetrics | ring buffer (frozen) | persistence / >1 node / longer retention | architect |
| Grafana dashboards | defer; frontend covers demo | dashboards a judged criterion | controller + frontend |
| OVN overlay + ACLs | day-2 | underlay trusted + multi-tenant isolation | infra |
| MicroCloud/MicroCeph/MicroOVN | day-2 | HA shared storage + flat networking | infra |
| MAAS | day-2 | real customer bare metal onboarded | infra |
| LXD → Incus | keep escape hatch (`lxd-to-incus`) | AGPL/Canonical governance concern | infra |
| k3s | ADOPT-LATER | agent scheduling moves to Kubernetes | infra |
| Netdata edge-ML anomaly detection | roadmap | replace static thresholds with learned baselines | controller |

---

## 8. Consolidated citations

Bootstrap/cluster/network (stream 1 full list in `01-bootstrap-cluster-network.md` §9): MAAS https://canonical.com/maas · MicroCloud https://canonical.com/microcloud · Foreman https://github.com/theforeman/foreman · netboot.xyz https://github.com/netbootxyz/netboot.xyz · PXE/iPXE https://canonical.com/maas/docs/about-maas-networking · cloud-init https://docs.cloud-init.io/ · Ansible https://github.com/ansible/ansible · OpenTofu https://opentofu.org · LXD https://canonical.com/lxd · Incus https://linuxcontainers.org/incus/ · OpenStack Kolla https://docs.openstack.org/kolla-ansible/2025.1/user/quickstart.html · k3s https://github.com/k3s-io/k3s · Talos https://talos.dev · Proxmox https://www.proxmox.com · LXD live migration https://canonical.com/lxd/docs/latest/howto/instances_migrate/ · OVN https://www.ovn.org/ · VXLAN https://docs.kernel.org/networking/vxlan.html · LXD fan https://canonical.com/lxd/docs/latest/reference/network_bridge/ · Tailscale https://tailscale.com · WireGuard https://www.wireguard.com · Nebula https://github.com/slackhq/nebula · Cilium https://github.com/cilium/cilium.

Serving/agents (stream 2 full list in `02-model-serving-agent-runtime.md` §11): llama.cpp https://github.com/ggml-org/llama.cpp · vLLM https://github.com/vllm-project/vllm · Ollama https://github.com/ollama/ollama · SGLang https://github.com/sgl-project/sglang · TGI https://github.com/huggingface/text-generation-inference · GGUF https://huggingface.co/docs/hub/en/gguf · GCP GPUs https://docs.cloud.google.com/compute/docs/gpus · LXD GPU device https://canonical.com/lxd/docs/latest/reference/devices_gpu/ · ADK https://github.com/google/adk-python · LangGraph https://github.com/langchain-ai/langgraph · AutoGen https://github.com/microsoft/autogen · CrewAI https://github.com/crewAIInc/crewAI · OpenHands https://github.com/OpenHands/openhands · Packer+cloud-init https://oneuptime.com/blog/post/2026-03-02-how-to-use-cloud-init-with-packer-for-automated-builds-on-ubuntu/view · bootc https://www.redhat.com/en/blog/building-hardened-image-based-foundation-ai-agents.

Observability/control-loop (stream 3 full list in `03-aiops-observability-control-loop.md` Appendix B): Datadog https://www.datadoghq.com/pricing/ · Netdata https://github.com/netdata/netdata · Prometheus https://prometheus.io · Alertmanager https://github.com/prometheus/alertmanager · Grafana https://github.com/grafana/grafana · VictoriaMetrics https://github.com/VictoriaMetrics/VictoriaMetrics · Zabbix https://www.zabbix.com/license · AWX https://github.com/ansible/awx · Rundeck https://github.com/rundeck/rundeck · StackStorm https://github.com/StackStorm/st2 · Kubernetes operator https://kubernetes.io/docs/concepts/extend-kubernetes/operator/ · K8sGPT https://k8sgpt.ai.

Prior art (stream 4 full list in `04-prior-art-positioning.md`): Datadog AIOps https://docs.datadoghq.com/watchdog/ · Samsung × Mistral https://kommunikasjon.ntb.no/pressemelding/19056888/samsung-and-mistral-ai-announce-strategic-partnership-for-intelligence-driven-semiconductor-infrastructure · Mistral licensing https://huggingface.co/mistralai/Mistral-Medium-3.5-128B/blob/main/LICENSE · Open WebUI license https://github.com/open-webui/open-webui/blob/main/LICENSE · Canonical AI https://canonical.com/solutions/ai · Tailscale SSH https://tailscale.com/docs/features/tailscale-ssh · Robusta https://robusta.dev.

---

## 9. What the verifier should check

1. Every row in §1 has exactly one primary verdict and at least one citation — count: 8 (bootstrap) + 7 (clustering) + 7 (networking) + 8 (serving) + 7 (agents) + 13 (observability) + 7 (prior art) = **57 areas**.
2. §3 shortlist is actionable: commit-list maps to W1 contracts in §4; optional/borrow/reject lists are explicit.
3. §5 resolutions and §6 BQ-1 are the only items that were "unclear"; §7 items are decisions, not ambiguities.
4. No item left as a bare "TBD" — every area has a verdict; the single open artifact (BQ-1) has an owner and a proposed resolution.
