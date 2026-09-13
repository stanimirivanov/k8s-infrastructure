"""Validate, bootstrap and inspect the local Argo CD GitOps control plane."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

if __package__:
    from . import cluster
    from .control_plane_registry import valid_token
else:
    import cluster
    from control_plane_registry import valid_token

NAMESPACE = "argocd"
OWNER = "perfeng-environment"
ROOT_APPLICATION = "perfeng-local-root"
SAMPLE_SUT_APPLICATION = "perfeng-local-sample-sut"
REPOSITORY_CREDENTIALS = "perfeng-github-repository-credentials"
REPOSITORY_PREFIX = "https://github.com/stanimirivanov"
REPOSITORY_URL = REPOSITORY_PREFIX + "/perfeng-environment.git"
TOKEN_ENVIRONMENT_VARIABLE = "PERFENG_REPOSITORIES_READ_TOKEN"
DEPLOYMENTS = (
    "argocd-applicationset-controller",
    "argocd-dex-server",
    "argocd-notifications-controller",
    "argocd-redis",
    "argocd-repo-server",
    "argocd-server",
)
STATEFULSETS = ("argocd-application-controller",)


def kubectl(root: Path, *, namespace: str = NAMESPACE) -> list[str]:
    """Return the explicitly bound local-cluster command prefix."""
    return [
        "kubectl",
        "--kubeconfig",
        str(cluster.kubeconfig(root)),
        "--context",
        cluster.CONTEXT,
        "--namespace",
        namespace,
        "--request-timeout=30s",
    ]


def load_yaml(path: Path) -> dict[str, Any]:
    """Load one mapping from a reviewed YAML file."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid YAML document: {path.name}")
    return value


def argocd_version(kustomization: dict[str, Any]) -> str:
    """Return the explicit release selected by the official manifest resource."""
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
        or not all(item.isdigit() for item in numbers)
    ):
        raise ValueError("Argo CD must use an explicit semantic release tag")
    return version


def validate_configuration(root: Path = cluster.ROOT) -> str:
    """Validate the explicit Argo CD release and local objects without external access."""
    kustomization = load_yaml(root / "gitops/bootstrap/argocd/kustomization.yaml")
    version = argocd_version(kustomization)

    application = load_yaml(root / "gitops/bootstrap/local-root-application.yaml")
    metadata = application.get("metadata", {})
    spec = application.get("spec", {})
    if (
        application.get("kind") != "Application"
        or metadata.get("name") != ROOT_APPLICATION
        or metadata.get("namespace") != NAMESPACE
        or metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != OWNER
        or spec.get("source")
        != {
            "repoURL": REPOSITORY_URL,
            "targetRevision": "main",
            "path": "gitops/environments/local",
        }
        or spec.get("destination")
        != {"server": "https://kubernetes.default.svc", "namespace": NAMESPACE}
        or spec.get("syncPolicy", {}).get("automated")
        != {"enabled": True, "prune": False, "selfHeal": False}
    ):
        raise ValueError("Invalid local root Application")

    root_kustomization = load_yaml(root / "gitops/environments/local/kustomization.yaml")
    project = load_yaml(root / "gitops/environments/local/project.yaml")
    sample_sut = load_yaml(root / "gitops/environments/local/sample-sut-application.yaml")
    workspace = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    repository_urls = sorted(item["url"] for item in workspace["repositories"])
    project_spec = project.get("spec", {})
    if (
        root_kustomization.get("resources") != ["project.yaml", "sample-sut-application.yaml"]
        or project.get("kind") != "AppProject"
        or project.get("metadata", {}).get("name") != "perfeng-local"
        or project.get("metadata", {}).get("annotations", {}).get("argocd.argoproj.io/sync-wave")
        != "-1"
        or set(project_spec)
        != {
            "description",
            "sourceRepos",
            "destinations",
            "namespaceResourceWhitelist",
        }
        or sorted(project_spec.get("sourceRepos", [])) != repository_urls
        or project_spec.get("destinations")
        != [
            {"server": "https://kubernetes.default.svc", "namespace": namespace}
            for namespace in ("monitoring", "perf-generators", "perf-platform", "perf-sut")
        ]
        or project_spec.get("namespaceResourceWhitelist") != [{"group": "*", "kind": "*"}]
    ):
        raise ValueError("Invalid local root Kustomize configuration")
    sample_metadata = sample_sut.get("metadata", {})
    sample_spec = sample_sut.get("spec", {})
    if (
        sample_sut.get("apiVersion") != "argoproj.io/v1alpha1"
        or sample_sut.get("kind") != "Application"
        or set(sample_metadata) != {"name", "namespace", "labels"}
        or sample_metadata.get("name") != SAMPLE_SUT_APPLICATION
        or sample_metadata.get("namespace") != NAMESPACE
        or sample_metadata.get("labels")
        != {
            "app.kubernetes.io/managed-by": OWNER,
            "perfeng.io/environment": "local",
            "perfeng.io/workload": "sample-sut",
        }
        or set(sample_spec) != {"project", "source", "destination", "syncPolicy"}
        or sample_spec.get("project") != "perfeng-local"
        or sample_spec.get("source")
        != {
            "repoURL": REPOSITORY_URL,
            "targetRevision": "main",
            "path": "charts/sample-sut",
            "helm": {"releaseName": "sample-sut"},
        }
        or sample_spec.get("destination")
        != {"server": "https://kubernetes.default.svc", "namespace": "perf-sut"}
        or sample_spec.get("syncPolicy", {}).get("automated")
        != {"enabled": True, "prune": False, "selfHeal": False}
        or set(sample_spec.get("syncPolicy", {})) != {"automated", "syncOptions"}
        or sample_spec.get("syncPolicy", {}).get("syncOptions") != ["CreateNamespace=true"]
    ):
        raise ValueError("Invalid local sample SUT Application")
    return version


def credential_document(token: str) -> dict[str, Any]:
    """Build immutable private-repository credentials for the GitHub owner prefix."""
    if not valid_token(token):
        raise ValueError("Invalid private-repository credential input")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": REPOSITORY_CREDENTIALS,
            "namespace": NAMESPACE,
            "labels": {
                "argocd.argoproj.io/secret-type": "repo-creds",
                "app.kubernetes.io/managed-by": OWNER,
                "perfeng.io/purpose": "private-repository-read",
            },
        },
        "immutable": True,
        "type": "Opaque",
        "stringData": {
            "type": "git",
            "url": REPOSITORY_PREFIX,
            "username": "token",
            "password": token,
        },
    }


def validate_stored_credentials(value: dict[str, Any]) -> None:
    """Reject an existing credential Secret that is not the exact owned shape."""
    expected = credential_document("x" * 20)
    try:
        data = {
            key: base64.b64decode(encoded, validate=True).decode("utf-8")
            for key, encoded in value["data"].items()
        }
        if (
            value["metadata"]["name"] != REPOSITORY_CREDENTIALS
            or value["metadata"]["namespace"] != NAMESPACE
            or value["metadata"]["labels"] != expected["metadata"]["labels"]
            or value["immutable"] is not True
            or value["type"] != "Opaque"
            or data.get("type") != "git"
            or data.get("url") != REPOSITORY_PREFIX
            or data.get("username") != "token"
            or set(data) != {"type", "url", "username", "password"}
            or not valid_token(data["password"])
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError(
            f"Invalid owned immutable Secret {REPOSITORY_CREDENTIALS}; "
            "replace it through explicit recovery"
        ) from None


def ensure_credentials(kube: list[str], *, create: bool) -> None:
    """Validate and reuse Git credentials, or create them from the environment."""
    current = cluster.run(
        [*kube, "get", "secret", REPOSITORY_CREDENTIALS, "--ignore-not-found", "-o", "json"]
    )
    if current.strip():
        validate_stored_credentials(json.loads(current))
        print("Reusing existing Argo CD repository credentials (not rotated).", flush=True)
        return
    if not create:
        raise ValueError("Argo CD repository credentials are missing; run deploy first")
    token = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE)
    if token is None:
        raise ValueError(f"{TOKEN_ENVIRONMENT_VARIABLE} is required to read the private repository")
    print(f"Using {TOKEN_ENVIRONMENT_VARIABLE} from the environment.", flush=True)
    cluster.run(
        [*kube, "create", "-f", "-"],
        input_data=json.dumps(credential_document(token)).encode(),
    )


def verify_namespace(kube: list[str], *, create: bool) -> None:
    """Refuse to adopt a GitOps namespace with a different owner."""
    current = cluster.run(
        [*kube, "get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json"]
    )
    if not current.strip():
        if create:
            return
        raise ValueError("Argo CD namespace is missing; run deploy first")
    labels = json.loads(current).get("metadata", {}).get("labels", {})
    if (
        labels.get("app.kubernetes.io/managed-by") != OWNER
        or labels.get("perfeng.io/purpose") != "gitops-control-plane"
    ):
        raise ValueError("Existing argocd namespace is not owned by perfeng-environment")


def wait_for_argocd(kube: list[str]) -> None:
    """Wait for every workload in the pinned non-HA installation."""
    for name in DEPLOYMENTS:
        cluster.run([*kube, "rollout", "status", f"deployment/{name}", "--timeout=300s"])
    for name in STATEFULSETS:
        cluster.run([*kube, "rollout", "status", f"statefulset/{name}", "--timeout=300s"])


def ensure_root(kube: list[str], root: Path, *, apply: bool) -> None:
    """Ownership-check and optionally apply the local root Application."""
    current = cluster.run(
        [*kube, "get", "application", ROOT_APPLICATION, "--ignore-not-found", "-o", "json"]
    )
    if current.strip():
        labels = json.loads(current).get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/managed-by") != OWNER:
            raise ValueError("Existing root Application is not owned by perfeng-environment")
    elif not apply:
        raise ValueError("Local root Application is missing; run deploy first")
    if apply:
        content = (root / "gitops/bootstrap/local-root-application.yaml").read_bytes()
        cluster.run([*kube, "apply", "-f", "-"], input_data=content)


def wait_for_application(kube: list[str], name: str) -> None:
    """Require an Application to exist, synchronize and become healthy."""
    cluster.run([*kube, "wait", "--for=create", f"application/{name}", "--timeout=180s"])
    for field, value in (("sync", "Synced"), ("health", "Healthy")):
        cluster.run(
            [
                *kube,
                "wait",
                f"--for=jsonpath={{.status.{field}.status}}={value}",
                f"application/{name}",
                "--timeout=180s",
            ]
        )


def execute(action: str, root: Path = cluster.ROOT) -> None:
    """Deploy or health-check the pinned local GitOps control plane."""
    validate_configuration(root)
    cluster.verify_local_docker()
    cluster.verify_context(root)
    kube = kubectl(root)
    verify_namespace(kube, create=action == "deploy")
    if action == "deploy":
        cluster.run(
            [
                *kube,
                "apply",
                "--server-side",
                "--force-conflicts",
                "-k",
                str(root / "gitops/bootstrap/argocd"),
            ]
        )
    wait_for_argocd(kube)
    ensure_credentials(kube, create=action == "deploy")
    ensure_root(kube, root, apply=action == "deploy")
    wait_for_application(kube, ROOT_APPLICATION)
    wait_for_application(kube, SAMPLE_SUT_APPLICATION)
    cluster.run(
        [
            *kubectl(root, namespace="perf-sut"),
            "rollout",
            "status",
            "deployment/sample-sut",
            "--timeout=180s",
        ]
    )
    print(f"Local Argo CD GitOps action completed: {action}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "deploy", "health"])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "validate":
            version = validate_configuration()
            print(f"Validated official Argo CD {version} manifest and local Applications.")
        elif args.execute:
            execute(args.action)
        else:
            print(f"Preview: {args.action} the pinned local Argo CD GitOps control plane.")
            if args.action == "deploy":
                print(f"Missing Git credentials use {TOKEN_ENVIRONMENT_VARIABLE}.")
            print("Preview only. Add --execute to access the network or local cluster.")
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as error:
        print(f"Local GitOps error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
