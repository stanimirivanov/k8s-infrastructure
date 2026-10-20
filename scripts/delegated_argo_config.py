"""Pure, bounded consumer delegation and immutable controller asset selection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import yaml

from .consumer_profile import Target
from .consumer_state import read_bounded
from .local_kind import ROOT

SELECTION = ROOT / "clusters/local-kind/isolated-local/addons/argo/releases.json"
OWNER = "k8s-infrastructure"
LABEL = "k8s-infrastructure.io/consumer"
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Delegation:
    """Public operator-reviewed authority; never credentials or workload desired state."""

    consumer: str
    workload_namespaces: tuple[str, ...]
    approved_templates: tuple[str, ...]
    database_namespaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.database_namespaces)) != len(self.database_namespaces) or not set(
            self.database_namespaces
        ) <= set(self.workload_namespaces):
            raise ValueError("Database namespaces must be distinct opted-in workload namespaces")
        for values in (self.workload_namespaces, self.approved_templates):
            if not 1 <= len(values) <= 16 or len(set(values)) != len(values):
                raise ValueError("Delegation requires one to sixteen distinct names per boundary")
            if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", value) for value in values):
                raise ValueError("Delegation names must be ordinary bounded DNS labels")
        if any(
            not name.startswith(f"{self.consumer}-")
            or name in {self.gitops, self.controllers, self.managed}
            for name in self.workload_namespaces
        ):
            raise ValueError("Workload namespaces must be separate and consumer-prefixed")

    @property
    def gitops(self) -> str:
        return f"{self.consumer}-gitops-system"

    @property
    def controllers(self) -> str:
        return f"{self.consumer}-workflow-system"

    @property
    def managed(self) -> str:
        return f"{self.consumer}-releases"

    @property
    def digest(self) -> str:
        value = [self.consumer, self.workload_namespaces, self.approved_templates]
        if self.database_namespaces:
            value.append(self.database_namespaces)
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def load_delegation(path: Path, target: Target) -> Delegation:
    value = json.loads(read_bounded(path))
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {"consumer", "workloadNamespaces", "approvedTemplates"},
            {"consumer", "workloadNamespaces", "approvedTemplates", "databaseNamespaces"},
        )
        or value["consumer"] != target.consumer
        or any(
            not isinstance(value[key], list)
            or not all(isinstance(name, str) for name in value[key])
            for key in ("workloadNamespaces", "approvedTemplates")
        )
        or (
            "databaseNamespaces" in value
            and (
                not isinstance(value["databaseNamespaces"], list)
                or not all(isinstance(name, str) for name in value["databaseNamespaces"])
            )
        )
    ):
        raise ValueError(
            "Delegation must select the exact consumer and public namespace/template lists"
        )
    return Delegation(
        target.consumer,
        tuple(value["workloadNamespaces"]),
        tuple(value["approvedTemplates"]),
        tuple(value.get("databaseNamespaces", ())),
    )


def selection() -> dict[str, dict[str, str]]:
    value = json.loads(SELECTION.read_text(encoding="utf-8"))
    if set(value) != {"argocd", "argocd-apis", "workflows"}:
        raise ValueError("Unexpected delegated Argo asset inventory")
    for name, asset in value.items():
        version = asset.get("version", "")
        if name == "workflows":
            url = f"https://github.com/argoproj/argo-workflows/releases/download/{version}/namespace-install.yaml"
        else:
            file = "install.yaml" if name == "argocd-apis" else "namespace-install.yaml"
            url = f"https://raw.githubusercontent.com/argoproj/argo-cd/{version}/manifests/{file}"
        if (
            set(asset) != {"version", "url", "sha256"}
            or not re.fullmatch(r"v\d+\.\d+\.\d+", version)
            or asset["url"] != url
            or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])
        ):
            raise ValueError("Delegated Argo requires official versioned, SHA-256-pinned assets")
    if value["argocd"]["version"] != value["argocd-apis"]["version"]:
        raise ValueError("Argo CD controller and API versions must agree")
    return value


def parse_asset(data: bytes, asset: dict[str, str]) -> list[dict[str, Any]]:
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError("Delegated Argo asset size or SHA-256 mismatch")
    # The pinned CRD manifests are large. Prefer PyYAML's safe native parser
    # when available; the fallback retains safe loading without libyaml.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    documents = list(yaml.load_all(data, Loader=loader))
    if not documents or not all(isinstance(item, dict) for item in documents):
        raise ValueError("Invalid delegated Argo resource documents")
    return documents


def download_assets() -> dict[str, list[dict[str, Any]]]:
    """Network-only byte verification; never contact Kubernetes or write a cache."""
    result = {}
    for name, asset in selection().items():
        print(f"Verifying pinned {name} manifest...", flush=True)
        with urlopen(asset["url"], timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
        result[name] = parse_asset(data, asset)
    return result


def labels(delegation: Delegation, *, shared: bool = False) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": OWNER,
        "k8s-infrastructure.io/capability": "delegated-argo",
        "k8s-infrastructure.io/delegation": "shared" if shared else delegation.digest[:32],
        **({} if shared else {LABEL: delegation.consumer}),
    }
