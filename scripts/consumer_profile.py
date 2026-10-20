"""Pure selection of the reviewed isolated local-kind consumer profile."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .local_kind import NODE_IMAGE, ROOT, normalize_state_root

PROFILE = ROOT / "clusters/local-kind/isolated-local/profile.yaml"
PROFILES = ("isolated-local", "isolated-argo-local", "isolated-network-local")
LABEL = "k8s-infrastructure.io/consumer"


def load_profile(path: Path | None = None, *, name: str = "isolated-local") -> dict[str, Any]:
    """Accept only the reviewed budget/topology; external arbitrary profiles are unsupported."""
    if name not in PROFILES:
        raise ValueError("Unknown reviewed isolated consumer profile")
    selected = path or ROOT / "clusters/local-kind" / name / "profile.yaml"
    value = yaml.safe_load(selected.read_text(encoding="utf-8"))
    expected = {
        "version": 1,
        "profile": name,
        "kindVersion": "v0.31.0",
        "nodeImage": NODE_IMAGE,
        "minimumEngine": {"cpus": 4, "memoryBytes": 12 * 1024**3},
        "nodes": {
            "control-plane": {
                "cpus": 1 if name == "isolated-local" else 2,
                "memoryBytes": (2 if name == "isolated-local" else 4) * 1024**3,
            },
            "worker": {"cpus": 2, "memoryBytes": 4 * 1024**3},
        },
    }
    try:
        matches = json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(
            expected, sort_keys=True
        )
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError("Isolated consumer profile differs from reviewed topology/budgets")
    return value


@dataclass(frozen=True)
class Target:
    """An explicit consumer, local Docker context and private state boundary."""

    consumer: str
    docker_context: str
    state_root: Path
    profile: str = "isolated-local"
    https_port: int | None = None

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError("Unknown reviewed isolated consumer profile")
        if self.profile == "isolated-network-local":
            if type(self.https_port) is not int or not 1024 <= self.https_port <= 65535:
                raise ValueError("Networking profile requires explicit HTTPS host port 1024-65535")
        elif self.https_port is not None:
            raise ValueError("Existing profiles do not accept an HTTPS listener or CNI change")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", self.consumer):
            raise ValueError("Consumer must be a lowercase slug")
        if len(self.consumer) > 32 or self.consumer in {"perfeng", "perfeng-local"}:
            raise ValueError("Reserved or oversized consumer identity")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", self.docker_context):
            raise ValueError("Explicit named Docker context required")
        object.__setattr__(self, "state_root", normalize_state_root(self.state_root))

    @property
    def name(self) -> str:
        return f"{self.consumer}-local"

    @property
    def context(self) -> str:
        return f"kind-{self.name}"

    @property
    def directory(self) -> Path:
        return self.state_root / ".local"

    def path(self, suffix: str) -> Path:
        if suffix not in {"kubeconfig", "owner.json", "kind.yaml"}:
            raise ValueError("Unknown consumer state path")
        path = self.directory / f"{self.name}.{suffix}"
        for candidate in (path, path.parent):
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError("Redirected consumer state refused")
            if candidate.exists() and candidate.resolve() != candidate:
                raise ValueError("Redirected consumer state refused")
        return path


def configuration(target: Target) -> dict[str, Any]:
    """No host mounts or Perfeng placement; only the network profile maps explicit loopback HTTPS."""
    profile = load_profile(name=target.profile)
    result: dict[str, Any] = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": target.name,
        "networking": {"apiServerAddress": "127.0.0.1"},
        "nodes": [
            {
                "role": role,
                "image": profile["nodeImage"],
                "labels": {LABEL: target.consumer, "k8s-infrastructure.io/role": role},
            }
            for role in profile["nodes"]
        ],
    }
    if target.profile == "isolated-network-local":
        result["networking"] = {
            **result["networking"],
            "disableDefaultCNI": True,
            "kubeProxyMode": "none",
        }
        result["nodes"][1]["extraPortMappings"] = [
            {
                "containerPort": 8443,
                "hostPort": target.https_port,
                "listenAddress": "127.0.0.1",
                "protocol": "TCP",
            }
        ]
    return result


def identity(target: Target) -> dict[str, str]:
    """Bind semantics canonically, independently of platform checkout line endings."""
    encoded = json.dumps(
        load_profile(name=target.profile), sort_keys=True, separators=(",", ":")
    ).encode()
    result = {
        "format": "isolated-local-owner.v1",
        "consumer": target.consumer,
        "name": target.name,
        "context": target.context,
        "dockerContext": target.docker_context,
        "profile": target.profile,
        "profileDigest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }
    if target.profile == "isolated-network-local":
        result["httpsPort"] = str(target.https_port)
    return result
