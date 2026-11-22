"""Reviewed minimal-demo Argo inventory, startup order and staged image pins."""

from __future__ import annotations

import yaml

from .delegated_argo_config import Delegation
from .kubernetes import reference
from .network_resources import resources

ARGO = "quay.io/argoproj/argocd:v3.5.2"
DEX = "ghcr.io/dexidp/dex:v2.45.1"
REDIS = "public.ecr.aws/docker/library/redis:8.2.3-alpine"
CLI = "quay.io/argoproj/argocli:v4.1.5"
WORKFLOW = "quay.io/argoproj/workflow-controller:v4.1.5"
EXECUTOR = "quay.io/argoproj/argoexec:v4.1.5"

# Linux amd64 child manifests, verified against each public registry.
IMAGES = {
    ARGO: "478701ca1208502315ae55eecb50180292739f6b579d4bbfa9015a43e7c71e68",
    DEX: "f5f9fb373188b0f701b80edd68b3f4745ced69306c9d382999f11b7774335a98",
    REDIS: "e499175dfb27569cd40010c2eee346113db95fdd0efc88ab9fd70a9e807f4542",
    CLI: "12d83a550dbc0e070c09378802941dc16d1a55bcc0aaa00b3f80c34b0df536ec",
    WORKFLOW: "67b446fa9aa6844383b831afc34fd65231b033a8586a35652dfb364aaf35fa03",
    EXECUTOR: "e9f41adeb7b90a8549036e7c48421eb0e5c134a0151e6a56c45c66553b314ff9",
}

# Start dependencies first and restore consumer desired-state controllers last.
CONTROLLERS = {
    "argocd-redis": ("redis", REDIS, "secret-init"),
    "argocd-repo-server": ("argocd-repo-server", ARGO, "copyutil"),
    "argocd-dex-server": ("dex", DEX, "copyutil"),
    "argocd-server": ("argocd-server", ARGO, None),
    "argocd-notifications-controller": ("argocd-notifications-controller", ARGO, None),
    "workflow-controller": ("workflow-controller", WORKFLOW, None),
    "argo-server": ("argo-server", CLI, None),
    "argocd-applicationset-controller": ("argocd-applicationset-controller", ARGO, None),
    "argocd-application-controller": ("argocd-application-controller", ARGO, None),
}


def selection() -> dict[str, str]:
    return {image: f"{image}@sha256:{digest}" for image, digest in IMAGES.items()}


def required_images() -> tuple[str, ...]:
    """Return immutable worker images for the nine controller Pods."""
    return tuple(pin for image, pin in selection().items() if image != EXECUTOR)


def controller_names(delegation: Delegation) -> dict[str, tuple[str, str]]:
    return {
        name: (
            "StatefulSet" if name == "argocd-application-controller" else "Deployment",
            delegation.gitops if name.startswith("argocd-") else delegation.controllers,
        )
        for name in CONTROLLERS
    }


def controller_documents(delegation: Delegation, documents: list[dict]) -> list[dict]:
    expected = [(kind, name, ns) for name, (kind, ns) in controller_names(delegation).items()]
    workloads = [item for item in documents if item["kind"] in {"Deployment", "StatefulSet"}]
    indexed = {reference(item): item for item in workloads}
    if len(workloads) != len(expected) or indexed.keys() != set(expected):
        raise ValueError("Minimal-demo Argo workload inventory differs from reviewed releases")
    return [indexed[key] for key in expected]


def bound_controllers(delegation: Delegation, documents: list[dict]) -> None:
    pins = selection()
    for item in controller_documents(delegation, documents):
        spec = item["spec"]
        pod = spec["template"]["spec"]
        main_name, image, init_name = CONTROLLERS[item["metadata"]["name"]]
        main, init = pod["containers"], pod.get("initContainers", [])
        wanted_init = [(init_name, ARGO)] if init_name else []
        if (
            [(c["name"], c["image"]) for c in main] != [(main_name, image)]
            or [(c["name"], c["image"]) for c in init] != wanted_init
            or pod.get("ephemeralContainers")
            or any(c.get("restartPolicy") for c in init)
            or spec.get("replicas", 1) != 1
        ):
            raise ValueError("Minimal-demo Argo container inventory differs from reviewed releases")
        spec["replicas"] = 1
        if item["kind"] == "Deployment":
            spec["strategy"] = {"type": "Recreate"}
        main[0].update(
            image=pins[image], imagePullPolicy="Never", resources=resources(50, 500, 64, 384)
        )
        for container in init:
            container.update(
                image=pins[ARGO], imagePullPolicy="Never", resources=resources(50, 500, 384, 384)
            )

    config = next(
        (
            item
            for item in documents
            if reference(item)
            == ("ConfigMap", "workflow-controller-configmap", delegation.controllers)
        ),
        None,
    )
    if config is None:
        raise ValueError("Minimal-demo Argo Workflow controller configuration is missing")
    config["data"]["executor"] = yaml.safe_dump(
        {"image": pins[EXECUTOR], "imagePullPolicy": "Never"}
    )
