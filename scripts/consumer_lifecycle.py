"""Explicit lifecycle operations for one positively identified consumer cluster."""

from __future__ import annotations

from typing import Any

from . import consumer_runtime as runtime
from . import consumer_state as state
from .consumer_profile import (
    NETWORK_PROFILES,
    TRANSITION_TEST_PROFILE,
    Target,
    identity,
    load_profile,
)
from .local_kind import ROOT


def owned(target: Target, *, require_ready: bool = True) -> dict[str, Any]:
    """No mutation occurs until context, node IDs and incarnation all agree."""
    record = state.load(target)
    if require_ready and record["phase"] != "ready":
        raise ValueError("Consumer creation is incomplete; inspect retained state before recovery")
    if runtime.prerequisites(target) != record["endpoint"]:
        raise ValueError("Docker endpoint changed for the selected consumer")
    if target.name not in runtime.clusters(target):
        raise ValueError("Owned consumer cluster is missing; no automatic replacement")
    nodes = runtime.node_identity(target)
    if {name: node["id"] for name, node in nodes.items()} != record["nodeIds"]:
        raise ValueError("Consumer node containers changed; refusing adoption")
    runtime.verify_context(target)
    if not record["incarnation"] or runtime.incarnation(target) != record["incarnation"]:
        raise ValueError("Cluster incarnation changed or remains unobserved")
    if require_ready:
        runtime.verify_nodes(nodes, profile=target.profile)
    return record


def observe(target: Target, *, wait: bool = False) -> dict[str, Any]:
    """Return only public readiness/capacity metadata, never kubeconfig or credentials."""
    nodes = runtime.read_readiness(target, wait=wait)
    runtime.kubectl(
        target, ["-n", "kube-system", "rollout", "status", "deployment/coredns", "--timeout=60s"]
    )
    return {
        **identity(target),
        "incarnation": runtime.incarnation(target),
        "kubeconfigReference": str(target.path("kubeconfig")),
        **source_identity(target),
        "toolVersions": runtime.tools(target),
        "nodeAllocatable": nodes,
        "containerBudgets": load_profile(name=target.profile)["nodes"],
        "baseReadiness": "observed",
        **(
            {"testOnly": True, "qualificationScope": "disposable memory-transition protocol only"}
            if target.profile == TRANSITION_TEST_PROFILE
            else {}
        ),
        "capabilitiesNotQualified": [
            "shared-host performance isolation",
            "network policy",
            "trusted ingress",
            "Argo CD/Workflows",
            "persistent storage/database",
            "backup/restore",
            "consumer workload acceptance",
            *(
                ["full-installation capacity and performance"]
                if target.profile in {"isolated-network-full-local", TRANSITION_TEST_PROFILE}
                else []
            ),
        ],
    }


def source_identity(target: Target) -> dict[str, Any]:
    """Archive consumers bind provenance externally; do not discover a parent repository."""
    missing = {"providerRevision": None, "providerDirty": None, "sourceBinding": "caller-required"}
    if not (ROOT / ".git").exists():
        return missing
    try:
        revision = runtime.command(target, ["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()
        dirty = bool(
            runtime.command(target, ["git", "-C", str(ROOT), "status", "--porcelain"]).strip()
        )
        return {
            "providerRevision": revision,
            "providerDirty": dirty,
            "sourceBinding": "git-observed",
        }
    except (OSError, ValueError):
        return missing


def create(target: Target) -> dict[str, Any]:
    """Create only an absent target. Partial failures retain evidence, never auto-delete."""
    endpoint = runtime.prerequisites(target, creating=True)
    runtime.tools(target)
    if target.name in runtime.clusters(target):
        raise ValueError("Named consumer cluster already exists; refusing adoption")
    if target.profile in NETWORK_PROFILES:
        from .network_runtime import listener_available

        listener_available(target)
    record = state.reserve(target, endpoint)
    try:
        runtime.command(
            target,
            [
                "kind",
                "create",
                "cluster",
                "--name",
                target.name,
                "--config",
                str(target.path("kind.yaml")),
                "--kubeconfig",
                str(target.path("kubeconfig")),
                "--wait",
                # Custom-CNI nodes cannot be Ready until the next bootstrap
                # phase. The original Ready gate runs after Cilium installation.
                "0s" if target.profile in NETWORK_PROFILES else "300s",
                "--retain",
            ],
        )
        runtime.verify_context(target)
        nodes = runtime.node_identity(target)
        record["nodeIds"] = {name: node["id"] for name, node in nodes.items()}
        record["incarnation"] = runtime.incarnation(target)
        record["phase"] = "provisioned"
        state.save(target, record)
        for node in nodes.values():
            budget = load_profile(name=target.profile)["nodes"][node["role"]]
            runtime.command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "update",
                    "--cpus",
                    str(budget["cpus"]),
                    "--memory",
                    str(budget["memoryBytes"]),
                    "--memory-swap",
                    str(budget["memoryBytes"]),
                    "--pids-limit",
                    "2048",
                    node["id"],
                ],
            )
        runtime.verify_nodes(runtime.node_identity(target), profile=target.profile)
        from .consumer_capacity import configure

        configure(target)
        if target.profile in NETWORK_PROFILES:
            from .network_runtime import install

            install(target)
        return finish_creation(target, record)
    except Exception:
        record["phase"] = "partial"
        state.save(target, record)
        raise


def finish_creation(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    """Mark ready only after CNI bootstrap and the original base readiness checks."""
    runtime.kubectl(target, ["wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s"])
    result = observe(target, wait=True)
    record["phase"] = "ready"
    state.save(target, record)
    return result


def health(target: Target) -> dict[str, Any]:
    owned(target)
    return observe(target)


def ensure(target: Target) -> dict[str, Any]:
    """Healthy exact-owner reuse only; orphaned state and foreign clusters are refused."""
    runtime.prerequisites(target)
    existing = target.name in runtime.clusters(target)
    has_state = target.path("owner.json").exists()
    if existing and has_state:
        return health(target)
    if existing or any(
        target.path(suffix).exists() for suffix in ("owner.json", "kubeconfig", "kind.yaml")
    ):
        raise ValueError("Incomplete or foreign consumer identity; refusing adoption/repair")
    return create(target)


def down(target: Target, confirmation: str | None) -> None:
    """Deletion is permanent and separate from health, ensure and failed-create handling."""
    if confirmation != target.name:
        raise ValueError(
            "Deletion requires exact --confirm-delete target name; cluster-local data is lost"
        )
    record = state.load(target)
    if record["phase"] != "deleting":
        record = owned(target, require_ready=False)
        record["phase"] = "deleting"
        state.save(target, record)
    elif runtime.prerequisites(target) != record["endpoint"]:
        raise ValueError("Docker endpoint changed during removal recovery")
    expected = {f"{target.name}-control-plane", f"{target.name}-worker"}
    if set(record["nodeIds"]) != expected:
        raise ValueError("Removal journal lacks the complete verified node identity")
    for name, container_id in record["nodeIds"].items():
        if (
            not isinstance(container_id, str)
            or len(container_id) != 64
            or any(char not in "0123456789abcdef" for char in container_id)
        ):
            raise ValueError("Invalid immutable node identity in removal journal")
        present = runtime.command(
            target,
            [
                "docker",
                "--context",
                target.docker_context,
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"id={container_id}",
                "--format",
                "{{.ID}}",
            ],
        ).strip()
        if not present:
            continue
        if present != container_id:
            raise ValueError("Node identity changed during removal")
        if runtime.inspect_node(target, container_id, name)["id"] != container_id:
            raise ValueError("Removal journal node identity changed")
        # Never ask kind to rediscover containers by a possibly reused cluster name.
        runtime.command(
            target,
            [
                "docker",
                "--context",
                target.docker_context,
                "rm",
                "--force",
                "--volumes",
                container_id,
            ],
        )
    if target.name in runtime.clusters(target):
        raise ValueError("A named cluster remains; never delete replacement containers")
    _finish_removal(target)


def _finish_removal(target: Target) -> None:
    # The Kubernetes API is gone. Prove none of the recorded containers survived.
    remaining = runtime.command(
        target,
        [
            "docker",
            "--context",
            target.docker_context,
            "ps",
            "--all",
            "--filter",
            f"label=io.x-k8s.kind.cluster={target.name}",
            "--format",
            "{{.ID}}",
        ],
    ).strip()
    if remaining:
        raise ValueError("Owned node containers remain; retaining private ownership state")
    state.remove(target)
