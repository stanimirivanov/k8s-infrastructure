variables {
  cluster_name        = "fixture-sandbox"
  plan_binding_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

run "baseline_profile" {
  command = plan

  plan_options {
    refresh = false
  }

  assert {
    condition     = kind_cluster.baseline.name == "fixture-sandbox"
    error_message = "The baseline plan must use the declared cluster name."
  }

  assert {
    condition     = kind_cluster.baseline.node_image == "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
    error_message = "The baseline plan must use the reviewed kind node image digest."
  }

  assert {
    condition     = try(kind_cluster.baseline.networking.api_server_address, null) == "127.0.0.1"
    error_message = "The baseline plan must bind the Kubernetes API to loopback."
  }

  assert {
    condition     = sort([for node in kind_cluster.baseline.node : node.role]) == tolist(["control-plane", "worker"])
    error_message = "The baseline plan must include exactly one control plane and one worker."
  }
}

run "rejects_invalid_cluster_name" {
  command = plan

  variables {
    cluster_name = "Invalid.cluster"
  }

  expect_failures = [var.cluster_name]
}

run "rejects_invalid_plan_binding" {
  command = plan

  variables {
    plan_binding_sha256 = "bad-digest"
  }

  expect_failures = [var.plan_binding_sha256]
}
