"""Pure contracts for reviewed retained and disposable profile enlargements."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

from .consumer_profile import TRANSITION_TEST_PROFILE, Target, configuration, identity, load_profile
from .node_capacity import envelope, kubelet_configuration, quantity, reservations

PREDECESSOR = "isolated-network-local"
SUCCESSOR = "isolated-network-full-local"
SUCCESSORS = (SUCCESSOR, TRANSITION_TEST_PROFILE)
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


def paired(target: Target, *, successor_profile: str = SUCCESSOR) -> Target:
    if target.profile != PREDECESSOR or successor_profile not in SUCCESSORS:
        raise ValueError("Only reviewed network-profile transition successors are supported")
    successor = Target(
        target.consumer,
        target.docker_context,
        target.state_root,
        successor_profile,
        target.https_port,
    )
    if configuration(target) != configuration(successor):
        raise ValueError("Transition would change kind configuration; separate migration required")
    return successor


def request_successor(target: Target, request: dict) -> Target:
    """Select only an explicit reviewed digest; test scopes require qual- ownership."""
    if not isinstance(request, dict):
        raise ValueError("A reviewed transition request is required")
    for profile in SUCCESSORS:
        if profile == TRANSITION_TEST_PROFILE and not target.consumer.startswith("qual-"):
            continue
        candidate = paired(target, successor_profile=profile)
        if request.get("successorDigest") == identity(candidate)["profileDigest"]:
            return candidate
    raise ValueError("Unreviewed successor digest or non-disposable test consumer")


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
    successor = request_successor(target, value)
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


def desired_configuration(
    current: dict, node: dict, role: str, *, successor_profile: str = SUCCESSOR
) -> dict:
    """Rebase only a proven predecessor policy, retaining every unrelated setting."""
    if successor_profile not in SUCCESSORS:
        raise ValueError("Unreviewed transition successor")
    old_budget = load_profile(name=PREDECESSOR)["nodes"][role]
    if kubelet_configuration(current, node["oldMachineCapacity"], old_budget) != current:
        raise ValueError("Predecessor must already have the exact reviewed capacity policy")
    unreserved = copy.deepcopy(current)
    del unreserved["systemReserved"]
    del unreserved["kubeReserved"]
    desired = kubelet_configuration(
        unreserved, node["newMachineCapacity"], load_profile(name=successor_profile)["nodes"][role]
    )
    if len(encoded(desired)) > 65536:
        raise ValueError("Transition configuration exceeds the bounded size")
    return desired


def preview(target: Target, *, successor_profile: str = SUCCESSOR) -> dict:
    successor = paired(target, successor_profile=successor_profile)
    return {
        "format": FORMAT,
        "execution": "not-performed",
        "cluster": target.name,
        "context": target.context,
        "dockerContext": target.docker_context,
        "predecessor": identity(target),
        "successor": identity(successor),
        "minimumEngine": load_profile(name=successor.profile)["minimumEngine"],
        "schedulerEnvelopes": {
            r: envelope(b) for r, b in load_profile(name=successor.profile)["nodes"].items()
        },
        "effects": "Explicit foundation-only journaled node enlargement, private versioned backups, guarded config writes and at most two kubelet restarts per execution; ownership finalized only after verification",
        "exclusions": [
            "engine resize/restart",
            "cluster replacement",
            "automatic rollback",
            "consumer activation",
        ],
        "liveQualification": "pending",
        **(
            {
                "qualificationScope": "disposable memory-transition protocol only; CPU growth and full installation unqualified"
            }
            if successor.profile == TRANSITION_TEST_PROFILE
            else {}
        ),
    }
