# Termini GCP node user-data renderer — provider-free local module.
#
# Turns the SHARED node-bootstrap cloud-init template
# (infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl, reached through the
# infra/gcp/cloud-init/node.yaml.tftpl symlink) into per-node user-data for the
# GCP VMs. It declares NO provider and NO resources, so the render is a pure
# function that can be evaluated offline with no GCP credentials:
#
#   terraform -chdir=infra/gcp/userdata init -backend=false
#   terraform -chdir=infra/gcp/userdata apply -auto-approve   # 0 resources
#   terraform -chdir=infra/gcp/userdata output -json node_user_data
#
# infra/gcp/verify-cloud-init.sh drives exactly that. See task t_5e9c9d96.

terraform {
  required_version = ">= 1.5.0"
}

locals {
  # Node C first-boot script with its __LLAMACPP_COMMIT__/__MODEL_*__/__PORT__/
  # __INFERENCE_BACKEND__ tokens resolved. replace() (not a templatefile) keeps
  # the shell script's own ${VAR} interpolations literal.
  llama_serve_script = replace(
    replace(
      replace(
        replace(
          replace(
            replace(
              replace(
                file(var.llama_serve_script_path),
                "__LLAMACPP_COMMIT__", var.llamacpp_commit
              ),
              "__MODEL_URL__", var.inference_model_url
            ),
            "__MODEL_FILE__", var.inference_model_file
          ),
          "__MODEL_SHA256__", var.inference_model_sha256
        ),
        "__MODEL_ALIAS__", var.inference_model_alias
      ),
      "__INFERENCE_BACKEND__", var.inference_backend
    ),
    "__PORT__", tostring(var.inference_port)
  )

  # Pre-rendered YAML blocks handed to the shared template. Building the
  # conditionals HERE (rather than with Terraform `%{ if }` inside the template)
  # keeps that one template renderable by BOTH Terraform templatefile and
  # envsubst (infra/bootstrap/render-cloud-init.sh) — envsubst has no
  # conditionals. Empty => the stanza/entry is omitted entirely.
  ssh_authorized_keys_block = var.team_ssh_pubkey == "" ? "" : "ssh_authorized_keys:\n  - ${var.team_ssh_pubkey}"

  llama_serve_write_file = join("\n", [
    "  - path: /usr/local/sbin/termini-llama-serve.sh",
    "    permissions: '0755'",
    "    owner: root:root",
    "    content: |",
    # indent() skips the FIRST line, so indent every line of the script body
    # explicitly (prefix + replace) to keep the YAML block scalar consistent.
    "      ${replace(trimspace(local.llama_serve_script), "\n", "\n      ")}",
  ])
  llama_serve_runcmd = "  - [ /usr/local/sbin/termini-llama-serve.sh ]"

  # Node C's inference FRONT DOOR payload (task t_359ed606): the four committed
  # files install-inference-proxy.sh owns, staged so cloud-init can CALL the
  # installer instead of carrying a copy of its heredocs. base64 (filebase64)
  # keeps a 50 KB python file byte-exact — no block-scalar indentation or
  # escaping can alter it — and infra/gcp/verify-cloud-init.sh decodes each entry
  # and compares its sha256 with the repo file.
  front_door_write_files = join("\n", [
    for f in var.inference_front_door_files : join("\n", [
      "  - path: ${f.path}",
      "    permissions: '${f.mode}'",
      "    owner: root:root",
      "    encoding: b64",
      "    content: \"${filebase64(f.source)}\"",
    ])
  ])

  # Per-node cloud-init user-data, rendered from the SHARED bootstrap template.
  node_user_data = {
    for k, v in var.node_spec : k => templatefile(var.cloud_init_template, {
      node_name                 = k
      role_hint                 = v.role
      underlay_ip               = "${var.node_ip[k]}/${split("/", var.network_cidr)[1]}"
      underlay_gw               = var.gateway_ip
      seed_ip                   = var.seed_ip
      controller_url            = var.controller_url
      controller_ca_pubkey      = var.controller_ca_pubkey
      telemetry_interval_ms     = var.telemetry_interval_ms
      team_ssh_pubkey           = var.team_ssh_pubkey
      enrollment_token          = var.enrollment_token
      image_alias               = var.image_alias
      image_version             = var.image_version
      ssh_authorized_keys_block = local.ssh_authorized_keys_block
      # Node C only (serve_inference): keep the llama.cpp serve block, plus the
      # inference front door the first boot installs (t_359ed606).
      extra_write_files = (v.inference && var.serve_inference) ? join("\n", [local.llama_serve_write_file, local.front_door_write_files]) : ""
      extra_runcmd      = (v.inference && var.serve_inference) ? local.llama_serve_runcmd : ""
    })
  }
}

output "node_user_data" {
  description = "Map of node name -> rendered cloud-init user-data (sensitive: carries the enrollment token)."
  value       = local.node_user_data
  sensitive   = true
}
