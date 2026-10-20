"""Render pinned upstream controllers with explicit namespace and permission scopes."""

from __future__ import annotations

import copy
import json

import yaml

from .delegated_argo_config import LABEL, Delegation, labels, selection
from .delegated_argo_policy import admission, binding, delegated_roles, resource, stamp
from .kubernetes import reference as reference


def render(
    delegation: Delegation, assets: dict[str, list[dict]], *, networking: bool = False
) -> tuple[list[dict], list[dict]]:
    """Never install the cluster-wide CD manifest; extract only its three CRDs."""
    shared = [
        copy.deepcopy(item)
        for key in ("argocd-apis", "workflows")
        for item in assets[key]
        if item["kind"] in {"CustomResourceDefinition", "PriorityClass"}
    ]
    expected = {
        "applications.argoproj.io",
        "applicationsets.argoproj.io",
        "appprojects.argoproj.io",
        *[
            f"{name}.argoproj.io"
            for name in (
                "clusterworkflowtemplates",
                "cronworkflows",
                "workflowartifactgctasks",
                "workfloweventbindings",
                "workflows",
                "workflowtaskresults",
                "workflowtasksets",
                "workflowtemplates",
            )
        ],
    }
    if {
        item["metadata"]["name"] for item in shared if item["kind"] == "CustomResourceDefinition"
    } != expected or len(shared) != 12:
        raise ValueError("Shared Argo API inventory differs from reviewed releases")
    for item in shared:
        item["metadata"].setdefault("labels", {}).update(labels(delegation, shared=True))
    documents = [
        resource("Namespace", namespace, None)
        for namespace in (delegation.gitops, delegation.controllers)
    ]
    documents += controllers(assets["argocd"], delegation.gitops, delegation, workflows=False)
    documents += controllers(
        assets["workflows"], delegation.controllers, delegation, workflows=True
    )
    documents += delegated_roles(delegation, networking=networking)
    documents += admission(
        delegation,
        next(item for item in shared if item["metadata"]["name"] == "workflows.argoproj.io"),
    )
    return shared, stamp(documents, delegation)


def controllers(
    upstream: list[dict], namespace: str, delegation: Delegation, *, workflows: bool
) -> list[dict]:
    documents = []
    for original in upstream:
        if original["kind"] in {"CustomResourceDefinition", "PriorityClass"}:
            continue
        item = copy.deepcopy(original)
        if item["kind"] in {"ClusterRole", "ClusterRoleBinding", "Namespace"}:
            raise ValueError("Consumer controller manifest unexpectedly requests cluster authority")
        item["metadata"]["namespace"] = namespace
        name, kind = item["metadata"]["name"], item["kind"]
        if kind == "RoleBinding":
            for subject in item["subjects"]:
                if subject["kind"] != "ServiceAccount":
                    raise ValueError("Unexpected upstream role subject")
                subject["namespace"] = namespace
        if kind in {"Deployment", "StatefulSet"}:
            pod = item["spec"]["template"]["spec"]
            pod["nodeSelector"] = {
                LABEL: delegation.consumer,
                "k8s-infrastructure.io/role": "worker",
            }
            for container in [*pod["containers"], *pod.get("initContainers", [])]:
                container["resources"] = {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "384Mi"},
                }
            if workflows:
                pod["containers"][0]["args"] = (["server"] if name == "argo-server" else []) + [
                    "--namespaced",
                    "--managed-namespace",
                    delegation.managed,
                ]
                if name == "argo-server":
                    pod["containers"][0]["args"] += ["--auth-mode=client"]
        if kind == "Service" and (
            item["spec"].get("type", "ClusterIP") != "ClusterIP" or "externalIPs" in item["spec"]
        ):
            raise ValueError("Controller servers must remain internal")
        if workflows and kind == "Role":
            # Controller settings stay in the system namespace; its workflow
            # permissions are independently bound in the consumer-managed one.
            delegated = copy.deepcopy(item)
            delegated["metadata"]["namespace"] = delegation.managed
            documents.extend(
                [
                    delegated,
                    binding(
                        f"delegated-{name}",
                        delegation.managed,
                        name,
                        "argo" if name == "argo-role" else "argo-server",
                        namespace,
                    ),
                ]
            )
        if kind == "ConfigMap" and workflows and name == "workflow-controller-configmap":
            item["data"] = {
                "instanceID": delegation.consumer,
                "workflowRestrictions": "templateReferencing: Secure\n",
                "parallelism": "2",
                "executor": yaml.safe_dump(
                    {"image": f"quay.io/argoproj/argoexec:{selection()['workflows']['version']}"}
                ),
            }
        if kind == "ConfigMap" and not workflows:
            if name == "argocd-cm":
                item.setdefault("data", {}).update(
                    {
                        "admin.enabled": "false",
                        "users.anonymous.enabled": "false",
                        "application.sync.requireOverridePrivilegeForRevisionSync": "true",
                        "accounts.release-releaser": "apiKey",
                        "accounts.release-viewer": "apiKey",
                    }
                )
            if name == "argocd-rbac-cm":
                item["data"] = {
                    "policy.default": "",
                    "policy.csv": "\n".join(
                        [
                            "p, role:release-viewer, applications, get, */*, allow",
                            "p, role:release-releaser, applications, get, */*, allow",
                            "p, role:release-releaser, applications, sync, */*, allow",
                            "p, role:release-releaser, applications, override, */*, deny",
                            "p, role:release-releaser, applications, update, */*, deny",
                            "p, role:release-releaser, applications, create, */*, deny",
                            "p, role:release-releaser, applications, delete, */*, deny",
                            "g, release-viewer, role:release-viewer",
                            "g, release-releaser, role:release-releaser",
                        ]
                    ),
                }
        documents.append(item)
    if not workflows:
        # Explicit namespace registration prevents an in-cluster informer from
        # requiring/watching cluster-wide resources it was never delegated.
        documents.append(
            resource(
                "Secret",
                "delegated-in-cluster",
                namespace,
                type="Opaque",
                stringData={
                    "name": delegation.consumer,
                    "server": "https://kubernetes.default.svc",
                    "namespaces": ",".join((*delegation.workload_namespaces, delegation.managed)),
                    "clusterResources": "false",
                    "config": json.dumps({}),
                },
            )
        )
        documents[-1]["metadata"]["labels"] = {"argocd.argoproj.io/secret-type": "cluster"}
    return documents
