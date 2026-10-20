"""Target-verified installation and observation of delegated Argo capabilities."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import consumer_lifecycle, consumer_runtime
from .consumer_profile import Target
from .delegated_argo_config import LABEL, Delegation, download_assets, selection
from .delegated_argo_render import reference, render

FIELD_MANAGER = "k8s-infrastructure-delegated-argo"


def kube(
    target: Target,
    args: list[str],
    documents: list[dict] | None = None,
    *,
    request_timeout: int = 30,
) -> str:
    command = [
        "kubectl",
        "--kubeconfig",
        str(target.path("kubeconfig")),
        "--context",
        target.context,
        f"--request-timeout={request_timeout}s",
        *args,
    ]
    return consumer_runtime.command(
        target,
        command,
        input_data=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}).encode()
        if documents is not None
        else None,
    )


def fingerprint(item: dict) -> str:
    """Bind the desired shared API schema; account for Kubernetes CRD defaults."""
    value = (
        dict(item.get("spec", {}))
        if item["kind"] == "CustomResourceDefinition"
        else {
            key: item.get(key)
            for key in ("value", "globalDefault", "description", "preemptionPolicy")
        }
    )
    if item["kind"] == "CustomResourceDefinition":
        value.setdefault("preserveUnknownFields", False)
        value.setdefault("conversion", {"strategy": "None"})
    else:
        value["globalDefault"] = value["globalDefault"] or False
        value["preemptionPolicy"] = value["preemptionPolicy"] or "PreemptLowerPriority"
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_resources(target: Target, documents: list[dict]) -> dict[tuple, dict]:
    """Batch public objects. For Secrets request metadata only, never credential bytes."""
    public = [item for item in documents if item["kind"] != "Secret"]
    found: dict[tuple, dict] = {}
    if public:
        raw = kube(target, ["get", "-f", "-", "--ignore-not-found", "-o", "json"], public)
        # kubectl succeeds with no output when every requested object is absent.
        # Only this successful empty response means an empty inventory; malformed
        # nonempty JSON and command failures must still stop the preflight.
        output = json.loads(raw) if raw.strip() else {"items": []}
        for item in output.get("items", [output] if output.get("kind") != "List" else []):
            found[reference(item)] = item
    for item in documents:
        if item["kind"] != "Secret":
            continue
        _, name, namespace = reference(item)
        if namespace is None:
            raise ValueError("Controller Secrets must be namespaced")
        output = kube(
            target,
            [
                "-n",
                namespace,
                "get",
                "secret",
                name,
                "--ignore-not-found",
                "-o",
                "jsonpath={.metadata}",
            ],
        )
        if output.strip():
            found[reference(item)] = {"kind": "Secret", "metadata": json.loads(output)}
    return found


def verify_ownership(expected: dict, actual: dict) -> None:
    wanted = expected["metadata"]["labels"]
    if actual["metadata"].get("deletionTimestamp"):
        raise ValueError("Argo resource is being removed; no implicit recreation")
    if any(actual["metadata"].get("labels", {}).get(key) != value for key, value in wanted.items()):
        raise ValueError("Foreign or incompatible Argo resource; no adoption or forced conflict")


def namespaces_delegated(target: Target, delegation: Delegation) -> None:
    """Consumer bootstrap owns namespaces; provider only binds explicitly granted roles."""
    for namespace in (*delegation.workload_namespaces, delegation.managed):
        value = json.loads(kube(target, ["get", "namespace", namespace, "-o", "json"]))
        metadata = value.get("metadata", {})
        if metadata.get("name") != namespace or any(
            metadata.get("labels", {}).get(key) != expected
            for key, expected in {
                LABEL: delegation.consumer,
                "app.kubernetes.io/managed-by": delegation.consumer,
            }.items()
        ):
            raise ValueError(
                "Missing consumer-owned namespace delegation; provider will not create/adopt it"
            )


def inventory(
    target: Target, shared: list[dict], documents: list[dict], *, complete: bool
) -> dict[tuple, dict]:
    found = read_resources(target, [*shared, *documents])
    for expected in [*shared, *documents]:
        actual = found.get(reference(expected))
        if actual is None:
            if complete:
                raise ValueError("Delegated Argo resource is missing; readiness is incomplete")
            continue
        verify_ownership(expected, actual)
        if expected in shared and fingerprint(expected) != fingerprint(actual):
            raise ValueError(
                "Shared Argo API schema/priority differs; explicit compatibility review required"
            )
    return found


def apply(target: Target, delegation: Delegation) -> dict[str, Any]:
    if target.profile != "isolated-argo-local":
        raise ValueError(
            "Delegated Argo requires the explicit isolated-argo-local profile; no implicit resizing or adoption"
        )
    print("Verifying consumer target and namespace delegation...", flush=True)
    consumer_lifecycle.health(target)
    namespaces_delegated(target, delegation)
    shared, documents = render(delegation, download_assets())
    found = inventory(target, shared, documents, complete=False)
    missing = [item for item in shared if reference(item) not in found]
    print(
        f"Shared Argo API preflight: {len(shared) - len(missing)} compatible, {len(missing)} absent.",
        flush=True,
    )
    if missing:
        # Establish large CRDs sequentially: parallel schema compilation can
        # saturate a bounded local API server during first install.
        for item in missing:
            write(target, [item])
    for item in shared:
        if item["kind"] == "CustomResourceDefinition":
            kube(
                target,
                [
                    "wait",
                    "--for=condition=Established",
                    f"crd/{item['metadata']['name']}",
                    "--timeout=60s",
                ],
            )
    # Admission is established before submission Roles exist. All ownership
    # checks precede the first write; a failure retains partial state.
    for kinds in (
        {"Namespace"},
        {"ValidatingAdmissionPolicy"},
        {"ValidatingAdmissionPolicyBinding"},
    ):
        write(target, [item for item in documents if item["kind"] in kinds])
    verify_admission(target, documents)
    print(
        "Admission type checking passed; applying scoped controllers and delegated roles...",
        flush=True,
    )
    write(
        target,
        [
            item
            for item in documents
            if item["kind"]
            not in {"Namespace", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
        ],
    )
    return observe(target, delegation, shared, documents)


def write(target: Target, documents: list[dict]) -> None:
    if documents:
        kube(
            target,
            ["apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", "-"],
            documents,
            # Initial CRD schema compilation may exceed normal API latency on
            # the bounded local control plane; the operation remains finite.
            request_timeout=120
            if any(item["kind"] == "CustomResourceDefinition" for item in documents)
            else 30,
        )


def verify_admission(target: Target, documents: list[dict]) -> None:
    policies = [item for item in documents if item["kind"] == "ValidatingAdmissionPolicy"]
    deadline = time.monotonic() + 60
    while True:
        found = read_resources(target, policies)
        pending = False
        for policy in policies:
            actual = found.get(reference(policy), {})
            status = actual.get("status", {})
            if status.get("typeChecking", {}).get("expressionWarnings"):
                raise ValueError(
                    "Admission policy type checking failed; no release authority granted"
                )
            pending |= not actual or status.get("observedGeneration") != actual.get(
                "metadata", {}
            ).get("generation")
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise ValueError(
                "Admission policy type checking timed out; no release authority granted"
            )
        time.sleep(2)


def health(target: Target, delegation: Delegation) -> dict[str, Any]:
    consumer_lifecycle.health(target)
    namespaces_delegated(target, delegation)
    shared, documents = render(delegation, download_assets())
    return observe(target, delegation, shared, documents)


def contains(expected: Any, actual: Any) -> bool:
    """Allow API omission of empty arrays/defaults, never extra entries or changed values."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            (key in actual and contains(value, actual[key]))
            or (key not in actual and isinstance(value, list) and not value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(contains(a, b) for a, b in zip(expected, actual, strict=True))
        )
    return expected == actual


def verify_security(expected: dict, actual: dict) -> None:
    """Defaults may add fields; they must never hide additional security authority."""
    kind = expected["kind"]
    if kind.startswith("ValidatingAdmission"):
        if set(actual["spec"]) != set(expected["spec"]):
            raise ValueError("Unexpected admission scope, condition or parameter override")
        if kind == "ValidatingAdmissionPolicy":
            scope = actual["spec"]["matchConstraints"]
            if (
                scope.get("objectSelector", {})
                or scope.get("matchPolicy", "Equivalent") != "Equivalent"
                or scope["namespaceSelector"]
                != expected["spec"]["matchConstraints"]["namespaceSelector"]
            ):
                raise ValueError("Admission matching has been narrowed or redirected")
            if any(rule.get("scope", "*") != "*" for rule in scope["resourceRules"]):
                raise ValueError("Admission resource scope has been narrowed")
    if kind == "Service" and (
        actual["spec"].get("type", "ClusterIP") != "ClusterIP" or actual["spec"].get("externalIPs")
    ):
        raise ValueError("Controller service exposure changed")
    if kind in {"Deployment", "StatefulSet"}:
        wanted = expected["spec"]["template"]["spec"]
        pod = actual["spec"]["template"]["spec"]
        for key in (
            "hostNetwork",
            "hostPID",
            "hostIPC",
            "volumes",
            "securityContext",
            "initContainers",
        ):
            if key not in wanted and pod.get(key) not in (None, False, [], {}):
                raise ValueError("Unexpected controller pod security/volume fields")
        for a, b in zip(
            [*wanted["containers"], *wanted.get("initContainers", [])],
            [*pod["containers"], *pod.get("initContainers", [])],
            strict=True,
        ):
            if a.get("securityContext", {}) != b.get("securityContext", {}):
                raise ValueError("Controller security context changed")
            if "volumeMounts" not in a and b.get("volumeMounts"):
                raise ValueError("Unexpected controller volume mounts")


def observe(
    target: Target, delegation: Delegation, shared: list[dict], documents: list[dict]
) -> dict:
    found = inventory(target, shared, documents, complete=True)
    for item in documents:
        actual = found[reference(item)]
        verify_security(item, actual)
        for key in ("spec", "data", "rules", "subjects", "roleRef"):
            if key in item and not contains(item[key], actual.get(key)):
                raise ValueError(
                    f"Delegated {item['kind']} {item['metadata']['name']} {key} drift; inspect the selected target"
                )
        if item["kind"] == "ConfigMap" and item.get("data", {}) != actual.get("data", {}):
            raise ValueError("Unexpected controller configuration/RBAC policy fields")
        if item["kind"] in {"Deployment", "StatefulSet"}:
            pod = actual["spec"]["template"]["spec"]
            if any(pod.get(key, False) for key in ("hostNetwork", "hostPID", "hostIPC")) or any(
                c.get("securityContext", {}).get("privileged", False) for c in pod["containers"]
            ):
                raise ValueError("Unexpected privileged controller workload")
            kube(
                target,
                [
                    "-n",
                    item["metadata"]["namespace"],
                    "rollout",
                    "status",
                    f"{item['kind'].lower()}/{item['metadata']['name']}",
                    "--timeout=300s",
                ],
            )
    verify_admission(target, documents)
    return {
        "consumer": delegation.consumer,
        "cluster": target.name,
        "context": target.context,
        "incarnation": consumer_runtime.incarnation(target),
        **consumer_lifecycle.source_identity(target),
        "gitopsNamespace": delegation.gitops,
        "controllerNamespace": delegation.controllers,
        "managedNamespace": delegation.managed,
        "instanceID": delegation.consumer,
        "delegationDigest": delegation.digest,
        "versions": {key: value["version"] for key, value in selection().items()},
        "controllerReadiness": "observed",
        "admissionCompilation": "observed",
        "liveAuthorizationAndExecution": "requires separate qualify action",
        "resourceEnvelope": {
            "controllerPods": 9,
            "requestsCpu": "450m",
            "requestsMemory": "576Mi",
            "limitsCpu": "4500m",
            "limitsMemory": "3456Mi",
        },
        "notQualified": [
            "network isolation",
            "trusted ingress",
            "SSO/token issuance",
            "step-level approval",
            "persistent audit/archive/backup",
            "consumer product release",
        ],
    }


def remove(target: Target, delegation: Delegation, confirmation: str | None) -> dict:
    """Remove one instance, retaining shared APIs, Secrets and all consumer records."""
    if confirmation != f"{target.name}/argo":
        raise ValueError("Instance removal requires exact --confirm-remove <cluster>/argo")
    consumer_lifecycle.owned(target)
    namespaces_delegated(target, delegation)
    shared, documents = render(delegation, download_assets())
    found = inventory(target, shared, documents, complete=False)
    workflows = json.loads(
        kube(target, ["-n", delegation.managed, "get", "workflows", "-o", "json"])
    )
    if any(
        item.get("status", {}).get("phase") not in {"Succeeded", "Failed", "Error"}
        for item in workflows.get("items", [])
    ):
        raise ValueError(
            "Nonterminal workflows remain; stop/inspect through reviewed consumer operations first"
        )
    # Remove delegated access first, controllers next, and admission last.
    selected = [item for item in documents if item["kind"] not in {"Namespace", "Secret"}]
    selected.sort(
        key=lambda item: (
            0
            if item["kind"] == "RoleBinding"
            else 2
            if item["kind"].startswith("ValidatingAdmission")
            else 1
        )
    )
    plurals = {
        "RoleBinding": "rolebindings",
        "Role": "roles",
        "ServiceAccount": "serviceaccounts",
        "ConfigMap": "configmaps",
        "Deployment": "deployments",
        "StatefulSet": "statefulsets",
        "Service": "services",
        "NetworkPolicy": "networkpolicies",
        "ValidatingAdmissionPolicy": "validatingadmissionpolicies",
        "ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
    }
    for item in selected:
        actual = found.get(reference(item))
        if actual is None:
            continue
        namespace = item["metadata"].get("namespace")
        prefix = "/api/v1" if item["apiVersion"] == "v1" else f"/apis/{item['apiVersion']}"
        path = (
            prefix
            + (f"/namespaces/{namespace}" if namespace else "")
            + f"/{plurals[item['kind']]}/{item['metadata']['name']}"
        )
        # Kubernetes verifies UID at deletion, so a replacement with the same
        # name cannot be deleted between the ownership read and the write.
        command = [
            "kubectl",
            "--kubeconfig",
            str(target.path("kubeconfig")),
            "--context",
            target.context,
            "--request-timeout=30s",
            "delete",
            "--raw",
            path,
            "-f",
            "-",
        ]
        consumer_runtime.command(
            target,
            command,
            input_data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": actual["metadata"]["uid"]},
                    "propagationPolicy": "Foreground",
                }
            ).encode(),
        )
    # Retention is deliberate; never remove shared APIs or sweep a namespace.
    return {
        "consumer": delegation.consumer,
        "outcome": "instance-removal-requested",
        "retained": [
            "shared APIs and PriorityClass",
            "namespaces",
            "Secrets",
            "consumer WorkflowTemplates and workflow/evidence records",
        ],
    }
