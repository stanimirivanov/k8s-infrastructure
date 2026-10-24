"""Explicit, ownership-checked allocatable reconciliation for isolated kind nodes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml

from . import consumer_lifecycle as lifecycle
from . import consumer_runtime as runtime
from .consumer_profile import LABEL, PROFILES, Target, load_profile
from .node_capacity import (
    POLICY,
    AllocatableMismatch,
    envelope,
    kubelet_configuration,
    pod_requests,
    verify_allocatable,
)

# Fixed provider-owned paths; never interpolate target names or configuration into
# shell text. The only positional input is a validated SHA-256, checked again
# immediately before replacement. Backups stay inside the original owned node.
# Keep each precondition standalone: errexit ignores non-final failures in && lists.
READ = """set -eu
p=/var/lib/kubelet/config.yaml
test ! -L /var/lib/kubelet
test ! -L "$p"
test -f "$p"
test "$(stat -c %s "$p")" -le 65536
sha256sum "$p"
base64 --wrap=0 "$p"
"""
WRITE = """set -eu
p=/var/lib/kubelet/config.yaml
b=/var/lib/kubelet/config.before-allocatable.yaml
n=/var/lib/kubelet/config.allocatable-next
umask 077
set -C
test ! -L /var/lib/kubelet
test ! -L "$p"
test -f "$p"
test "$(sha256sum "$p" | cut -d ' ' -f 1)" = "$1"
test ! -e "$b"
test ! -L "$b"
test ! -e "$n"
test ! -L "$n"
cat "$p" > "$b"
cat > "$n"
chmod 600 "$n"
test "$(sha256sum "$p" | cut -d ' ' -f 1)" = "$1"
mv -T -- "$n" "$p"
"""


def preview(target: Target, action: str = "apply") -> dict:
    """Pure preview: no command, state-file read or target mutation."""
    return {
        "action": action,
        "cluster": target.name,
        "context": target.context,
        "dockerContext": target.docker_context,
        "capacityPolicy": POLICY,
        "podEnvelopes": {
            role: envelope(b) for role, b in load_profile(name=target.profile)["nodes"].items()
        },
        "rawCapacity": "host-derived; not rewritten or usable as a workload budget",
        "effects": "Require a foundation-only target and quiesced workload scheduling/configuration; verify original ownership; preflight both configs; atomically replace changed kubelet configs, retain originals, restart only nodes needing reconciliation; verify allocatable",
        "exclusions": [
            "Docker resize",
            "cluster replacement",
            "profile/owner migration",
            "consumer workloads",
        ],
        "execution": "not-performed",
    }


def observations(target: Target) -> dict[str, dict]:
    """Read only the exact two nodes and reject pressure, placement or UID gaps."""
    data = json.loads(
        runtime.kubectl(target, ["--request-timeout=30s", "get", "nodes", "-o", "json"])
    )
    roles = load_profile(name=target.profile)["nodes"]
    expected = {f"{target.name}-{role}": role for role in roles}
    items = data.get("items", [])
    if len(items) != 2 or {n.get("metadata", {}).get("name") for n in items} != set(expected):
        raise ValueError("Unexpected capacity target topology")
    result = {}
    for item in items:
        meta = item["metadata"]
        role = expected[meta["name"]]
        labels = meta.get("labels", {})
        conditions = {c["type"]: c["status"] for c in item.get("status", {}).get("conditions", [])}
        if (
            not meta.get("uid")
            or labels.get("kubernetes.io/os") != "linux"
            or labels.get("kubernetes.io/arch") != "amd64"
            or item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion") != "v1.35.0"
            or labels.get(LABEL) != target.consumer
            or labels.get("k8s-infrastructure.io/role") != role
            or any(
                conditions.get(k) != "False"
                for k in ("MemoryPressure", "DiskPressure", "PIDPressure")
            )
        ):
            raise ValueError("Capacity target identity or pressure is unsuitable")
        result[role] = item
    return result


def read_configuration(target: Target, node_id: str) -> tuple[str, dict]:
    raw = runtime.command(
        target, ["docker", "--context", target.docker_context, "exec", node_id, "sh", "-ceu", READ]
    )
    digest_line, separator, body = raw.partition("\n")
    digest = digest_line.split()[0] if digest_line.split() else ""
    if not separator or not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("Cannot bind original kubelet configuration")
    content = base64.b64decode(body, validate=True)
    if len(content) > 65536 or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Kubelet config changed during bounded preflight read")
    config = yaml.safe_load(content.decode("utf8"))
    if not isinstance(config, dict):
        raise ValueError("Unexpected kubelet configuration")
    return digest, config


def wait_allocatable(target: Target) -> dict[str, Any]:
    """A finite observation wait; API/identity errors fail immediately."""
    deadline = time.monotonic() + 120
    budgets = load_profile(name=target.profile)["nodes"]
    while True:
        nodes = observations(target)
        try:
            return {
                role: verify_allocatable(node["status"], budgets[role])
                for role, node in nodes.items()
            }
        except AllocatableMismatch:
            if time.monotonic() >= deadline:
                raise ValueError(
                    "Kubelet allocatable did not converge; retain configs and inspect target"
                ) from None
            time.sleep(2)


def preflight_workloads(target: Target) -> None:
    """Refuse repair after consumer/controller activation or above the new envelope.

    This first repair capability is intentionally foundation-only. Operators must
    quiesce scheduling/config changes for the approved window; observations are
    not an atomic admission lock. Never evict applications as a side effect.
    """
    allowed = {"kube-system", "local-path-storage", "cert-manager", "cnpg-system", "cilium-secrets"}
    controllers = json.loads(
        runtime.kubectl(
            target,
            [
                "--request-timeout=30s",
                "get",
                "deployments,statefulsets,daemonsets,jobs,cronjobs",
                "-A",
                "-o",
                "json",
            ],
        )
    )
    if any(
        c.get("metadata", {}).get("namespace") not in allowed for c in controllers.get("items", [])
    ):
        raise ValueError(
            "Consumer controllers present; capacity repair needs a reviewed maintenance path"
        )
    pods = json.loads(
        runtime.kubectl(target, ["--request-timeout=30s", "get", "pods", "-A", "-o", "json"])
    )
    active = [
        p
        for p in pods.get("items", [])
        if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
    ]
    if any(p.get("metadata", {}).get("namespace") not in allowed for p in active):
        raise ValueError("Consumer Pods present; initial capacity repair refused")
    for role, budget in load_profile(name=target.profile)["nodes"].items():
        limits = envelope(budget)
        selected = [
            p
            for p in active
            if p.get("spec", {}).get("nodeName") in (None, "", f"{target.name}-{role}")
        ]
        requests = [pod_requests(p) for p in selected]
        if (
            sum(r["cpu"] for r in requests) > limits["cpuMillicores"]
            or sum(r["memory"] for r in requests) > limits["memoryBytes"]
        ):
            raise ValueError(
                "Existing or pending Pod requests exceed the corrected scheduler envelope"
            )
    # CNPG's Cluster is an operator-managed desired state, not a StatefulSet.
    # Refuse it even before its first Pod exists. Missing CRD is a known absence;
    # a discovery/authorization failure must never be interpreted as absence.
    api = runtime.kubectl(
        target,
        [
            "--request-timeout=30s",
            "get",
            "crd",
            "clusters.postgresql.cnpg.io",
            "--ignore-not-found",
            "-o",
            "name",
        ],
    )
    if api.strip():
        clusters = json.loads(
            runtime.kubectl(
                target,
                ["--request-timeout=30s", "get", "clusters.postgresql.cnpg.io", "-A", "-o", "json"],
            )
        )
        if clusters.get("items"):
            raise ValueError(
                "Consumer database desired state present; foundation-only repair refused"
            )


def verify_owner(target: Target) -> dict:
    record = lifecycle.owned(target, require_ready=False)
    if record.get("phase") not in {"ready", "provisioned", "partial"}:
        raise ValueError("Capacity repair refuses creation reservation or deleting state")
    return record


def configure(target: Target) -> dict:
    """Explicit repair also works during owned provisioning, before CNI readiness.

    Preflight both nodes before the first write. A partial write/restart failure
    stops; never roll back, delete a backup, rewrite the owner record or resize.
    Re-execution skips a healthy unchanged node and can restart an already-written
    config whose prior restart did not complete. Foreign reservations are refused.
    """
    original_owner = verify_owner(target)
    containers = runtime.node_identity(target)
    runtime.verify_nodes(containers, profile=target.profile)
    nodes = observations(target)
    preflight_workloads(target)
    budgets = load_profile(name=target.profile)["nodes"]
    plans = []
    for role, node in nodes.items():
        container = containers[node["metadata"]["name"]]
        node_id = container["id"]
        if not re.fullmatch("[a-f0-9]{64}", node_id):
            raise ValueError("Invalid owned node container identity")
        digest, current = read_configuration(target, node_id)
        desired = kubelet_configuration(current, node["status"].get("capacity", {}), budgets[role])
        if len((json.dumps(desired) + "\n").encode()) > 65536:
            raise ValueError("Corrected kubelet configuration exceeds bounded file size")
        try:
            verify_allocatable(node["status"], budgets[role])
            converged = True
        except AllocatableMismatch:
            converged = False
        plans.append((role, node["metadata"]["uid"], node_id, digest, current, desired, converged))
    changed = []
    for role, uid, node_id, digest, current, desired, converged in plans:
        if current == desired and converged:
            continue
        verify_owner(target)
        preflight_workloads(target)
        if observations(target)[role]["metadata"]["uid"] != uid:
            raise ValueError("Node UID changed before capacity reconciliation")
        observed_digest, observed_config = read_configuration(target, node_id)
        if (observed_digest, observed_config) != (digest, current):
            raise ValueError("Kubelet configuration changed after capacity preflight")
        if current != desired:
            runtime.command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "exec",
                    "-i",
                    node_id,
                    "sh",
                    "-ceu",
                    WRITE,
                    "capacity-config",
                    digest,
                ],
                input_data=(json.dumps(desired) + "\n").encode(),
            )
        runtime.command(
            target,
            [
                "docker",
                "--context",
                target.docker_context,
                "exec",
                node_id,
                "systemctl",
                "restart",
                "kubelet",
            ],
        )
        changed.append(role)
    result = wait_allocatable(target)
    verify_owner(target)
    runtime.verify_nodes(runtime.node_identity(target), profile=target.profile)
    final = observations(target)
    if any(final[role]["metadata"]["uid"] != uid for role, uid, *_ in plans):
        raise ValueError("Node UID changed while waiting for allocatable")
    if original_owner["phase"] == "ready":
        lifecycle.health(target)
    return {
        "cluster": target.name,
        "incarnation": runtime.incarnation(target),
        "capacityPolicy": POLICY,
        "restartedNodes": changed,
        "nodes": result,
        "backupReference": "/var/lib/kubelet/config.before-allocatable.yaml on changed owned nodes",
        "dockerBudgetsChanged": False,
        "execution": "performed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "health", "qualify"))
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = Target(
            args.consumer, args.docker_context, args.state_root, args.profile, args.https_port
        )
        if args.action == "qualify":
            from .capacity_qualification import preview as qualification_preview
            from .capacity_qualification import qualify

            result = qualify(target) if args.execute else qualification_preview(target)
        elif not args.execute:
            result = preview(target, args.action)
        elif args.action == "apply":
            result = configure(target)
        else:
            lifecycle.health(target)
            budgets = load_profile(name=target.profile)["nodes"]
            result = {
                role: verify_allocatable(n["status"], budgets[role])
                for role, n in observations(target).items()
            }
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, yaml.YAMLError, TypeError, KeyError):
        print(
            "Capacity reconciliation refused or incomplete; retain node configs/backups and inspect the selected target; private details withheld."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
