"""Prepare and operate the selected minimal-demo Argo controllers."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import yaml

from . import bootstrap_failure as failure
from . import (
    consumer_lifecycle,
    delegated_argo_config,
    delegated_argo_runtime,
    minimal_argo_assets,
    minimal_argo_fence,
    minimal_argo_qualification,
    resource_peaks,
)
from . import consumer_runtime as runtime
from . import minimal_bootstrap as bootstrap
from .consumer_profile import MINIMAL_PROFILE, Target, identity
from .consumer_state import read_bounded
from .delegated_argo_config import Delegation, labels
from .delegated_argo_render import render
from .host_admission import digest
from .kubernetes import contains, kube, reference
from .minimal_network import check_failure_events, save_receipt, staged_images, verify_pod_authority
from .network_resources import EXTRA_MEMORY, ROLE, SYSTEM, normalized, pod_bounds

SCHEMA = "minimal-argo-lifecycle.v1"
ACTIONS = ("install", "suspend", "resume", "status", "qualify")
INSTALLATION = "k8s-infrastructure.io/argo-lifecycle-installation"


def receipt_path(target: Target) -> Path:
    return target.directory / f"{target.name}.argo-lifecycle.json"


def private(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        raise ValueError("Expected private local Argo evidence")


def binding(target: Target, delegation: Delegation, owner: dict) -> dict:
    return {
        **identity(target),
        "schema": SCHEMA,
        "ownerNonce": owner["nonce"],
        "incarnation": owner["incarnation"],
        "sourceArtifactDigest": bootstrap.source_digest(),
        "delegationDigest": delegation.digest,
        "assetSelectionDigest": digest(
            {
                "manifests": delegated_argo_config.selection(),
                "images": minimal_argo_assets.selection(),
            }
        ),
    }


def load_receipt(path: Path, expected: dict) -> dict:
    private(path)
    result = json.loads(read_bounded(path, 1024**2))
    if not isinstance(result, dict) or any(result.get(k) != v for k, v in expected.items()):
        raise ValueError("Argo lifecycle receipt identity differs from target, source or assets")
    return result


def resources(target: Target, namespace: str, name: str) -> list[dict]:
    raw = kube(target, ["-n", namespace, "get", name, "-o", "json"])
    result = json.loads(raw)
    if result.get("kind") != "List" or not isinstance(result.get("items"), list):
        raise ValueError("Expected an exact Kubernetes list")
    return result["items"]


def controller_owner(item: dict) -> dict:
    return next(
        (
            ref
            for ref in item["metadata"].get("ownerReferences", [])
            if ref.get("controller") is True
        ),
        {},
    )


def spec_digest(workload: dict) -> str:
    return digest({key: field for key, field in workload["spec"].items() if key != "replicas"})


def controller_workloads(target: Target, delegation: Delegation) -> dict[str, dict]:
    workloads = [
        item
        for namespace in (delegation.gitops, delegation.controllers)
        for item in resources(target, namespace, "deployments,statefulsets")
    ]
    found = {item["metadata"]["name"]: item for item in workloads}
    if len(found) != len(workloads):
        raise ValueError("Argo controller inventory is ambiguous")
    return found


def inventory(target: Target, delegation: Delegation, receipt: dict) -> dict[str, dict]:
    found = controller_workloads(target, delegation)
    records = receipt["controllers"]
    exact = minimal_argo_assets.controller_names(delegation)
    if set(found) != set(records) or set(records) != set(exact):
        raise ValueError("Argo controller inventory changed")
    for name, expected in records.items():
        item = found[name]
        metadata = item["metadata"]
        if (
            (item["kind"], metadata["namespace"]) != exact[name]
            or metadata.get("annotations", {}).get(INSTALLATION) != receipt["installation"]
            or not contains(labels(delegation), metadata.get("labels"))
            or item["kind"] != expected["kind"]
            or metadata["namespace"] != expected["namespace"]
            or metadata["uid"] != expected["uid"]
            or spec_digest(item) != expected["specDigest"]
            or metadata.get("deletionTimestamp")
            or type(item["spec"]["replicas"]) is not int
            or item["spec"]["replicas"] not in (0, 1)
        ):
            raise ValueError("Argo controller identity, specification or replica range changed")
    return found


def controller_pods(
    target: Target, delegation: Delegation, workloads: dict[str, dict]
) -> dict[str, list[dict]]:
    deployment_ids = {
        item["metadata"]["uid"]: name
        for name, item in workloads.items()
        if item["kind"] == "Deployment"
    }
    owners = {
        ("StatefulSet", item["metadata"]["uid"]): name
        for name, item in workloads.items()
        if item["kind"] == "StatefulSet"
    }
    pods: dict[str, list[dict]] = {name: [] for name in workloads}
    for namespace in (delegation.gitops, delegation.controllers):
        for replica_set in resources(target, namespace, "replicasets"):
            owner = controller_owner(replica_set)
            if owner.get("kind") == "Deployment" and owner.get("uid") in deployment_ids:
                owners["ReplicaSet", replica_set["metadata"]["uid"]] = deployment_ids[owner["uid"]]
        for pod in resources(target, namespace, "pods"):
            owner = controller_owner(pod)
            name = owners.get((owner.get("kind"), owner.get("uid")))
            if not name or workloads[name]["metadata"]["namespace"] != namespace:
                raise ValueError("Unexpected Pod ownership in Argo controller namespaces")
            pods[name].append(pod)
    return pods


def observation(target: Target, workloads: dict[str, dict], pods: dict[str, list[dict]]) -> dict:
    return {
        name: {
            "replicas": item["spec"]["replicas"],
            "observed": converged(target, item, pods[name]),
            "pods": [pod["metadata"]["uid"] for pod in pods[name]],
        }
        for name, item in workloads.items()
    }


def pod_ready(target: Target, workload: dict, pod: dict) -> bool:
    spec, status = pod["spec"], pod["status"]
    want = workload["spec"]["template"]["spec"]
    if (
        pod["metadata"].get("deletionTimestamp")
        or status.get("phase") != "Running"
        or not any(
            entry.get("type") == "Ready" and entry.get("status") == "True"
            for entry in status.get("conditions", [])
        )
    ):
        return False
    if spec.get("nodeName") != f"{target.name}-worker":
        raise ValueError("Observed Argo Pod is outside its exact worker placement")
    checked_pod(want, spec)
    for stage, field in (
        ("containers", "containerStatuses"),
        ("initContainers", "initContainerStatuses"),
    ):
        observed = {container["name"]: container for container in status.get(field, [])}
        if set(observed) != {container["name"] for container in want.get(stage, [])}:
            return False
        for wanted in want.get(stage, []):
            container = observed[wanted["name"]]
            if container.get("restartCount") != 0:
                raise ValueError("Argo startup restarted a container; inspect installed evidence")
            if stage == "containers" or wanted.get("restartPolicy") == "Always":
                if not container.get("ready") or "running" not in container.get("state", {}):
                    return False
            elif container.get("state", {}).get("terminated", {}).get("exitCode") != 0:
                return False
    return True


def checked_pod(template: dict, admitted: dict) -> dict:
    if (
        admitted.get("nodeSelector") != template["nodeSelector"]
        or admitted.get("ephemeralContainers")
        or normalized(pod_bounds(admitted)) != normalized(pod_bounds(template))
        or any(
            [(c["name"], c.get("image"), c.get("imagePullPolicy")) for c in admitted.get(stage, [])]
            != [
                (c["name"], c.get("image"), c.get("imagePullPolicy"))
                for c in template.get(stage, [])
            ]
            for stage in ("containers", "initContainers")
        )
        or any(
            admitted.get(field) != template.get(field)
            for field in (
                "affinity",
                "schedulerName",
                "priorityClassName",
                "schedulingGates",
                "resourceClaims",
            )
        )
    ):
        raise ValueError("Admitted Argo Pod differs from finite worker startup selection")
    # Argo's reviewed ServiceAccounts use normal mount defaulting except for
    # the repo-server Pod. Validate the same standard projection as Cilium.
    verify_pod_authority(
        {"automountServiceAccountToken": True, **template},
        {"automountServiceAccountToken": True, **admitted},
    )
    return admitted


def admit_pod(target: Target, workload: dict) -> dict:
    metadata, template = workload["metadata"], workload["spec"]["template"]
    controller = (
        "statefulset-controller" if workload["kind"] == "StatefulSet" else "replicaset-controller"
    )
    name = metadata["name"] + "-lifecycle-admission"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            **deepcopy(template.get("metadata", {})),
            "name": name,
            "namespace": metadata["namespace"],
        },
        "spec": template["spec"],
    }
    if workload["kind"] == "StatefulSet":
        pod["metadata"]["ownerReferences"] = [
            {
                "kind": "StatefulSet",
                "apiVersion": "apps/v1",
                "name": metadata["name"],
                "uid": metadata["uid"],
                "controller": True,
            }
        ]
    raw = kube(
        target,
        [
            f"--as=system:serviceaccount:kube-system:{controller}",
            "--as-group=system:authenticated",
            "--as-group=system:serviceaccounts",
            "--as-group=system:serviceaccounts:kube-system",
            "create",
            "--dry-run=server",
            "-f",
            "-",
            "-o",
            "json",
        ],
        [pod],
    )
    value = json.loads(raw)
    items = value["items"] if value.get("kind") == "List" else [value]
    if len(items) != 1 or reference(items[0]) != reference(pod) or items[0]["spec"].get("nodeName"):
        raise ValueError("Argo Pod admission returned an unexpected resource or placement")
    return checked_pod(template["spec"], items[0]["spec"])


def converged(target: Target, workload: dict, pods: list[dict]) -> bool:
    replicas = workload["spec"]["replicas"]
    status = workload.get("status", {})
    return (
        status.get("observedGeneration") == workload["metadata"]["generation"]
        and status.get("replicas", 0) == replicas
        and status.get("readyReplicas", 0) == replicas
        and len(pods) == replicas
        and (not pods or pod_ready(target, workload, pods[0]))
    )


def terminal_workflows(target: Target, delegation: Delegation) -> None:
    if any(
        item.get("status", {}).get("phase") not in {"Succeeded", "Failed", "Error"}
        for item in resources(target, delegation.managed, "workflows.argoproj.io")
    ):
        raise ValueError("Nonterminal Workflows remain in the delegated release namespace")


def scale(target: Target, workload: dict, replicas: int) -> None:
    current = workload["spec"]["replicas"]
    if current == replicas:
        return
    metadata = workload["metadata"]
    checks = [
        ("metadata/uid", metadata["uid"]),
        ("metadata/resourceVersion", metadata["resourceVersion"]),
        ("spec/replicas", current),
    ]
    patch = [{"op": "test", "path": "/" + key, "value": expected} for key, expected in checks] + [
        {"op": "replace", "path": "/spec/replicas", "value": replicas}
    ]
    kube(
        target,
        [
            "-n",
            metadata["namespace"],
            "patch",
            f"{workload['kind'].lower()}/{metadata['name']}",
            "--type=json",
            "--field-manager=k8s-infrastructure-minimal-argo-lifecycle",
            "-p",
            json.dumps(patch),
        ],
    )


def wait(target: Target, delegation: Delegation, receipt: dict, desired: dict) -> dict:
    while True:
        runtime.remaining_timeout()
        live = inventory(target, delegation, receipt)
        pods = controller_pods(target, delegation, live)
        result = observation(target, live, pods)
        if any(result[name]["replicas"] != replicas for name, replicas in desired.items()):
            raise ValueError("Argo replica intent changed during observation")
        if all(result[name]["observed"] for name in desired):
            return result
        time.sleep(min(2, runtime.remaining_timeout()))


def summary(target: Target, receipt: dict, controllers: dict) -> dict:
    return {
        "target": target.name,
        "incarnation": receipt["incarnation"],
        "phase": receipt["phase"],
        "controllers": controllers,
        "evidence": receipt_path(target).name,
        "installedAcceptance": False,
    }


def suspend(target: Target, delegation: Delegation, receipt: dict) -> dict:
    phase = receipt["phase"]
    if phase not in {"running", "suspending", "suspended", "resuming", "restoring-admission"}:
        raise ValueError("Complete dormant Argo installation before suspending")
    live = inventory(target, delegation, receipt)
    terminal_workflows(target, delegation)
    pods = controller_pods(target, delegation, live)
    if phase in {"running", "resuming", "restoring-admission"}:
        before = {name: [pod["metadata"]["uid"] for pod in items] for name, items in pods.items()}
        if phase == "running":
            receipt["restoreReplicas"] = {
                name: item["spec"]["replicas"] for name, item in live.items()
            }
        receipt.update(
            phase="suspending",
            history={"suspendStartedAtUnixNs": time.time_ns(), "previousPods": before},
        )
        save_receipt(receipt_path(target), receipt)
    minimal_argo_fence.set_closed(target, delegation, True, expected_uid=receipt["fenceUID"])
    if phase != "suspended":
        for name in reversed(list(minimal_argo_assets.controller_names(delegation))):
            terminal_workflows(target, delegation)
            current = inventory(target, delegation, receipt)
            controller_pods(target, delegation, current)
            scale(target, current[name], 0)
    stopped = wait(target, delegation, receipt, dict.fromkeys(live, 0))
    terminal_workflows(target, delegation)
    receipt["phase"] = "suspended"
    receipt["history"].setdefault("suspendCompletedAtUnixNs", time.time_ns())
    save_receipt(receipt_path(target), receipt)
    return summary(target, receipt, stopped)


def reviewed_applications(target: Target, delegation: Delegation) -> None:
    # ApplicationSets can rewrite generated Application intent at controller start.
    if resources(target, delegation.gitops, "applicationsets.argoproj.io"):
        raise ValueError("ApplicationSet reconciliation requires a separately reviewed phase")
    for item in resources(target, delegation.gitops, "applications.argoproj.io"):
        automated = item.get("spec", {}).get("syncPolicy", {}).get("automated")
        if automated is not None and automated.get("enabled") is not False:
            raise ValueError("Application automated sync must be disabled before restoring Argo")
        operation = item.get("status", {}).get("operationState")
        if (
            item["metadata"].get("deletionTimestamp")
            or item.get("operation") is not None
            or operation is not None
            and operation.get("phase") not in {"Succeeded", "Failed", "Error"}
        ):
            raise ValueError("Application operation or deletion remains in progress")


def live_worker_pods(target: Target) -> list[dict]:
    raw = kube(target, ["get", "pods", "--all-namespaces", "-o", "json"])
    collection = json.loads(raw)
    if collection.get("kind") != "List" or not isinstance(collection.get("items"), list):
        raise ValueError("Expected an exact Kubernetes Pod list")
    worker = f"{target.name}-worker"
    pods = []
    for item in collection["items"]:
        spec = item["spec"]
        if item["status"].get("phase") in {"Succeeded", "Failed"} and not item["metadata"].get(
            "deletionTimestamp"
        ):
            continue
        node = spec.get("nodeName")
        if node == worker:
            pods.append(item)
        elif node is None and spec.get("nodeSelector", {}).get(ROLE) != "control-plane":
            raise ValueError("Unassigned Pod can change the worker envelope; inspect placement")
    return pods


def check_worker_fit(target: Target, pending: dict | None = None) -> None:
    readiness = runtime.read_readiness(target)
    capacity = next(item["schedulerEnvelope"] for item in readiness if item["role"] == "worker")
    cpu = memory_requests = memory_limits = extra = system_cpu = system_memory = 0
    for pod in live_worker_pods(target):
        if pod["spec"].get("ephemeralContainers"):
            raise ValueError("Ephemeral containers are outside the finite worker envelope")
        counts = pod_bounds(pod["spec"])
        requested_cpu = counts["effectiveRequests"]["cpu"]
        memory_request = counts["effectiveRequests"]["memory"]
        steady = counts["steadyLimits"]["memory"]
        startup = counts["effectiveLimits"]["memory"]
        starting = pod["status"].get("phase") != "Running" or any(
            condition["type"] == "Initialized" and condition["status"] != "True"
            for condition in pod["status"].get("conditions", [])
        )
        cpu += requested_cpu
        memory_requests += memory_request
        memory_limits += startup if starting else steady
        if starting:
            extra += startup - steady
        if pod["metadata"]["namespace"] in {"kube-system", "local-path-storage"} and pod[
            "metadata"
        ].get("labels", {}).get("k8s-app") not in {"cilium", "cilium-envoy"}:
            system_cpu += requested_cpu
            system_memory += startup if starting else steady
    cpu += max(0, SYSTEM["worker"]["cpu"] - system_cpu)
    memory_requests += max(0, SYSTEM["worker"]["memory"] - system_memory)
    memory_limits += max(0, SYSTEM["worker"]["memory"] - system_memory)
    if pending:
        counts = pod_bounds(pending)
        cpu += counts["effectiveRequests"]["cpu"]
        memory_requests += counts["effectiveRequests"]["memory"]
        memory_limits += counts["effectiveLimits"]["memory"]
        extra += counts["effectiveLimits"]["memory"] - counts["steadyLimits"]["memory"]
    if (
        extra > EXTRA_MEMORY
        or cpu > capacity["cpuMillicores"]
        or max(memory_requests, memory_limits) > capacity["memoryBytes"]
    ):
        raise ValueError("Argo startup or resident Pods exceed the reviewed worker envelope")


def resume(target: Target, delegation: Delegation, receipt: dict) -> dict:
    phase = receipt["phase"]
    if phase not in {"suspended", "resuming", "restoring-admission", "running"}:
        raise ValueError("Complete installation or interrupted suspension before resuming")
    intent = receipt["restoreReplicas"]
    live = inventory(target, delegation, receipt)
    if set(intent) != set(live) or any(
        type(count) is not int or count not in (0, 1) for count in intent.values()
    ):
        raise ValueError("Argo recovery receipt does not identify the exact prior replicas")
    if phase == "running":
        return summary(target, receipt, wait(target, delegation, receipt, intent))
    if phase != "restoring-admission":
        restore_controllers(target, delegation, receipt, live)
    restored = wait(target, delegation, receipt, intent)
    if receipt["history"]["restoredPods"] != {
        name: item["pods"] for name, item in restored.items()
    }:
        raise ValueError("Argo Pods changed after the recorded restoration")
    if all(intent[name] for name in ("argo-server", "workflow-controller")):
        minimal_argo_fence.set_closed(target, delegation, False, expected_uid=receipt["fenceUID"])
    receipt["phase"] = "running"
    receipt["history"]["resumeCompletedAtUnixNs"] = time.time_ns()
    save_receipt(receipt_path(target), receipt)
    return summary(target, receipt, restored)


def restore_controllers(target: Target, delegation: Delegation, receipt: dict, live: dict) -> None:
    phase = receipt["phase"]
    intent = receipt["restoreReplicas"]
    reviewed_applications(target, delegation)
    terminal_workflows(target, delegation)
    nodes = runtime.node_identity(target)
    runtime.verify_nodes(nodes, profile=target.profile)
    worker = nodes[f"{target.name}-worker"]
    staged_images(
        target,
        [{**worker, "name": f"{target.name}-worker"}],
        {"worker": list(minimal_argo_assets.required_images())},
    )
    minimal_argo_fence.set_closed(target, delegation, True, expected_uid=receipt["fenceUID"])
    if phase == "suspended":
        wait(target, delegation, receipt, dict.fromkeys(live, 0))
        receipt.update(phase="resuming", startupPending=None, startupCompleted=[])
        receipt["history"]["resumeStartedAtUnixNs"] = time.time_ns()
        save_receipt(receipt_path(target), receipt)
    order = list(minimal_argo_assets.controller_names(delegation))
    recorded = receipt["startupCompleted"]
    if recorded != order[: len(recorded)] or receipt["startupPending"] not in [
        None,
        *order[len(recorded) : len(recorded) + 1],
    ]:
        raise ValueError("Argo recovery receipt has an invalid startup checkpoint")
    completed = {name: intent[name] for name in recorded}
    if completed:
        wait(target, delegation, receipt, completed)
    for name in order[len(recorded) :]:
        live = inventory(target, delegation, receipt)
        if any(live[key]["spec"]["replicas"] != count for key, count in completed.items()):
            raise ValueError("An already restored Argo controller changed before startup completed")
        workload = live[name]
        if workload["spec"]["replicas"] > intent[name] or (
            workload["spec"]["replicas"] and receipt["startupPending"] != name
        ):
            raise ValueError("Argo replica intent changed outside the recorded restoration")
        if workload["spec"]["replicas"] < intent[name]:
            wait(target, delegation, receipt, {**completed, name: 0})
            reviewed_applications(target, delegation)
            terminal_workflows(target, delegation)
            check_worker_fit(target, admit_pod(target, workload))
            receipt["startupPending"] = name
            save_receipt(receipt_path(target), receipt)
            current = inventory(target, delegation, receipt)[name]
            if current["spec"]["replicas"] != 0:
                raise ValueError("Argo replica intent changed before the startup patch")
            scale(target, current, intent[name])
        completed[name] = intent[name]
        wait(target, delegation, receipt, completed)
        receipt["startupPending"] = None
        recorded.append(name)
        save_receipt(receipt_path(target), receipt)
    check_worker_fit(target)
    restored = wait(target, delegation, receipt, intent)
    receipt["phase"] = "restoring-admission"
    receipt["history"]["restoredPods"] = {name: item["pods"] for name, item in restored.items()}
    save_receipt(receipt_path(target), receipt)


def create_missing(target: Target, documents: list[dict], found: dict) -> None:
    missing = [item for item in documents if reference(item) not in found]
    if missing:
        # Create refuses an object that appeared since preflight. Apply could
        # otherwise change that object's fields before our next ownership read.
        kube(target, ["create", "-f", "-"], missing)


def check_installed_document(expected: dict, actual: dict) -> None:
    delegated_argo_runtime.verify_ownership(expected, actual)
    delegated_argo_runtime.verify_security(expected, actual)
    for key in ("spec", "data", "rules", "subjects", "roleRef"):
        if key in expected and not contains(expected[key], actual.get(key)):
            raise ValueError("Existing Argo specification differs from the reviewed installation")
    if expected["kind"] == "ConfigMap" and expected.get("data", {}) != actual.get("data", {}):
        raise ValueError("Existing Argo configuration differs from the reviewed installation")


def prepare(target: Target, delegation: Delegation, receipt: dict) -> dict:
    if receipt.get("phase") == "suspended" and "resumeStartedAtUnixNs" not in receipt["history"]:
        return suspend(target, delegation, receipt)
    if receipt.get("phase") not in {None, "installing"}:
        raise ValueError("Argo already prepared; use an explicit lifecycle action")
    runtime.read_readiness(target)
    delegated_argo_runtime.namespaces_delegated(target, delegation)
    shared, documents = render(
        delegation, delegated_argo_config.download_assets(), minimal=True, networking=True
    )
    workloads = minimal_argo_assets.controller_documents(delegation, documents)
    live = controller_workloads(target, delegation)
    if receipt.get("phase") is None:
        if live:
            raise ValueError("Argo workloads already exist; refusing to adopt an installation")
        receipt.update(
            phase="installing",
            installation=str(uuid4()),
            controllers={},
            restoreReplicas={
                item["metadata"]["name"]: item["spec"]["replicas"] for item in workloads
            },
            history={},
        )
        save_receipt(receipt_path(target), receipt)
    dormant = []
    for reviewed in workloads:
        item = deepcopy(reviewed)
        item["spec"]["replicas"] = 0
        item["metadata"].setdefault("annotations", {})[INSTALLATION] = receipt["installation"]
        dormant.append(item)
    names = {item["metadata"]["name"] for item in dormant}
    if not set(live) <= names:
        raise ValueError("Unexpected Argo workload in a controller namespace")
    for item in dormant:
        name = item["metadata"]["name"]
        if name in live:
            check_dormant_workload(item, live[name], receipt["controllers"].get(name))
        elif name in receipt["controllers"]:
            raise ValueError("Recorded Argo workload is missing; refusing replacement")
    support = [item for item in documents if item["kind"] not in {"Deployment", "StatefulSet"}]
    found = delegated_argo_runtime.inventory(target, shared, support, complete=False)
    for item in support:
        if (actual := found.get(reference(item))) is not None:
            check_installed_document(item, actual)

    for item in shared:
        create_missing(target, [item], found)
        if item["kind"] == "CustomResourceDefinition":
            kube(
                target,
                [
                    "wait",
                    "--for=condition=Established",
                    f"crd/{item['metadata']['name']}",
                    "--timeout=30s",
                ],
            )
    for kinds in (
        {"Namespace"},
        {"ConfigMap"},
        {"ValidatingAdmissionPolicy"},
        {"ValidatingAdmissionPolicyBinding"},
    ):
        create_missing(target, [item for item in support if item["kind"] in kinds], found)
    delegated_argo_runtime.verify_admission(target, support)
    early = {
        "Namespace",
        "ConfigMap",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }
    create_missing(target, [item for item in support if item["kind"] not in early], found)
    fence_uid = minimal_argo_fence.read_fence(target, delegation)["metadata"]["uid"]
    if receipt.get("fenceUID", fence_uid) != fence_uid:
        raise ValueError("Argo Workflow fence identity changed during dormant installation")
    receipt["fenceUID"] = fence_uid
    save_receipt(receipt_path(target), receipt)
    minimal_argo_fence.set_closed(target, delegation, True, expected_uid=fence_uid)
    for item in dormant:
        name = item["metadata"]["name"]
        if name not in live:
            kube(target, ["create", "-f", "-"], [item])
        current = controller_workloads(target, delegation)[name]
        record = check_dormant_workload(item, current, receipt["controllers"].get(name))
        receipt["controllers"][name] = record
        save_receipt(receipt_path(target), receipt)
    stopped = wait(target, delegation, receipt, dict.fromkeys(names, 0))
    receipt["phase"] = "suspended"
    receipt["history"]["installedDormantAtUnixNs"] = time.time_ns()
    save_receipt(receipt_path(target), receipt)
    return summary(target, receipt, stopped)


def check_dormant_workload(expected: dict, actual: dict, recorded: dict | None) -> dict:
    check_installed_document(expected, actual)
    metadata = actual["metadata"]
    if (
        reference(expected) != reference(actual)
        or metadata.get("annotations", {}).get(INSTALLATION)
        != expected["metadata"]["annotations"][INSTALLATION]
        or not metadata.get("uid")
        or actual["spec"]["replicas"] != 0
    ):
        raise ValueError("Dormant Argo workload does not belong to the recorded installation")
    bounds = pod_bounds(actual["spec"]["template"]["spec"])
    if normalized(bounds) != normalized(pod_bounds(expected["spec"]["template"]["spec"])):
        raise ValueError("Dormant Argo workload has unreviewed startup resources")
    result = {
        "kind": actual["kind"],
        "namespace": metadata["namespace"],
        "uid": metadata["uid"],
        "specDigest": spec_digest(actual),
    }
    if recorded is not None and result != recorded:
        raise ValueError("Recorded dormant Argo workload changed")
    return result


def qualify(
    target: Target, delegation: Delegation, receipt: dict, request: dict, host_id: str, path: Path
) -> dict:
    if not target.consumer.startswith("qual-") or receipt["phase"] != "running":
        raise ValueError("Argo qualification requires a running disposable qual- target")
    current = inventory(target, delegation, receipt)
    current_pods = observation(target, current, controller_pods(target, delegation, current))
    history = receipt["history"]
    if (
        not {
            "suspendStartedAtUnixNs",
            "suspendCompletedAtUnixNs",
            "resumeStartedAtUnixNs",
            "resumeCompletedAtUnixNs",
            "previousPods",
            "restoredPods",
        }
        <= history.keys()
        or any(not state["observed"] for state in current_pods.values())
        or history["restoredPods"] != {name: entry["pods"] for name, entry in current_pods.items()}
    ):
        raise ValueError("Require an observed suspend/resume cycle with the same restored Pods")
    validated_path = Path(os.path.abspath(path))
    if validated_path.parent != target.directory or validated_path.suffix != ".jsonl":
        raise ValueError("Use an existing private peak receipt in the exact target directory")
    private(validated_path)
    if validated_path.stat().st_size > resource_peaks.MAX_RECEIPT_BYTES:
        raise ValueError("Peak evidence exceeds its declared byte bound")
    measurement = resource_peaks.bind(target, request, host_id)
    window, samples, peak, checksum = minimal_argo_qualification.peak_records(
        target, validated_path, measurement.provenance
    )
    evidence = minimal_argo_qualification.assess(target, history, current, window, samples, peak)
    check_failure_events(target)
    final = inventory(target, delegation, receipt)
    if current_pods != observation(target, final, controller_pods(target, delegation, final)):
        raise ValueError("Argo controller Pods changed while qualifying the observed cycle")
    phase = "incomplete" if evidence["reasons"] else "qualified"
    output = target.directory / f"{target.name}.argo-qualification.{window['collectionId']}.json"
    record = {
        "schema": "minimal-argo-qualification.v1",
        "sourceArtifactDigest": receipt["sourceArtifactDigest"],
        "assetSelectionDigest": receipt["assetSelectionDigest"],
        "delegationDigest": receipt["delegationDigest"],
        "ownerNonce": receipt["ownerNonce"],
        "installation": receipt["installation"],
        "lifecycleReceiptDigest": digest(receipt),
        "peakReceiptDigest": checksum,
        "windowIdentity": measurement.provenance,
        "lifecycle": history,
        **summary(target, {**receipt, "phase": phase}, current_pods),
        **evidence,
    }
    if os.path.lexists(output):
        private(output)
        if json.loads(read_bounded(output, 1024**2)) != record:
            raise ValueError("Qualification receipt exists for this window and differs; retain it")
    else:
        save_receipt(output, record)
    return {
        "phase": phase,
        "target": target.name,
        "incarnation": receipt["incarnation"],
        "qualificationEvidence": output.name,
        "reasons": evidence["reasons"],
        "installedAcceptance": False,
    }


def status(target: Target, delegation: Delegation, receipt: dict) -> dict:
    if receipt["phase"] == "installing":
        found = controller_workloads(target, delegation)
        expected = minimal_argo_assets.controller_names(delegation)
        if any(
            name not in expected
            or (item["kind"], item["metadata"]["namespace"]) != expected[name]
            or item["metadata"].get("annotations", {}).get(INSTALLATION) != receipt["installation"]
            or not contains(labels(delegation), item["metadata"].get("labels"))
            or (
                name in receipt["controllers"]
                and item["metadata"]["uid"] != receipt["controllers"][name]["uid"]
            )
            for name, item in found.items()
        ):
            raise ValueError("Partial dormant Argo installation differs from the receipt")
    else:
        found = inventory(target, delegation, receipt)
    evidence = summary(
        target, receipt, observation(target, found, controller_pods(target, delegation, found))
    )
    if "fenceUID" in receipt:
        fence = minimal_argo_fence.read_fence(target, delegation)
        if fence["metadata"]["uid"] != receipt["fenceUID"]:
            raise ValueError("Argo Workflow fence identity changed")
        evidence["workflowAdmission"] = fence["data"]["mode"]
    return evidence


def operate(
    target: Target,
    delegation: Delegation,
    action: str,
    timeout: int,
    *,
    request: dict | None = None,
    host_id: str | None = None,
    peak_receipt: Path | None = None,
) -> dict:
    if target.profile != MINIMAL_PROFILE or target.consumer != delegation.consumer:
        raise ValueError("Exact minimal-demo consumer target required")
    with runtime.deadline(timeout), bootstrap.network_operation(target):
        owner = consumer_lifecycle.owned(target, require_ready=False)
        if owner["phase"] != "provisioned":
            raise ValueError("Expected provisioned minimal-demo ownership")
        expected = binding(target, delegation, owner)
        path = receipt_path(target)
        receipt = (
            load_receipt(path, expected)
            if action != "install" or os.path.lexists(path)
            else expected
        )
        if action == "install":
            return prepare(target, delegation, receipt)
        if action == "suspend":
            return suspend(target, delegation, receipt)
        if action == "resume":
            return resume(target, delegation, receipt)
        if action == "qualify":
            if request is None or host_id is None or peak_receipt is None:
                raise ValueError(
                    "Qualification requires the original request, host ID and private peak receipt"
                )
            return qualify(target, delegation, receipt, request, host_id, peak_receipt)
        return status(target, delegation, receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--delegation", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--peak-receipt", type=Path)
    parser.add_argument("--confirm-target")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = Target(
            args.consumer, args.docker_context, args.state_root, MINIMAL_PROFILE, args.https_port
        )
        delegation = delegated_argo_config.load_delegation(args.delegation, target)
        if not 10 <= args.timeout_seconds <= 900:
            raise ValueError("Choose a lifecycle deadline of 10 to 900 seconds")
        if not args.execute:
            result = {
                "action": args.action,
                **identity(target),
                "execution": "not-performed",
                "delegationDigest": delegation.digest,
                "timeoutSeconds": args.timeout_seconds,
                "assetSelection": delegated_argo_config.selection(),
                "requiredWorkerImages": minimal_argo_assets.required_images(),
            }
        else:
            if args.confirm_target != target.name:
                raise ValueError("Execution requires exact --confirm-target matching this cluster")
            with redirect_stdout(sys.stderr), resource_peaks.operation_limit(args.timeout_seconds):
                request = json.loads(read_bounded(args.request)) if args.request else None
                result = operate(
                    target,
                    delegation,
                    args.action,
                    args.timeout_seconds,
                    request=request,
                    host_id=args.host_id,
                    peak_receipt=args.peak_receipt,
                )
        print(json.dumps(result, indent=2))
        return int(result.get("phase") == "incomplete")
    except (runtime.CommandFailure, TimeoutError, KeyboardInterrupt) as error:
        print(json.dumps({"failure": failure.evidence(error, args.action, "incomplete")}))
    except (json.JSONDecodeError, yaml.YAMLError, OSError, TypeError, KeyError):
        print("Minimal Argo input, evidence or command failed. Private details withheld.")
    except ValueError as error:
        print(f"Minimal Argo refused or incomplete. {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
