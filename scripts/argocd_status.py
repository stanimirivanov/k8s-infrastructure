"""Bounded waits and safe diagnostics for local Argo CD resources."""

from __future__ import annotations

import json
import re
from typing import Any

if __package__:
    from . import cluster
else:
    import cluster

ARGOCD_ROLLOUT_TIMEOUT = 300
APPLICATION_WAIT_TIMEOUT = 180
APPLICATION_GRACE_TIMEOUT = 60
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
GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")


def diagnostic_json(command: list[str]) -> dict[str, Any] | None:
    """Read one diagnostic object without replacing the original failure."""
    try:
        output = cluster.run(command)
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
    message = GITHUB_TOKEN.sub("[redacted]", message)
    return message if len(message) <= limit else message[: limit - 3] + "..."


def pod_summary(value: dict[str, Any]) -> str:
    """Return concise readiness and restart evidence for a Pod."""
    metadata = value.get("metadata", {})
    status = value.get("status", {})
    containers = status.get("containerStatuses", [])
    ready = sum(item.get("ready") is True for item in containers if isinstance(item, dict))
    restarts = sum(
        item.get("restartCount", 0)
        for item in containers
        if isinstance(item, dict) and isinstance(item.get("restartCount", 0), int)
    )
    reasons = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        state = container.get("state", {})
        if isinstance(state, dict):
            for details in state.values():
                if isinstance(details, dict) and isinstance(details.get("reason"), str):
                    reasons.append(details["reason"])
    reason = ",".join(sorted(set(reasons))) or "none"
    return (
        f"Pod {metadata.get('name', 'unknown')}: phase={status.get('phase', 'Unknown')}, "
        f"ready={ready}/{len(containers)}, restarts={restarts}, reason={reason}"
    )


def workload_diagnostics(kube: list[str], kind: str, name: str) -> list[str]:
    """Read concise status for one Argo CD workload and its Pods."""
    workload = diagnostic_json([*kube, "get", kind, name, "-o", "json"])
    lines = []
    if workload is None:
        lines.append(f"{kind.title()} {name}: status unavailable")
    else:
        spec = workload.get("spec", {})
        status = workload.get("status", {})
        desired = spec.get("replicas", 1)
        lines.append(
            f"{kind.title()} {name}: desired={desired}, "
            f"updated={status.get('updatedReplicas', 0)}, "
            f"ready={status.get('readyReplicas', 0)}, "
            f"available={status.get('availableReplicas', status.get('currentReplicas', 0))}"
        )
    pods = diagnostic_json(
        [
            *kube,
            "get",
            "pods",
            "-l",
            f"app.kubernetes.io/name={name}",
            "-o",
            "json",
        ]
    )
    items = [] if pods is None else pods.get("items", [])
    if not isinstance(items, list) or not items:
        lines.append(f"Pods for {name}: status unavailable")
    else:
        lines.extend(pod_summary(item) for item in items if isinstance(item, dict))
    return lines


def wait_for_workload(kube: list[str], kind: str, name: str) -> None:
    """Wait for one named Argo CD workload and diagnose rollout failure."""
    target = f"Argo CD {kind} {name}"
    print(f"Waiting for {target} rollout ({ARGOCD_ROLLOUT_TIMEOUT}s)...", flush=True)
    try:
        cluster.run(
            [
                *kube,
                "rollout",
                "status",
                f"{kind}/{name}",
                f"--timeout={ARGOCD_ROLLOUT_TIMEOUT}s",
            ]
        )
    except ValueError as error:
        for line in workload_diagnostics(kube, kind, name):
            print(line, flush=True)
        raise ValueError(
            f"{target} did not complete its rollout within {ARGOCD_ROLLOUT_TIMEOUT}s"
        ) from error


def wait_for_argocd(kube: list[str]) -> None:
    """Wait for every workload in the pinned non-HA installation."""
    for name in DEPLOYMENTS:
        wait_for_workload(kube, "deployment", name)
    for name in STATEFULSETS:
        wait_for_workload(kube, "statefulset", name)


def application_status(kube: list[str], name: str) -> dict[str, Any] | None:
    """Read one Application without failing if it is not created yet."""
    return diagnostic_json([*kube, "get", "application", name, "--ignore-not-found", "-o", "json"])


def application_diagnostics(name: str, value: dict[str, Any] | None) -> list[str]:
    """Return bounded status and condition messages for one Application."""
    if value is None:
        return [f"Application {name}: resource does not exist or status is unavailable"]
    status = value.get("status", {})
    sync = status.get("sync", {})
    health = status.get("health", {})
    operation = status.get("operationState", {})
    lines = [
        f"Application {name}: sync={sync.get('status', 'Unknown')}, "
        f"health={health.get('status', 'Unknown')}, "
        f"revision={sync.get('revision', 'unknown')}, "
        f"operation={operation.get('phase', 'none')}"
    ]
    conditions = status.get("conditions", [])
    if not isinstance(conditions, list) or not conditions:
        lines.append("Conditions: none reported")
    else:
        for condition in conditions:
            if isinstance(condition, dict):
                lines.append(
                    f"Condition {condition.get('type', 'Unknown')}: "
                    f"{compact_message(condition.get('message'))}"
                )
    if operation.get("message"):
        lines.append(f"Operation message: {compact_message(operation['message'])}")
    return lines


def application_reached(value: dict[str, Any] | None, field: str, expected: str) -> bool:
    """Return whether an Application reports one expected status value."""
    if value is None:
        return False
    return value.get("status", {}).get(field, {}).get("status") == expected


def wait_for_application_state(kube: list[str], name: str, field: str, expected: str) -> None:
    """Wait for Application state with one diagnosed, bounded grace period."""
    target = f"Application {name} to report {field}={expected}"
    command = [
        *kube,
        "wait",
        f"--for=jsonpath={{.status.{field}.status}}={expected}",
        f"application/{name}",
    ]
    print(f"Waiting for {target} ({APPLICATION_WAIT_TIMEOUT}s)...", flush=True)
    try:
        cluster.run([*command, f"--timeout={APPLICATION_WAIT_TIMEOUT}s"])
        return
    except ValueError:
        current = application_status(kube, name)
        for line in application_diagnostics(name, current):
            print(line, flush=True)
        if application_reached(current, field, expected):
            print(f"{target} as the primary wait expired; continuing.", flush=True)
            return
        print(
            f"Allowing one {APPLICATION_GRACE_TIMEOUT}s grace period for {target}...",
            flush=True,
        )
        try:
            cluster.run([*command, f"--timeout={APPLICATION_GRACE_TIMEOUT}s"])
            return
        except ValueError as grace_error:
            current = application_status(kube, name)
            for line in application_diagnostics(name, current):
                print(line, flush=True)
            raise ValueError(
                f"{target} failed after {APPLICATION_WAIT_TIMEOUT}s plus "
                f"{APPLICATION_GRACE_TIMEOUT}s grace"
            ) from grace_error


def wait_for_application(kube: list[str], name: str) -> None:
    """Require an Application to exist, synchronize and become healthy."""
    print(
        f"Waiting for Application {name} to be created ({APPLICATION_WAIT_TIMEOUT}s)...",
        flush=True,
    )
    try:
        cluster.run(
            [
                *kube,
                "wait",
                "--for=create",
                f"application/{name}",
                f"--timeout={APPLICATION_WAIT_TIMEOUT}s",
            ]
        )
    except ValueError as error:
        current = application_status(kube, name)
        for line in application_diagnostics(name, current):
            print(line, flush=True)
        if current is None:
            raise ValueError(f"Application {name} was not created") from error
        print(f"Application {name} appeared as the creation wait expired; continuing.", flush=True)
    wait_for_application_state(kube, name, "sync", "Synced")
    wait_for_application_state(kube, name, "health", "Healthy")
