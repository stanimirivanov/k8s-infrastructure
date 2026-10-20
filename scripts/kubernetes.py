"""Shared bounded Kubernetes command and structural comparison primitives.

Capability modules verify ownership and incarnation before mutations. This
adapter never selects an ambient kubeconfig or prints private response data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import consumer_runtime
from .consumer_profile import Target


def kube(
    target: Target,
    args: list[str],
    documents: list[dict] | None = None,
    *,
    request_timeout: int = 30,
) -> str:
    command = [
        "kubectl",
        "--kubeconfig",
        str(target.path("kubeconfig")),
        "--context",
        target.context,
        f"--request-timeout={request_timeout}s",
        *args,
    ]
    return consumer_runtime.command(
        target,
        command,
        input_data=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}).encode()
        if documents is not None
        else None,
    )


def fingerprint(item: dict) -> str:
    """Bind the desired shared API schema; account for Kubernetes CRD defaults."""
    value = (
        dict(item.get("spec", {}))
        if item["kind"] == "CustomResourceDefinition"
        else {
            key: item.get(key)
            for key in ("value", "globalDefault", "description", "preemptionPolicy")
        }
    )
    if item["kind"] == "CustomResourceDefinition":
        value.setdefault("preserveUnknownFields", False)
        value.setdefault("conversion", {"strategy": "None"})
    else:
        value["globalDefault"] = value["globalDefault"] or False
        value["preemptionPolicy"] = value["preemptionPolicy"] or "PreemptLowerPriority"
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def contains(expected: Any, actual: Any) -> bool:
    """Allow API omission of empty arrays/defaults, never extra entries or changed values."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            (key in actual and contains(value, actual[key]))
            or (key not in actual and isinstance(value, list) and not value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(contains(a, b) for a, b in zip(expected, actual, strict=True))
        )
    return expected == actual


def reference(item: dict[str, Any]) -> tuple[str, str, str | None]:
    return item["kind"], item["metadata"]["name"], item["metadata"].get("namespace")
