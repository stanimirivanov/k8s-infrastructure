"""Pure scheduler-envelope policy for Docker-capped kind nodes.

Kubelet reports machine capacity even inside a capped kind container. Account for
that inaccessible capacity using systemReserved; kubeReserved and memory eviction
then leave real headroom inside the container. Never rewrite Node status.
"""

from __future__ import annotations

import copy
import re
from decimal import Decimal, localcontext
from typing import Any

CPU_HEADROOM_M = 250
MEMORY_HEADROOM = 512 * 1024**2
EVICTION_MEMORY = 100 * 1024**2
POLICY = "isolated-kind-allocatable.v1"


class AllocatableMismatch(ValueError):
    """Valid observations whose scheduling envelope has not converged."""


def quantity(value: object, *, cpu: bool = False, allow_zero: bool = False) -> int:
    """Parse bounded Kubernetes CPU/memory quantities without float rounding.

    Node status uses cores/millicores and bytes/binary units. Accept exact SI
    subunits too, but refuse negative, non-finite, fractional base units and
    unexpected encodings instead of silently rounding an unsafe observation.
    """
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid node resource quantity")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)", value)
    if match is None:
        raise ValueError("Invalid node resource quantity")
    number, suffix = match.groups()
    units = (
        {"": 1000, "m": 1, "u": Decimal("0.001"), "n": Decimal("0.000001")}
        if cpu
        else {
            "": 1,
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "k": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
        }
    )
    if suffix not in units:
        raise ValueError("Unsupported node resource quantity unit")
    with localcontext() as context:
        context.prec = 80  # Bounded 40-character observations and unit scaling stay exact.
        result = Decimal(number) * units[suffix]
    if (
        result != result.to_integral_value()
        or result < 0
        or (result == 0 and not allow_zero)
        or result > 2**63 - 1
    ):
        raise ValueError("Invalid or inexact node resource quantity")
    return int(result)


def envelope(budget: dict) -> dict[str, int]:
    """The Pod envelope includes system Pods; daemons and eviction are separate."""
    cpu = budget.get("cpus")
    memory = budget.get("memoryBytes")
    if type(cpu) is not int or type(memory) is not int:
        raise ValueError("Node budget must use integer CPUs and bytes")
    if cpu * 1000 <= CPU_HEADROOM_M or memory <= MEMORY_HEADROOM + EVICTION_MEMORY:
        raise ValueError("Node budget leaves no Pod scheduling capacity")
    return {
        "cpuMillicores": cpu * 1000 - CPU_HEADROOM_M,
        "memoryBytes": memory - MEMORY_HEADROOM - EVICTION_MEMORY,
    }


def reservations(capacity: dict, budget: dict) -> dict:
    """Exclude host-only resources before reserving genuine in-container headroom."""
    envelope(budget)
    cpu = quantity(capacity.get("cpu"), cpu=True)
    memory = quantity(capacity.get("memory"))
    if cpu < budget["cpus"] * 1000 or memory < budget["memoryBytes"]:
        raise ValueError("Reported machine capacity is smaller than the enforced node budget")
    return {
        "systemReserved": {
            "cpu": f"{cpu - budget['cpus'] * 1000}m",
            "memory": str(memory - budget["memoryBytes"]),
        },
        "kubeReserved": {"cpu": f"{CPU_HEADROOM_M}m", "memory": str(MEMORY_HEADROOM)},
    }


def kubelet_configuration(current: dict, capacity: dict, budget: dict) -> dict:
    """Preserve unrelated settings; never overwrite an unknown reservation policy.

    Existing configs with these exact reservations are idempotent. A different
    engine size/reservation needs explicit review; no automatic rebasing of an
    operator's settings is allowed. Default disk eviction thresholds are retained.
    """
    if (
        not isinstance(current, dict)
        or current.get("kind") != "KubeletConfiguration"
        or current.get("apiVersion") != "kubelet.config.k8s.io/v1beta1"
    ):
        raise ValueError("Unexpected kubelet configuration")
    desired = reservations(capacity, budget)
    for key, value in desired.items():
        if current.get(key) not in (None, {}, value):
            raise ValueError("Foreign kubelet reservations; explicit compatibility review required")
    if (
        current.get("enforceNodeAllocatable", ["pods"]) != ["pods"]
        or current.get("cgroupsPerQOS", True) is not True
    ):
        raise ValueError("Unsupported kubelet allocatable enforcement")
    if (
        current.get("reservedSystemCPUs")
        or current.get("evictionSoft")
        or current.get("cpuManagerPolicy", "none") != "none"
        or current.get("memoryManagerPolicy", "None") != "None"
    ):
        raise ValueError("Foreign kubelet resource management policy")
    eviction = current.get("evictionHard")
    if eviction is None:
        eviction = {
            "memory.available": "100Mi",
            "nodefs.available": "10%",
            "nodefs.inodesFree": "5%",
            "imagefs.available": "15%",
        }
    if not isinstance(eviction, dict) or eviction.get("memory.available", "100Mi") != "100Mi":
        raise ValueError("Unexpected kubelet memory eviction threshold")
    result = copy.deepcopy(current)
    result.update(desired)
    result.update(enforceNodeAllocatable=["pods"], cgroupsPerQOS=True)
    result["evictionHard"] = {**eviction, "memory.available": "100Mi"}
    return result


def verify_allocatable(status: dict, budget: dict) -> dict[str, Any]:
    """Health requires the exact safe envelope, not merely a non-empty quantity."""
    capacity = status.get("capacity", {})
    reservations(capacity, budget)
    observed = status.get("allocatable", {})
    expected = envelope(budget)
    if (
        quantity(observed.get("cpu"), cpu=True) != expected["cpuMillicores"]
        or quantity(observed.get("memory")) != expected["memoryBytes"]
    ):
        raise AllocatableMismatch(
            "Node allocatable differs from capped envelope; preview explicit capacity repair"
        )
    if not re.fullmatch(r"[1-9][0-9]{0,3}", str(observed.get("pods", ""))):
        raise ValueError("Node Pod capacity is invalid")
    return {
        "rawMachineCapacity": {key: capacity[key] for key in ("cpu", "memory")},
        "schedulerEnvelope": expected,
        "capacityPolicy": POLICY,
    }


def pod_requests(pod: dict) -> dict[str, int]:
    """Conservative request accounting for the initial foundation-only repair.

    Traditional init containers run sequentially: use the maximum of application
    sums and each init request, then add Pod overhead. Advanced resize, Pod-level
    resources and restartable init sidecars require a separate reviewed repair
    path, rather than guessing their scheduling semantics.
    """
    spec = pod.get("spec", {})
    if (
        spec.get("resources")
        or pod.get("status", {}).get("resize")
        or any(c.get("restartPolicy") == "Always" for c in spec.get("initContainers", []))
    ):
        raise ValueError("Unsupported Pod resource/resize policy during capacity repair")
    result = {}
    for resource in ("cpu", "memory"):

        def request(value, *, is_cpu=resource == "cpu"):
            return quantity(value, cpu=is_cpu, allow_zero=True)

        app = sum(
            request(c.get("resources", {}).get("requests", {}).get(resource, "0"))
            for c in spec.get("containers", [])
        )
        initial = max(
            (
                request(c.get("resources", {}).get("requests", {}).get(resource, "0"))
                for c in spec.get("initContainers", [])
            ),
            default=0,
        )
        result[resource] = max(app, initial) + request(spec.get("overhead", {}).get(resource, "0"))
    return result
