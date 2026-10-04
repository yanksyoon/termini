# Termini GCP footprint — VPC + underlay subnet + firewall + three VMs (A/B/C).
#
# One VPC and one subnetwork carry all three nodes so they share the frozen
# underlay CIDR 10.10.0.0/24 (docs/contracts/infra-decisions.md D2). Static
# internal addresses are pinned at 10.10.0.11/12/13 to match that plan.

locals {
  # Frozen node roles/addresses from the D2 addressing plan.
  #   Node A = LXD seed + compute, Node B = compute, Node C = GPU inference host.
  node_spec = {
    node-a = { role = "compute", host = 11, guest_cidr = "10.20.1.0/24", seed = true, gpu = false }
    node-b = { role = "compute", host = 12, guest_cidr = "10.20.2.0/24", seed = false, gpu = false }
    node-c = { role = "inference", host = 13, guest_cidr = "10.20.3.0/24", seed = false, gpu = true }
  }

  node_ip     = { for k, v in local.node_spec : k => cidrhost(var.network_cidr, v.host) }
  gateway_ip  = cidrhost(var.network_cidr, 1)
  seed_node   = [for k, v in local.node_spec : k if v.seed][0]
  seed_ip     = local.node_ip[local.seed_node]
  node_names  = sort(keys(local.node_spec))
  common_tags = ["termini-node"]
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

# --- Compute instances --------------------------------------------------------

resource "google_compute_instance" "node" {
  for_each = local.node_spec

  name         = "${var.name_prefix}-${each.key}"
  machine_type = each.value.gpu ? var.gpu_machine_type : var.compute_machine_type
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

  # Real accelerator on Node C; nothing on A/B.
  dynamic "guest_accelerator" {
    for_each = each.value.gpu ? [1] : []
    content {
      type  = var.gpu_accelerator_type
      count = var.gpu_accelerator_count
    }
  }

  scheduling {
    # GPU instances cannot live-migrate; they must terminate on host maintenance.
    on_host_maintenance = each.value.gpu ? "TERMINATE" : "MIGRATE"
    automatic_restart   = true
    provisioning_model  = "STANDARD"
  }

  service_account {
    scopes = var.instance_scopes
  }

  metadata = merge(
    {
      # Activation-only cloud-init (D3): writes /etc/termini/config.yaml and starts
      # the pre-installed bootstrap daemon. No package installs, no model downloads.
      user-data = templatefile("${path.module}/cloud-init/node.yaml.tftpl", {
        node_name             = each.key
        role_hint             = each.value.role
        underlay_ip           = "${local.node_ip[each.key]}/${split("/", var.network_cidr)[1]}"
        underlay_gw           = local.gateway_ip
        seed_ip               = local.seed_ip
        guest_cidr            = each.value.guest_cidr
        controller_url        = var.controller_url
        controller_ca_pubkey  = var.controller_ssh_ca_pubkey
        telemetry_interval_ms = var.telemetry_interval_ms
      })
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
        accelerator  = local.node_spec[name].gpu ? { type = var.gpu_accelerator_type, count = var.gpu_accelerator_count } : null
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
