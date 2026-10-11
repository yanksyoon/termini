# Termini controller VM — the control plane, on the same underlay as the nodes.
#
# Created ad-hoc with `gcloud` during the Phase 3 deployment and back-filled here
# so `infra/gcp` describes the whole footprint. It is NOT part of the node
# fleet: the nodes are the customer's managed metal, the controller is the
# control plane.
#
# Why the controller is a VM and not Cloud Run: it persists to SQLite, and Cloud
# Run's container filesystem is ephemeral and per-instance, which would break the
# single-owner operator lease (migration 0007) and make audit provenance
# unverifiable across restarts. See issue #96.

resource "google_compute_address" "controller" {
  name         = "${var.name_prefix}-controller-ip"
  region       = var.region
  address_type = "EXTERNAL"
  # Deliberately NO description. `description` forces replacement on a static
  # address, so adding one would have allocated a NEW public IP on the next
  # apply — rotating the address out from under the Let's Encrypt certificate and
  # the deployed dashboard's ?apiBase= URL.
}

resource "google_compute_instance" "controller" {
  name         = "${var.name_prefix}-controller"
  machine_type = var.controller_machine_type
  zone         = var.zone
  description  = "Termini control plane: REST API, operator loop, SQLite on a persistent boot disk."

  tags   = ["${var.name_prefix}-controller"]
  labels = merge(var.labels, { role = "controller" })

  deletion_protection       = false
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      # A stock Ubuntu image, not the pre-baked node image: the controller does
      # not need the node toolchain, and it has no third-party Python deps (pure
      # stdlib), so nothing has to be baked in.
      image = "${var.controller_image_project}/${var.controller_image_family}"
      size  = var.controller_boot_disk_gb
      type  = var.controller_boot_disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.underlay.id
    network_ip = var.controller_private_ip
    access_config {
      nat_ip = google_compute_address.controller.address
    }
  }
}

# Public ingress for the controller. This is what lets a browser load the
# Firebase-hosted dashboard (https://…) and call the controller — an HTTPS page
# cannot call a plain-HTTP origin, so 443 must be reachable and TLS terminated on
# the VM. 80 is needed for the ACME http-01 challenge.
#
# target_tags is the controller's own tag, NOT local.common_tags: the earlier
# `-allow-ssh` rule targets termini-node, so a controller tagged differently had
# no SSH at all until this rule existed.
resource "google_compute_firewall" "controller_public" {
  name          = "${var.name_prefix}-allow-controller-public"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = var.controller_source_ranges
  target_tags   = ["${var.name_prefix}-controller"]
  description   = "Public HTTPS/ACME to the controller, plus admin SSH."

  allow {
    protocol = "tcp"
    ports    = ["22", "80", "443"]
  }
}
