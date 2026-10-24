"""Pure contracts for the one reviewed retained profile enlargement."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

from .consumer_profile import Target, configuration, identity, load_profile
from .node_capacity import envelope, kubelet_configuration, quantity, reservations

PREDECESSOR = "isolated-network-local"
SUCCESSOR = "isolated-network-full-local"
FORMAT = "isolated-profile-transition.v1"
ROLES = ("control-plane", "worker")
STAGES = ("pending", "cap-intent", "write-intent", "restart-intent", "verified")
PHASES = ("prepared", "backups", "nodes", "finalizing", "complete")


def encoded(value: object) -> bytes:
    """Canonical private journal/owner bytes, independent of checkout line endings."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paired(target: Target) -> Target:
    if target.profile != PREDECESSOR:
        raise ValueError("Only the reviewed predecessor-to-full transition is supported")
    successor = Target(
        target.consumer, target.docker_context, target.state_root, SUCCESSOR, target.https_port
    )
    if configuration(target) != configuration(successor):
        raise ValueError("Transition would change kind configuration; separate migration required")
    return successor


def _hex(value: object, size: int) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[a-f0-9]{{{size}}}", value) is not None


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def machine(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"cpu", "memory"}:
        raise ValueError("Explicit CPU/memory capacity observation required")
    return {"cpu": quantity(value["cpu"], cpu=True), "memory": quantity(value["memory"])}


def validate_request(target: Target, value: dict) -> dict:
    """Bind reviewed prior evidence; never infer an old policy from live reservations."""
    successor = paired(target)
    expected = {
        "format",
        "operationId",
        "providerRevision",
        "predecessorDigest",
        "successorDigest",
        "incarnation",
        "nodes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["format"] != FORMAT
        or not _hex(value["operationId"], 32)
        or not _hex(value["providerRevision"], 40)
        or not _uuid(value["incarnation"])
        or value["predecessorDigest"] != identity(target)["profileDigest"]
        or value["successorDigest"] != identity(successor)["profileDigest"]
        or not isinstance(value["nodes"], dict)
        or set(value["nodes"]) != set(ROLES)
    ):
        raise ValueError("Transition request does not bind the reviewed identities and profiles")
    for role, node in value["nodes"].items():
        if (
            not isinstance(node, dict)
            or set(node)
            != {"containerId", "uid", "oldConfigSha256", "oldMachineCapacity", "newMachineCapacity"}
            or not _hex(node["containerId"], 64)
            or not _uuid(node["uid"])
            or not _hex(node["oldConfigSha256"], 64)
        ):
            raise ValueError("Transition requires original node IDs, UIDs and config hashes")
        for key, profile in (
            ("oldMachineCapacity", target.profile),
            ("newMachineCapacity", successor.profile),
        ):
            machine(node[key])
            reservations(node[key], load_profile(name=profile)["nodes"][role])
    if (
        len({n["containerId"] for n in value["nodes"].values()}) != 2
        or len({n["uid"] for n in value["nodes"].values()}) != 2
    ):
        raise ValueError("Transition node identities must be distinct")
    return copy.deepcopy(value)


def desired_configuration(current: dict, node: dict, role: str) -> dict:
    """Rebase only a proven predecessor policy, retaining every unrelated setting."""
    old_budget = load_profile(name=PREDECESSOR)["nodes"][role]
    if kubelet_configuration(current, node["oldMachineCapacity"], old_budget) != current:
        raise ValueError("Predecessor must already have the exact reviewed capacity policy")
    unreserved = copy.deepcopy(current)
    del unreserved["systemReserved"]
    del unreserved["kubeReserved"]
    desired = kubelet_configuration(
        unreserved, node["newMachineCapacity"], load_profile(name=SUCCESSOR)["nodes"][role]
    )
    if len(encoded(desired)) > 65536:
        raise ValueError("Transition configuration exceeds the bounded size")
    return desired


def preview(target: Target) -> dict:
    successor = paired(target)
    return {
        "format": FORMAT,
        "execution": "not-performed",
        "cluster": target.name,
        "context": target.context,
        "dockerContext": target.docker_context,
        "predecessor": identity(target),
        "successor": identity(successor),
        "minimumEngine": load_profile(name=SUCCESSOR)["minimumEngine"],
        "schedulerEnvelopes": {
            r: envelope(b) for r, b in load_profile(name=SUCCESSOR)["nodes"].items()
        },
        "effects": "Explicit foundation-only journaled node enlargement, private versioned backups, guarded config writes and at most two kubelet restarts per execution; ownership finalized only after verification",
        "exclusions": [
            "engine resize/restart",
            "cluster replacement",
            "automatic rollback",
            "consumer activation",
        ],
        "liveQualification": "pending",
    }
