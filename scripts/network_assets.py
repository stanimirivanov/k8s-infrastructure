"""Pinned Cilium/Gateway assets and deterministic, non-cluster Helm rendering."""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

import yaml

from . import network_resources
from .consumer_profile import LABEL, MINIMAL_PROFILE, Target
from .local_kind import ROOT

DIRECTORY = ROOT / "clusters/local-kind/isolated-network-local"
RELEASE_FILE = DIRECTORY / "releases.json"
OWNER = "k8s-infrastructure"
MANAGER = "k8s-infrastructure-network"
SELECTION_LABEL = "k8s-infrastructure.io/network-selection"
GATEWAY_CRDS = {
    f"{name}.gateway.networking.k8s.io"
    for name in (
        "gatewayclasses",
        "gateways",
        "httproutes",
        "grpcroutes",
        "referencegrants",
        "backendtlspolicies",
        "tlsroutes",
    )
}


def selection(data: bytes | None = None) -> dict:
    """Admit only reviewed official release URLs, bounded inventory and image digests."""
    value = json.loads(RELEASE_FILE.read_bytes() if data is None else data)
    if (
        set(value) != {"cilium", "gateway", "ciliumCRDs", "probes"}
        or len(value["ciliumCRDs"]) != 22
    ):
        raise ValueError("Unexpected networking release inventory")
    cilium, gateway = value["cilium"], value["gateway"]
    for item in (cilium, gateway):
        if not re.fullmatch(r"\d+\.\d+\.\d+", item["version"]):
            raise ValueError("Networking assets require exact release versions")
    if (
        cilium["url"] != f"https://helm.cilium.io/cilium-{cilium['version']}.tgz"
        or gateway["url"]
        != f"https://github.com/kubernetes-sigs/gateway-api/releases/download/v{gateway['version']}/standard-install.yaml"
    ):
        raise ValueError("Networking assets must use official release URLs")
    urls = set()
    for item in [cilium, gateway, *value["ciliumCRDs"]]:
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or item["url"] in urls:
            raise ValueError("Missing SHA-256 or duplicate networking asset")
        urls.add(item["url"])
    for item in value["ciliumCRDs"]:
        if not re.fullmatch(
            rf"https://raw\.githubusercontent\.com/cilium/cilium/v{re.escape(cilium['version'])}/pkg/k8s/apis/cilium\.io/client/crds/v2(?:alpha1)?/cilium[a-z0-9]+\.yaml",
            item["url"],
        ):
            raise ValueError("Foreign Cilium CRD source refused")
    if set(value["probes"]) != {"server", "client"}:
        raise ValueError("Unexpected neutral networking probes")
    for name, repository in (("server", "mccutchen/go-httpbin"), ("client", "curlimages/curl")):
        if not re.fullmatch(
            rf"docker\.io/{repository}:\d+\.\d+\.\d+@sha256:[a-f0-9]{{64}}", value["probes"][name]
        ):
            raise ValueError(
                "Neutral probe images require reviewed repository and immutable digest"
            )
    return value


def values_configuration() -> dict:
    """Reject undeclared hooks/arguments, exposure and host-mount changes before Helm."""
    values = yaml.safe_load((DIRECTORY / "cilium-values.yaml").read_text(encoding="utf-8"))
    if set(values) != {
        "kubeProxyReplacement",
        "k8sServicePort",
        "ipam",
        "cgroup",
        "operator",
        "resources",
        "envoy",
        "gatewayAPI",
        "hubble",
    }:
        raise ValueError("Unreviewed networking configuration fields")
    if (
        values["kubeProxyReplacement"] is not True
        or values["k8sServicePort"] != 6443
        or values["ipam"] != {"mode": "kubernetes"}
        or values["cgroup"] != {"autoMount": {"enabled": False}, "hostRoot": "/sys/fs/cgroup"}
        or values["gatewayAPI"]
        != {
            "enabled": True,
            "gatewayClass": {"create": "true"},
            "hostNetwork": {
                "enabled": True,
                "nodes": {"matchLabels": {"k8s-infrastructure.io/role": "worker"}},
            },
        }
        or values["hubble"]
        != {"enabled": False, "relay": {"enabled": False}, "ui": {"enabled": False}}
        or set(values["operator"]) != {"replicas", "skipCRDCreation", "resources"}
        or values["operator"]["replicas"] != 1
        or values["operator"]["skipCRDCreation"] is not True
        or set(values["envoy"]) != {"enabled", "resources"}
        or values["envoy"]["enabled"] is not True
    ):
        raise ValueError("Networking exposure, API ownership or host configuration differs")
    return values


def verified(data: bytes, asset: dict) -> bytes:
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError("Networking asset size or SHA-256 mismatch")
    return data


def download(*, release: dict | None = None) -> tuple[bytes, list[dict]]:
    """Separate network gate: every archive/manifest is checked before parsing/execution."""
    release = selection() if release is None else release
    assets = [release["cilium"], release["gateway"], *release["ciliumCRDs"]]

    def fetch(asset: dict) -> bytes:
        with urlopen(asset["url"], timeout=30) as response:
            return verified(response.read(32 * 1024 * 1024 + 1), asset)

    print("Verifying pinned Cilium chart, 22 Cilium CRDs and Gateway API bundle...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        data = list(pool.map(fetch, assets))
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    gateway = list(yaml.load_all(data[1], Loader=loader))
    selected = [item for item in gateway if item["metadata"]["name"] in GATEWAY_CRDS]
    cilium = [yaml.load(item, Loader=loader) for item in data[2:]]
    if (
        len(selected) != 7
        or len({item["metadata"]["name"] for item in [*selected, *cilium]}) != 29
        or any(item.get("kind") != "CustomResourceDefinition" for item in [*selected, *cilium])
    ):
        raise ValueError("Unexpected Gateway/Cilium API manifest inventory")
    return data[0], [*selected, *cilium]


def download_bounded(data: bytes, *, timeout: int) -> tuple[bytes, list[dict]]:
    """Put a wall-clock bound on the worker, including blocking HTTP reads."""
    with tempfile.TemporaryDirectory(prefix="network-download-") as directory:
        path = Path(directory)
        subprocess.run(
            [sys.executable, "-m", "scripts.network_assets", directory],
            input=data,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        return (path / "chart.tgz").read_bytes(), json.loads((path / "crds.json").read_bytes())


def labels(target: Target, *, release: dict | None = None) -> dict[str, str]:
    release = selection() if release is None else release
    reviewed = {key: value for key, value in release.items() if key != "probes"}
    reviewed["values"] = yaml.safe_load(
        (DIRECTORY / "cilium-values.yaml").read_text(encoding="utf-8")
    )
    if target.profile == MINIMAL_PROFILE:
        reviewed["boundedResources"] = network_resources.selection()
    digest = hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()[:32]
    return ownership_labels(target, digest)


def ownership_labels(target: Target, digest: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": OWNER,
        "k8s-infrastructure.io/capability": "network",
        LABEL: target.consumer,
        SELECTION_LABEL: digest,
    }


def render(
    target: Target,
    chart: bytes,
    crds: list[dict],
    *,
    release: dict | None = None,
    timeout: int = 60,
) -> tuple[list[dict], list[dict]]:
    """Use upstream Helm templates; no custom operator or Helm release Secret state."""
    release = selection() if release is None else release
    verified(chart, release["cilium"])
    values = values_configuration()
    values["k8sServiceHost"] = f"{target.name}-control-plane"
    values["nodeSelector"] = {LABEL: target.consumer}
    values["operator"]["nodeSelector"] = {
        LABEL: target.consumer,
        "k8s-infrastructure.io/role": "worker",
    }
    with tempfile.TemporaryDirectory(prefix="network-render-") as directory:
        path = Path(directory)
        (path / "cilium.tgz").write_bytes(chart)
        (path / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
        result = subprocess.run(
            [
                "helm",
                "template",
                "cilium",
                str(path / "cilium.tgz"),
                "--namespace",
                "kube-system",
                "--kube-version",
                "1.35.0",
                "--values",
                str(path / "values.yaml"),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise ValueError("Pinned networking Helm render failed; no cluster access performed")
    objects = list(
        yaml.load_all(result.stdout, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    )
    if not objects or any(
        not isinstance(item, dict) or item["kind"] in {"Secret", "CustomResourceDefinition"}
        for item in objects
    ):
        raise ValueError("Unexpected network render, Secret generation or competing API installer")
    documents = copy.deepcopy(objects)
    # skipCRDCreation disables the operator installer but the upstream chart
    # retains its schema-write RBAC. Remove that unused competing authority.
    for item in documents:
        if item["kind"] != "ClusterRole":
            continue
        rules = []
        for rule in item["rules"]:
            if "customresourcedefinitions" in rule.get("resources", []):
                if rule["resources"] != ["customresourcedefinitions"] or rule["apiGroups"] != [
                    "apiextensions.k8s.io"
                ]:
                    raise ValueError("Unexpected mixed CRD authority requires review")
                rule["verbs"] = [verb for verb in rule["verbs"] if verb in {"get", "list", "watch"}]
                if not rule["verbs"]:
                    continue
            rules.append(rule)
        item["rules"] = rules
    shared = copy.deepcopy(crds)
    ownership_labels = labels(target, release=release)
    for item in [*shared, *documents]:
        item["metadata"].setdefault("labels", {}).update(ownership_labels)
    with tarfile.open(fileobj=io.BytesIO(chart)) as archive:
        member = archive.getmember("cilium/values.yaml")
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("Unexpected pinned chart defaults")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("Pinned chart defaults are missing")
        defaults = yaml.load(stream.read(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    agent, operator, envoy = (
        defaults["image"],
        defaults["operator"]["image"],
        defaults["envoy"]["image"],
    )
    images = {
        f"{agent['repository']}:{agent['tag']}@{agent['digest']}",
        f"{operator['repository']}-generic:{operator['tag']}@{operator['genericDigest']}",
        f"{envoy['repository']}:{envoy['tag']}@{envoy['digest']}",
    }
    if target.profile == MINIMAL_PROFILE:
        network_resources.configure(target, network_resources.controllers(documents))
    validate_rendered(documents, images, minimal=target.profile == MINIMAL_PROFILE)
    if target.profile == MINIMAL_PROFILE:
        network_resources.rendered_bounds(target, documents)
    return shared, documents


def validate_rendered(documents: list[dict], images: set[str], *, minimal: bool = False) -> None:
    """Assert functional security controls in the actual upstream render."""
    by_name = {(item["kind"], item["metadata"]["name"]): item for item in documents}
    config = by_name["ConfigMap", "cilium-config"]["data"]
    if config.get("skip-crd-creation") != "true":
        raise ValueError("Cilium must use provider-installed schemas")
    budgets = {
        "cilium": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "1000m", "memory": "768Mi"},
        },
        "cilium-envoy": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        },
        "cilium-operator": {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "384Mi"},
        },
    }
    if minimal:
        budgets = network_resources.MAIN
    network_resources.controllers(documents)
    if by_name["Deployment", "cilium-operator"]["spec"]["replicas"] != 1:
        raise ValueError("Local network profile requires exactly one operator")
    for key, expected in {
        "kube-proxy-replacement": "true",
        "enable-gateway-api": "true",
        "enable-policy": "default",
    }.items():
        if config.get(key) != expected:
            raise ValueError(f"Cilium required configuration differs: {key}")
    for item in documents:
        if item["kind"] in {"Deployment", "DaemonSet"}:
            pod = item["spec"]["template"]["spec"]
            if (
                len(pod["containers"]) != 1
                or pod["containers"][0].get("resources") != budgets[item["metadata"]["name"]]
            ):
                raise ValueError(
                    "Network controller requests/limits differ from the reviewed envelope"
                )
            for container in [*pod["containers"], *pod.get("initContainers", [])]:
                if container["image"] not in images:
                    raise ValueError("Network images must retain upstream immutable digests")
        if item["kind"] == "Service" and (
            item["spec"].get("type", "ClusterIP") != "ClusterIP" or item["spec"].get("externalIPs")
        ):
            raise ValueError("Network control-plane Services must remain private")
        if item["kind"] == "ClusterRole":
            for rule in item["rules"]:
                if "customresourcedefinitions" in rule.get("resources", []) and set(
                    rule["verbs"]
                ) & {"create", "update", "patch", "delete", "*"}:
                    raise ValueError("Network controllers must not compete for provider-owned CRDs")
        if (
            item["kind"] in {"RoleBinding", "ClusterRoleBinding"}
            and item["roleRef"]["name"] == "cluster-admin"
        ):
            raise ValueError("Cluster-admin binding is not a network capability")
    if (
        by_name["GatewayClass", "cilium"]["spec"]["controllerName"]
        != "io.cilium/gateway-controller"
    ):
        raise ValueError("Unexpected Gateway implementation")


def download_worker(path: Path) -> None:
    """Exchange the bounded download through the parent-owned private directory."""
    chart, crds = download(release=selection(sys.stdin.buffer.read(64 * 1024 + 1)))
    (path / "chart.tgz").write_bytes(chart)
    (path / "crds.json").write_text(json.dumps(crds), encoding="utf-8")


if __name__ == "__main__":
    download_worker(Path(sys.argv[1]))
