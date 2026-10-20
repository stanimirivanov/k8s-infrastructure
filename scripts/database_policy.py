"""Explicit database namespace opt-in, request quotas and native storage admission."""

from __future__ import annotations

import hashlib
import json
import re

from .consumer_profile import LABEL, Target
from .database_assets import STORAGE_CLASS, labels
from .database_runtime import apply_objects, inventory
from .kubernetes import contains, kube, reference
from .network_runtime import require_profile


def objects(target: Target, namespaces: tuple[str, ...]) -> list[dict]:
    if not namespaces or len(namespaces) > 16 or len(set(namespaces)) != len(namespaces):
        raise ValueError("Explicit distinct database namespaces required")
    if any(
        not ns.startswith(target.consumer + "-")
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", ns)
        for ns in namespaces
    ):
        raise ValueError("Database namespace must belong to the selected consumer")
    digest = hashlib.sha256(json.dumps(sorted(namespaces)).encode()).hexdigest()[:32]
    stamp = {**labels(target), "k8s-infrastructure.io/database-delegation": digest}
    result: list[dict] = [
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "database-storage", "namespace": ns, "labels": stamp},
            "spec": {"hard": {"requests.storage": "8Gi", "persistentvolumeclaims": "4"}},
        }
        for ns in namespaces
    ]
    name = target.consumer + "-database-storage"
    # Kubernetes 1.35 publishes resources through an allOf reference that its
    # admission type checker does not resolve. Keep the rest of the PVC typed;
    # only this nested object needs dynamic field lookup. Native qualification
    # still requires positive/negative controls, including fractional sizes.
    pvc = (
        "has(object.spec.storageClassName) && object.spec.storageClassName == '"
        + STORAGE_CLASS
        + "'"
        " && object.spec.accessModes == ['ReadWriteOnce']"
        " && has(object.spec.resources) && has(dyn(object.spec.resources).requests)"
        " && 'storage' in dyn(object.spec.resources).requests"
        " && dyn(object.spec.resources).requests['storage'].matches('^[1-4]Gi$')"
    )
    cluster = (
        "has(object.spec.storage) && has(object.spec.storage.storageClass)"
        " && object.spec.storage.storageClass == '" + STORAGE_CLASS + "'"
        " && object.spec.storage.size.matches('^[1-4]Gi$')"
        " && object.spec.instances >= 1 && object.spec.instances <= 2"
        " && (!has(object.spec.backup) || !has(object.spec.backup.barmanObjectStore))"
    )
    # Separate resource schemas keep API-server CEL type checking meaningful;
    # a union policy would reference Cluster-only fields on a typed PVC.
    for suffix, group, resource, expression in (
        ("pvc", "", "persistentvolumeclaims", pvc),
        ("cluster", "postgresql.cnpg.io", "clusters", cluster),
    ):
        policy_name = name + "-" + suffix
        result.append(
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicy",
                "metadata": {"name": policy_name, "labels": stamp},
                "spec": {
                    "failurePolicy": "Fail",
                    "matchConstraints": {
                        "resourceRules": [
                            {
                                "apiGroups": [group],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE"],
                                "resources": [resource],
                                "scope": "Namespaced",
                            }
                        ]
                    },
                    "matchConditions": [
                        {
                            "name": "delegated-database-namespace",
                            "expression": "request.namespace in " + json.dumps(list(namespaces)),
                        }
                    ],
                    "validations": [
                        {
                            "expression": expression,
                            "message": "Database storage requires local-retained RWO, whole 1-4Gi claims, at most two instances, and plugin-based backups",
                        }
                    ],
                },
            }
        )
        result.append(
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicyBinding",
                "metadata": {"name": policy_name, "labels": stamp},
                "spec": {"policyName": policy_name, "validationActions": ["Deny"]},
            }
        )
    return result


def inspect_namespaces(target: Target, namespaces: tuple[str, ...]) -> None:
    require_profile(target)
    objects(target, namespaces)
    for name in namespaces:
        namespace = json.loads(kube(target, ["get", "namespace", name, "-o", "json"]))
        if (
            namespace["metadata"].get("deletionTimestamp")
            or namespace["metadata"].get("labels", {}).get(LABEL) != target.consumer
        ):
            raise ValueError("Database namespace is foreign, deleting or not explicitly delegated")


def health(target: Target, namespaces: tuple[str, ...]) -> dict:
    inspect_namespaces(target, namespaces)
    desired = objects(target, namespaces)
    found = inventory(target, desired)
    for item in desired:
        actual = found.get(reference(item))
        if actual is None or not contains(item["spec"], actual.get("spec")):
            raise ValueError("Database namespace storage enforcement missing or changed")
        if item["kind"] == "ResourceQuota" and item["spec"] != actual["spec"]:
            raise ValueError("Database storage quota scope differs")
        if item["kind"] == "ValidatingAdmissionPolicyBinding" and actual["spec"].get(
            "matchResources"
        ):
            raise ValueError("Database admission binding was narrowed")
        if item["kind"] == "ValidatingAdmissionPolicy":
            constraints = actual["spec"].get("matchConstraints", {})
            if any(
                constraints.get(key)
                for key in ("namespaceSelector", "objectSelector", "excludeResourceRules")
            ):
                raise ValueError("Database admission scope was narrowed")
            status = actual.get("status", {})
            if status.get("observedGeneration") != actual["metadata"]["generation"]:
                raise ValueError("Database storage admission type checking incomplete")
            if status.get("typeChecking", {}).get("expressionWarnings"):
                raise ValueError("Database storage admission type checking failed")
    return {
        "databaseNamespaces": namespaces,
        "maximumClaimRequest": "4Gi",
        "namespaceStorageRequests": "8Gi",
        "maximumClaims": 4,
        "physicalQuotaEnforced": False,
    }


def configure(target: Target, namespaces: tuple[str, ...]) -> dict:
    inspect_namespaces(target, namespaces)
    desired = objects(target, namespaces)
    inventory(target, desired)
    apply_objects(target, desired)
    # The API server compiles CEL asynchronously; do not equate apply with a pass.
    import time

    deadline = time.monotonic() + 60
    while True:
        try:
            return health(target, namespaces)
        except ValueError as error:
            if "type checking incomplete" not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(2)
