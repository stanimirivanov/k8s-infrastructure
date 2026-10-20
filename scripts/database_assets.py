"""Pinned upstream database controllers; public configuration stays a ConfigMap."""

from __future__ import annotations

import base64
import concurrent.futures
import copy
import hashlib
import json
import re
from urllib.request import urlopen

import yaml

from .consumer_profile import LABEL, Target
from .local_kind import ROOT

DIRECTORY = ROOT / "clusters/local-kind/isolated-network-local/database"
MANAGER = "k8s-infrastructure-database"
STORAGE_CLASS = "local-retained"
MAX_BYTES = 16 * 1024 * 1024
CONTROLLERS = {
    "cert-manager-cainjector": ("cert-manager", "control-plane", "50m", "64Mi", "256Mi"),
    "cert-manager": ("cert-manager", "control-plane", "50m", "64Mi", "256Mi"),
    "cert-manager-webhook": ("cert-manager", "control-plane", "50m", "64Mi", "128Mi"),
    "cnpg-controller-manager": ("cnpg-system", "worker", "100m", "128Mi", "512Mi"),
    "barman-cloud": ("cnpg-system", "worker", "50m", "128Mi", "256Mi"),
}


def selection() -> dict:
    """Require official versioned assets and immutable images, including runtime helpers."""
    value = json.loads((DIRECTORY / "releases.json").read_text(encoding="utf-8"))
    sources = {
        "cnpg": "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/v{v}/releases/cnpg-{v}.yaml",
        "barman": "https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v{v}/manifest.yaml",
        "cert-manager": "https://github.com/cert-manager/cert-manager/releases/download/v{v}/cert-manager.yaml",
        "kind-storage": "https://raw.githubusercontent.com/kubernetes-sigs/kind/v{v}/pkg/build/nodeimage/const_storage.go",
    }
    if set(value) != {"assets", "images"} or set(value["assets"]) != set(sources):
        raise ValueError("Unexpected database asset inventory")
    for name, asset in value["assets"].items():
        if (
            set(asset) != {"version", "url", "sha256"}
            or not re.fullmatch(r"\d+\.\d+\.\d+", asset["version"])
            or asset["url"] != sources[name].format(v=asset["version"])
            or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])
        ):
            raise ValueError("Database assets require official versioned URLs and SHA-256")
    repositories = {
        "cnpg": "ghcr.io/cloudnative-pg/cloudnative-pg",
        "barman": "ghcr.io/cloudnative-pg/plugin-barman-cloud",
        "sidecar": "ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar",
        "postgres": "ghcr.io/cloudnative-pg/postgresql",
        "cert-manager": "quay.io/jetstack/cert-manager-controller",
        "cert-manager-cainjector": "quay.io/jetstack/cert-manager-cainjector",
        "cert-manager-webhook": "quay.io/jetstack/cert-manager-webhook",
        "acme-solver": "quay.io/jetstack/cert-manager-acmesolver",
        "kind-provisioner": "docker.io/kindest/local-path-provisioner",
        "kind-helper": "docker.io/kindest/local-path-helper",
    }
    if set(value["images"]) != set(repositories):
        raise ValueError("Unexpected database runtime image inventory")
    for name, repository in repositories.items():
        if not re.fullmatch(
            rf"{re.escape(repository)}:[\w.-]+@sha256:[a-f0-9]{{64}}", value["images"][name]
        ):
            raise ValueError("Database runtime images require reviewed repositories and digests")
    return value


def parse(data: bytes, asset: dict) -> list[dict]:
    """Check bytes before safe YAML parsing or Go-source inspection."""
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError("Database asset size or SHA-256 mismatch")
    return list(yaml.load_all(data, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)))


def download() -> dict[str, list[dict]]:
    release = selection()

    def fetch(pair: tuple[str, dict]) -> tuple[str, list[dict]]:
        name, asset = pair
        with urlopen(asset["url"], timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
        if name == "kind-storage":
            if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError("Kind storage source SHA-256 mismatch")
            text = data.decode("utf-8")
            for key in ("kind-provisioner", "kind-helper"):
                if release["images"][key].split("@")[0] not in text:
                    raise ValueError("Kind storage images differ from reviewed kind source")
            manifest = text.split("const defaultStorageManifest = `", 1)[1].rsplit("`", 1)[0]
            for variable, key in (
                ("storageProvisionerImage", "kind-provisioner"),
                ("storageHelperImage", "kind-helper"),
            ):
                manifest = manifest.replace(
                    "` + " + variable + " + `", release["images"][key].split("@")[0]
                )
            return name, list(
                yaml.load_all(manifest, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            )
        return name, parse(data, asset)

    print("Verifying pinned CNPG, Barman, cert-manager and kind storage assets...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(fetch, release["assets"].items()))


def labels(target: Target) -> dict[str, str]:
    digest = hashlib.sha256(json.dumps(selection(), sort_keys=True).encode()).hexdigest()[:32]
    return {
        "app.kubernetes.io/managed-by": "k8s-infrastructure",
        LABEL: target.consumer,
        "k8s-infrastructure.io/capability": "database",
        "k8s-infrastructure.io/database-selection": digest,
    }


def render(target: Target, assets: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Transform verified upstream resources, not a reimplementation of their operators."""
    release = selection()
    images = release["images"]
    mapping = {image.split("@")[0]: image for image in images.values()}
    result: dict[str, list[dict]] = {}
    for name in ("cert-manager", "cnpg", "barman"):
        documents = copy.deepcopy(assets[name])
        bound = {
            d["roleRef"]["name"]
            for d in documents
            if d["kind"] in {"ClusterRoleBinding", "RoleBinding"}
        }
        selected = []
        for item in documents:
            kind, metadata = item["kind"], item["metadata"]
            if kind == "ClusterRole" and metadata["name"] not in bound:
                continue  # Public editor/viewer convenience roles are not provider authority.
            metadata["labels"] = {
                key: value
                for key, value in metadata.get("labels", {}).items()
                if not key.startswith("rbac.authorization.k8s.io/aggregate-to-")
            }
            metadata["labels"].update(labels(target))
            if kind == "Secret":
                if name != "barman" or set(item.get("data", {})) != {"SIDECAR_IMAGE"}:
                    raise ValueError("Unexpected upstream Secret; no credential import")
                original = base64.b64decode(
                    "".join(item["data"]["SIDECAR_IMAGE"].split()), validate=True
                ).decode()
                if original != images["sidecar"].split("@")[0]:
                    raise ValueError("Barman sidecar source differs")
                item["kind"] = "ConfigMap"
                item.pop("type", None)
                item["data"] = {"SIDECAR_IMAGE": images["sidecar"]}
            if kind == "Service":
                item["spec"].setdefault("type", "ClusterIP")
            if kind == "Deployment":
                configure_controller(item, target, mapping)
            selected.append(item)
        result[name] = selected
    crds = [d for docs in result.values() for d in docs if d["kind"] == "CustomResourceDefinition"]
    deployments = [d for docs in result.values() for d in docs if d["kind"] == "Deployment"]
    if len(crds) != 18 or {d["metadata"]["name"] for d in deployments} != set(CONTROLLERS):
        raise ValueError("Database upstream API/controller inventory differs")
    if any(d["kind"] == "Secret" for docs in result.values() for d in docs):
        raise ValueError("Database source must not contain credential resources")
    identities = [
        (d["kind"], d["metadata"]["name"], d["metadata"].get("namespace"))
        for docs in result.values()
        for d in docs
    ]
    if len(set(identities)) != len(identities):
        raise ValueError("Database resources must have disjoint ownership")
    return result


def configure_controller(item: dict, target: Target, mapping: dict[str, str]) -> None:
    name = item["metadata"]["name"]
    namespace, role, cpu, memory, limit = CONTROLLERS[name]
    if item["metadata"].get("namespace") != namespace or item["spec"].get("replicas", 1) != 1:
        raise ValueError("Database controller topology differs")
    item["spec"]["replicas"] = 1
    pod = item["spec"]["template"]["spec"]
    if (
        len(pod["containers"]) != 1
        or pod.get("initContainers")
        or any(pod.get(key) for key in ("hostNetwork", "hostPID", "hostIPC"))
        or pod.get("securityContext", {}).get("runAsNonRoot") is not True
        or any("hostPath" in volume for volume in pod.get("volumes", []))
    ):
        raise ValueError("Unexpected database controller pod authority")
    pod["nodeSelector"] = {LABEL: target.consumer, "k8s-infrastructure.io/role": role}
    pod["tolerations"] = (
        [
            {
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "Exists",
                "effect": "NoSchedule",
            }
        ]
        if role == "control-plane"
        else []
    )
    container = pod["containers"][0]
    security = container.get("securityContext", {})
    if (
        security.get("allowPrivilegeEscalation") is not False
        or security.get("privileged")
        or security.get("readOnlyRootFilesystem") is not True
        or security.get("capabilities", {}).get("drop") != ["ALL"]
        or security.get("capabilities", {}).get("add")
    ):
        raise ValueError("Unexpected database controller container authority")
    container["image"] = mapping[container["image"]]
    container["resources"] = {
        "requests": {"cpu": cpu, "memory": memory},
        "limits": {"cpu": "500m", "memory": limit},
    }
    for env in container.get("env", []):
        if env["name"] == "OPERATOR_IMAGE_NAME":
            env["value"] = container["image"]
        reference = env.get("valueFrom", {})
        if env["name"] == "SIDECAR_IMAGE":
            reference["configMapKeyRef"] = reference.pop("secretKeyRef")
    container["args"] = [
        "--log-level=info" if value == "--log-level=debug" else value
        for value in container.get("args", [])
    ]
    for index, value in enumerate(container["args"]):
        if value.startswith("--acme-http01-solver-image="):
            container["args"][index] = (
                "--acme-http01-solver-image=" + selection()["images"]["acme-solver"]
            )


def storage_class(target: Target) -> dict:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": STORAGE_CLASS, "labels": labels(target)},
        "provisioner": "rancher.io/local-path",
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": False,
    }
