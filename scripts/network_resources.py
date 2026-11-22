"""Finite resource policy for the minimal-demo Cilium controllers and probes."""

from __future__ import annotations

from .consumer_profile import LABEL, Target, load_profile
from .node_capacity import envelope, quantity

ROLE = "k8s-infrastructure.io/role"
MIB = 1024**2
ROLES = ("control-plane", "worker")
CONTROLLERS = ("cilium", "cilium-envoy", "cilium-operator")
INIT = {
    "config": 512,
    "apply-sysctl-overwrites": 512,
    "mount-bpf-fs": 512,
    "clean-cilium-state": 512,
    "install-cni-binaries": 1024,
}
SYSTEM = {
    "control-plane": {"cpu": 700, "memory": 2048 * MIB},
    "worker": {"cpu": 100, "memory": 256 * MIB},
}
SYSTEM_CPU_LIMIT = {"control-plane": 1500, "worker": 500}
EXTRA_MEMORY = 512 * MIB


def resources(cpu_request: int, cpu_limit: int, memory_request: int, memory_limit: int) -> dict:
    return {
        "requests": {"cpu": f"{cpu_request}m", "memory": f"{memory_request}Mi"},
        "limits": {"cpu": f"{cpu_limit}m", "memory": f"{memory_limit}Mi"},
    }


MAIN = {
    "cilium": resources(100, 1000, 256, 512),
    "cilium-envoy": resources(50, 500, 64, 128),
    "cilium-operator": resources(50, 500, 128, 256),
}


def pod_bounds(spec: dict) -> dict:
    """Account for serial init, concurrent sidecars, steady containers and overhead."""
    if spec.get("resources") or spec.get("runtimeClassName"):
        raise ValueError("Pod-level resources or RuntimeClass require independent admission review")
    app, init = spec["containers"], spec.get("initContainers", [])
    names = [c["name"] for c in [*app, *init]]
    if not app or len(names) != len(set(names)):
        raise ValueError("Missing or ambiguous Pod container inventory")
    overhead = spec.get("overhead", {})
    if set(overhead) - {"cpu", "memory"}:
        raise ValueError("Unreviewed Pod overhead resource")
    result: dict = {
        "containers": {
            c["name"]: {
                "stage": (
                    "regular"
                    if c in app
                    else "restartable-init"
                    if c.get("restartPolicy") == "Always"
                    else "init"
                ),
                "resources": c.get("resources", {}),
            }
            for c in [*app, *init]
        },
        "overhead": {},
    }
    if any(
        set(budget) - {"cpu", "memory"}
        for c in [*app, *init]
        for budget in c.get("resources", {}).values()
    ):
        raise ValueError("Unreviewed container resource dimension")
    for resource in ("cpu", "memory"):
        is_cpu = resource == "cpu"
        extra = quantity(str(overhead.get(resource, "0")), cpu=is_cpu, allow_zero=True)
        result["overhead"][resource] = extra
        for field in ("requests", "limits"):
            amounts = {}
            for c in [*app, *init]:
                raw = c.get("resources", {}).get(field, {}).get(resource)
                if raw is None:
                    raise ValueError(f"Container {c['name']} lacks finite {resource} {field}")
                amounts[c["name"]] = quantity(str(raw), cpu=is_cpu)
                if field == "limits":
                    request = c["resources"]["requests"][resource]
                    if quantity(str(request), cpu=is_cpu) > amounts[c["name"]]:
                        raise ValueError(f"Container {c['name']} {resource} request exceeds limit")
                    if (
                        c in init
                        and resource == "memory"
                        and quantity(str(request)) != amounts[c["name"]]
                    ):
                        raise ValueError(f"Initializer {c['name']} must fully request its memory")
            sidecars = 0
            startup = 0
            for c in init:
                amount = amounts[c["name"]]
                startup = max(startup, sidecars + amount)
                if c.get("restartPolicy") == "Always":
                    sidecars += amount
            steady = sidecars + sum(amounts[c["name"]] for c in app)
            result.setdefault("steady" + field.title(), {})[resource] = steady + extra
            result.setdefault("effective" + field.title(), {})[resource] = (
                max(steady, startup) + extra
            )
    return result


def roles_for(name: str) -> tuple[str, ...]:
    return ("worker",) if name == "cilium-operator" else ROLES


def selection() -> dict:
    return {
        "main": MAIN,
        "init": {name: resources(100, 1000, memory, memory) for name, memory in INIT.items()},
        "placement": {name: roles_for(name) for name in CONTROLLERS},
        "rollout": "delete-before-replacement-v1",
        "systemReservation": SYSTEM,
        "systemCPULimitMillicores": SYSTEM_CPU_LIMIT,
        "extraMemoryBytes": EXTRA_MEMORY,
    }


def configure(target: Target, controllers: dict[str, dict]) -> None:
    """Set profile-scoped resources on the pinned chart's exact container inventory."""
    for name, item in controllers.items():
        spec = item["spec"]
        pod = spec["template"]["spec"]
        main = pod["containers"]
        initial = pod.get("initContainers", [])
        expected_main = "cilium-agent" if name == "cilium" else name
        if (
            [c["name"] for c in main] != [expected_main]
            or [c["name"] for c in initial] != (list(INIT) if name == "cilium" else [])
            or any(c.get("restartPolicy") for c in initial)
        ):
            raise ValueError("Minimal-demo Cilium container inventory changed")
        main[0]["resources"] = MAIN[name]
        for c in initial:
            c["resources"] = selection()["init"][c["name"]]
        pod["nodeSelector"] = {
            LABEL: target.consumer,
            **({ROLE: "worker"} if name == "cilium-operator" else {}),
        }
        if item["kind"] == "Deployment":
            spec["strategy"] = {"type": "Recreate"}
        else:
            spec["updateStrategy"] = {"type": "OnDelete"}
        pod["automountServiceAccountToken"] = True


def stage_fit(
    target: Target,
    stages: dict[str, dict],
    *,
    next_controller: str | None = None,
    fixtures: dict[str, list[dict]] | None = None,
) -> dict:
    """Apply scheduler requests and concurrent memory limits on each fixed node."""
    budgets = load_profile(name=target.profile)["nodes"]
    results = {}
    for role, reserved in SYSTEM.items():
        cpu, mem_requests, mem_limits = reserved["cpu"], reserved["memory"], reserved["memory"]
        for name, bounds in stages.items():
            if role not in roles_for(name):
                continue
            cpu += bounds["effectiveRequests"]["cpu"]
            mem_requests += bounds["effectiveRequests"]["memory"]
            startup = bounds["effectiveLimits"]["memory"]
            steady = bounds["steadyLimits"]["memory"]
            if startup - steady > EXTRA_MEMORY:
                raise ValueError(f"{name} initialization exceeds the reviewed memory allowance")
            mem_limits += startup if name == next_controller else steady
        for bounds in (fixtures or {}).get(role, []):
            cpu += bounds["effectiveRequests"]["cpu"]
            mem_requests += bounds["effectiveRequests"]["memory"]
            mem_limits += bounds["effectiveLimits"]["memory"]
        limit = envelope(budgets[role])
        if cpu > limit["cpuMillicores"] or max(mem_requests, mem_limits) > limit["memoryBytes"]:
            raise ValueError(f"Cilium startup exceeds the {role} ledger envelope")
        results[role] = {
            "cpuRequestedMillicores": cpu,
            "memoryRequestedBytes": mem_requests,
            "activeMemoryLimitBytes": mem_limits,
            "schedulerEnvelope": limit,
        }
    return results


def controllers(documents: list[dict]) -> dict[str, dict]:
    workloads = [item for item in documents if item["kind"] in {"Deployment", "DaemonSet"}]
    named = {item["metadata"]["name"]: item for item in workloads}
    if (
        set(named) != set(CONTROLLERS)
        or len(workloads) != len(CONTROLLERS)
        or any(
            named[name]["kind"] != ("Deployment" if name == "cilium-operator" else "DaemonSet")
            for name in CONTROLLERS
        )
    ):
        raise ValueError("Unexpected network controller inventory")
    return named


def rendered_bounds(target: Target, documents: list[dict]) -> dict:
    """Validate every initial startup stage and one later replacement without surge."""
    accounted: dict[str, dict] = {}
    stages = {}
    workloads = controllers(documents)
    for name in CONTROLLERS:
        pod = workloads[name]["spec"]["template"]["spec"]
        accounted[name] = pod_bounds(pod)
        stages[name] = stage_fit(target, accounted, next_controller=name)
    return {
        "pods": accounted,
        "startupByController": stages,
        "oneReplacementByController": {
            name: stage_fit(target, accounted, next_controller=name) for name in CONTROLLERS
        },
        "systemReservation": SYSTEM,
        "systemCPULimitMillicores": SYSTEM_CPU_LIMIT,
    }


def normalized(bounds: dict) -> dict:
    """Compare every individual resource family after API quantity normalization."""
    return {
        **bounds,
        "containers": {
            name: {
                "stage": container["stage"],
                "resources": {
                    field: {
                        key: quantity(str(value), cpu=key == "cpu") for key, value in budget.items()
                    }
                    for field, budget in container["resources"].items()
                },
            }
            for name, container in bounds["containers"].items()
        },
    }
