"""Install and verify Argo CD on the reviewed local substrate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import argocd_status, local_kind

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "argocd"
OWNER = "k8s-infrastructure"
LEGACY_OWNER = "perfeng-environment"
ACTIONS = ("validate", "bootstrap", "health")
BOOTSTRAP = Path("clusters/local-kind/perfeng-local/addons/argocd")


def load_yaml(path: Path) -> dict[str, Any]:
    """Load one mapping from a reviewed YAML file."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid YAML document: {path.name}")
    return value


def argocd_version(kustomization: dict[str, Any]) -> str:
    """Return the semantic release selected by the official install manifest."""
    resources = kustomization.get("resources")
    if (
        set(kustomization) != {"apiVersion", "kind", "namespace", "resources"}
        or kustomization.get("apiVersion") != "kustomize.config.k8s.io/v1beta1"
        or kustomization.get("kind") != "Kustomization"
        or kustomization.get("namespace") != NAMESPACE
        or not isinstance(resources, list)
        or len(resources) != 2
        or resources[0] != "namespace.yaml"
        or not isinstance(resources[1], str)
    ):
        raise ValueError("Invalid Argo CD Kustomize bootstrap")

    manifest = urlparse(resources[1])
    parts = manifest.path.split("/")
    if (
        manifest.scheme != "https"
        or manifest.netloc != "raw.githubusercontent.com"
        or manifest.query
        or manifest.fragment
        or len(parts) != 6
        or parts[1:3] != ["argoproj", "argo-cd"]
        or parts[4:] != ["manifests", "install.yaml"]
    ):
        raise ValueError("Argo CD must use the official versioned install manifest")

    version = parts[3]
    numbers = version.removeprefix("v").split(".")
    if (
        not version.startswith("v")
        or len(numbers) != 3
        or not all(number.isdigit() for number in numbers)
    ):
        raise ValueError("Argo CD must use an explicit semantic release tag")
    return version


def validate_namespace(namespace: dict[str, Any]) -> None:
    """Require the exact infrastructure-owned namespace identity."""
    expected = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": NAMESPACE,
                "app.kubernetes.io/managed-by": OWNER,
                "app.kubernetes.io/part-of": "kubernetes-substrate",
            },
        },
    }
    if namespace != expected:
        raise ValueError("Invalid Argo CD namespace identity")


def validate_configuration(root: Path = ROOT) -> str:
    """Validate the complete bootstrap selection without external access."""
    bootstrap = root / BOOTSTRAP
    version = argocd_version(load_yaml(bootstrap / "kustomization.yaml"))
    validate_namespace(load_yaml(bootstrap / "namespace.yaml"))
    return version


def kubectl(state_root: Path) -> list[str]:
    """Return a command prefix bound to the dedicated local substrate."""
    return [
        "kubectl",
        "--kubeconfig",
        str(local_kind.kubeconfig(state_root)),
        "--context",
        local_kind.CONTEXT,
        "--namespace",
        NAMESPACE,
        "--request-timeout=30s",
    ]


def namespace_command(kube: list[str]) -> list[str]:
    """Build the read-only namespace ownership query."""
    return [*kube, "get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json"]


def validate_live_namespace(
    output: str,
    *,
    allow_missing: bool = False,
    allow_legacy: bool = False,
) -> None:
    """Require an owned namespace or the exact reviewed legacy handoff identity."""
    if not output.strip():
        if allow_missing:
            return
        raise ValueError("Argo CD namespace is missing")
    value = json.loads(output)
    if not isinstance(value, dict):
        raise ValueError("Invalid Argo CD namespace response")
    metadata = value.get("metadata")
    if (
        value.get("apiVersion") != "v1"
        or value.get("kind") != "Namespace"
        or not isinstance(metadata, dict)
        or metadata.get("name") != NAMESPACE
        or not isinstance(metadata.get("labels"), dict)
    ):
        raise ValueError("Invalid Argo CD namespace response")
    labels = metadata["labels"]
    owner = labels.get("app.kubernetes.io/managed-by")
    if owner == OWNER:
        if (
            labels.get("app.kubernetes.io/name") != NAMESPACE
            or labels.get("app.kubernetes.io/part-of") != "kubernetes-substrate"
        ):
            raise ValueError("Argo CD namespace has incomplete infrastructure ownership")
        return
    if allow_legacy and owner == LEGACY_OWNER:
        if labels.get("perfeng.io/purpose") != "gitops-control-plane":
            raise ValueError("Argo CD namespace has an invalid legacy ownership identity")
        return
    raise ValueError("Argo CD namespace has an unexpected owner")


def commands(action: str, root: Path = ROOT, state_root: Path = ROOT) -> list[list[str]]:
    """Build the bootstrap or health command sequence for offline preview."""
    if action not in {"bootstrap", "health"}:
        raise ValueError("Unknown Argo CD action")
    validate_configuration(root)
    kube = kubectl(state_root)
    result = [namespace_command(kube)]
    if action == "bootstrap":
        result.append(
            [
                *kube,
                "apply",
                "--server-side",
                "--force-conflicts",
                "-k",
                str(root / BOOTSTRAP),
            ]
        )
        result.append(namespace_command(kube))
    result.extend(argocd_status.rollout_commands(kube))
    return result


def execute(action: str, root: Path = ROOT, state_root: Path = ROOT) -> None:
    """Operate only an owned cluster and an approved namespace handoff."""
    if action not in {"bootstrap", "health"}:
        raise ValueError("Unknown Argo CD action")
    state_root = local_kind.normalize_state_root(state_root)
    validate_configuration(root)
    local_kind.execute("health", state_root)
    kube = kubectl(state_root)
    current = local_kind.run(namespace_command(kube))
    validate_live_namespace(
        current,
        allow_missing=action == "bootstrap",
        allow_legacy=action == "bootstrap",
    )
    if action == "bootstrap":
        local_kind.run(commands(action, root, state_root)[1])
        validate_live_namespace(local_kind.run(namespace_command(kube)))
    argocd_status.wait_for_argocd(kube)
    print(f"Local Argo CD action completed: {action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--state-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            version = validate_configuration()
            print(f"Validated official Argo CD {version} bootstrap.")
        elif args.execute:
            execute(args.action, state_root=args.state_root)
        else:
            state_root = local_kind.normalize_state_root(args.state_root)
            for command in commands(args.action, state_root=state_root):
                print(json.dumps(command))
            print("Preview only. Add --execute to operate Argo CD on the selected substrate.")
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        print(f"Local Argo CD bootstrap error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
