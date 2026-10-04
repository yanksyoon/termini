output "inventory_path" {
  description = "Absolute path of the generated inventory JSON (the downstream contract)."
  value       = local_file.inventory.filename
}

output "network" {
  description = "The shared underlay network created for the three nodes."
  value = {
    vpc        = google_compute_network.vpc.name
    subnetwork = google_compute_subnetwork.underlay.name
    cidr       = var.network_cidr
    gateway    = local.gateway_ip
  }
}

output "nodes" {
  description = "Per-node public/private address, role and machine type."
  value = {
    for name in local.node_names : name => {
      hostname     = google_compute_instance.node[name].name
      role         = local.node_spec[name].role
      machine_type = google_compute_instance.node[name].machine_type
      private_ip   = google_compute_instance.node[name].network_interface[0].network_ip
      public_ip    = google_compute_instance.node[name].network_interface[0].access_config[0].nat_ip
      accelerator  = local.node_spec[name].gpu ? var.gpu_accelerator_type : null
      ssh          = "ssh ${var.ssh_user}@${google_compute_instance.node[name].network_interface[0].access_config[0].nat_ip}"
    }
  }
}

output "controller_hint" {
  description = "Seed node and inference endpoint the controller/agents use (D2 cross-node reachability)."
  value = {
    lxd_seed_address   = "${local.seed_ip}:8443"
    inference_base_url = "http://${local.node_ip["node-c"]}:8080"
    underlay_cidr      = var.network_cidr
  }
}
