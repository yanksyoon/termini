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

  node_spec = {
    node-a = { role = "compute", inference = false }
    node-b = { role = "compute", inference = false }
    node-c = { role = "inference", inference = true }
  }
  node_ip                = { node-a = "10.10.0.11", node-b = "10.10.0.12", node-c = "10.10.0.13" }
  gateway_ip             = "10.10.0.1"
  seed_ip                = "10.10.0.11"
  network_cidr           = "10.10.0.0/24"
  controller_url         = "https://controller.termini.internal"
  controller_ca_pubkey   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICACALIFORNIAEXAMPLE termini-ssh-ca"
  telemetry_interval_ms  = 1000
  team_ssh_pubkey        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEAMKEYVERIFYEXAMPLE termini-team-key"
  enrollment_token       = "verify-enrollment-token"
  image_alias            = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
  image_version          = "ubuntu-2404-lts-amd64"
  serve_inference        = true
  llamacpp_commit        = "7fe450e19305b828c199d602c23a8337aaa1f03b"
  inference_model_url    = "https://example.invalid/DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf"
  inference_model_file   = "DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf"
  inference_model_sha256 = "87bcba20b4846d8dadf753d3ff48f9285d131fc95e3e0e7e934d4f20bc896f5d"
  inference_model_alias  = "deepseek-flash"
  inference_port         = 8080
}

output "node_user_data" {
  value     = module.ud.node_user_data
  sensitive = true
}
