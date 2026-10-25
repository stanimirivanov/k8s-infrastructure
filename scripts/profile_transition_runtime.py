"""Bounded foundation-only retained enlargement; recovery is always explicit."""

from __future__ import annotations

import json
import time

from . import consumer_capacity as capacity
from . import consumer_lifecycle as lifecycle
from . import consumer_runtime as runtime
from . import consumer_state as owner
from . import profile_transition_private as private
from . import profile_transition_state as store
from .consumer_profile import TRANSITION_TEST_PROFILE, Target, identity, load_profile
from .node_capacity import verify_allocatable
from .profile_transition_model import (
    FORMAT,
    ROLES,
    desired_configuration,
    digest,
    encoded,
    machine,
    request_successor,
    validate_request,
)


def successor_owner(target: Target, journal: dict) -> bytes:
    previous = json.loads(journal["predecessorOwner"])
    return encoded({**previous, **identity(request_successor(target, journal["request"]))})


def foundation(target: Target) -> tuple[list, bool]:
    """Freeze controller/namespace identity; refuse all persistent consumer volumes.

    Pod identities can change on kubelet restart. Controller UIDs and generations
    must not; this is a quiesced foundation, not application maintenance.
    """
    documents = json.loads(
        runtime.kubectl(
            target,
            [
                "get",
                "namespaces,deployments,statefulsets,daemonsets,jobs,cronjobs,persistentvolumes,persistentvolumeclaims",
                "-A",
                "-o",
                "json",
            ],
        )
    )
    result = []
    ready = True
    for item in documents["items"]:
        kind, meta = item["kind"], item["metadata"]
        if kind in {"PersistentVolume", "PersistentVolumeClaim", "StatefulSet", "Job", "CronJob"}:
            raise ValueError(
                "Initial retained transition refuses persistent volumes/claims or workload controllers"
            )
        if not meta.get("uid") or meta.get("deletionTimestamp"):
            raise ValueError("Foundation resource identity is missing or deleting")
        result.append(
            [kind, meta.get("namespace", ""), meta["name"], meta["uid"], meta.get("generation", 0)]
        )
        spec, status = item.get("spec", {}), item.get("status", {})
        if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
            desired = (
                status.get("desiredNumberScheduled", 0)
                if kind == "DaemonSet"
                else spec.get("replicas", 1)
            )
            observed = (
                status.get("numberReady", 0)
                if kind == "DaemonSet"
                else status.get("readyReplicas", 0)
            )
            ready = (
                ready
                and desired > 0
                and observed == desired
                and status.get("observedGeneration", -1) >= meta.get("generation", 0)
            )
    keys = {tuple(entry[:3]) for entry in result}
    required = {
        ("Namespace", "", "kube-system"),
        ("Deployment", "kube-system", "coredns"),
        ("DaemonSet", "kube-system", "cilium"),
        ("DaemonSet", "kube-system", "cilium-envoy"),
        ("Deployment", "kube-system", "cilium-operator"),
        ("Deployment", "local-path-storage", "local-path-provisioner"),
    }
    if not required <= keys:
        raise ValueError("Required network/DNS/storage foundation resources are absent")
    return sorted(result), ready


def bind(target: Target, request: dict, journal: dict | None = None) -> tuple[bytes, dict, dict]:
    """Observe exact reviewed identities without passing unfinished state to ensure."""
    successor = request_successor(target, request)
    runtime.prerequisites(successor, creating=True)
    source = lifecycle.source_identity(target)
    if (
        source.get("providerRevision") != request["providerRevision"]
        or source.get("providerDirty") is not False
    ):
        raise ValueError("Transition requires the exact reviewed clean provider Git revision")
    current = store.read(target.path("owner.json"), 16384)
    if journal is None:
        record = owner.load(target)
        if json.loads(current) != record or record["phase"] != "ready":
            raise ValueError("Transition requires the retained ready predecessor owner")
    else:
        allowed = {journal["predecessorOwner"].encode()}
        if journal["phase"] in {"finalizing", "complete"}:
            allowed.add(successor_owner(target, journal))
        if current not in allowed:
            raise ValueError("Ownership changed outside the recorded transition boundary")
        record = json.loads(current)
        if (
            digest(store.read(target.path("kind.yaml"))) != journal["kindHash"]
            or digest(store.read(target.path("kubeconfig"))) != journal["kubeconfigHash"]
        ):
            raise ValueError("Retained configuration/access bytes changed")
    if (
        runtime.engine(target) != record["endpoint"]
        or target.name not in runtime.clusters(target)
        or runtime.incarnation(target) != request["incarnation"]
        or record["incarnation"] != request["incarnation"]
    ):
        raise ValueError("Retained endpoint or cluster incarnation changed")
    runtime.verify_context(target)
    containers = runtime.node_identity(target)
    nodes = capacity.observations(target)
    for role in ROLES:
        evidence = request["nodes"][role]
        name = f"{target.name}-{role}"
        observed_capacity = nodes[role]["status"]["capacity"]
        if not isinstance(observed_capacity, dict):
            raise ValueError("Node capacity must be a resource map")
        # Kubernetes reports other resources too; only project live observations.
        # The reviewed request still passes the exact-key machine validator.
        observed_machine = machine({key: observed_capacity.get(key) for key in ("cpu", "memory")})
        if (
            record["nodeIds"].get(name) != evidence["containerId"]
            or containers[name]["id"] != evidence["containerId"]
            or nodes[role]["metadata"]["uid"] != evidence["uid"]
            or observed_machine != machine(evidence["newMachineCapacity"])
        ):
            raise ValueError("Original node identity or stable engine capacity changed")
    capacity.preflight_workloads(successor)
    snapshot, _ = foundation(target)
    if journal is not None and snapshot != journal["foundation"]:
        raise ValueError("Foundation identity/configuration changed during transition")
    return current, containers, nodes


def node_ready(role: str, node: dict) -> bool:
    conditions = {entry["type"]: entry["status"] for entry in node["status"]["conditions"]}
    spec = node.get("spec", {})

    def allowed(taint: dict) -> bool:
        return (
            role == "control-plane"
            and taint.get("key") == "node-role.kubernetes.io/control-plane"
            and taint.get("effect") == "NoSchedule"
        )

    return (
        conditions.get("Ready") == "True"
        and conditions.get("NetworkUnavailable", "False") == "False"
        and not spec.get("unschedulable", False)
        and not any(
            t.get("effect") in {"NoSchedule", "NoExecute"} and not allowed(t)
            for t in spec.get("taints", [])
        )
    )


def preflight(target: Target, request: dict) -> dict:
    """Preflight both configs/paths before any journal or cluster mutation."""
    current, containers, nodes = bind(target, request)
    runtime.verify_nodes(containers, profile=target.profile)
    for role in ROLES:
        if not check_caps(
            containers[f"{target.name}-{role}"], load_profile(name=target.profile)["nodes"][role]
        ):
            raise ValueError("Predecessor caps or no-swap policy differ")
        if not node_ready(role, nodes[role]):
            raise ValueError("Predecessor nodes must be Ready and uncordoned before transition")
    snapshot, ready = foundation(target)
    if not ready:
        raise ValueError("Foundation controllers are not ready for transition")
    hashes = {}
    repair_hashes = {}
    for role in ROLES:
        node = request["nodes"][role]
        observed, config = capacity.read_configuration(target, node["containerId"])
        if observed != node["oldConfigSha256"]:
            raise ValueError("Predecessor config hash differs from reviewed request")
        desired = desired_configuration(
            config, node, role, successor_profile=request_successor(target, request).profile
        )
        private.execute(
            target, node["containerId"], private.PREFLIGHT, request["operationId"], observed
        )
        hashes[role] = digest(encoded(desired))
        repair_hashes[role] = private.repair_hash(
            target, node["containerId"], request["operationId"]
        )
    # Re-observe before reserving the journal; a preview is not an admission lock.
    repeated, repeated_containers, _ = bind(target, request)
    if (
        current != repeated
        or containers != repeated_containers
        or foundation(target)[0] != snapshot
    ):
        raise ValueError("Preflight identity changed before journal reservation")
    return {
        "format": FORMAT,
        "request": request,
        "requestHash": digest(encoded(request)),
        "phase": "prepared",
        "stages": {r: "pending" for r in ROLES},
        "predecessorOwner": current.decode(),
        "predecessorOwnerHash": digest(current),
        "kindHash": digest(store.read(target.path("kind.yaml"))),
        "kubeconfigHash": digest(store.read(target.path("kubeconfig"))),
        "foundation": snapshot,
        "desiredHashes": hashes,
        "repairBackupHashes": repair_hashes,
    }


def check_caps(node: dict, budget: dict) -> bool:
    return (
        node.get("cpu") == budget["cpus"] * 10**9
        and node.get("memory") == budget["memoryBytes"]
        and node.get("pids") == 2048
        and node.get("swap") == budget["memoryBytes"]
    )


def guarded(target: Target, journal: dict) -> dict:
    """Known phase-specific caps/hashes only; unknown partial states never converge."""
    _, containers, nodes = bind(target, journal["request"], journal)
    for role, stage in journal["stages"].items():
        actual = containers[f"{target.name}-{role}"]
        before = load_profile(name=target.profile)["nodes"][role]
        after = load_profile(name=request_successor(target, journal["request"]).profile)["nodes"][
            role
        ]
        permitted = (
            [before]
            if stage == "pending"
            else [before, after]
            if stage == "cap-intent"
            else [after]
        )
        if not any(check_caps(actual, budget) for budget in permitted):
            raise ValueError("Node caps differ from the recorded transition phase")
        evidence = journal["request"]["nodes"][role]
        if (
            private.repair_hash(target, evidence["containerId"], journal["request"]["operationId"])
            != journal["repairBackupHashes"][role]
        ):
            raise ValueError("Original repair backup changed during transition")
        if stage == "verified":
            verify_allocatable(nodes[role]["status"], after)
            if not node_ready(role, nodes[role]):
                raise ValueError("Previously verified node is no longer Ready/schedulable")
        if journal["phase"] in {"nodes", "finalizing", "complete"}:
            private.execute(
                target,
                evidence["containerId"],
                private.VERIFY_PATHS,
                journal["request"]["operationId"],
                evidence["oldConfigSha256"],
                stage,
            )
        observed = private.current_hash(target, evidence["containerId"])
        hashes = (
            {evidence["oldConfigSha256"]}
            if stage in {"pending", "cap-intent"}
            else {journal["desiredHashes"][role]}
            if stage in {"restart-intent", "verified"}
            else {evidence["oldConfigSha256"], journal["desiredHashes"][role]}
        )
        if observed not in hashes:
            raise ValueError("Node config differs from the recorded transition phase")
    return containers


def wait_node(target: Target, journal: dict, role: str) -> None:
    deadline = time.monotonic() + 120
    budget = load_profile(name=request_successor(target, journal["request"]).profile)["nodes"][role]
    while True:
        _, _, nodes = bind(target, journal["request"], journal)
        node = nodes[role]
        from .node_capacity import AllocatableMismatch

        try:
            verify_allocatable(node["status"], budget)
            if node_ready(role, node) and foundation(target)[1]:
                return
        except AllocatableMismatch:
            pass
        if time.monotonic() >= deadline:
            raise ValueError("Transition node readiness did not converge; retain state")
        time.sleep(2)


def advance(target: Target, journal: dict, *, recovery: bool) -> None:
    operation = journal["request"]["operationId"]
    if journal["phase"] in {"prepared", "backups"}:
        guarded(target, journal)
        journal["phase"] = "backups"
        store.save(target, journal)
        for role in ROLES:
            node = journal["request"]["nodes"][role]
            private.execute(
                target,
                node["containerId"],
                private.BACKUP,
                operation,
                node["oldConfigSha256"],
                "recover" if recovery else "apply",
            )
        journal["phase"] = "nodes"
        store.save(target, journal)
    for role in ROLES:
        node = journal["request"]["nodes"][role]
        guarded(target, journal)
        if journal["stages"][role] == "verified":
            continue
        original = private.backup_configuration(
            target, node["containerId"], operation, node["oldConfigSha256"]
        )
        desired = desired_configuration(
            original,
            node,
            role,
            successor_profile=request_successor(target, journal["request"]).profile,
        )
        if digest(encoded(desired)) != journal["desiredHashes"][role]:
            raise ValueError("Recorded desired policy changed")
        if journal["stages"][role] == "pending":
            if not foundation(target)[1]:
                raise ValueError("Foundation is not ready to advance to another node")
            journal["stages"][role] = "cap-intent"
            store.save(target, journal)
        if journal["stages"][role] == "cap-intent":
            containers = guarded(target, journal)
            budget = load_profile(name=request_successor(target, journal["request"]).profile)[
                "nodes"
            ][role]
            if not check_caps(containers[f"{target.name}-{role}"], budget):
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
                        node["containerId"],
                    ],
                )
            # Do not declare the write phase until the actual cap change is proved.
            actual = runtime.node_identity(target)[f"{target.name}-{role}"]
            if not check_caps(actual, budget) or actual["id"] != node["containerId"]:
                raise ValueError("Enlarged node budget was not observed")
            journal["stages"][role] = "write-intent"
            store.save(target, journal)
        if journal["stages"][role] == "write-intent":
            guarded(target, journal)
            private.write(target, node, operation, desired, recovery=recovery)
            journal["stages"][role] = "restart-intent"
            store.save(target, journal)
        if journal["stages"][role] == "restart-intent":
            guarded(target, journal)
            runtime.command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "exec",
                    node["containerId"],
                    "systemctl",
                    "restart",
                    "kubelet",
                ],
            )
            wait_node(target, journal, role)
            journal["stages"][role] = "verified"
            store.save(target, journal)


def finalize(target: Target, journal: dict, *, recovery: bool) -> dict:
    successor = request_successor(target, journal["request"])
    guarded(target, journal)
    runtime.verify_nodes(runtime.node_identity(target), profile=successor.profile)
    runtime.read_readiness(successor, wait=True)
    if not foundation(target)[1]:
        raise ValueError("Foundation is not ready for ownership finalization")
    if journal["phase"] != "complete":
        journal["phase"] = "finalizing"
        store.save(target, journal)
        current, _, _ = bind(target, journal["request"], journal)
        desired = successor_owner(target, journal)
        if digest(current) == journal["predecessorOwnerHash"]:
            store.replace(target.path("owner.json"), desired, journal["predecessorOwnerHash"])
        elif current != desired or not recovery:
            raise ValueError("Ownership finalization requires explicit recorded recovery")
        bind(target, journal["request"], journal)
        journal["phase"] = "complete"
        store.save(target, journal)
    return {
        "execution": "performed",
        "cluster": target.name,
        "operationId": journal["request"]["operationId"],
        "requestHash": journal["requestHash"],
        "profile": successor.profile,
        "profileDigest": identity(successor)["profileDigest"],
        "incarnation": journal["request"]["incarnation"],
        "journalReference": str(target.path("transition.json")),
        "originalRepairBackupsRetained": True,
        "kindAndKubeconfigBytesPreserved": True,
        "liveFullInstallationQualification": "pending",
        **(
            {
                "qualificationScope": "disposable memory-transition protocol only; CPU growth and full installation unqualified"
            }
            if successor.profile == TRANSITION_TEST_PROFILE
            else {}
        ),
    }


def execute(target: Target, request: dict, action: str) -> dict:
    request = validate_request(target, request)
    if action == "inspect":
        if store.exists(target.path("transition.json")):
            if not store.exists(target.path("transition.lock")):
                raise ValueError("Transition journal has no original lock file")
            with store.locked(target):
                journal = store.load(target, request)
                guarded(target, journal)
                return {
                    "execution": "read-only",
                    "cluster": target.name,
                    "requestHash": journal["requestHash"],
                    "phase": journal["phase"],
                    "stages": journal["stages"],
                    "reviewRequired": "Explicit recovery scope; no retry performed",
                }
        journal = preflight(target, request)
        return {
            "execution": "read-only",
            "cluster": target.name,
            "requestHash": journal["requestHash"],
            "desiredHashes": journal["desiredHashes"],
            "repairBackupHashes": journal["repairBackupHashes"],
            "readyForApproval": True,
            "liveQualification": "pending",
        }
    if action not in {"apply", "recover"}:
        raise ValueError("Unknown transition operation")
    with store.locked(target):
        if action == "apply":
            if store.exists(target.path("transition.json")):
                raise ValueError("Existing transition journal; inspect and explicitly recover")
            journal = preflight(target, request)
            store.exclusive(target.path("transition.json"), encoded(journal))
        else:
            journal = store.load(target, request)
        if journal["phase"] not in {"finalizing", "complete"}:
            advance(target, journal, recovery=action == "recover")
        return finalize(target, journal, recovery=action == "recover")
