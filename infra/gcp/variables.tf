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
  description = "GCP zone for all three VMs. Must offer the GPU accelerator (L4 is in us-central1-a/b/c/f)."
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

# --- Machine types (task acceptance: A/B compute-optimized, C a real GPU) -----

variable "compute_machine_type" {
  description = "Machine type for Nodes A and B (compute nodes)."
  type        = string
  default     = "n2-standard-4"
}

variable "gpu_machine_type" {
  description = "Machine type for Node C (GPU node). g2-standard-4 ships exactly one NVIDIA L4."
  type        = string
  default     = "g2-standard-4"
}

variable "gpu_accelerator_type" {
  description = "Accelerator type attached to Node C (must match gpu_machine_type)."
  type        = string
  default     = "nvidia-l4"
}

variable "gpu_accelerator_count" {
  description = "Number of accelerators on Node C (g2-standard-4 == 1)."
  type        = number
  default     = 1
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
