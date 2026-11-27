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
  type    = string
  default = "perfeng-iac-pilot-74"

  validation {
    condition     = contains(["perfeng-iac-pilot-74", "perfeng-retained-434"], var.cluster_name)
    error_message = "Select the registered #74 pilot or #434 retained cluster name."
  }
}

resource "kind_cluster" "pilot" {
  name                 = var.cluster_name
  node_image           = "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
  wait_for_ready       = 180
  wait_for_nodes_ready = true

  networking {
    api_server_address = "127.0.0.1"
  }

  node {
    role = "control-plane"
    labels = {
      "workload" = "control-plane"
    }
  }

  node {
    role = "worker"
    labels = {
      "workload"             = "performance-generator"
      "perfeng.io/node-type" = "generator"
    }
  }

  node {
    role = "worker"
    labels = {
      "workload"             = "sut"
      "perfeng.io/node-type" = "sut"
    }
  }
}
