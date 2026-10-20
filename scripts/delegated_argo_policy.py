"""Native admission and delegated Kubernetes roles; no custom approval service."""

from __future__ import annotations

import json
from typing import Any

from .delegated_argo_config import Delegation, labels
from .delegated_argo_schema import submission_guards


def resource(kind: str, name: str, namespace: str | None, **body: Any) -> dict[str, Any]:
    group = {
        "Role": "rbac.authorization.k8s.io/v1",
        "RoleBinding": "rbac.authorization.k8s.io/v1",
        "ValidatingAdmissionPolicy": "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding": "admissionregistration.k8s.io/v1",
    }.get(kind, "v1")
    return {
        "apiVersion": group,
        "kind": kind,
        "metadata": {"name": name, **({"namespace": namespace} if namespace else {})},
        **body,
    }


def rule(group: str, resources: list[str], verbs: list[str], **extra: Any) -> dict[str, Any]:
    return {"apiGroups": [group], "resources": resources, "verbs": verbs, **extra}


def binding(name: str, namespace: str, role: str, account: str, account_ns: str) -> dict:
    return resource(
        "RoleBinding",
        name,
        namespace,
        roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role},
        subjects=[{"kind": "ServiceAccount", "name": account, "namespace": account_ns}],
    )


def delegated_roles(delegation: Delegation, *, networking: bool = False) -> list[dict[str, Any]]:
    """Release identities cannot author templates, execute Pods, or read Secrets."""
    ns = delegation.managed
    documents = []
    for account, rules in {
        "release-viewer": [
            rule("argoproj.io", ["workflows", "workflowtemplates"], ["get", "list", "watch"])
        ],
        "release-releaser": [
            rule("argoproj.io", ["workflows"], ["create", "get"]),
            rule(
                "argoproj.io",
                ["workflowtemplates"],
                ["get"],
                resourceNames=list(delegation.approved_templates),
            ),
        ],
        "release-approver": [rule("argoproj.io", ["workflows"], ["get", "patch"])],
        "release-executor": [rule("argoproj.io", ["workflowtaskresults"], ["create", "patch"])],
    }.items():
        documents.extend(
            [
                resource(
                    "ServiceAccount",
                    account,
                    ns,
                    automountServiceAccountToken=account == "release-executor",
                ),
                resource("Role", account, ns, rules=rules),
                binding(account, ns, account, account, ns),
            ]
        )
    # The GitOps controller can reconcile ordinary application resources, but
    # cannot create namespaces, RBAC, admission policies or cluster API types.
    workload_rules = [
        rule(
            "",
            ["configmaps", "secrets", "services", "serviceaccounts", "persistentvolumeclaims"],
            ["get", "list", "watch", "create", "update", "patch", "delete"],
        ),
        rule("", ["pods", "events"], ["get", "list", "watch"]),
        rule(
            "apps",
            ["deployments", "replicasets", "statefulsets", "daemonsets"],
            ["get", "list", "watch", "create", "update", "patch", "delete"],
        ),
        rule(
            "batch",
            ["jobs", "cronjobs"],
            ["get", "list", "watch", "create", "update", "patch", "delete"],
        ),
        rule(
            "networking.k8s.io",
            ["networkpolicies", "ingresses"],
            ["get", "list", "watch", "create", "update", "patch", "delete"],
        ),
    ]
    if networking:
        workload_rules.extend(
            [
                rule(
                    "gateway.networking.k8s.io",
                    ["gateways", "httproutes"],
                    ["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
                rule(
                    "cilium.io",
                    ["ciliumnetworkpolicies"],
                    ["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
            ]
        )
    for namespace in delegation.workload_namespaces:
        documents.extend(
            [
                resource("Role", "delegated-argocd", namespace, rules=workload_rules),
                binding(
                    "delegated-argocd",
                    namespace,
                    "delegated-argocd",
                    "argocd-application-controller",
                    delegation.gitops,
                ),
            ]
        )
    documents.extend(
        [
            resource(
                "Role",
                "delegated-release-templates",
                ns,
                rules=[
                    rule(
                        "argoproj.io",
                        ["workflowtemplates"],
                        ["get", "list", "watch", "create", "update", "patch"],
                    )
                ],
            ),
            binding(
                "delegated-release-templates",
                ns,
                "delegated-release-templates",
                "argocd-application-controller",
                delegation.gitops,
            ),
        ]
    )
    return documents


def admission(delegation: Delegation, workflow_crd: dict) -> list[dict[str, Any]]:
    """Deny every submission override and constrain approval to initial activation.

    Kubernetes RBAC cannot restrict patch fields. The approver therefore has no
    unrestricted resume/status authority: it may only clear the initial global
    suspend flag. Step-level approval needs a separately reviewed interface.
    """
    ns = delegation.managed
    controller = f"system:serviceaccount:{delegation.controllers}:argo"
    approver = f"system:serviceaccount:{ns}:release-approver"
    releaser = f"system:serviceaccount:{ns}:release-releaser"
    refs = json.dumps(list(delegation.approved_templates))
    guards = submission_guards(workflow_crd)
    common = (
        "has(object.spec.workflowTemplateRef) && has(object.spec.workflowTemplateRef.name) && "
        f"object.spec.workflowTemplateRef.name in {refs} && "
        f"({guards['reference']}) && "
        "has(object.spec.arguments) && has(object.spec.arguments.parameters) && "
        "size(object.spec.arguments.parameters) == 1 && "
        "object.spec.arguments.parameters[0].name == 'release-digest' && "
        "has(object.spec.arguments.parameters[0].value) && "
        "object.spec.arguments.parameters[0].value.matches('^sha256:[a-f0-9]{64}$') && "
        f"({guards['arguments']}) && ({guards['parameter']}) && "
        f"has(object.spec.suspend) && ({guards['spec']})"
    )
    create = (
        f"request.operation == 'CREATE' && request.userInfo.username == '{releaser}' && "
        f"object.spec.suspend == true && (!has(object.status) || ({guards['status']})) && "
        "has(object.metadata.annotations) && object.metadata.annotations == {'k8s-infrastructure.io/submitted-by': request.userInfo.username} && "
        "(!has(object.metadata.ownerReferences) || size(object.metadata.ownerReferences) == 0) && "
        "(!has(object.metadata.finalizers) || size(object.metadata.finalizers) == 0) && "
        f"has(object.metadata.labels) && object.metadata.labels == {{'workflows.argoproj.io/controller-instanceid': '{delegation.consumer}'}}"
    )
    resume = (
        f"request.operation == 'UPDATE' && request.userInfo.username == '{approver}' && "
        "oldObject.spec.suspend == true && object.spec.suspend == false && "
        "object.spec.workflowTemplateRef == oldObject.spec.workflowTemplateRef && "
        "object.spec.arguments == oldObject.spec.arguments && "
        "has(object.status) == has(oldObject.status) && "
        "(!has(object.status) || object.status == oldObject.status)"
    )
    resume += (
        " && has(object.metadata.annotations) && has(oldObject.metadata.annotations)"
        " && !('k8s-infrastructure.io/approved-by' in oldObject.metadata.annotations)"
        " && size(object.metadata.annotations) == size(oldObject.metadata.annotations) + 1"
        " && object.metadata.annotations['k8s-infrastructure.io/approved-by'] == request.userInfo.username"
        " && oldObject.metadata.annotations.all(k, object.metadata.annotations[k] == oldObject.metadata.annotations[k])"
    )
    for key in ("labels", "ownerReferences", "finalizers"):
        resume += (
            f" && has(object.metadata.{key}) == has(oldObject.metadata.{key})"
            f" && (!has(object.metadata.{key}) || object.metadata.{key} == oldObject.metadata.{key})"
        )
    # Foreground deletion is completed by Kubernetes' garbage collector, not
    # the workflow controller. Permit only its removal of that system finalizer
    # from an already deleting object, preserving the release and audit record.
    remaining = "oldObject.metadata.finalizers.filter(f, f != 'foregroundDeletion')"
    collect = (
        "request.operation == 'UPDATE' && "
        "request.userInfo.username == 'system:serviceaccount:kube-system:generic-garbage-collector' && "
        "has(oldObject.metadata.deletionTimestamp) && "
        "object.metadata.deletionTimestamp == oldObject.metadata.deletionTimestamp && "
        "object.spec == oldObject.spec && has(object.status) == has(oldObject.status) && "
        "(!has(object.status) || object.status == oldObject.status) && "
        "has(oldObject.metadata.finalizers) && 'foregroundDeletion' in oldObject.metadata.finalizers && "
        f"((has(object.metadata.finalizers) && object.metadata.finalizers == {remaining}) || "
        f"(!has(object.metadata.finalizers) && size({remaining}) == 0))"
    )
    for key in ("labels", "ownerReferences", "annotations"):
        collect += (
            f" && has(object.metadata.{key}) == has(oldObject.metadata.{key})"
            f" && (!has(object.metadata.{key}) || object.metadata.{key} == oldObject.metadata.{key})"
        )
    definitions = [
        (
            "submission",
            ["workflows"],
            f"request.userInfo.username == '{controller}' || ({collect}) || (({common}) && (({create}) || ({resume})))",
        ),
        (
            "templates",
            ["workflowtemplates"],
            f"object.metadata.name in {refs} && (request.operation == 'CREATE' || object.spec == oldObject.spec)",
        ),
    ]
    documents = []
    for suffix, resources, expression in definitions:
        name = f"{delegation.consumer}-argo-{suffix}"
        documents.extend(
            [
                resource(
                    "ValidatingAdmissionPolicy",
                    name,
                    None,
                    spec={
                        "failurePolicy": "Fail",
                        "matchConstraints": {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": ns}
                            },
                            "resourceRules": [
                                {
                                    "apiGroups": ["argoproj.io"],
                                    "apiVersions": ["v1alpha1"],
                                    "operations": ["CREATE", "UPDATE"],
                                    "resources": resources,
                                }
                            ],
                        },
                        "validations": [
                            {
                                "expression": expression,
                                "message": "Delegated release requires an approved immutable template/digest and authorized operation; overrides are forbidden.",
                            }
                        ],
                    },
                ),
                resource(
                    "ValidatingAdmissionPolicyBinding",
                    name,
                    None,
                    spec={"policyName": name, "validationActions": ["Deny", "Audit"]},
                ),
            ]
        )
    return documents


def stamp(documents: list[dict], delegation: Delegation) -> list[dict]:
    for document in documents:
        document["metadata"].setdefault("labels", {}).update(labels(delegation))
    return documents
