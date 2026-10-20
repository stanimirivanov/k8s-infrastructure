"""Explicit fresh-profile CNI installation and read-only capability observation."""

from __future__ import annotations

import json
import socket
import time
from copy import deepcopy
from typing import Any

from . import consumer_lifecycle, consumer_runtime
from .consumer_profile import Target
from .kubernetes import contains, fingerprint, kube, reference
from .network_assets import MANAGER, download, labels, render, selection


def require_profile(target: Target) -> int:
    if target.profile != "isolated-network-local" or target.https_port is None:
        raise ValueError(
            "Networking requires a fresh isolated-network-local profile; no CNI replacement"
        )
    return target.https_port


def listener_available(target: Target) -> None:
    """Preflight only; Docker must atomically reserve this same port during creation."""
    require_profile(target)
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", target.https_port))
        except OSError:
            raise ValueError(
                "Reviewed HTTPS listener is occupied; no automatic port substitution"
            ) from None


def inventory(target: Target, documents: list[dict]) -> dict[tuple, dict]:
    """Read only public provider resources; credential payloads never enter inventory."""
    if any(item["kind"] == "Secret" for item in documents):
        raise ValueError("Networking inventory does not admit Secret payloads")
    raw = kube(target, ["get", "-f", "-", "--ignore-not-found", "-o", "json"], documents)
    result = json.loads(raw) if raw.strip() else {"items": []}
    found = {
        reference(item): item
        for item in result.get("items", [result] if result.get("kind") != "List" else [])
    }
    for item in documents:
        actual = found.get(reference(item))
        if actual is None:
            continue
        if actual["metadata"].get("deletionTimestamp") or any(
            actual["metadata"].get("labels", {}).get(key) != value
            for key, value in labels(target).items()
        ):
            raise ValueError(
                "Foreign, deleting or incompatible network resource; no adoption or forced upgrade"
            )
        if item["kind"] == "CustomResourceDefinition" and fingerprint(item) != fingerprint(actual):
            raise ValueError(
                "Shared network schema differs; explicit compatibility review required"
            )
    return found


def apply_objects(target: Target, documents: list[dict]) -> None:
    if documents:
        kube(
            target,
            ["apply", "--server-side", f"--field-manager={MANAGER}", "-f", "-"],
            documents,
            request_timeout=120,
        )


def install(target: Target) -> None:
    """Only called after the exact new-profile node IDs and incarnation are journaled."""
    require_profile(target)
    consumer_lifecycle.owned(target, require_ready=False)
    daemonsets = kube(
        target,
        ["-n", "kube-system", "get", "daemonsets", "-o", "jsonpath={.items[*].metadata.name}"],
    )
    if {"kindnet", "kube-proxy", "calico-node", "kube-flannel-ds"} & set(daemonsets.split()):
        raise ValueError("Existing CNI or kube-proxy detected; no in-place network replacement")
    shared, documents = render(target, *download())
    # GatewayClass cannot be queried before its API exists. Preflight every
    # built-in object and shared schema before the first mutation.
    builtins = [item for item in documents if item["kind"] != "GatewayClass"]
    found = inventory(target, [*shared, *builtins])
    classes = [item for item in documents if item["kind"] == "GatewayClass"]
    class_api = next(
        item
        for item in shared
        if item["metadata"]["name"] == "gatewayclasses.gateway.networking.k8s.io"
    )
    if reference(class_api) in found:
        inventory(target, classes)
    print(
        "Network ownership/schema preflight passed; installing missing shared APIs...", flush=True
    )
    for item in shared:
        if reference(item) not in found:
            apply_objects(target, [item])
    for item in shared:
        kube(
            target,
            [
                "wait",
                "--for=condition=Established",
                f"crd/{item['metadata']['name']}",
                "--timeout=60s",
            ],
        )
    # Preflight the new API before creating any class, even on first install.
    inventory(target, classes)
    apply_objects(target, [item for item in documents if item["kind"] == "Namespace"])
    apply_objects(target, [item for item in documents if item["kind"] != "Namespace"])
    observe(target, shared, documents)


def verify_pod_template(expected: dict, actual: dict) -> None:
    """Defaults cannot hide injected credentials, privileges, mounts or arguments."""
    wanted, pod = expected["spec"]["template"]["spec"], actual["spec"]["template"]["spec"]
    for key in (
        "hostNetwork",
        "hostPID",
        "hostIPC",
        "serviceAccountName",
        "securityContext",
        "volumes",
        "initContainers",
        "hostAliases",
    ):
        if key not in wanted and pod.get(key) not in (None, False, {}, []):
            raise ValueError("Unexpected network controller pod authority")
    for a, b in zip(
        [*wanted["containers"], *wanted.get("initContainers", [])],
        [*pod["containers"], *pod.get("initContainers", [])],
        strict=True,
    ):
        for key in (
            "image",
            "command",
            "args",
            "env",
            "envFrom",
            "volumeMounts",
            "resources",
            "securityContext",
        ):
            if key not in a and b.get(key) not in (None, {}, []):
                raise ValueError("Unexpected network container configuration or authority")
            if key == "securityContext" and a.get(key, {}) != b.get(key, {}):
                raise ValueError("Network container security context differs")


def comparable_spec(item: dict) -> dict:
    """Normalize only observed API serialization of controller pod fields.

    Kubernetes canonicalizes whole CPU quantities to strings and omits false
    mount flags, zero probe delays and null template annotations. Preserve the
    original manifests and all other fields so actual changes still fail.
    """
    spec = deepcopy(item["spec"])
    if item["kind"] in {"Deployment", "DaemonSet"}:
        metadata = spec["template"].setdefault("metadata", {})
        if metadata.get("annotations") in (None, {}):
            metadata["annotations"] = None
        pod = spec["template"]["spec"]
        for container in [*pod["containers"], *pod.get("initContainers", [])]:
            for budget in container.get("resources", {}).values():
                cpu = budget.get("cpu")
                if type(cpu) is int:
                    budget["cpu"] = str(cpu)
                elif (
                    isinstance(cpu, str)
                    and cpu.endswith("m")
                    and cpu[:-1].isdigit()
                    and int(cpu[:-1]) % 1000 == 0
                ):
                    budget["cpu"] = str(int(cpu[:-1]) // 1000)
            for mount in container.get("volumeMounts", []):
                mount.setdefault("readOnly", False)
            for name in ("livenessProbe", "readinessProbe", "startupProbe"):
                if name in container:
                    container[name].setdefault("initialDelaySeconds", 0)
    return spec


def observe(target: Target, shared: list[dict], documents: list[dict]) -> dict[str, Any]:
    found = inventory(target, [*shared, *documents])
    for item in [*shared, *documents]:
        actual = found.get(reference(item))
        if actual is None:
            raise ValueError("Network resource missing; no readiness pass")
        for key in ("spec", "rules", "roleRef", "subjects", "data"):
            expected_value = (
                comparable_spec(item) if key == "spec" and key in item else item.get(key)
            )
            actual_value = (
                comparable_spec(actual) if key == "spec" and key in actual else actual.get(key)
            )
            if key in item and not contains(expected_value, actual_value):
                raise ValueError(f"Network {item['kind']} {item['metadata']['name']} {key} drift")
        if item["kind"] == "ConfigMap" and item.get("data", {}) != actual.get("data", {}):
            raise ValueError("Unexpected Cilium configuration fields")
        if item["kind"] == "ClusterRole" and actual.get("aggregationRule"):
            raise ValueError("Unexpected aggregate network authority")
        if item["kind"] == "ValidatingAdmissionPolicy":
            deadline = time.monotonic() + 60
            while True:
                status = actual.get("status", {})
                if status.get("typeChecking", {}).get("expressionWarnings"):
                    raise ValueError("Gateway admission type checking failed; no readiness pass")
                if status.get("observedGeneration") == actual["metadata"].get("generation"):
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("Gateway admission type checking is incomplete")
                time.sleep(2)
                actual = json.loads(
                    kube(
                        target,
                        [
                            "get",
                            "validatingadmissionpolicy",
                            item["metadata"]["name"],
                            "-o",
                            "json",
                        ],
                    )
                )
        if item["kind"] in {"Deployment", "DaemonSet"}:
            verify_pod_template(item, actual)
            kube(
                target,
                [
                    "-n",
                    item["metadata"].get("namespace", "kube-system"),
                    "rollout",
                    "status",
                    f"{item['kind'].lower()}/{item['metadata']['name']}",
                    "--timeout=300s",
                ],
            )
    kube(target, ["wait", "--for=condition=Accepted", "gatewayclass/cilium", "--timeout=90s"])
    return {
        "profile": target.profile,
        "cluster": target.name,
        "context": target.context,
        "incarnation": consumer_runtime.incarnation(target),
        **consumer_lifecycle.source_identity(target),
        "ciliumVersion": selection()["cilium"]["version"],
        "gatewayAPIVersion": selection()["gateway"]["version"],
        "gatewayClass": "cilium",
        "listener": {"address": "127.0.0.1", "hostPort": target.https_port, "gatewayPort": 8443},
        "controllerReadiness": "observed",
        "trafficAndTLS": "requires separate qualify action",
        "trust": "consumer-owned certificate Secret; explicit public CA bundle; no automatic host trust",
        "resourceEnvelope": {"controllerPods": 5, "requestsCPU": "350m", "requestsMemory": "768Mi"},
        "notQualified": [
            "consumer product",
            "browser/OS trust installation",
            "production DNS/ACME",
            "persistent storage",
            "shared-host performance isolation",
        ],
    }


def health(target: Target) -> dict[str, Any]:
    require_profile(target)
    consumer_lifecycle.health(target)
    return observe(target, *render(target, *download()))


def resume(target: Target) -> dict:
    """Explicit recovery of this exact partially provisioned CNI profile, never ensure repair."""
    require_profile(target)
    record = consumer_lifecycle.owned(target, require_ready=False)
    consumer_runtime.verify_nodes(consumer_runtime.node_identity(target), profile=target.profile)
    install(target)
    return consumer_lifecycle.finish_creation(target, record)
