# Termini GCP footprint — input variables.
# The defaults encode the frozen W1 decisions in docs/contracts/infra-decisions.md
# (D2 addressing plan, D7 storage, D5 GPU). Override in terraform.tfvars.

variable "project" {
  description = "GCP project id that owns the three nodes."
  type        = string
}

variable "region" {
  description = "GCP region for the shared underlay VPC/subnet."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for all three VMs. Must be a zone that offers the machine types below (and, if the accelerator is re-enabled, one that offers it)."
  type        = string
  default     = "us-central1-a"
}

variable "name_prefix" {
  description = "Prefix for every resource name so the footprint tears down cleanly."
  type        = string
  default     = "termini"
}

# --- Network (frozen D2 plan) ------------------------------------------------

variable "network_cidr" {
  description = "The single underlay CIDR shared by all three VMs (docs/contracts/infra-decisions.md D2)."
  type        = string
  default     = "10.10.0.0/24"
}

# --- Machine types (task acceptance: A/B compute-optimized, C CPU-heavy) ------

variable "compute_machine_type" {
  description = "Machine type for Nodes A and B (compute nodes)."
  type        = string
  default     = "n2-standard-4"
}

variable "inference_machine_type" {
  description = <<-EOT
    Machine type for Node C (the inference host). Default n2-standard-8: CPU-only
    llama.cpp needs several cores to run a small quantized model acceptably.
    When the GPU is re-enabled (gpu_accelerator_count = 1) set this to
    g2-standard-4, which ships exactly one NVIDIA L4.
  EOT
  type        = string
  default     = "n2-standard-8"
}

variable "gpu_accelerator_type" {
  description = "Accelerator type attached to Node C when gpu_accelerator_count > 0 (must match inference_machine_type, e.g. nvidia-l4 on g2-standard-4)."
  type        = string
  default     = "nvidia-l4"
}

variable "gpu_accelerator_count" {
  description = <<-EOT
    Number of accelerators on Node C. 0 (default) = CPU-only Node C: no
    guest_accelerator block is emitted and Node C is a plain CPU VM. Set to 1
    (and inference_machine_type = "g2-standard-4") to re-enable the GPU after the
    billing account is upgraded from free tier.
  EOT
  type        = number
  default     = 0

  validation {
    condition     = var.gpu_accelerator_count == 0 || var.gpu_accelerator_count == 1
    error_message = "gpu_accelerator_count must be 0 (CPU-only) or 1 (single accelerator)."
  }
}

# --- Node C inference serving (CPU llama.cpp) ---------------------------------

variable "serve_inference" {
  description = "Provision a host-exposed CPU llama.cpp server on Node C at boot (pinned deepseek-flash GGUF, OpenAI-compatible on :8080). Set false to keep Node C activation-only."
  type        = bool
  default     = true
}

variable "inference_model_url" {
  description = "GGUF model URL downloaded onto Node C when serve_inference is true. Default is the pinned deepseek-flash weight from image/models.lock.json (t_ab18d582)."
  type        = string
  default     = "https://huggingface.co/bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF/resolve/main/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf"
}

variable "inference_model_file" {
  description = "Filename the GGUF model is saved as under /var/lib/termini/models on Node C."
  type        = string
  default     = "DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf"
}

variable "inference_model_sha256" {
  description = "sha256 of the GGUF; first boot aborts unless the downloaded file matches (pinned in image/models.lock.json)."
  type        = string
  default     = "87bcba20b4846d8dadf753d3ff48f9285d131fc95e3e0e7e934d4f20bc896f5d"
}

variable "inference_model_alias" {
  description = "Model alias llama-server advertises and accepts (frozen contract: deepseek-flash, agent-runtime.md AR-6)."
  type        = string
  default     = "deepseek-flash"
}

variable "inference_port" {
  description = "TCP port Node C serves llama.cpp on (frozen contract: 8080, underlay-only)."
  type        = number
  default     = 8080
}

variable "llamacpp_commit" {
  description = "Pinned llama.cpp commit built on Node C (matches image/models.lock.json)."
  type        = string
  default     = "7fe450e19305b828c199d602c23a8337aaa1f03b"
}

# --- Boot image / disk --------------------------------------------------------

variable "image_project" {
  description = "Project hosting the boot image family."
  type        = string
  default     = "ubuntu-os-cloud"
}

variable "image_family" {
  description = "Boot image family. The pre-baked Termini Node OS replaces this once the Packer pipeline lands (D3)."
  type        = string
  default     = "ubuntu-2404-lts-amd64"
}

variable "boot_disk_gb" {
  description = "Boot disk size in GiB per node (D7 loop-backed ZFS pool lives on this disk)."
  type        = number
  default     = 60
}

variable "boot_disk_type" {
  description = "Boot disk type (pd-balanced recommended for the demo)."
  type        = string
  default     = "pd-balanced"
}

variable "node_image_version" {
  description = "Image version label written to /etc/termini/bootstrap-origin (informational). Empty falls back to the boot image family."
  type        = string
  default     = ""
}

# --- Access -------------------------------------------------------------------

variable "ssh_user" {
  description = "Linux user recorded in the inventory for SSH access."
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "Optional break-glass SSH public key injected at provisioning (node-bootstrap.md §7). Empty = none."
  type        = string
  default     = ""
}

variable "team_ssh_public_key" {
  description = <<-EOT
    Team SSH public key cloud-init injects into every node's authorized_keys
    (node-bootstrap.md §7 — the injected-key login the acceptance requires).
    Empty falls back to ssh_public_key; if both are empty no ssh_authorized_keys
    stanza is emitted (activation-only).
  EOT
  type        = string
  default     = ""
}

variable "ssh_source_ranges" {
  description = "CIDRs allowed to reach TCP/22 on the nodes. DEMO DEFAULT IS OPEN — tighten to your admin CIDR or Google IAP (35.235.240.0/20) before any real use."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# --- Controller wiring (node-bootstrap.md §3.2) -------------------------------

variable "controller_url" {
  description = "Controller HTTPS endpoint written into each node's /etc/termini/config.yaml."
  type        = string
  default     = "https://controller.termini.internal"
}

variable "controller_ssh_ca_pubkey" {
  description = "Controller SSH CA public key injected into TrustedUserCAKeys on every node (D4). Empty = skip."
  type        = string
  default     = ""
}

variable "enrollment_token" {
  description = "Single-use controller enrollment token written to /var/lib/termini/enrollment.token on every node (node-bootstrap.md §3.2). Empty = nodes activate but cannot register."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telemetry_interval_ms" {
  description = "Demo telemetry cadence pushed to nodes (D8: 1000 during the demo)."
  type        = number
  default     = 1000
}

variable "instance_scopes" {
  description = "OAuth scopes granted to the default compute service account on each node."
  type        = list(string)
  default     = ["https://www.googleapis.com/auth/logging.write", "https://www.googleapis.com/auth/monitoring.write"]
}

# --- Metadata / inventory -----------------------------------------------------

variable "labels" {
  description = "Labels applied to every resource for clean teardown. Values must be lowercase."
  type        = map(string)
  default = {
    project    = "termini"
    managed-by = "terraform"
    workstream = "infra"
    task       = "t-f2173236"
  }
}

variable "inventory_path" {
  description = "Well-known absolute path where the generated inventory JSON is written for downstream tasks."
  type        = string
  default     = "/home/ubuntu/.termini/inventory.json"
}

# --- Control plane (controller VM) --------------------------------------------
# Added when the controller was back-filled into IaC during the Phase 3
# deployment; it had been created ad-hoc with `gcloud`.

variable "controller_machine_type" {
  description = "Machine type for the controller VM. It runs the REST API, the operator loop and SQLite, and needs no accelerator."
  type        = string
  default     = "n2-standard-2"
}

variable "controller_image_project" {
  description = "Image project for the controller boot disk (stock Ubuntu, not the pre-baked node image)."
  type        = string
  default     = "ubuntu-os-cloud"
}

variable "controller_image_family" {
  description = "Image family for the controller boot disk."
  type        = string
  default     = "ubuntu-2404-lts-amd64"
}

variable "controller_boot_disk_gb" {
  description = "Controller boot disk size. The SQLite database lives here, so it must persist."
  type        = number
  default     = 30
}

variable "controller_boot_disk_type" {
  description = "Controller boot disk type."
  type        = string
  default     = "pd-balanced"
}

variable "controller_private_ip" {
  description = "Static underlay address for the controller, inside network_cidr. Pinned so the TLS hostname and the nodes' controller_url stay valid across rebuilds."
  type        = string
  default     = "10.10.0.2"
}

variable "controller_source_ranges" {
  description = "Sources allowed to reach the controller on 22/80/443. Default is public, because the Firebase-hosted dashboard runs in a browser; tighten to your office/VPN ranges for real use."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

