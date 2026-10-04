# AI hackathon

Registration link: https://aibuildercup.com/

### Tags

AI governance, AI fleet management, AI infrastructure operations

### Introduction

Companies that operate on sensitive data or data that includes competitive advantage have a hard time adjusting to AI due to governance issues.
Recently, companies like Samsung have established a deal with Mistral to deploy on-site AI solutions to tackle such issues. However, the issue with maintenance and 
operations still remain. Furthermore, for small scale businesses that also rely on governance, it is hard to set up the infrastructure without expert knowledge.
To address the problem, we propose a solution that bootstraps AI infrastructure remotely. Your hardware, your data, our operations. BencP0+r4B34\P0+r4B35\P0+r6B42\P0+r5053\P0+r5045\hmarking from
already existing solutions like Datadog, we know that some functions of the infrastructure can be designated to a third party solution.

### Idea

We will achieve day-0 operations (bootstrapping) to day 2 operations (maintenance).
Once the user brings the hardware, the installer will automatically ping the controller. The controller will then operate on the set of hardwares connected to the given
customer, bootstrapping their AI-first operating system by default. The AI-first operating system includes VMs for agent fleet, nodes with GPU serving AI inference and
firewall rules to secure the Virtual AI cloud.
Once there are issues detected with the underlying infrastructure such as disk failures, network latency, …etc, the AI operator will take on follow up actions to unblock
such disasters. If they require physical human help, follow up actions will be forwarded to the person responsible (i.e. replace faulty disk).

### Design

A. Central Control Plane (Cloud Side)
Controller (Google Cloud VM): Serves as the central orchestrator and AI brain. It receives system registrations, assigns roles, determines resource allocations, and issues autonomous operational commands.
Post-Bootstrap Management Engine (Process Box - Top): Represents the Controller's decision logic divided into two phases:
Post-Bootstrap Phase A: Listens for incoming nodes, detects hardware specs, and allocates node roles (Agent vs. Inference VM host).
Post-Bootstrap Phase B: Collects telemetry metrics and runs the AI operator loop (detecting failures and executing fixes).
Customer Operations Frontend: The web dashboard displaying real-time node health, live AI logs, resource utilization, and user intervention controls.

B. Bare-Metal Physical Network (Local Data Plane)
Physical Network (VPC): The local network environment connecting physical server nodes (Nodes A, B, and C).
Physical Hardware Nodes (Nodes A, B, C):
Nodes A & B: Standard compute nodes intended to host worker/agent workloads.
Node C (GPU): A high-performance node equipped with a GPU to host model inference workloads.
Virtualized Guest Instances (VMs):
Agent VMs (on Nodes A & B): Small, isolated execution environments running task-oriented AI agents. 
Inference VM (llama.cpp on Node C): Dedicated VM running local LLM inference engines utilizing physical GPU passthrough.

C. Node Bootstrapping Engine (Process Box - Bottom)
Local Cloud-Init & Installer: Pre-baked installer logic executed locally on each physical node upon power-on. It gathers system hardware specs (CPU, RAM, IP) and initializes the local cloud system (LXD or OpenStack).



Specification
What are the operations scenarios we're willing to showcase with AI & how will we simulate the operations scenario?
⁠What cloud system will we use to tie the nodes together - LXD? OpenStack? How do we make them share the same network according to each cloud technology?
⁠How do we smartly allocate the agent VM resources? Can we infer from the user's natural language?
How will we serve the pre-baked agentic OS? What should be pre-installed in the OS, how do we serve it to the customers as AI ready to use (perhaps the frontend should expose SSH-able keys)?


1. Operations Scenarios to Showcase & How to Simulate Them

Scenario A: The out of memory

What Happens: An inference VM running LLMs spikes in memory, starving neighboring Agent VMs and causing dropped heartbeats.

AI Action: The Gemini agent reads system metrics, identifies `llama.cpp` using 95% RAM, resizes the VM specs on the fly via the controller API, or live-migrates an Agent VM to Node B.

How to Simulate:
Run a simple chaos script inside an Inference VM:
```bash
# Simulates immediate memory exhaustion / high load
stress-ng --vm 2 --vm-bytes 85% --timeout 30s
```
Scenario B: Physical Disk Degradation / Drive Failure

What Happens: Disk write latency spikes, or a mount point flips to Read-Only.

AI Action: The AI operator flags the degrading drive on Node A, drains the node (evacuates running Agent VMs to Node B), and alerts the user on the frontend: "Node A disk failure predicted. Workloads migrated to Node B safely."

How to Simulate:
Simulate I/O latency or read-only status on the host node using `tc` (traffic control for disk I/O) or simply dropping a fake error line into `/var/log/syslog`:
```bash
logger -p daemon.crit "EXT4-fs error (device sda1): journal has aborted / I/O error"

```


2. Networking & Cloud Infrastructure: LXD vs. OpenStack

Use LXD (or LXD + MicroCloud)

Feature
LXD Cluster / MicroCloud
OpenStack
Setup Time
5–10 minutes (single snap command).
Hours/Days (extreme complexity).
Resource Overhead
Very low (runs native Linux containers or light QEMU VMs).
Heavy (requires multiple controller/compute services running background daemons).
Cluster API
Clean, developer-friendly REST API.
Complex client SDKs.


How to Make Nodes Share the Same Network

To give every VM across Node A, Node B, and Node C an IP address on the *same logical subnet* (so Agent VMs on Node A can reach `llama.cpp` on Node C directly):

1. Overlay Networking (OVN / VXLAN):
LXD has native support for OVN (Open Virtual Network) and VXLAN overlays.
When you form an LXD cluster (`lxd cluster add`), LXD manages a virtual bridge (e.g., `lxdbr0`) across all nodes.
VMs on Node A and Node C receive IP addresses in the same CIDR (e.g., `10.0.100.0/24`) wrapped over your demo VPC physical network.
2. Simplified Fallback for Demo (LXD Fan Networking):
If OVN is too complex during setup, LXD supports "Fan Networking" out of the box. It automatically tunnels traffic between host IPs using a `/8` subnet map, allowing cross-node IP routing with zero manual network setup.


3. Smart Resource Allocation from Natural Language
You can infer VM resource sizing from user prompts by placing an Intent & Specs Extraction Layer (using Gemini) between the user prompt and the LXD Controller.

```
User Prompt: "Deploy 3 worker agents and 1 fast LLM model for coding tasks"
                       │
                       ▼
            [ Gemini 2.0 Flash / ADK ]
  (Extracts Structured JSON Specs using Function Calling)
                       │
                       ▼
{
  "allocations": [
    {"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "target_node": "auto"},
    {"type": "inference_vm", "count": 1, "vcpus": 8, "ram_gb": 16, "passthrough_gpu": true, "target_node": "Node C"}
  ]
}
                       │
                       ▼
             [ GCP Controller API ] ──► Triggers LXD REST API to spin up instances

```
Topology Balancing:
The GCP controller passes current host metrics (`Node A: 30% load`, `Node C: GPU present`) as system prompt context to Gemini.
Gemini decides where to place each VM dynamically based on physical hardware capacity.


4. Pre-Baked Agentic OS & Distribution Strategy
To make hardware "AI ready" instantly, you need a deterministic Cloud-Init + OS Image Pipeline.

A. What Should Be Pre-installed in the Image?
1. Host OS (The "Installer" image running on Node A/B/C):
Minimal Ubuntu Server 24.04 LTS base.
LXD Snap pre-installed.
A lightweight Bootstrap Daemon / Agent (written in Go or Python) running as a `systemd` service.
Pre-configured SSH daemon with public keys derived from the Controller.

2. Guest VM OS (The "Agent VM" image inside LXD):
Pre-installed Python 3.12, Node.js, `pip` packages (LangChain, AutoGen, CrewAI, etc.).
Pre-downloaded base models or execution runtime binaries (e.g., `llama.cpp` server) to prevent long download times during the hackathon demo.

B. How to Serve It to Customers (The Flow)
```
[ User UI ] ──► (Generates SSH Keypair) ──► [ Controller ]
                                                 │
 [ Node Bootstrapping via Cloud-Init ] ◄──────────┘

```
1. User Registers on Frontend: The Web Frontend automatically generates an SSH Keypair for the project.
2. Pre-Baked Image Bootstrapping:
You provide a minimal `cloud-init.yaml` pre-seed file (or a pre-built ISO file).
When the node boots up, `cloud-init` runs **Bootstrap Phase A**:
Phone home to GCP Controller with `Registration ID` and system hardware specs (CPUs, RAM, GPU detect).
Inject the project's public SSH key into `/root/.ssh/authorized_keys`.
Start polling the Controller for cluster commands.
3. Exposing Access on Frontend:
The Customer Frontend displays:
Node status (Connected, CPU/RAM stats).
One-click Web Terminal (xterm.js via WebSocket) or `ssh root@<node-ip>` string populated with the matching private key.



Some points to pay attention to (based on the adversarial agent analysis)

1. You claimed zero data leaves the on-premise hardware, but your prompt parser and AIOps error log analyzer are constantly sending workload context to Google Cloud APIs.
Demo Fix: Explicitly state that user prompts pass through an On-Premise Regex & PII Redactor before hitting the Gemini API, or limit GCP Gemini’s scope strictly to hardware/OS telemetry (CPUs, thermals, disk metrics), not application log contents.

2. `cloud-init` is notoriously slow Installing packages, running system updates, and setting up LXD cluster overlays over `cloud-init` takes 3 to 7 minutes on fresh hardware. Downloading a 4GB quantized LLM model for `llama.cpp` during the demo will crash the presentation due to network bottlenecks or timeouts.
Demo Fix: Do NOT perform real cold-boot provisioning live. Have the LXD cluster and `llama.cpp` binary 100% pre-baked and pre-warmed on disk. The "installer" script for the demo should merely activate an already-installed daemon, register the token in under 5 seconds, and instantly attach the node.

3. LXD Fan Networking maps subnets over host IPs using basic IP encapsulation. However, if the demo setup loses connectivity, or if MTU sizes on the host physical network mismatch, cross-node VM traffic silently drops packets. Furthermore, if an Agent VM on Node A makes sequential multi-turn calls to `llama.cpp` on Node C, routing overhead over a virtual bridge can add unpredictable latency during live streaming responses.
Demo Fix: For demo reliability, hardcode a direct Host-to-Host static route or expose `llama.cpp` directly on Node C’s host IP (`http://<NODE_C_IP>:8080`), allowing Agent VMs on Node A to call Node C over standard HTTP without relying on complex OVN/Fan virtual bridges during a live presentation.

4. stress-ng allocates memory at the Linux kernel level inside the target guest VM. Unless LXD/cgroups limits are strict, the Linux Kernel Out-Of-Memory (OOM) Killer will simply terminate the `stress-ng` process inside the VM before the GCP Controller even receives its telemetry polling cycle (which usually runs every 10–15 seconds).
Demo Fix: Ensure your GCP Controller telemetry agent polls metrics at a high frequency (e.g., every 1–2 seconds) during the demo, or simulate the fault via a persistent metric threshold trigger so the AI agent gets to execute an actual remediation command (like calling LXD API to increase memory limit) before the kernel kills the process.

5. LLMs are prone to unconstrained outputs. If a user types "I want a super fast agent to do heavy parallel crunching," Gemini might output `vcpus: 128` and `ram_gb: 512`. If the GCP Controller passes these specs directly to the LXD API without validation, LXD execution will fail because the physical host only has 16 vCPUs and 32GB RAM.
Demo Fix: Enforce strict Deterministic Hard Guardrails inside the Controller code. Gemini can recommend specs, but the GCP Controller must pass the JSON through a validator that clamps requests to `min(requested_ram, max_host_ram)`.

