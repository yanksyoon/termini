# Termini — Spec Answers (judge-facing)

- Owner: `architect` (kanban task `t_74cdbb2d`)
- Date: 2026-10-04
- Audience: **judges and demo reviewers** — written to be read live, not by engineers.
- Source: the four open questions flagged in `termini/project.md` (the "Specification" block).
- Relationship to the build: each answer below is the plain-language version of a
  decision already frozen in `docs/contracts/` (and reconciled in
  `docs/architecture.md` §8). The "Technical justification" line in each section points
  at the exact document a judge can open during the demo; the "How the demo proves
  this" line names something visible or measurable on screen.

Quick map from question → frozen decision:

| # | project.md question | Frozen answer lives in |
|---|----------------------|------------------------|
| 1 | Operations scenarios & simulation | `docs/contracts/infra-decisions.md` D7/D8 · `node-telemetry.md` §5 · `architecture.md` R24 |
| 2 | Cloud system + shared network | `docs/contracts/infra-decisions.md` D1/D2/D5 |
| 3 | Smart VM allocation from natural language | `docs/contracts/vm-allocation.schema.json` · `controller-api.md` §5 |
| 4 | Pre-baked agentic OS + serving + SSH keys | `docs/contracts/infra-decisions.md` D3/D4/D6 · `governance.md` |

---

## 1. What are the operations scenarios we're willing to showcase with AI, and how will we simulate them?

**Answer (plain language).** We showcase two real failure scenarios, each with the
operator detecting it and executing an actual fix. Scenario A is *out-of-memory*: an
inference workload eats RAM, starves the neighboring agent VMs, and the operator sees it,
restarts the offending workload, and restores the fleet. Scenario B is *disk degradation*: a
drive's write latency climbs or a mount flips read-only, and the operator flags the
drive, stops the heaviest workload on that node, and raises an operator alert. We simulate
both with off-the-shelf tools — `stress-ng` to burn memory, and a single injected
`logger` line to fake a disk I/O error — so a judge can see the exact same failure a
production customer would.

**Technical justification (verifiable).** The operator does not guess: it runs on frozen
threshold rules over 1-second telemetry. `oom-guard-01` fires when `ram_used_bytes` is
≥95% of total for 2 consecutive samples and restarts the heaviest workload
(`instance.restart`); `disk-fail-01` fires on
`disk[].mount_ro == true` and stops the heaviest workload; `disk-degrade-01` fires on
`disk[].write_latency_ms ≥ 50` for 5 samples and stops the heaviest workload
(`node-telemetry.md` §5). Every autonomous
change is recorded in a `guardrail_audit` record that links the exact telemetry sample
(`trigger.sample_seq`, `rule_id`) to the command it justified.

**The contested area — OOM race conditions.** The honest risk is that the kernel's
OOM-killer terminates `stress-ng` inside the VM before a slow poll ever notices. We
resolve this with three concrete mechanisms, not a promise: (1) telemetry cadence is
**1000 ms** during the demo (D8), so the window is one second, not 10–15; (2) `oom-guard-01`
acts directly on the threshold — there is no model in the loop, so
remediation latency is not model latency; (3) the **chaos harness's deterministic ramp**
drives the metric across the threshold on cue, so the rule fires deterministically even if `stress-ng --vm-bytes 85%`
never quite reaches 95%×2 (`architecture.md` R24). We state plainly that beating the
kernel OOM-killer is best-effort; what we demonstrate deterministically is the
detect → decide → execute → audit loop.

**How the demo proves this.** Run the stress; the
dashboard shows `ram_used_bytes` crossing 95%, then an `instance.restart` command acks `succeeded`,
and the runaway workload comes back — a real remediation on the live cluster,
traceable end-to-end in the audit log.

---

## 2. What cloud system will we use to tie the nodes together — LXD? OpenStack? — and how do we make them share the same network?

**Answer (plain language).** We use **LXD** (from the snap, pinned to the `5.21/stable`
LTS channel) as one 3-member cluster spanning Nodes A, B, and C. OpenStack is rejected —
it needs hours-to-days of setup and a stack of control-plane daemons for problems this
demo does not have. Networking is the key honest choice: we do **not** stretch a virtual
overlay across the nodes. Each node keeps a local NAT bridge (`lxdbr0`) on its own subnet,
and cross-node traffic travels the physical underlay network by real IP address. The GPU
inference service on Node C is simply reachable at `http://10.10.0.13:8080`.

**Technical justification (verifiable).** The frozen decisions are `infra-decisions.md`
D1 (LXD, not MicroCloud/OpenStack), D2 (underlay routing, no OVN/Fan/VXLAN), and D5
(host-exposed `llama.cpp` on the underlay IP). Every cross-node service is addressed by
underlay IP:port, and one firewall rule on Node C — `allow tcp/8080 from 10.10.0.0/24` —
authorizes all agent VMs, because each VM's traffic SNATs through its own node's underlay
IP.

**The contested area — cross-node latency.** The adversarial risk called out in
`project.md` is that overlay tunnels (OVN/Fan/VXLAN) silently drop packets on MTU
mismatch and add routing latency to every multi-turn agent ↔ LLM call. The concrete
resolution is to remove the tunnel: a single static underlay route either works or it
does not, there is no encapsulation to misconfigure, and the guest MTU is clamped to the
underlay MTU (`1460` on GCP) to avoid PMTU black holes (`infra-decisions.md` D2). Host
exposure of `llama.cpp` also removes one virtualization hop from the streaming path.

**How the demo proves this.** From an agent VM on Node A, `curl
http://10.10.0.13:8080/v1/models` returns the model list over standard HTTP, and a
streamed chat completion flows Node-A → Node-C with no tunnel in the path — a judge can
watch the route (`traceroute`) and the streamed tokens at once.

---

## 3. How do we smartly allocate agent VM resources — can we infer them from the user's natural language?

**Answer (plain language).** Yes. The user types a plain-English request, and an
intent-extraction layer turns it into a structured spec (how many VMs, how many CPUs, how
much RAM, which node). That spec then passes through a deterministic validator before it
touches the cluster: the validator clamps out-of-range numbers down to what the physical
hosts actually have, and rejects anything structurally impossible. The language model
recommends; the validator decides.

**Technical justification (verifiable).** Extraction runs **on-prem** (DeepSeek-flash on
Node C, using a `json_object` response format with tolerant markdown parsing) so raw prompts never leave the customer's
premises (`controller-api.md` §4.5). The output is an `AllocationRequest` validated
against `vm-allocation.schema.json` and the clamp policy in `controller-api.md` §5.
Placement is then by capacity: the controller passes live host metrics to the model as
context, but the final node assignment is made by the guardrail against the registered
per-node capacity.

**The contested area — unconstrained LLM output.** The risk is a prompt like "super fast
agent for heavy parallel crunching" yielding `vcpus: 128, ram_gb: 512` that LXD cannot
honor. The concrete resolution is deterministic hard guardrails, not model politeness:
absolute caps in the schema (`vcpus ≤ 128`, `ram_gb ≤ 512`, `count ≤ 64`), per-node
ceilings with `ram_reserve_gb` and `disk_reserve_ratio` reserved for the host OS, and
preferred ranges per VM type (agent_vm `vcpus [1,8]`, `ram_gb [1,16]`). Numbers are
clamped; structural violations (e.g. `passthrough_gpu: true`) are rejected outright; the
plan is all-or-nothing so a rejected class creates nothing.

**How the demo proves this.** Type an absurd request — "128 vCPU, 512 GB RAM worker" — and
watch the returned plan show clamped values within the host's real capacity, with the
per-field `from → to` clamping visible in the `guardrail_audit` record.

---

## 4. How will we serve the pre-baked agentic OS — what is pre-installed, and how do we hand it to customers AI-ready (SSH-able)?

**Answer (plain language).** We ship immutable, pre-baked images, so nothing is
downloaded or installed at boot. The host image ("Termini Node OS") is Ubuntu 24.04 with
LXD, time-sync, a default-deny firewall, and SSH already on
disk; the guest image ("agent-vm") carries Python/Node and the agent runtime; and the
llama.cpp binary plus model weights are pre-warmed on Node C. At demo time cloud-init only
registers the node — under 5 seconds. Access is browser-first:
the controller holds an SSH key and the dashboard offers a one-click web terminal plus an
`ssh` string — no passwords.

**Technical justification (verifiable).** The frozen decisions are `infra-decisions.md`
D3 (immutable images + offline-preloaded workloads + activation-only cloud-init), D4
(controller-held SSH key), and D6 (outbound-only mTLS control channel). The dashboard
terminal is brokered over that outbound channel (`architecture.md` R14), and the `ssh`
string plus web terminal are served from the controller-held key.

**The contested area — data governance ("zero data leaves on-prem").** The fair challenge
is that a prompt parser and an error-log analyzer normally send workload context to cloud
APIs. We make "your data, our operations" enforceable, not aspirational, with a
C0/C1/C2 data taxonomy: only hardware/OS telemetry (C0) and transformed identifiers (C1)
may egress; application logs and customer payloads (C2) never leave. The controller-side
governance gate re-verifies every inbound payload against the taxonomy and
fails closed on any anomaly — a non-conforming batch is rejected whole. External model calls are scoped to C0
telemetry only, through a static, versioned prompt template. On the SSH side, the
`project.md` idea of displaying a project private key on the frontend is replaced by a
controller-held key surfaced as a web terminal and an `ssh` string.

**How the demo proves this.** Power on a node and watch it reach *registered* in under 5
seconds with zero package installs; open the web terminal to get a live shell with no
password; then inject a C2 field and watch the governance gate reject the batch
fail-closed, and pull `GET /v1/operator/audits` to show each remediation action traceable
by its `call_id`.
