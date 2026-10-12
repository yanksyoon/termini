# termini-node.pkr.hcl — Packer recipe for the Termini pre-baked node image.
#
# Produces a qcow2 (and, on GCP, a machine image via a follow-on export) whose
# first boot installs nothing: LXD, llama.cpp, CUDA runtime (gpu variant) and the
# pinned GGUF weights are all already on disk. Validated by `build-image.sh --check`.
#
# Required vars are supplied by build-image.sh from the frozen pins:
#   variant, version, base_url, base_sha256, llamacpp_commit, out_dir
packer {
  required_version = ">= 1.9.0"
  required_plugins {
    qemu = {
      version = ">= 1.0.6"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "variant" { type = string }
variable "version" { type = string }
variable "base_url" { type = string }
variable "base_sha256" { type = string }
variable "llamacpp_commit" { type = string }
variable "out_dir" { type = string }
variable "cpus" { type = number, default = 8 }
variable "memory" { type = number, default = 16384 }
variable "disk_size" { type = string, default = "80G" }
variable "ssh_username" { type = string, default = "ubuntu" }
variable "ssh_private_key_file" { type = string, default = "~/.ssh/packer_id_ed25519" }
variable "ssh_public_key" { type = string, default = "" }
# Directory holding the workload-image stage (the tarball + images.json manifest
# written by tools/stage_workload_image.sh). Empty = upload no stage (only valid
# with TERMINI_ALLOW_MISSING_WORKLOAD_IMAGE=1 for a deliberate non-demo bake).
variable "stage_dir" { type = string, default = "" }

locals {
  provisioners = [
    "00-base.sh",
    "10-node-os.sh",
    "15-nvidia-driver.sh",
    "20-gpu-cuda.sh",
    "30-llamacpp.sh",
    "40-models.sh",
    "50-agent-deps.sh",
    "60-node-bootstrap.sh",
  ]
  image_stamp = "termini-node-${var.version}-${var.variant}"
}

source "qemu" "termini_node" {
  iso_url          = var.base_url
  iso_checksum     = "sha256:${var.base_sha256}"
  disk_image       = true
  output_directory = "${var.out_dir}/packer-${local.image_stamp}"
  format           = "qcow2"
  accelerator      = "kvm"
  cpus             = var.cpus
  memory           = var.memory
  disk_size        = var.disk_size
  ssh_username     = var.ssh_username
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout      = "10m"
  shutdown_command = "sudo shutdown -P now"
  # NoCloud seed: injects the ephemeral build key so Packer can SSH in.
  cd_content = var.ssh_public_key == "" ? {} : {
    "user-data" = "#cloud-config\nssh_authorized_keys:\n  - ${var.ssh_public_key}\n"
    "meta-data" = "instance-id: termini-bake\n"
  }
  cd_label = "cidata"
}

build {
  sources = ["source.qemu.termini_node"]

  # Recipe + offline preflight land in the image.
  provisioner "file" {
    source      = "${path.root}/../first-boot-preflight.sh"
    destination = "/tmp/termini-first-boot-preflight"
  }
  provisioner "file" {
    source      = "${path.root}/../models.lock.json"
    destination = "/tmp/models.lock.json"
  }
  provisioner "file" {
    source      = "${path.root}/../systemd/termini-firstboot-preflight.service"
    destination = "/tmp/termini-firstboot-preflight.service"
  }

  provisioner "file" {
    source      = "${path.root}/../../../infra/bootstrap/node/termini-bootstrap.py"
    destination = "/tmp/termini-bootstrap.py"
  }
  provisioner "file" {
    source      = "${path.root}/../../../infra/bootstrap/node/termini-bootstrap.service"
    destination = "/tmp/termini-bootstrap.service"
  }

  # The workload-image stage (D3). The workload image the node must ship — the
  # guest the controller launches as `agent-vm/<version>` (and the demo launches
  # per-node as `agent-os/w4`) — plus the images.json manifest that names its
  # aliases. build-image.sh populates this on the build host
  # (tools/stage_workload_image.sh); provision/10-node-os.sh imports it into the
  # image's local LXD store so first boot never contacts a registry.
  provisioner "file" {
    source      = "${path.root}/../provision/import-staged-images.sh"
    destination = "/tmp/termini-import-staged-images.sh"
  }
  provisioner "shell" {
    inline = ["mkdir -p /tmp/termini-images-staged"]
  }
  dynamic "provisioner" {
    for_each = var.stage_dir == "" ? [] : [var.stage_dir]
    labels   = ["file"]
    content {
      source      = provisioner.value
      destination = "/tmp/termini-images-staged"
    }
  }

  # The numbered provisioners, in order. Overlay-mounted so they run as root.
  dynamic "provisioner" {
    for_each = local.provisioners
    labels   = ["shell"]
    content {
      script = "${path.root}/../provision/${provisioner.value}"
      environment_vars = [
        "TERMINI_VARIANT=${var.variant}",
        "LLAMACPP_COMMIT=${var.llamacpp_commit}",
        "DEBIAN_FRONTEND=noninteractive",
        "IMAGE_STAGE=/tmp/termini-images-staged",
        "TERMINI_IMPORT_HELPER=/tmp/termini-import-staged-images.sh",
      ]
      execute_command = "echo 'packer' | {{ .Vars }} sudo -E -S bash '{{ .Path }}'"
    }
  }

  # Stamp identity + the offline sentinel the first-boot preflight asserts.
  provisioner "shell" {
    environment_vars = ["TERMINI_VARIANT=${var.variant}", "TERMINI_VERSION=${var.version}"]
    inline = [
      "sudo install -m 0755 /tmp/termini-first-boot-preflight /usr/local/sbin/termini-first-boot-preflight",
      "sudo install -d -m 0755 /opt/termini /etc/termini /var/log/termini",
      "sudo install -m 0644 /tmp/models.lock.json /opt/termini/models.lock.json",
      "sudo install -m 0644 /tmp/termini-firstboot-preflight.service /etc/systemd/system/termini-firstboot-preflight.service",
      "echo \"${local.image_stamp}\" | sudo tee /etc/termini/image-version >/dev/null",
      "echo \"baked $(date -u +%Y-%m-%dT%H:%M:%SZ) variant=${var.variant} version=${var.version} llama.cpp=${var.llamacpp_commit}\" | sudo tee /var/lib/termini/.baked-offline >/dev/null",
      # 60-node-bootstrap.sh installs + enables termini-bootstrap.service from the
      # canonical infra/bootstrap/node sources; here we only finish the first-boot
      # preflight and re-assert both enables (idempotent).
      "sudo systemctl enable termini-firstboot-preflight.service termini-bootstrap.service || true",
    ]
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${var.out_dir}/${local.image_stamp}.qcow2.sha256"
  }
}
