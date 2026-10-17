"""Install the verified, namespace-scoped Argo Workflows substrate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import yaml

from . import local_kind

ROOT = Path(__file__).resolve().parents[1]
ADDON = Path("clusters/local-kind/perfeng-local/addons/argo-workflows")
NAMESPACE = "argo-workflows"
CRDS = frozenset(
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
)
KINDS = Counter(
    {
        "CustomResourceDefinition": 8,
        "ConfigMap": 1,
        "Deployment": 2,
        "PriorityClass": 1,
        "Role": 2,
        "RoleBinding": 2,
        "Service": 1,
        "ServiceAccount": 2,
    }
)
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def load_selection(root: Path = ROOT) -> dict[str, str]:
    """Validate the immutable upstream release selection without network access."""
    selection = json.loads((root / ADDON / "release.json").read_text(encoding="utf-8"))
    if not isinstance(selection, dict) or set(selection) != {"version", "url", "sha256"}:
        raise ValueError("Invalid Argo Workflows release selection")
    version, url, digest = (selection[key] for key in ("version", "url", "sha256"))
    if (
        not isinstance(version, str)
        or re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version) is None
        or url
        != f"https://github.com/argoproj/argo-workflows/releases/download/{version}/namespace-install.yaml"
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("Argo Workflows requires an official versioned manifest and SHA-256")
    return selection


def validate_namespace(root: Path = ROOT) -> dict[str, Any]:
    """Require the exact locally owned namespace and selected release label."""
    value = yaml.safe_load((root / ADDON / "namespace.yaml").read_text(encoding="utf-8"))
    version = load_selection(root)["version"]
    expected = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": NAMESPACE,
                "app.kubernetes.io/version": version,
                "app.kubernetes.io/managed-by": "k8s-infrastructure",
                "app.kubernetes.io/part-of": "kubernetes-substrate",
            },
        },
    }
    if value != expected:
        raise ValueError("Invalid Argo Workflows namespace identity")
    return expected


def validate_manifest(data: bytes, selection: dict[str, str]) -> None:
    """Check the pinned asset's topology after its byte identity is proven."""
    if hashlib.sha256(data).hexdigest() != selection["sha256"]:
        raise ValueError("Argo Workflows manifest SHA-256 mismatch")
    documents = list(yaml.safe_load_all(data))
    if Counter(document.get("kind") for document in documents) != KINDS:
        raise ValueError("Argo Workflows manifest resource inventory changed")
    resources = {(item["kind"], item["metadata"]["name"]): item for item in documents}
    if {name for kind, name in resources if kind == "CustomResourceDefinition"} != CRDS:
        raise ValueError("Argo Workflows CRD inventory changed")
    if {name for kind, name in resources if kind == "Role"} != {
        "argo-role",
        "argo-server-role",
    }:
        raise ValueError("Argo Workflows namespaced roles changed")
    if {name for kind, name in resources if kind == "RoleBinding"} != {
        "argo-binding",
        "argo-server-binding",
    }:
        raise ValueError("Argo Workflows namespaced bindings changed")
    for binding, role, account in (
        ("argo-binding", "argo-role", "argo"),
        ("argo-server-binding", "argo-server-role", "argo-server"),
    ):
        item = resources["RoleBinding", binding]
        if item["roleRef"] != {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": role,
        } or item["subjects"] != [{"kind": "ServiceAccount", "name": account}]:
            raise ValueError("Argo Workflows binding is not namespace-scoped")
    service = resources["Service", "argo-server"]
    if service["spec"].get("type", "ClusterIP") != "ClusterIP" or "externalIPs" in service["spec"]:
        raise ValueError("Argo Workflows server must remain internal")
    for name, account, image, args in (
        ("argo-server", "argo-server", "quay.io/argoproj/argocli", ["server", "--namespaced"]),
        (
            "workflow-controller",
            "argo",
            "quay.io/argoproj/workflow-controller",
            ["--namespaced"],
        ),
    ):
        deployment = resources["Deployment", name]
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        if (
            container.get("args") != args
            or container["image"] != f"{image}:{selection['version']}"
            or pod["serviceAccountName"] != account
        ):
            raise ValueError(f"Argo Workflows {name} is not pinned and namespaced")


def download_manifest(selection: dict[str, str]) -> bytes:
    """Fetch bounded official bytes; a changed release asset cannot be applied."""
    deadline = time.monotonic() + 120
    chunks: list[bytes] = []
    size = 0
    with urlopen(selection["url"], timeout=30) as response:
        while chunk := response.read(64 * 1024):
            size += len(chunk)
            if size > MAX_MANIFEST_BYTES:
                raise ValueError("Argo Workflows manifest exceeds the reviewed size limit")
            if time.monotonic() > deadline:
                raise ValueError("Argo Workflows manifest download timed out")
            chunks.append(chunk)
    data = b"".join(chunks)
    validate_manifest(data, selection)
    return data


def kubectl(state_root: Path) -> list[str]:
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
    return [*kube, "get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json"]


def validate_live_namespace(output: str, expected: dict[str, Any], *, allow_missing: bool) -> None:
    if not output.strip():
        if allow_missing:
            return
        raise ValueError("Argo Workflows namespace is missing")
    value = json.loads(output)
    if not isinstance(value, dict):
        raise ValueError("Invalid Argo Workflows namespace response")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid Argo Workflows namespace response")
    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
    if not isinstance(labels, dict):
        raise ValueError("Invalid Argo Workflows namespace response")
    if (
        value.get("apiVersion") != "v1"
        or value.get("kind") != "Namespace"
        or metadata.get("name") != NAMESPACE
        or any(labels.get(key) != item for key, item in expected["metadata"]["labels"].items())
    ):
        raise ValueError("Argo Workflows namespace has an unexpected owner or version")


def commands(action: str, root: Path = ROOT, state_root: Path = ROOT) -> list[list[str]]:
    if action not in {"bootstrap", "health"}:
        raise ValueError("Unknown Argo Workflows action")
    validate_namespace(root)
    kube = kubectl(state_root)
    result = [namespace_command(kube)]
    if action == "bootstrap":
        result.extend(
            [
                [*kube, "get", "crd", *sorted(CRDS), "--ignore-not-found", "-o", "name"],
                [
                    *kube,
                    "get",
                    "priorityclass",
                    "workflow-controller",
                    "--ignore-not-found",
                    "-o",
                    "name",
                ],
                [*kube, "apply", "--server-side", "-f", str(root / ADDON / "namespace.yaml")],
                [*kube, "apply", "--server-side", "-f", "-"],
                namespace_command(kube),
            ]
        )
    result.extend(
        [
            [*kube, "rollout", "status", f"deployment/{name}", "--timeout=300s"]
            for name in ("workflow-controller", "argo-server")
        ]
    )
    return result


def execute(action: str, root: Path = ROOT, state_root: Path = ROOT) -> None:
    state_root = local_kind.normalize_state_root(state_root)
    expected = validate_namespace(root)
    local_kind.execute("health", state_root)
    plan = commands(action, root, state_root)
    current = local_kind.run(plan[0])
    validate_live_namespace(current, expected, allow_missing=action == "bootstrap")
    if action == "bootstrap":
        selection = load_selection(root)
        data = download_manifest(selection)
        if not current.strip() and (
            local_kind.run(plan[1]).strip() or local_kind.run(plan[2]).strip()
        ):
            raise ValueError(
                "Workflow CRDs or priority class already exist without an owned namespace"
            )
        local_kind.run(plan[3])
        validate_live_namespace(local_kind.run(plan[0]), expected, allow_missing=False)
        local_kind.run(plan[4], input_data=data)
        validate_live_namespace(local_kind.run(plan[5]), expected, allow_missing=False)
    for command in plan[-2:]:
        local_kind.run(command)
    print(f"Local Argo Workflows action completed: {action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "asset-check", "bootstrap", "health"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--state-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            selection = load_selection()
            validate_namespace()
            print(f"Validated Argo Workflows {selection['version']} selection and namespace.")
        elif args.action == "asset-check":
            selection = load_selection()
            download_manifest(selection)
            print(f"Verified Argo Workflows {selection['version']} asset, topology and SHA-256.")
        elif args.execute:
            execute(args.action, state_root=args.state_root)
        else:
            state_root = local_kind.normalize_state_root(args.state_root)
            selection = load_selection()
            for command in commands(args.action, state_root=state_root):
                print(json.dumps(command))
            print(f"Manifest: {selection['url']} (SHA-256 {selection['sha256']})")
            print("Preview only. Add --execute to contact the selected kind cluster.")
    except (OSError, URLError, ValueError, TypeError, KeyError, yaml.YAMLError) as error:
        print(f"Local Argo Workflows error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
