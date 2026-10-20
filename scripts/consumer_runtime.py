"""Bounded Docker/kind access and identity observations for isolated consumers."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import yaml

from .consumer_profile import LABEL, Target, load_profile
from .consumer_state import read_bounded
from .local_kind import run

# Inspect only identity and resource metadata; never container environments or secrets.
NODE_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"cluster":{{json (index .Config.Labels "io.x-k8s.kind.cluster")}},'
    '"role":{{json (index .Config.Labels "io.x-k8s.kind.role")}},'
    '"memory":{{json .HostConfig.Memory}},"cpu":{{json .HostConfig.NanoCpus}},'
    '"pids":{{json .HostConfig.PidsLimit}},"image":{{json .Config.Image}}}'
)


def command(target: Target, args: list[str], *, input_data: bytes | None = None) -> str:
    try:
        return run(args, input_data=input_data, docker_context=target.docker_context)
    except OSError:
        raise ValueError(
            f"Required {args[0]} command is unavailable; private diagnostics withheld"
        ) from None


def kubectl(target: Target, args: list[str]) -> str:
    return command(
        target,
        [
            "kubectl",
            "--kubeconfig",
            str(target.path("kubeconfig")),
            "--context",
            target.context,
            *args,
        ],
    )


def engine(target: Target) -> str:
    """Resolve only the explicit context; ambient remote Docker overrides are discarded."""
    endpoint = json.loads(
        command(
            target,
            [
                "docker",
                "context",
                "inspect",
                target.docker_context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
        )
    )
    if not isinstance(endpoint, str) or not endpoint.startswith(("unix://", "npipe://")):
        raise ValueError("Consumer profiles require a local Docker socket context")
    return endpoint


def prerequisites(target: Target, *, creating: bool = False) -> str:
    profile = load_profile(name=target.profile)
    version = command(target, ["kind", "version"]).split()
    if len(version) < 2 or version[1] != profile["kindVersion"]:
        raise ValueError("Consumer profile requires reviewed kind version")
    endpoint = engine(target)
    info = json.loads(
        command(
            target,
            [
                "docker",
                "--context",
                target.docker_context,
                "info",
                "--format",
                '{"os":{{json .OSType}},"architecture":{{json .Architecture}},"cpus":{{json .NCPU}},"memory":{{json .MemTotal}}}',
            ],
        )
    )
    if info.get("os") != "linux" or info.get("architecture") not in {"amd64", "x86_64"}:
        raise ValueError("Consumer kind nodes require a Linux amd64 Docker engine")
    if creating and (
        info.get("cpus", 0) < profile["minimumEngine"]["cpus"]
        or info.get("memory", 0) < profile["minimumEngine"]["memoryBytes"]
    ):
        raise ValueError("Docker engine does not meet reviewed consumer headroom minimums")
    return endpoint


def clusters(target: Target) -> list[str]:
    return command(target, ["kind", "get", "clusters"]).splitlines()


def node_identity(target: Target) -> dict[str, dict[str, Any]]:
    """Accept precisely the two named node containers, not a matching label alone."""
    names = command(target, ["kind", "get", "nodes", "--name", target.name]).splitlines()
    expected = {f"{target.name}-control-plane": "control-plane", f"{target.name}-worker": "worker"}
    if set(names) != set(expected) or len(names) != 2:
        raise ValueError("Consumer node topology is incomplete or foreign")
    return {name: inspect_node(target, name, name) for name in names}


def inspect_node(target: Target, reference: str, name: str) -> dict[str, Any]:
    """Validate one immutable journal ID or freshly created kind node name."""
    expected = {f"{target.name}-control-plane": "control-plane", f"{target.name}-worker": "worker"}
    if name not in expected:
        raise ValueError("Unexpected consumer node name")
    node = json.loads(
        command(
            target,
            [
                "docker",
                "--context",
                target.docker_context,
                "inspect",
                "--format",
                NODE_FORMAT,
                reference,
            ],
        )
    )
    if (
        node.get("cluster") != target.name
        or node.get("role") != expected[name]
        or node.get("name") != f"/{name}"
        or node.get("image") != load_profile(name=target.profile)["nodeImage"]
        or not re.fullmatch(r"[a-f0-9]{64}", node.get("id", ""))
    ):
        raise ValueError("Foreign node container identity")
    return node


def verify_context(target: Target) -> None:
    expected = yaml.safe_load(command(target, ["kind", "get", "kubeconfig", "--name", target.name]))
    actual = yaml.safe_load(read_bounded(target.path("kubeconfig")))
    if (
        not isinstance(expected, dict)
        or expected.get("current-context") != target.context
        or not expected.get("clusters")
        or actual != expected
    ):
        raise ValueError("Dedicated consumer kubeconfig does not match the exact kind target")


def incarnation(target: Target) -> str:
    value = kubectl(
        target, ["get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"]
    ).strip()
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value):
        raise ValueError("Cluster incarnation is unavailable")
    return value


def tools(target: Target) -> dict[str, Any]:
    """Observe exact public versions; kubectl's minor skew is checked before create."""
    client = json.loads(command(target, ["kubectl", "version", "--client", "-o", "json"]))
    version = client.get("clientVersion", {}).get("gitVersion", "")
    if not re.fullmatch(r"v1\.(?:34|35|36)\.[0-9]+", version):
        raise ValueError("kubectl must be a supported adjacent minor of Kubernetes 1.35")
    return {
        "kind": command(target, ["kind", "version"]).strip(),
        "kubectl": version,
        "docker": json.loads(
            command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "version",
                    "--format",
                    '{"client":{{json .Client.Version}},"server":{{json .Server.Version}}}',
                ],
            )
        ),
    }


def verify_nodes(nodes: dict[str, dict[str, Any]], *, profile: str = "isolated-local") -> None:
    for node in nodes.values():
        budget = load_profile(name=profile)["nodes"][node["role"]]
        if (
            node["memory"] != budget["memoryBytes"]
            or node["cpu"] != budget["cpus"] * 10**9
            or node["pids"] != 2048
        ):
            raise ValueError("Owned node resource budget differs from the reviewed profile")


def read_readiness(target: Target, *, wait: bool = False) -> list[dict[str, Any]]:
    """Allow bootstrap controllers to converge; never relax the final placement check.

    Ready conditions can precede removal of bootstrap NoSchedule taints. Only
    creation waits (at most 90 seconds); ordinary health remains an immediate
    observation. Command and parsing failures are never retried here.
    """
    deadline = time.monotonic() + 90
    while True:
        data = json.loads(kubectl(target, ["get", "nodes", "-o", "json"]))
        try:
            return check_readiness(target, data)
        except ValueError:
            remaining = deadline - time.monotonic()
            if not wait or remaining <= 0:
                raise
            print("Waiting for complete node readiness and placement...", flush=True)
            time.sleep(min(2, remaining))


def check_readiness(target: Target, data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get("items", [])
    expected = {f"{target.name}-control-plane", f"{target.name}-worker"}
    if len(items) != 2 or {item.get("metadata", {}).get("name") for item in items} != expected:
        raise ValueError("Unexpected Kubernetes node topology")
    result = []
    for item in items:
        labels = item.get("metadata", {}).get("labels", {})
        role = item["metadata"]["name"].removeprefix(f"{target.name}-")
        conditions = {
            entry["type"]: entry["status"] for entry in item.get("status", {}).get("conditions", [])
        }
        if (
            labels.get(LABEL) != target.consumer
            or labels.get("k8s-infrastructure.io/role") != role
            or labels.get("kubernetes.io/os") != "linux"
            or labels.get("kubernetes.io/arch") != "amd64"
            or item.get("spec", {}).get("unschedulable", False)
            or conditions.get("Ready") != "True"
            or any(
                conditions.get(key) != "False"
                for key in ("MemoryPressure", "DiskPressure", "PIDPressure")
            )
            or conditions.get("NetworkUnavailable", "False") != "False"
        ):
            raise ValueError("Consumer node identity/readiness/pressure is not qualified")
        taints = item.get("spec", {}).get("taints", [])
        if any(
            taint.get("effect") in {"NoSchedule", "NoExecute"}
            and not (
                role == "control-plane"
                and taint.get("key") == "node-role.kubernetes.io/control-plane"
                and taint.get("effect") == "NoSchedule"
            )
            for taint in taints
        ):
            raise ValueError("Consumer node placement has an unexpected blocking taint")
        version = item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "")
        if version != "v1.35.0":
            raise ValueError("Consumer node Kubernetes version differs from pinned image")
        capacity = item.get("status", {}).get("allocatable", {})
        if not all(
            isinstance(capacity.get(key), str) and capacity[key]
            for key in ("cpu", "memory", "pods")
        ):
            raise ValueError("Node allocatable capacity is unavailable")
        result.append(
            {
                "name": item["metadata"]["name"],
                "role": role,
                "taints": [
                    {key: entry[key] for key in ("key", "effect") if key in entry}
                    for entry in taints
                ],
                "allocatable": {key: capacity[key] for key in ("cpu", "memory", "pods")},
            }
        )
    return result
