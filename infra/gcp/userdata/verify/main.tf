# Verification root for the GCP node user-data renderer (task t_5e9c9d96).
#
# Renders all three nodes with representative inputs through the SAME provider-free
# local module the real footprint uses — no GCP project, no credentials, no state
# to touch. infra/gcp/verify-cloud-init.sh runs this and asserts the acceptance
# properties on the output.
#
#   terraform -chdir=infra/gcp/userdata/verify init -backend=false
#   terraform -chdir=infra/gcp/userdata/verify apply -auto-approve
#   terraform -chdir=infra/gcp/userdata/verify output -json node_user_data
#
# This directory is NOT part of the module: Terraform only loads *.tf directly in
# a module directory, never in subdirectories.

module "ud" {
  source = "../"

  cloud_init_template     = abspath("${path.module}/../../cloud-init/node.yaml.tftpl")
  llama_serve_script_path = abspath("${path.module}/../../cloud-init/llama-serve.sh")

  # The front door the first boot installs (task t_359ed606). These are the SAME
  # four repo files infra/gcp/main.tf stages, read with filebase64 by the module.
  inference_front_door_files = [
    { source = abspath("${path.module}/../../../../infra/inference/install-inference-proxy.sh"), path = "/usr/local/src/termini-inference/install-inference-proxy.sh", mode = "0755" },
    { source = abspath("${path.module}/../../../../infra/inference/proxy.py"), path = "/usr/local/src/termini-inference/proxy.py", mode = "0644" },
    { source = abspath("${path.module}/../../../../infra/inference/termini-inference-proxy.service"), path = "/usr/local/src/termini-inference/termini-inference-proxy.service", mode = "0644" },
    { source = abspath("${path.module}/../../../../infra/inference/termini-inference-switch.sh"), path = "/usr/local/src/termini-inference/termini-inference-switch.sh", mode = "0755" },
  ]

  node_spec = {
    node-a = { role = "compute", inference = false }
    node-b = { role = "compute", inference = false }
    node-c = { role = "inference", inference = true }
  }
  node_ip               = { node-a = "10.10.0.11", node-b = "10.10.0.12", node-c = "10.10.0.13" }
  gateway_ip            = "10.10.0.1"
  seed_ip               = "10.10.0.11"
  network_cidr          = "10.10.0.0/24"
  controller_url        = "https://controller.termini.internal"
  controller_ca_pubkey  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICACALIFORNIAEXAMPLE termini-ssh-ca"
  telemetry_interval_ms = 1000
  team_ssh_pubkey       = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEAMKEYVERIFYEXAMPLE termini-team-key"
  enrollment_token      = "verify-enrollment-token"
  image_alias           = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
  image_version         = "ubuntu-2404-lts-amd64"
  serve_inference       = true
  llamacpp_commit       = "7fe450e19305b828c199d602c23a8337aaa1f03b"
  # The shipped pin (image/models.lock.json): the resident alias is `gemma`.
  inference_model_url    = "https://storage.googleapis.com/termini-510609-models/gemma-3-4b-it-gguf/gemma-3-4b-it-Q4_K_M.gguf"
  inference_model_file   = "gemma-3-4b-it-Q4_K_M.gguf"
  inference_model_sha256 = "04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19"
  inference_model_alias  = "gemma"
  inference_port         = 8080
  inference_backend      = "local"
}

output "node_user_data" {
  value     = module.ud.node_user_data
  sensitive = true
}
