"""Bounded scheduler/restart proof for an explicitly approved disposable kind target."""

from __future__ import annotations

import json
import time

from . import consumer_capacity as capacity
from . import consumer_lifecycle as lifecycle
from . import consumer_runtime as runtime
from .consumer_profile import LABEL, Target, load_profile
from .kubernetes import kube
from .network_assets import selection
from .node_capacity import envelope


def preview(target: Target) -> dict:
    """Pure target/fixture plan; neither ownership files nor a cluster are read."""
    if not target.consumer.startswith("qual-"):
        raise ValueError("Capacity qualification requires a disposable qual- consumer")
    return {
        **capacity.preview(target, "qualify"),
        "namespace": target.consumer + "-capacity",
        "fixtureImage": selection()["probes"]["client"],
        "maximumConcurrentPods": 3,
        "effects": [
            "Require an existing, exactly owned, healthy foundation-only disposable cluster",
            "Restart both recorded kubelets; verify original owner, node UIDs and safe envelopes",
            "Prove reconciliation is idempotent after restart",
            "On each node create one bounded positive control and CPU/memory over-envelope probes",
            "Require native Unschedulable evidence; delete only exact fixture Pod UIDs on success",
        ],
        "onFailure": "Stop and retain namespace, Pods and cluster; no automatic cleanup or deletion",
        "exclusions": [
            "cluster creation/deletion",
            "retained-target repair",
            "resize",
            "full-stack capacity acceptance",
        ],
    }


def pod(target: Target, role: str, probe: str) -> dict:
    """Over-envelope Pods cannot bind; they never consume their declared large limits."""
    limits = envelope(load_profile(name=target.profile)["nodes"][role])
    requests = {"cpu": "20m", "memory": "32Mi"}
    if probe == "cpu":
        requests["cpu"] = f"{limits['cpuMillicores'] + 1}m"
    elif probe == "memory":
        requests["memory"] = str(limits["memoryBytes"] + 1024**2)
    elif probe != "positive":
        raise ValueError("Unknown capacity probe")
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": role + "-" + probe, "namespace": target.consumer + "-capacity"},
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "preemptionPolicy": "Never",
            "activeDeadlineSeconds": 600,
            "nodeSelector": {LABEL: target.consumer, "k8s-infrastructure.io/role": role},
            "tolerations": [
                {
                    "key": "node-role.kubernetes.io/control-plane",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                }
            ],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": selection()["probes"]["client"],
                    "command": ["sleep", "600"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": requests,
                        "limits": {
                            "cpu": requests["cpu"] if probe == "cpu" else "100m",
                            "memory": requests["memory"] if probe == "memory" else "64Mi",
                        },
                    },
                }
            ],
        },
    }


def observe_pod(target: Target, namespace: str, name: str, uid: str) -> dict:
    value = json.loads(kube(target, ["-n", namespace, "get", "pod", name, "-o", "json"]))
    if value["metadata"].get("uid") != uid or value["metadata"].get("deletionTimestamp"):
        raise ValueError("Capacity fixture Pod identity changed")
    return value


def wait_probe(target: Target, name: str, uid: str, role: str, probe: str) -> dict:
    """API/image failures never count as a scheduler denial or positive control."""
    deadline = time.monotonic() + 120
    while True:
        value = observe_pod(target, target.consumer + "-capacity", name, uid)
        status = value.get("status", {})
        if probe == "positive":
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in status.get("conditions", [])
            )
            if ready and value["spec"].get("nodeName") == f"{target.name}-{role}":
                return {"podUID": uid, "positiveControl": "ready on selected node"}
        else:
            if value.get("spec", {}).get("nodeName") or status.get("phase") not in (
                None,
                "Pending",
            ):
                raise ValueError(
                    "Over-envelope fixture bound or ran; capacity qualification failed"
                )
            denied = any(
                c.get("type") == "PodScheduled"
                and c.get("status") == "False"
                and c.get("reason") == "Unschedulable"
                and f"Insufficient {probe}" in c.get("message", "")
                for c in status.get("conditions", [])
            )
            if denied and status.get("phase") == "Pending":
                return {
                    "podUID": uid,
                    "resource": probe,
                    "schedulerRejection": "Insufficient " + probe,
                }
        if time.monotonic() >= deadline:
            raise ValueError("Capacity probe did not reach its required native result within 120s")
        time.sleep(2)


def guard(target: Target, owner: dict, uids: dict[str, str]) -> None:
    """Bind every phase to the original creation claim, node containers and API UIDs."""
    if lifecycle.owned(target) != owner:
        raise ValueError("Capacity qualification owner changed")
    nodes = capacity.observations(target)
    if {role: value["metadata"]["uid"] for role, value in nodes.items()} != uids:
        raise ValueError("Capacity qualification node UID changed")
    for role, budget in load_profile(name=target.profile)["nodes"].items():
        capacity.verify_allocatable(nodes[role]["status"], budget)


def check_namespace(target: Target, namespace: str, uid: str) -> None:
    value = json.loads(kube(target, ["get", "namespace", namespace, "-o", "json"]))
    if value["metadata"].get("uid") != uid or value["metadata"].get("deletionTimestamp"):
        raise ValueError("Capacity qualification namespace identity changed")


def delete_probe(target: Target, namespace: str, name: str, uid: str) -> None:
    """A server-side UID precondition cannot delete a same-name replacement."""
    runtime.command(
        target,
        [
            "kubectl",
            "--kubeconfig",
            str(target.path("kubeconfig")),
            "--context",
            target.context,
            "--request-timeout=30s",
            "delete",
            "--raw",
            f"/api/v1/namespaces/{namespace}/pods/{name}",
            "-f",
            "-",
        ],
        input_data=json.dumps(
            {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
        ).encode(),
    )
    kube(target, ["-n", namespace, "wait", "--for=delete", "pod/" + name, "--timeout=60s"])


def qualify(target: Target) -> dict:
    """Operate only a previously created disposable foundation; preserve failures."""
    plan = preview(target)
    lifecycle.health(target)
    owner = lifecycle.owned(target)
    capacity.preflight_workloads(target)
    nodes = capacity.observations(target)
    uids = {role: value["metadata"]["uid"] for role, value in nodes.items()}
    guard(target, owner, uids)
    namespace = plan["namespace"]
    if kube(target, ["get", "namespace", namespace, "--ignore-not-found", "-o", "name"]).strip():
        raise ValueError(
            "Capacity qualification namespace exists; inspect retained fixtures before retry"
        )
    for node_id in owner["nodeIds"].values():
        guard(target, owner, uids)
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
        capacity.wait_allocatable(target)
        lifecycle.health(target)
        guard(target, owner, uids)
    reconciliation = capacity.configure(target)
    if reconciliation["restartedNodes"]:
        raise ValueError("Capacity reconciliation was not idempotent after restart")
    guard(target, owner, uids)
    created = json.loads(
        kube(
            target,
            ["create", "-f", "-", "-o", "json"],
            [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": namespace, "labels": {LABEL: target.consumer}},
                }
            ],
        )
    )
    namespace_uid = created.get("items", [created])[0]["metadata"]["uid"]
    if not namespace_uid:
        raise ValueError("Capacity fixture namespace UID missing")
    evidence = {}
    for role in nodes:
        fixtures = []
        evidence[role] = {}
        for probe in ("positive", "cpu", "memory"):
            guard(target, owner, uids)
            check_namespace(target, namespace, namespace_uid)
            desired = pod(target, role, probe)
            created = json.loads(kube(target, ["create", "-f", "-", "-o", "json"], [desired]))
            actual = created.get("items", [created])[0]
            name, uid = desired["metadata"]["name"], actual["metadata"]["uid"]
            if not uid:
                raise ValueError("Capacity fixture Pod UID missing")
            fixtures.append((name, uid))
            evidence[role][probe] = wait_probe(target, name, uid, role, probe)
        # Repeat the positive control after both denials to exclude a dead node.
        name, uid = fixtures[0]
        wait_probe(target, name, uid, role, "positive")
        for name, uid in fixtures:
            guard(target, owner, uids)
            check_namespace(target, namespace, namespace_uid)
            delete_probe(target, namespace, name, uid)
    if capacity.configure(target)["restartedNodes"]:
        raise ValueError("Post-fixture reconciliation unexpectedly changed capacity")
    guard(target, owner, uids)
    return {
        "cluster": target.name,
        "context": target.context,
        "incarnation": owner["incarnation"],
        "nodeUIDs": uids,
        "nodeIds": owner["nodeIds"],
        "schedulerEvidence": evidence,
        "kubeletRestart": "observed on original nodes",
        "reconciliation": "idempotent",
        "namespaceUID": namespace_uid,
        "fixtureRetention": "Pods UID-deleted on success; empty namespace and cluster retained",
        "notQualified": [
            "legacy retained-target repair",
            "full FleetIQ stack",
            "larger profile",
            "host isolation",
        ],
    }
