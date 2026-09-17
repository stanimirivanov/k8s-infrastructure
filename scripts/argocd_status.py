"""Bounded readiness checks and safe diagnostics for Argo CD controllers."""

from __future__ import annotations

import json
import re
from typing import Any

from . import local_kind

ROLLOUT_TIMEOUT = 300
DEPLOYMENTS = (
    "argocd-applicationset-controller",
    "argocd-dex-server",
    "argocd-notifications-controller",
    "argocd-redis",
    "argocd-repo-server",
    "argocd-server",
)
STATEFULSETS = ("argocd-application-controller",)
URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")


def mapping(value: Any) -> dict[str, Any]:
    """Return an external object mapping or an empty diagnostic view."""
    return value if isinstance(value, dict) else {}


def rollout_commands(kube: list[str]) -> list[list[str]]:
    """Build bounded rollout checks for the reviewed non-HA installation."""
    commands = [
        [*kube, "rollout", "status", f"deployment/{name}", f"--timeout={ROLLOUT_TIMEOUT}s"]
        for name in DEPLOYMENTS
    ]
    commands.extend(
        [*kube, "rollout", "status", f"statefulset/{name}", f"--timeout={ROLLOUT_TIMEOUT}s"]
        for name in STATEFULSETS
    )
    return commands


def diagnostic_json(command: list[str]) -> dict[str, Any] | None:
    """Read one diagnostic object without replacing the original failure."""
    try:
        output = local_kind.run(command)
        if not output.strip():
            return None
        value = json.loads(output)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def compact_message(value: Any, limit: int = 400) -> str:
    """Return bounded, redacted, single-line diagnostic text."""
    if not isinstance(value, str):
        return "unavailable"
    message = " ".join(value.split())
    message = URL_CREDENTIALS.sub(r"\1[redacted]@", message)
    message = TOKEN.sub("[redacted]", message)
    return message if len(message) <= limit else message[: limit - 3] + "..."


def pod_summary(value: dict[str, Any]) -> str:
    """Return concise readiness and restart evidence for one Pod."""
    metadata = mapping(value.get("metadata"))
    status = mapping(value.get("status"))
    containers = status.get("containerStatuses", [])
    if not isinstance(containers, list):
        containers = []
    valid_containers = [item for item in containers if isinstance(item, dict)]
    ready = sum(item.get("ready") is True for item in valid_containers)
    restarts = sum(
        item.get("restartCount", 0)
        for item in valid_containers
        if isinstance(item.get("restartCount", 0), int)
    )
    reasons = []
    for container in valid_containers:
        state = mapping(container.get("state"))
        for details in state.values():
            if isinstance(details, dict) and isinstance(details.get("reason"), str):
                reasons.append(details["reason"])
    reason = ",".join(sorted(set(reasons))) or "none"
    return (
        f"Pod {metadata.get('name', 'unknown')}: phase={status.get('phase', 'Unknown')}, "
        f"ready={ready}/{len(valid_containers)}, restarts={restarts}, reason={reason}"
    )


def workload_diagnostics(kube: list[str], kind: str, name: str) -> list[str]:
    """Read concise status for one controller and its Pods."""
    workload = diagnostic_json([*kube, "get", kind, name, "-o", "json"])
    lines = []
    if workload is None:
        lines.append(f"{kind.title()} {name}: status unavailable")
    else:
        spec = mapping(workload.get("spec"))
        status = mapping(workload.get("status"))
        lines.append(
            f"{kind.title()} {name}: desired={spec.get('replicas', 1)}, "
            f"updated={status.get('updatedReplicas', 0)}, "
            f"ready={status.get('readyReplicas', 0)}, "
            f"available={status.get('availableReplicas', status.get('currentReplicas', 0))}"
        )

    pods = diagnostic_json(
        [*kube, "get", "pods", "-l", f"app.kubernetes.io/name={name}", "-o", "json"]
    )
    items = [] if pods is None else pods.get("items", [])
    if not isinstance(items, list) or not items:
        lines.append(f"Pods for {name}: status unavailable")
    else:
        lines.extend(pod_summary(item) for item in items if isinstance(item, dict))
    return lines


def wait_for_workload(kube: list[str], kind: str, name: str) -> None:
    """Wait for one controller and diagnose a bounded rollout failure."""
    target = f"Argo CD {kind} {name}"
    print(f"Waiting for {target} rollout ({ROLLOUT_TIMEOUT}s)...", flush=True)
    try:
        local_kind.run(
            [*kube, "rollout", "status", f"{kind}/{name}", f"--timeout={ROLLOUT_TIMEOUT}s"]
        )
    except (OSError, ValueError) as error:
        for line in workload_diagnostics(kube, kind, name):
            print(line, flush=True)
        raise ValueError(
            f"{target} did not complete its rollout within {ROLLOUT_TIMEOUT}s"
        ) from error


def wait_for_argocd(kube: list[str]) -> None:
    """Wait for every controller in the reviewed non-HA installation."""
    for name in DEPLOYMENTS:
        wait_for_workload(kube, "deployment", name)
    for name in STATEFULSETS:
        wait_for_workload(kube, "statefulset", name)
