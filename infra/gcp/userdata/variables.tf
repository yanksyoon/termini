# Inputs for the GCP node user-data renderer (infra/gcp/userdata).

variable "cloud_init_template" {
  description = "Absolute path to the SHARED cloud-init template to render (infra/gcp/cloud-init/node.yaml.tftpl, a symlink to infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl). Absolute because a local module is copied under .terraform/modules and cannot reach sibling paths."
  type        = string
}

variable "llama_serve_script_path" {
  description = "Absolute path to the Node C llama.cpp serve script (infra/gcp/cloud-init/llama-serve.sh)."
  type        = string
}

variable "inference_front_door_files" {
  description = "Node C inference front-door payload staged into cloud-init (task t_359ed606): objects with `source` (absolute host path, read with filebase64), `path` (destination on the node) and `mode`. The files themselves are owned by infra/inference/install-inference-proxy.sh, which the first boot then CALLS; an empty list renders no payload (activation-only nodes)."
  type = list(object({
    source = string
    path   = string
    mode   = string
  }))
  default = []
}

variable "inference_backend" {
  description = "Backend a FRESH Node C serves through the front door: local (shipped since the inference split — D9) or gemini-proxy. Substituted for __INFERENCE_BACKEND__ in the serve script."
  type        = string
  default     = "local"
}

variable "node_spec" {
  description = "Per-node role/inference flags, keyed by node name (node-a/b/c)."
  type        = map(object({ role = string, inference = bool }))
}

variable "node_ip" {
  description = "Underlay IP per node name."
  type        = map(string)
}

variable "gateway_ip" {
  description = "Underlay default gateway (bootstrap_seed_ip is separate)."
  type        = string
}

variable "seed_ip" {
  description = "LXD seed underlay IP written as bootstrap_seed_ip."
  type        = string
}

variable "network_cidr" {
  description = "Underlay CIDR (its prefix length is appended to each node's underlay_ip)."
  type        = string
}

variable "controller_url" {
  description = "Controller base URL written into /etc/termini/config.yaml."
  type        = string
}

variable "controller_ca_pubkey" {
  description = "Controller SSH CA public key (TrustedUserCAKeys). Empty = comment-only CA file."
  type        = string
  default     = ""
}

variable "telemetry_interval_ms" {
  description = "Telemetry cadence written into the node config."
  type        = number
  default     = 1000
}

variable "team_ssh_pubkey" {
  description = "Team SSH public key injected into authorized_keys. Empty = no ssh_authorized_keys stanza."
  type        = string
  default     = ""
}

variable "enrollment_token" {
  description = "Single-use enrollment token written to /var/lib/termini/enrollment.token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "image_alias" {
  description = "Image alias/label written to /etc/termini/bootstrap-origin (informational)."
  type        = string
  default     = ""
}

variable "image_version" {
  description = "Image version label written to /etc/termini/bootstrap-origin (informational)."
  type        = string
  default     = ""
}

variable "serve_inference" {
  description = "Render the Node C llama.cpp serve block on the inference node."
  type        = bool
  default     = true
}

variable "llamacpp_commit" {
  description = "Pinned llama.cpp commit substituted into the serve script."
  type        = string
  default     = ""
}

variable "inference_model_url" {
  description = "GGUF model URL substituted into the serve script."
  type        = string
  default     = ""
}

variable "inference_model_file" {
  description = "GGUF filename substituted into the serve script."
  type        = string
  default     = ""
}

variable "inference_model_sha256" {
  description = "GGUF sha256 substituted into the serve script."
  type        = string
  default     = ""
}

variable "inference_model_alias" {
  description = "Served model alias substituted into the serve script."
  type        = string
  default     = ""
}

variable "inference_port" {
  description = "llama.cpp listen port substituted into the serve script."
  type        = number
  default     = 8080
}
