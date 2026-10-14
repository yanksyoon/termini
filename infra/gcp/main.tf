# Termini GCP footprint — VPC + underlay subnet + firewall + three VMs (A/B/C).
#
# One VPC and one subnetwork carry all three nodes so they share the frozen
# underlay CIDR 10.10.0.0/24 (docs/contracts/infra-decisions.md D2). Static
# internal addresses are pinned at 10.10.0.11/12/13 to match that plan.

locals {
  # Frozen node roles/addresses from the D2 addressing plan.
  #   Node A = LXD seed + compute, Node B = compute, Node C = inference host.
  # Node C is CPU-only by default (gpu_accelerator_count = 0): it runs llama.cpp
  # on CPU. Set gpu_accelerator_count = 1 + inference_machine_type = g2-standard-4
  # to attach a real L4 after the billing account is upgraded.
  node_spec = {
    node-a = { role = "compute", host = 11, guest_cidr = "10.20.1.0/24", seed = true, inference = false }
    node-b = { role = "compute", host = 12, guest_cidr = "10.20.2.0/24", seed = false, inference = false }
    node-c = { role = "inference", host = 13, guest_cidr = "10.20.3.0/24", seed = false, inference = true }
  }

  # Single switch: a non-zero accelerator count re-enables the GPU on Node C.
  accelerate_node_c = var.gpu_accelerator_count > 0

  node_ip     = { for k, v in local.node_spec : k => cidrhost(var.network_cidr, v.host) }
  gateway_ip  = cidrhost(var.network_cidr, 1)
  seed_node   = [for k, v in local.node_spec : k if v.seed][0]
  seed_ip     = local.node_ip[local.seed_node]
  node_names  = sort(keys(local.node_spec))
  common_tags = ["termini-node"]

  # The team SSH key goes into cloud-init `authorized_keys` (node-bootstrap.md §7).
  # Falls back to the break-glass key so a tfvars that only sets ssh_public_key
  # still injects a key; empty => no ssh_authorized_keys stanza at all.
  team_ssh_pubkey = var.team_ssh_public_key != "" ? var.team_ssh_public_key : var.ssh_public_key

  node_image_alias   = "${var.image_project}/${var.image_family}"
  node_image_version = var.node_image_version != "" ? var.node_image_version : var.image_family
}

# Per-node cloud-init user-data, rendered from the SHARED bootstrap template by a
# provider-free local module so it can be rendered offline with no GCP access:
#   terraform -chdir=infra/gcp/userdata output -json node_user_data
# (infra/gcp/cloud-init/node.yaml.tftpl is a symlink to the same template the
# LXD flow renders, so the two paths cannot drift — task t_5e9c9d96.)
module "userdata" {
  source = "./userdata"

  # Absolute paths: a local module is copied under .terraform/modules, so it
  # cannot reach the shared files via path.module. These are the SAME template
  # and serve script the LXD flow uses.
  cloud_init_template     = "${abspath(path.module)}/cloud-init/node.yaml.tftpl"
  llama_serve_script_path = "${abspath(path.module)}/cloud-init/llama-serve.sh"

  node_spec              = { for k, v in local.node_spec : k => { role = v.role, inference = v.inference } }
  node_ip                = local.node_ip
  gateway_ip             = local.gateway_ip
  seed_ip                = local.seed_ip
  network_cidr           = var.network_cidr
  controller_url         = var.controller_url
  controller_ca_pubkey   = var.controller_ssh_ca_pubkey
  telemetry_interval_ms  = var.telemetry_interval_ms
  team_ssh_pubkey        = local.team_ssh_pubkey
  enrollment_token       = var.enrollment_token
  image_alias            = local.node_image_alias
  image_version          = local.node_image_version
  serve_inference        = var.serve_inference
  llamacpp_commit        = var.llamacpp_commit
  inference_model_url    = var.inference_model_url
  inference_model_file   = var.inference_model_file
  inference_model_sha256 = var.inference_model_sha256
  inference_model_alias  = var.inference_model_alias
  inference_port         = var.inference_port
}

# --- Network -----------------------------------------------------------------

resource "google_compute_network" "vpc" {
  name                    = "${var.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  description             = "Termini demo VPC — one underlay for Nodes A/B/C (infra-decisions.md D2)."
}

resource "google_compute_subnetwork" "underlay" {
  name          = "${var.name_prefix}-underlay"
  ip_cidr_range = var.network_cidr
  region        = var.region
  network       = google_compute_network.vpc.id
  description   = "Single shared underlay subnet for Nodes A/B/C, CIDR ${var.network_cidr}."
}

# --- Static internal addresses (D2: .11 = A, .12 = B, .13 = C) ----------------

resource "google_compute_address" "node" {
  for_each     = local.node_spec
  name         = "${var.name_prefix}-${each.key}-ip"
  subnetwork   = google_compute_subnetwork.underlay.id
  address_type = "INTERNAL"
  address      = local.node_ip[each.key]
  region       = var.region
  description  = "Static underlay address for ${each.key} (${each.value.role})."
}

# --- Firewall rules (task requirement + D2 port map) -------------------------

resource "google_compute_firewall" "ssh" {
  name          = "${var.name_prefix}-allow-ssh"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = var.ssh_source_ranges
  target_tags   = local.common_tags
  description   = "Admin SSH. CA-signed certs only per D4; tighten source_ranges for real use."
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "controller_api" {
  name          = "${var.name_prefix}-allow-controller-api"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = [var.network_cidr]
  target_tags   = local.common_tags
  description   = "Controller registration/API ports from the underlay subnet."
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "lxd_cluster" {
  name          = "${var.name_prefix}-allow-lxd-cluster"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = [var.network_cidr]
  target_tags   = local.common_tags
  description   = "LXD/OVN cluster API (TCP 8443) between members and the controller."
  allow {
    protocol = "tcp"
    ports    = ["8443"]
  }
}

resource "google_compute_firewall" "vxlan" {
  name          = "${var.name_prefix}-allow-vxlan"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = [var.network_cidr]
  target_tags   = local.common_tags
  description   = "VXLAN/Geneve underlay transport for any future OVN overlay (UDP 4789)."
  allow {
    protocol = "udp"
    ports    = ["4789"]
  }
}

resource "google_compute_firewall" "inference" {
  name          = "${var.name_prefix}-allow-inference"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = [var.network_cidr]
  target_tags   = ["${var.name_prefix}-node-c"]
  description   = "llama.cpp HTTP on Node C only, reachable from the underlay subnet (D2/D5). Never public."
  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
}

# The intent-extraction instance (D5 follow-up): `POST /intents` extraction runs
# against its OWN llama.cpp process on Node C so the operator's ~12k-token
# decision prompt cannot starve it (controller-api.md §5.2). Node-c-only, same
# underlay source, never public.
resource "google_compute_firewall" "inference_intent" {
  name          = "${var.name_prefix}-allow-inference-intent"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = [var.network_cidr]
  target_tags   = ["${var.name_prefix}-node-c"]
  description   = "Intent-extraction llama.cpp HTTP on Node C only, from the underlay subnet. Never public."
  allow {
    protocol = "tcp"
    ports    = ["8090"]
  }
}

# --- Compute instances --------------------------------------------------------

resource "google_compute_instance" "node" {
  for_each = local.node_spec

  name         = "${var.name_prefix}-${each.key}"
  machine_type = each.value.inference ? var.inference_machine_type : var.compute_machine_type
  zone         = var.zone
  description  = "Termini ${each.value.role} node ${each.key}."

  # Tagged so the firewall rules and teardown tooling can select groups of nodes.
  tags = concat(local.common_tags, ["${var.name_prefix}-${each.key}"])

  labels = merge(var.labels, {
    node = each.key
    role = each.value.role
  })

  # A demo footprint must be destroyable in one command.
  deletion_protection       = false
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "${var.image_project}/${var.image_family}"
      size  = var.boot_disk_gb
      type  = var.boot_disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.underlay.id
    network_ip = google_compute_address.node[each.key].address
    # Ephemeral public IP so the node is SSH-reachable for the demo. No inbound
    # service other than SSH/8443/8080 is opened by the firewall above.
    access_config {}
  }

  # Accelerator on Node C ONLY when gpu_accelerator_count > 0. Default is 0, so
  # no guest_accelerator block is emitted and Node C is a plain CPU VM.
  dynamic "guest_accelerator" {
    for_each = (each.value.inference && local.accelerate_node_c) ? [1] : []
    content {
      type  = var.gpu_accelerator_type
      count = var.gpu_accelerator_count
    }
  }

  scheduling {
    # Accelerator instances cannot live-migrate; they must terminate on host
    # maintenance. CPU-only nodes migrate normally.
    on_host_maintenance = (each.value.inference && local.accelerate_node_c) ? "TERMINATE" : "MIGRATE"
    automatic_restart   = true
    provisioning_model  = "STANDARD"
  }

  service_account {
    scopes = var.instance_scopes
  }

  metadata = merge(
    {
      # Unified cloud-init (task t_5e9c9d96): the SAME template the LXD flow uses
      # (infra/bootstrap/cloud-init/node-bootstrap.yaml.tftpl, via the
      # cloud-init/node.yaml.tftpl symlink) gives every GCP node SSH key custody,
      # the config + enrollment token, the controller SSH CA, sshd hardening, and
      # the termini-bootstrap.service start. No package installs, no downloads.
      user-data      = module.userdata.node_user_data[each.key]
      enable-oslogin = "FALSE"
    },
    var.ssh_public_key == "" ? {} : { ssh-keys = "${var.ssh_user}:${var.ssh_public_key}" },
  )

  depends_on = [google_compute_address.node]
}

# --- Inventory for downstream tasks ------------------------------------------

locals {
  inventory = {
    schema_version = "1.0"
    generated_by   = "terraform"
    project        = var.project
    region         = var.region
    zone           = var.zone
    network = {
      vpc        = google_compute_network.vpc.name
      subnetwork = google_compute_subnetwork.underlay.name
      cidr       = var.network_cidr
      gateway    = local.gateway_ip
    }
    ssh = {
      user = var.ssh_user
      port = 22
    }
    nodes = [
      for name in local.node_names : {
        name         = name
        hostname     = google_compute_instance.node[name].name
        role         = local.node_spec[name].role
        machine_type = google_compute_instance.node[name].machine_type
        private_ip   = google_compute_instance.node[name].network_interface[0].network_ip
        public_ip    = google_compute_instance.node[name].network_interface[0].access_config[0].nat_ip
        zone         = google_compute_instance.node[name].zone
        accelerator  = (local.node_spec[name].inference && local.accelerate_node_c) ? { type = var.gpu_accelerator_type, count = var.gpu_accelerator_count } : null
        tags         = google_compute_instance.node[name].tags
        labels       = google_compute_instance.node[name].labels
      }
    ]
  }
}

resource "local_file" "inventory" {
  content         = jsonencode(local.inventory)
  filename        = var.inventory_path
  file_permission = "0644"
  depends_on      = [google_compute_instance.node]
}
