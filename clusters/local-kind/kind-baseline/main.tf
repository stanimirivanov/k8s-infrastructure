terraform {
  required_version = "= 1.13.10"

  backend "local" {}

  required_providers {
    kind = {
      source  = "registry.terraform.io/elioseverojunior/kind"
      version = "= 0.0.4"
    }
  }
}

variable "cluster_name" {
  type = string

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$", var.cluster_name))
    error_message = "Use a lowercase cluster name of at most 49 characters, beginning with a letter and ending with a letter or digit."
  }
}

variable "plan_binding_sha256" {
  type        = string
  description = "Digest of the reviewed target and source binding saved with the plan."

  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.plan_binding_sha256))
    error_message = "Use the SHA-256 of the plan binding record."
  }
}

resource "kind_cluster" "baseline" {
  name                 = var.cluster_name
  node_image           = "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
  wait_for_ready       = 180
  wait_for_nodes_ready = true

  networking {
    api_server_address = "127.0.0.1"
  }

  node {
    role = "control-plane"
  }

  node {
    role = "worker"
  }
}
