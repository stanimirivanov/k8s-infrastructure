"""Product-neutral physical host admission; saved observations are preview evidence only."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

GIB = 1024**3
POLICY = "physical-host-admission.v1"
CATEGORIES = {"retained": 11 * GIB, "images": 12 * GIB, "runtime": 4 * GIB, "preparation": 8 * GIB}
MAX_AGE_SECONDS = 60


class Refusal(ValueError):
    """A sanitized policy refusal safe to include in a public receipt."""


def digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise Refusal("Missing or invalid nonnegative integer metric")
    return value


def validate(request: dict) -> None:
    """Require explicit consumer bounds; these are reservations, not measured minima."""
    if request.get("version") != 1 or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,63}", request.get("target", "")
    ):
        raise Refusal("Invalid request version or target")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", request.get("artifactDigest", "")):
        raise Refusal("Immutable consumer artifact digest required")
    for field in ("memory", "disk"):
        items = request.get(field)
        if not isinstance(items, list) or not 1 <= len(items) <= 64:
            raise Refusal("Missing or oversized resource accounts")
        names = [item.get("id") for item in items]
        if any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
            for name in names
        ) or len(set(names)) != len(names):
            raise Refusal("Invalid or duplicate account identity")
        for item in items:
            integer(item.get("limitBytes"))
    kinds = {item.get("kind") for item in request["memory"]}
    if kinds != {"workload", "browser", "tooling"}:
        raise Refusal("Explicit workload, browser and tooling accounts required")
    tools = [item for item in request["memory"] if item["kind"] == "tooling"]
    if (
        sum(item["limitBytes"] for item in tools) > 2 * GIB
        or sum(integer(item.get("cpuMillicores")) for item in tools) > 1000
    ):
        raise Refusal("Aggregate host tooling exceeds one CPU or 2GiB")
    if {item.get("category") for item in request["disk"]} != set(CATEGORIES):
        raise Refusal("All four physical disk categories required")


def evaluate(request: dict, observation: dict, *, host_id: str, now: int) -> dict:
    """Pure incremental decision. The live caller supplies current host identity/time."""
    validate(request)
    if (
        observation.get("version") != 1
        or observation.get("requestDigest") != digest(request)
        or observation.get("target") != request["target"]
        or observation.get("hostId") != host_id
    ):
        raise Refusal("Stale request, target or host identity")
    age = integer(now) - integer(observation.get("observedAt"))
    if not 0 <= age <= MAX_AGE_SECONDS:
        raise Refusal("Stale or future observation; observe again")
    for field in ("memory", "disk"):
        if set(observation.get(field, {})) != {item["id"] for item in request[field]}:
            raise Refusal("Observation account inventory differs from request")
    incremental_memory = 0
    for item in request["memory"]:
        resident = integer(observation.get("memory", {}).get(item["id"], {}).get("residentBytes"))
        if resident > item["limitBytes"]:
            raise Refusal("Resident memory exceeds declared bound")
        incremental_memory += item["limitBytes"] - resident
    remaining_memory = integer(observation.get("memAvailableBytes")) - incremental_memory
    stores: dict[str, dict] = {}
    physical: dict[str, tuple] = {}
    categories = dict.fromkeys(CATEGORIES, 0)
    for item in request["disk"]:
        metric = observation.get("disk", {}).get(item["id"], {})
        filesystem, account = metric.get("filesystemId"), metric.get("physicalId")
        if (
            not isinstance(filesystem, str)
            or not filesystem
            or not isinstance(account, str)
            or not account
        ):
            raise Refusal("Missing physical filesystem/account identity")
        available, resident = (
            integer(metric.get("availableBytes")),
            integer(metric.get("residentBytes")),
        )
        if resident > item["limitBytes"]:
            raise Refusal("Resident disk exceeds declared bound")
        signature = (filesystem, item["category"], item["limitBytes"], resident)
        if account in physical and physical[account] != signature:
            raise Refusal("Conflicting aliases for physical account")
        store = stores.setdefault(filesystem, {"available": available, "incremental": 0})
        if store["available"] != available:
            raise Refusal("Conflicting shared filesystem observations")
        if account not in physical:
            physical[account] = signature
            categories[item["category"]] += item["limitBytes"]
            store["incremental"] += item["limitBytes"] - resident
    if any(categories[key] > bound for key, bound in CATEGORIES.items()):
        raise Refusal("Physical disk category exceeds approved envelope")
    filesystems: list[dict[str, Any]] = [
        {
            "filesystemId": key,
            "incrementalBytes": value["incremental"],
            "remainingBytes": value["available"] - value["incremental"],
        }
        for key, value in sorted(stores.items())
    ]
    admitted = remaining_memory >= 4 * GIB and all(
        item["remainingBytes"] >= 10 * GIB for item in filesystems
    )
    return {
        "policy": POLICY,
        "requestDigest": digest(request),
        "artifactDigest": request["artifactDigest"],
        "target": request["target"],
        "hostId": host_id,
        "observedAt": observation["observedAt"],
        "admitted": admitted,
        "memoryIncrementalBytes": incremental_memory,
        "memoryRemainingBytes": remaining_memory,
        "filesystems": filesystems,
        "reasons": ([] if remaining_memory >= 4 * GIB else ["insufficient-memory"])
        + (
            []
            if all(item["remainingBytes"] >= 10 * GIB for item in filesystems)
            else ["insufficient-disk"]
        ),
        "installedAcceptance": False,
    }
