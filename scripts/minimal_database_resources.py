"""Finite staged resources for minimal-demo database controller qualification."""

from . import network_resources as network
from .consumer_profile import Target
from .network_fixtures import workloads

STARTUP = (
    "cert-manager-webhook",
    "cert-manager-cainjector",
    "cert-manager",
    "cnpg-controller-manager",
    "barman-cloud",
)


def controllers(groups: dict) -> dict:
    return {
        item["metadata"]["name"]: item
        for group in groups.values()
        for item in group
        if item["kind"] == "Deployment"
    }


def required_images(groups: dict) -> dict[str, list[str]]:
    images = set()
    for item in controllers(groups).values():
        pod = item["spec"]["template"]["spec"]
        images.update(c["image"] for c in [*pod["containers"], *pod.get("initContainers", [])])
    return {"control-plane": [], "worker": sorted(images)}


def stage_fit(
    target: Target, cilium: dict, fixtures: list[dict], active: dict, starting: str | None = None
) -> dict:
    by_role: dict[str, list[dict]] = {role: [] for role in network.ROLES}
    for item in fixtures:
        if item["kind"] == "Pod":
            pod = item["spec"]
            by_role[pod["nodeSelector"][network.ROLE]].append(network.pod_bounds(pod))
    for name, bounds in active.items():
        steady = bounds["steadyLimits"]["memory"]
        peak = bounds["effectiveLimits"]["memory"]
        if peak - steady > network.EXTRA_MEMORY:
            raise ValueError(f"{name} initialization exceeds the reviewed memory allowance")
        by_role["worker"].append(
            {
                **bounds,
                "effectiveLimits": {
                    **bounds["effectiveLimits"],
                    "memory": peak if name == starting else steady,
                },
            }
        )
    return network.stage_fit(target, cilium, fixtures=by_role)


def rendered_bounds(
    target: Target, groups: dict, *, cilium: dict | None = None, fixtures: list[dict] | None = None
) -> dict:
    """Report serial startup, steady state and one confirmed stop-before-start replacement."""
    if cilium is None:
        selection = network.selection()
        cilium = {
            name: network.pod_bounds(
                {
                    "containers": [{"name": name, "resources": selection["main"][name]}],
                    "initContainers": [
                        {"name": init, "resources": resources}
                        for init, resources in selection["init"].items()
                        if name == "cilium"
                    ],
                }
            )
            for name in network.CONTROLLERS
        }
    fixtures = workloads(target) if fixtures is None else fixtures
    manifests = controllers(groups)
    if set(manifests) != set(STARTUP):
        raise ValueError("Unexpected minimal-demo database controller inventory")
    bounds = {
        name: network.pod_bounds(manifests[name]["spec"]["template"]["spec"]) for name in STARTUP
    }
    return {
        "pods": bounds,
        "startupByController": {
            name: stage_fit(target, cilium, fixtures, dict(list(bounds.items())[:index]), name)
            for index, name in enumerate(STARTUP, 1)
        },
        "steady": stage_fit(target, cilium, fixtures, bounds),
        "oneReplacementByController": {
            name: stage_fit(target, cilium, fixtures, bounds, name) for name in STARTUP
        },
    }
