"""Prepare the network candidate's foundation within the approved node reservations."""

from __future__ import annotations

import json
from collections.abc import Callable

import yaml

PATCH_DIRECTORY = "/kind/minimal-demo-patches"
STORAGE_MANIFEST = "/kind/manifests/default-storage.yaml"
WORKER_SELECTOR = {"k8s-infrastructure.io/role": "worker"}

# The four kubeadm requests are retained. Memory is explicitly reserved up to
# each cap. The worker values preserve two DNS replicas and one provisioner.
CONTROL_PLANE = {
    "kube-apiserver": (250, 550, 640, 640),
    "kube-controller-manager": (200, 300, 384, 384),
    "kube-scheduler": (100, 200, 384, 384),
    "etcd": (100, 450, 640, 640),
}
COREDNS = (25, 150, 64, 96)
PROVISIONER = (50, 200, 64, 64)


def resources(bounds: tuple[int, int, int, int]) -> dict:
    cpu_request, cpu_limit, memory_request, memory_limit = bounds
    return {
        "requests": {"cpu": f"{cpu_request}m", "memory": f"{memory_request}Mi"},
        "limits": {"cpu": f"{cpu_limit}m", "memory": f"{memory_limit}Mi"},
    }


def kubeadm_configuration() -> str:
    # kind v0.31.0 still emits v1beta3. Its merge matches the exact API version.
    return yaml.safe_dump(
        {
            "apiVersion": "kubeadm.k8s.io/v1beta3",
            "kind": "InitConfiguration",
            "patches": {"directory": PATCH_DIRECTORY},
        }
    )


def _kubeadm_patches() -> dict[str, dict]:
    return {
        **{
            name: {"spec": {"containers": [{"name": name, "resources": resources(bounds)}]}}
            for name, bounds in CONTROL_PLANE.items()
        },
        "corednsdeployment": {
            "spec": {
                "strategy": {"type": "Recreate", "rollingUpdate": None},
                "template": {
                    "spec": {
                        "nodeSelector": WORKER_SELECTOR,
                        "containers": [{"name": "coredns", "resources": resources(COREDNS)}],
                    },
                },
            },
        },
    }


def _bounded_storage(manifest: str) -> str:
    documents = list(yaml.safe_load_all(manifest))
    deployments = [item for item in documents if item["kind"] == "Deployment"]
    if len(deployments) != 1:
        raise ValueError("Pinned kind storage deployment inventory changed")
    deployment = deployments[0]
    expected_name = "local-path-provisioner"
    if (
        deployment["metadata"]["namespace"] != "local-path-storage"
        or deployment["metadata"]["name"] != expected_name
        or deployment["spec"]["replicas"] != 1
    ):
        raise ValueError("Pinned kind storage deployment identity changed")
    pod = deployment["spec"]["template"]["spec"]
    if [item["name"] for item in pod["containers"]] != [expected_name] or pod.get("initContainers"):
        raise ValueError("Pinned kind storage container inventory changed")
    pod["nodeSelector"] = {**pod.get("nodeSelector", {}), **WORKER_SELECTOR}
    pod["containers"][0]["resources"] = resources(PROVISIONER)
    deployment["spec"]["strategy"] = {"type": "Recreate"}
    return yaml.safe_dump_all(documents)


def prepare_control_plane(container_id: str, invoke: Callable[[list[str]], str]) -> None:
    """Write only reviewed bootstrap inputs in the fresh, capped control plane."""
    command = ["exec", container_id]
    storage = _bounded_storage(invoke([*command, "cat", STORAGE_MANIFEST]))
    invoke([*command, "mkdir", "-m", "0700", PATCH_DIRECTORY])
    atomic_write = (
        'umask 077; tmp=$(mktemp "$1.XXXXXX"); printf "%s\\n" "$2" > "$tmp"; mv -T -- "$tmp" "$1"'
    )
    updates = {
        **{
            f"{PATCH_DIRECTORY}/{name}+strategic.json": json.dumps(patch)
            for name, patch in _kubeadm_patches().items()
        },
        STORAGE_MANIFEST: storage,
    }
    for path, content in updates.items():
        invoke([*command, "sh", "-ceu", atomic_write, "minimal-foundation", path, content])
