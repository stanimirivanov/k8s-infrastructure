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
LABEL = "k8s-infrastructure.io/consumer"


def load_profile(path: Path = PROFILE) -> dict[str, Any]:
    """Accept only the reviewed budget/topology; external arbitrary profiles are unsupported."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = {
        "version": 1,
        "profile": "isolated-local",
        "kindVersion": "v0.31.0",
        "nodeImage": NODE_IMAGE,
        "minimumEngine": {"cpus": 4, "memoryBytes": 12 * 1024**3},
        "nodes": {
            "control-plane": {"cpus": 1, "memoryBytes": 2 * 1024**3},
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

    def __post_init__(self) -> None:
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
    """No host mounts, workload namespaces, fixed host ports or Perfeng placement labels."""
    profile = load_profile()
    return {
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


def identity(target: Target) -> dict[str, str]:
    """Bind semantics canonically, independently of platform checkout line endings."""
    encoded = json.dumps(load_profile(), sort_keys=True, separators=(",", ":")).encode()
    return {
        "format": "isolated-local-owner.v1",
        "consumer": target.consumer,
        "name": target.name,
        "context": target.context,
        "dockerContext": target.docker_context,
        "profile": "isolated-local",
        "profileDigest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }
