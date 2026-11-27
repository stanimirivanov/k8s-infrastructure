run "pilot_profile" {
  command = plan

  plan_options {
    refresh = false
  }

  assert {
    condition     = kind_cluster.pilot.name == "perfeng-iac-pilot-74"
    error_message = "The pilot plan must select perfeng-iac-pilot-74."
  }

  assert {
    condition     = kind_cluster.pilot.node_image == "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
    error_message = "The pilot plan must use the reviewed kind node image digest."
  }

  assert {
    condition     = try(kind_cluster.pilot.networking.api_server_address, null) == "127.0.0.1"
    error_message = "The pilot plan must bind the Kubernetes API to 127.0.0.1."
  }

  assert {
    condition = length(kind_cluster.pilot.node) == 3 && toset([
      for node in kind_cluster.pilot.node : { role = node.role, labels = node.labels }
      ]) == toset([
      { role = "control-plane", labels = tomap({ workload = "control-plane" }) },
      { role = "worker", labels = tomap({ workload = "performance-generator", "perfeng.io/node-type" = "generator" }) },
      { role = "worker", labels = tomap({ workload = "sut", "perfeng.io/node-type" = "sut" }) },
    ])
    error_message = "Expected one control plane and workers labeled generator and sut."
  }
}

run "retained_profile" {
  command = plan

  plan_options {
    refresh = false
  }

  variables {
    cluster_name = "perfeng-retained-434"
  }

  assert {
    condition     = kind_cluster.pilot.name == "perfeng-retained-434"
    error_message = "The retained plan must select perfeng-retained-434."
  }

  assert {
    condition     = kind_cluster.pilot.node_image == "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
    error_message = "The retained plan must use the reviewed kind node image digest."
  }

  assert {
    condition     = try(kind_cluster.pilot.networking.api_server_address, null) == "127.0.0.1"
    error_message = "The retained plan must bind the Kubernetes API to 127.0.0.1."
  }

  assert {
    condition = length(kind_cluster.pilot.node) == 3 && toset([
      for node in kind_cluster.pilot.node : { role = node.role, labels = node.labels }
      ]) == toset([
      { role = "control-plane", labels = tomap({ workload = "control-plane" }) },
      { role = "worker", labels = tomap({ workload = "performance-generator", "perfeng.io/node-type" = "generator" }) },
      { role = "worker", labels = tomap({ workload = "sut", "perfeng.io/node-type" = "sut" }) },
    ])
    error_message = "Expected one control plane and workers labeled generator and sut."
  }
}

run "rejects_unregistered_profile" {
  command = plan

  plan_options {
    refresh = false
  }

  variables {
    cluster_name = "perfeng-unregistered"
  }

  expect_failures = [var.cluster_name]
}
