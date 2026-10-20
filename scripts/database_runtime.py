"""Explicit database capability installation; no consumer database or credential setup."""

from __future__ import annotations

import json
from typing import Any

from . import consumer_lifecycle, consumer_runtime
from .consumer_profile import Target
from .database_assets import (
    MANAGER,
    download,
    labels,
    render,
    selection,
    storage_class,
)
from .kubernetes import contains, fingerprint, kube, reference
from .network_runtime import (
    comparable_spec,
    require_profile,
    verify_pod_template,
)
from .network_runtime import (
    health as network_health,
)

BUILTINS = {
    "Namespace",
    "CustomResourceDefinition",
    "ServiceAccount",
    "Role",
    "RoleBinding",
    "ClusterRole",
    "ClusterRoleBinding",
    "ConfigMap",
    "Service",
    "Deployment",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "StorageClass",
    "ResourceQuota",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
}


def inventory(target: Target, documents: list[dict]) -> dict[tuple, dict]:
    """Never request Secret payloads; refuse schema/owner/version drift before writes."""
    if not documents:
        return {}
    if any(item["kind"] == "Secret" for item in documents):
        raise ValueError("Database inventory never admits Secrets")
    raw = kube(target, ["get", "-f", "-", "--ignore-not-found", "-o", "json"], documents)
    data = json.loads(raw) if raw.strip() else {"items": []}
    found = {reference(item): item for item in data.get("items", [data])}
    for item in documents:
        actual = found.get(reference(item))
        if actual is None:
            continue
        if actual["metadata"].get("deletionTimestamp") or any(
            actual["metadata"].get("labels", {}).get(key) != value
            for key, value in item["metadata"].get("labels", labels(target)).items()
        ):
            raise ValueError("Foreign, deleting or incompatible database capability resource")
        if item["kind"] == "CustomResourceDefinition" and fingerprint(item) != fingerprint(actual):
            raise ValueError("Database API schema differs; explicit compatibility review required")
    return found


def apply_objects(target: Target, documents: list[dict]) -> None:
    if documents:
        kube(
            target,
            ["apply", "--server-side", "--field-manager=" + MANAGER, "-f", "-"],
            documents,
            request_timeout=120,
        )


def verify_driver(target: Target, documents: list[dict]) -> dict:
    """Qualify kind's pre-existing provisioner without adopting or modifying it."""
    for kind, name in (
        ("StorageClass", "standard"),
        ("Deployment", "local-path-provisioner"),
        ("ConfigMap", "local-path-config"),
    ):
        expected = next(d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name)
        args = ["get", kind, name, "-o", "json"]
        if kind != "StorageClass":
            args = ["-n", "local-path-storage", *args]
        actual = json.loads(kube(target, args))
        if kind == "StorageClass":
            if any(
                actual.get(key) != expected.get(key)
                for key in ("provisioner", "reclaimPolicy", "volumeBindingMode")
            ):
                raise ValueError("Kind default storage capability differs")
        elif kind == "ConfigMap":
            if actual.get("data") != expected.get("data"):
                raise ValueError("Kind storage setup, teardown or path differs; no implicit repair")
        elif not contains(comparable_spec(expected), comparable_spec(actual)):
            raise ValueError("Kind storage provisioner configuration differs")
    kube(
        target,
        [
            "-n",
            "local-path-storage",
            "rollout",
            "status",
            "deployment/local-path-provisioner",
            "--timeout=120s",
        ],
    )
    # This observes shared backing-filesystem availability, not a PVC reservation
    # or an enforced directory quota. No host path or data directory is exported.
    free = consumer_runtime.command(
        target,
        [
            "docker",
            "--context",
            target.docker_context,
            "exec",
            target.name + "-worker",
            "df",
            "-B1",
            "--output=avail",
            "/var",
        ],
    ).splitlines()
    available = int(free[-1].strip())
    if available < 4 * 1024**3:
        raise ValueError("Local backing filesystem has less than 4 GiB available")
    return {
        "backingFilesystemAvailableBytes": available,
        "capacityReserved": False,
        "requestedCapacityHardEnforced": False,
        "failureDomain": "one kind node on one shared Docker host",
    }


def observe(target: Target, documents: list[dict], capacity: dict) -> dict[str, Any]:
    found = inventory(target, documents)
    for item in documents:
        actual = found.get(reference(item))
        if actual is None:
            raise ValueError("Database capability resource missing")
        if item["kind"] == "ClusterRole" and actual.get("aggregationRule"):
            raise ValueError("Unexpected aggregate database controller authority")
        if item["kind"] == "ConfigMap" and item.get("data", {}) != actual.get("data", {}):
            raise ValueError("Unexpected database controller configuration fields")
        if item["kind"] == "StorageClass" and (
            any(actual.get(key) for key in ("parameters", "mountOptions", "allowedTopologies"))
            or any(
                actual["metadata"].get("annotations", {}).get(key) == "true"
                for key in (
                    "storageclass.kubernetes.io/is-default-class",
                    "storageclass.beta.kubernetes.io/is-default-class",
                )
            )
        ):
            raise ValueError("Retained storage class exposure or provisioning options differ")
        for key in (
            "spec",
            "rules",
            "roleRef",
            "subjects",
            "data",
            "webhooks",
            "provisioner",
            "reclaimPolicy",
            "volumeBindingMode",
            "allowVolumeExpansion",
        ):
            if key not in item:
                continue
            a = comparable_spec(item) if key == "spec" else item[key]
            b = comparable_spec(actual) if key == "spec" else actual.get(key)
            if not contains(a, b):
                raise ValueError(f"Database {item['kind']} {item['metadata']['name']} {key} drift")
        if item["kind"] == "Deployment":
            verify_pod_template(item, actual)
            kube(
                target,
                [
                    "-n",
                    item["metadata"]["namespace"],
                    "rollout",
                    "status",
                    "deployment/" + item["metadata"]["name"],
                    "--timeout=300s",
                ],
            )
        if item["kind"] == "Certificate":
            kube(
                target,
                [
                    "-n",
                    item["metadata"]["namespace"],
                    "wait",
                    "--for=condition=Ready",
                    "certificate/" + item["metadata"]["name"],
                    "--timeout=120s",
                ],
            )
    return {
        "cluster": target.name,
        "context": target.context,
        "incarnation": consumer_runtime.incarnation(target),
        **consumer_lifecycle.source_identity(target),
        "versions": {k: v["version"] for k, v in selection()["assets"].items()},
        "controllerReadiness": "observed",
        "storageClass": "local-retained",
        "accessModes": ["ReadWriteOnce"],
        "reclaimPolicy": "Retain",
        "bindingMode": "WaitForFirstConsumer",
        "volumeExpansion": False,
        **capacity,
        "dataRetention": "requires separate native qualification",
        "backupIntegration": {
            "plugin": "barman-cloud.cloudnative-pg.io",
            "endpoint": "consumer-supplied verified HTTPS object store",
            "identity": "consumer-supplied namespace Secret references; cloud workload identity unqualified",
            "backupRestore": "consumer drill required; no bucket or backup created",
        },
        "providerRecovery": "Git/release inventory plus private kubeconfig/owner journal outside the application database; kind/host loss requires rebuild and external consumer backup",
        "notQualified": [
            "off-cluster recovery",
            "node or host loss",
            "HA",
            "production CSI",
            "backup credentials or workload identity",
            "consumer product",
        ],
    }


def materialize(target: Target) -> tuple[dict[str, list[dict]], list[dict], dict]:
    require_profile(target)
    assets = download()
    groups = render(target, assets)
    documents = [item for docs in groups.values() for item in docs] + [storage_class(target)]
    return groups, documents, verify_driver(target, assets["kind-storage"])


def install(target: Target) -> dict:
    require_profile(target)
    network_health(target)
    groups, documents, capacity = materialize(target)
    # Custom certificate APIs cannot be queried before cert-manager CRDs exist.
    builtins = [item for item in documents if item["kind"] in BUILTINS]
    found = inventory(target, builtins)
    if any(item["kind"] not in BUILTINS for item in documents) and all(
        reference(item) in found for item in documents if item["kind"] == "CustomResourceDefinition"
    ):
        inventory(target, documents)
    for group in ("cert-manager", "cnpg", "barman"):
        selected = groups[group]
        schemas = [d for d in selected if d["kind"] == "CustomResourceDefinition"]
        for item in schemas:
            if reference(item) not in found:
                apply_objects(target, [item])
            kube(
                target,
                [
                    "wait",
                    "--for=condition=Established",
                    "crd/" + item["metadata"]["name"],
                    "--timeout=90s",
                ],
            )
        custom = [d for d in selected if d["kind"] not in BUILTINS]
        inventory(target, custom)
        apply_objects(target, [d for d in selected if d["kind"] == "Namespace"])
        apply_objects(
            target,
            [d for d in selected if d["kind"] not in {"Namespace", "CustomResourceDefinition"}],
        )
        for item in selected:
            if item["kind"] == "Deployment":
                kube(
                    target,
                    [
                        "-n",
                        item["metadata"]["namespace"],
                        "rollout",
                        "status",
                        "deployment/" + item["metadata"]["name"],
                        "--timeout=300s",
                    ],
                )
    apply_objects(target, [storage_class(target)])
    return observe(target, documents, capacity)


def health(target: Target) -> dict:
    require_profile(target)
    consumer_lifecycle.health(target)
    _, documents, capacity = materialize(target)
    return observe(target, documents, capacity)
