"""Explicit, staged Cilium and traffic qualification for an admitted disposable target."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from . import bootstrap_failure as failure
from . import consumer_runtime as runtime
from . import (
    host_admission,
    host_observation,
    minimal_bootstrap,
    minimal_foundation,
    network_qualification,
)
from . import network_resources as resources
from . import network_runtime as network
from .consumer_profile import MINIMAL_PROFILE, Target, identity, load_profile
from .kubernetes import contains, kube, reference
from .network_assets import (
    RELEASE_FILE,
    SELECTION_LABEL,
    download_bounded,
    ownership_labels,
    render,
    selection,
)
from .network_fixtures import workloads

STATIC_PODS = ("etcd", "kube-apiserver", "kube-controller-manager", "kube-scheduler")
FOUNDATION: dict[tuple[str, str, str | None], int] = {
    ("Deployment", "coredns", "kube-system"): 2,
    ("Deployment", "local-path-provisioner", "local-path-storage"): 1,
}


def check_foundation_bounds(requested: dict, limits: dict | None = None) -> None:
    """Compare witnessed requests against the approved aggregate system allowance."""
    total = sum(budget["cpu"] for budget in requested.values())
    allowance = sum(budget["cpu"] for budget in resources.SYSTEM.values())
    if total > allowance:
        raise ValueError(
            f"Stock foundation CPU requests total {total}m; system allowance is {allowance}m"
        )
    pending = requested["unassigned"]
    for role, budget in resources.SYSTEM.items():
        if any(requested[role][key] + pending[key] > limit for key, limit in budget.items()):
            raise ValueError(
                f"Observed or pending foundation requests exceed the {role} system allowance"
            )
        if limits is not None and (
            limits[role]["cpu"] + limits["unassigned"]["cpu"] > resources.SYSTEM_CPU_LIMIT[role]
            or limits[role]["memory"] + limits["unassigned"]["memory"] > budget["memory"]
        ):
            raise ValueError(
                f"Observed or pending foundation limits exceed the {role} system allowance"
            )


def list_resources(target: Target, names: str) -> list[dict]:
    return json.loads(kube(target, ["get", names, "-A", "-o", "json"]))["items"]


def owner_uid(item: dict) -> str | None:
    owners = [
        owner["uid"]
        for owner in item["metadata"].get("ownerReferences", [])
        if owner.get("controller") is True
    ]
    if len(owners) > 1:
        raise ValueError("Ambiguous controlling owner in the installed network inventory")
    return next(iter(owners), None)


def foundation(
    target: Target,
    installed: set[str],
    fixture_uids: set[str],
    selection_digest: str,
    *,
    additional: dict[tuple, str] | None = None,
) -> dict:
    """Inspect all Pod producers and residents before admitting another workload."""
    if list_resources(target, "persistentvolumeclaims,persistentvolumes"):
        raise ValueError("Existing persistent storage prevents disposable Cilium qualification")
    controllers = list_resources(
        target, "deployments,daemonsets,statefulsets,jobs,cronjobs,replicationcontrollers"
    )
    foundation_ids = set()
    network_ids = set()
    additional = additional or {}
    additional_ids = set()
    network_labels = ownership_labels(target, selection_digest)
    for item in controllers:
        key = reference(item)
        meta = item["metadata"]
        if meta.get("deletionTimestamp"):
            raise ValueError("Terminating controller in the disposable target")
        if key in FOUNDATION and item["spec"]["replicas"] == FOUNDATION[key]:
            foundation_ids.add(meta["uid"])
        elif (
            key[1] in installed
            and key[2] == "kube-system"
            and all(meta.get("labels", {}).get(k) == v for k, v in network_labels.items())
        ):
            network_ids.add(meta["uid"])
        elif key in additional and meta["uid"] == additional[key]:
            additional_ids.add(meta["uid"])
        else:
            raise ValueError("Unexpected controller in otherwise disposable Cilium target")
    if (
        len(foundation_ids) != len(FOUNDATION)
        or len(network_ids) != len(installed)
        or additional_ids != set(additional.values())
    ):
        raise ValueError("Incomplete foundation or staged Cilium controller inventory")
    network_ids.update(additional_ids)
    for replica in list_resources(target, "replicasets"):
        uid = owner_uid(replica)
        if uid in foundation_ids:
            foundation_ids.add(replica["metadata"]["uid"])
        elif uid in network_ids:
            network_ids.add(replica["metadata"]["uid"])
        else:
            raise ValueError("Unowned ReplicaSet in disposable Cilium target")
    observed = {role: {"cpu": 0, "memory": 0} for role in (*resources.ROLES, "unassigned")}
    limits = {role: {"cpu": 0, "memory": 0} for role in observed}
    expected = {
        name: (name, minimal_foundation.resources(budget))
        for name, budget in minimal_foundation.CONTROL_PLANE.items()
    }
    expected.update(
        {
            "coredns": ("coredns", minimal_foundation.resources(minimal_foundation.COREDNS)),
            "local-path-provisioner": (
                "local-path-provisioner",
                minimal_foundation.resources(minimal_foundation.PROVISIONER),
            ),
        }
    )
    counts: dict[str, int] = {}
    pods = list_resources(target, "pods")
    inventory = []
    for pod in pods:
        meta, spec = pod["metadata"], pod["spec"]
        if owner_uid(pod) in network_ids or meta["uid"] in fixture_uids:
            continue
        ns, name = meta["namespace"], meta["name"]
        node = spec.get("nodeName")
        role = node.removeprefix(f"{target.name}-") if node else "unassigned"
        component = name.removesuffix(f"-{target.name}-control-plane")
        if (
            ns == "kube-system"
            and component in STATIC_PODS
            and node == f"{target.name}-control-plane"
            and meta.get("annotations", {}).get("kubernetes.io/config.mirror")
        ):
            pass
        elif owner_uid(pod) not in foundation_ids:
            raise ValueError("Unexpected Pod in otherwise disposable Cilium target")
        elif spec.get("nodeSelector", {}).get(resources.ROLE) != "worker" or role not in {
            "worker",
            "unassigned",
        }:
            raise ValueError("Foundation add-on is not fixed to its reviewed worker placement")
        else:
            role = "worker"
            component = spec["containers"][0]["name"]
        statuses = pod.get("status", {})
        if (
            role not in observed
            or (node and node != f"{target.name}-{role}")
            or meta.get("deletionTimestamp")
            or statuses.get("phase") in {"Failed", "Succeeded", "Unknown"}
            or any(
                c.get("restartCount") != 0
                for c in [
                    *statuses.get("initContainerStatuses", []),
                    *statuses.get("containerStatuses", []),
                ]
            )
        ):
            raise ValueError(
                "Foundation Pod has failed, restarted, terminated or changed placement"
            )
        bounds = resources.pod_bounds(spec)
        expected_name, expected_resources = expected.get(component, (None, None))
        wanted = {"containers": [{"name": expected_name, "resources": expected_resources}]}
        if (
            expected_resources is None
            or spec.get("ephemeralContainers")
            or resources.normalized(bounds) != resources.normalized(resources.pod_bounds(wanted))
        ):
            raise ValueError(
                f"Foundation component {component} differs from the reviewed finite budget"
            )
        counts[component] = counts.get(component, 0) + 1
        for key in observed[role]:
            observed[role][key] += bounds["effectiveRequests"][key]
            limits[role][key] += bounds["effectiveLimits"][key]
        inventory.append(
            {
                "component": component
                if component in STATIC_PODS
                else spec["containers"][0]["name"],
                "role": role,
                "requests": bounds["effectiveRequests"],
                "limits": bounds["effectiveLimits"],
                "containers": [
                    {"name": c["name"], "resources": c.get("resources", {})}
                    for c in [*spec.get("initContainers", []), *spec["containers"]]
                ],
                "overhead": spec.get("overhead", {}),
            }
        )
    if counts != {**dict.fromkeys(STATIC_PODS, 1), "local-path-provisioner": 1, "coredns": 2}:
        raise ValueError("Incomplete foundation Pod inventory or unexpected replacement overlap")
    return {
        "systemRequestsByRole": observed,
        "systemLimitsByRole": limits,
        "podInventory": inventory,
    }


def reserve_receipt(
    target: Target, *, capability: Literal["network", "database", "delegation"] = "network"
) -> Path:
    path = target.directory / f"{target.name}.{capability}-qualification.json"
    if target.directory.is_symlink():
        raise ValueError("Redirected capability evidence path")
    # An existing receipt blocks retry, even when interrupted before the first write.
    with path.open("x", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
    return path


def save_receipt(path: Path, evidence: dict) -> None:
    descriptor, pending_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    pending = Path(pending_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(evidence, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def completed_containers(pod: dict, bounds: dict, staged: dict) -> dict:
    """Require actual successful init terminations and ready unchanged container instances."""
    status = pod["status"]
    regular = {c["name"]: c for c in status.get("containerStatuses", [])}
    initial = {c["name"]: c for c in status.get("initContainerStatuses", [])}
    actual = {**regular, **initial}
    if (
        status["phase"] != "Running"
        or not any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        )
        or set(actual) != set(bounds["containers"])
        or set(regular) & set(initial)
    ):
        raise ValueError("Network Pod never reached complete ready state")
    output = {}
    finished = ""
    for c in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
        name = c["name"]
        snapshot = actual[name]
        state = snapshot.get("state", {})
        expected = bounds["containers"][name]["stage"]
        phase = "terminated" if expected == "init" else "running"
        observed = state.get(phase, {})
        started = observed.get("startedAt", "")
        image = snapshot.get("imageID", "")
        if (
            snapshot.get("restartCount") != 0
            or snapshot.get("lastState")
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", started)
            or started < finished
        ):
            raise ValueError(
                f"Incomplete, restarted or changed network container evidence for {name}"
            )
        image_identity = re.sub(r"^(?:containerd|docker-pullable)://", "", image)
        admitted = staged.get(c["image"], {})
        if not image_identity or image_identity not in {
            admitted.get("configDigest"),
            admitted.get("repoDigest"),
        }:
            raise ValueError(f"Network container {name} did not run the staged image on its node")
        output[name] = {"imageID": image, "startedAt": started, "restartCount": 0}
        if phase == "terminated":
            finished = observed.get("finishedAt", "")
            if (
                observed.get("exitCode") != 0
                or observed.get("reason") == "OOMKilled"
                or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", finished)
                or finished < started
            ):
                raise ValueError(f"Initializer {name} did not complete successfully")
            output[name].update(exitCode=0, finishedAt=finished)
        elif snapshot.get("ready") is not True:
            raise ValueError(f"Network container {name} is unready")
    return output


TOKEN_MOUNT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"
# The pinned Kubernetes ServiceAccount admission plugin adds this projection.
TOKEN_PROJECTION = {
    "defaultMode": 420,
    "sources": [
        {"serviceAccountToken": {"path": "token", "expirationSeconds": 3607}},
        {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
        {
            "downwardAPI": {
                "items": [
                    {
                        "path": "namespace",
                        "fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"},
                    }
                ]
            }
        },
    ],
}


def verify_pod_authority(template: dict, pod: dict) -> None:
    defaults = {
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "hostUsers": True,
        "shareProcessNamespace": False,
        "serviceAccountName": "default",
        "automountServiceAccountToken": None,
        "securityContext": {},
        "imagePullSecrets": [],
    }
    if any(
        template.get(key, default) != pod.get(key, default) for key, default in defaults.items()
    ):
        raise ValueError("Network Pod authority differs from the reviewed specification")

    token_mount = None
    volumes = []
    for volume in pod.get("volumes", []):
        name = volume["name"]
        if (
            template.get("automountServiceAccountToken") is True
            and token_mount is None
            and re.fullmatch(r"kube-api-access-[a-z0-9]{5}", name)
            and volume == {"name": name, "projected": TOKEN_PROJECTION}
        ):
            token_mount = {"name": name, "mountPath": TOKEN_MOUNT_PATH, "readOnly": True}
        else:
            volumes.append(volume)
    if not contains(template.get("volumes", []), volumes):
        raise ValueError("Network Pod volume authority differs from the reviewed specification")

    for group in ("containers", "initContainers"):
        for expected, actual in zip(template.get(group, []), pod.get(group, []), strict=True):
            wanted_mounts = expected.get("volumeMounts", [])
            if token_mount and all(
                mount["mountPath"] != TOKEN_MOUNT_PATH for mount in wanted_mounts
            ):
                wanted_mounts = [*wanted_mounts, token_mount]
            mounts = [{"readOnly": False, **mount} for mount in actual.get("volumeMounts", [])]
            if (
                expected["name"] != actual["name"]
                or expected.get("securityContext", {}) != actual.get("securityContext", {})
                or mounts != [{"readOnly": False, **mount} for mount in wanted_mounts]
                or any(
                    not contains(expected.get(field, []), actual.get(field, []))
                    for field in ("args", "command", "env", "envFrom", "volumeDevices")
                )
            ):
                raise ValueError(
                    "Network container authority differs from the reviewed specification"
                )


def controller_pods(
    target: Target, controllers: dict[str, dict], accounted: dict, staged: dict
) -> dict:
    """Tie Ready Pods to their admitted owner, fixed node and exact running resource bounds."""
    references = list(controllers.values())
    found = network.inventory(target, references)
    count_expected = sum(len(resources.roles_for(name)) for name in controllers)
    for expected in references:
        actual = found.get(reference(expected))
        if actual is None or not contains(
            network.comparable_spec(expected, minimal=True),
            network.comparable_spec(actual, minimal=True),
        ):
            raise ValueError("Admitted Cilium controller template drifted")
        network.verify_pod_template(expected, actual)
    uids = {item["metadata"]["uid"]: key[1] for key, item in found.items()}
    for replica in list_resources(target, "replicasets"):
        owner = owner_uid(replica)
        if owner in uids:
            uids[replica["metadata"]["uid"]] = uids[owner]
    evidence = {}
    for pod in list_resources(target, "pods"):
        controller = uids.get(owner_uid(pod))
        if controller is None:
            continue
        spec = pod["spec"]
        node = spec.get("nodeName", "")
        role = node.removeprefix(f"{target.name}-")
        key = f"{controller}/{role}"
        template = controllers[controller]["spec"]["template"]["spec"]
        actual = resources.pod_bounds(spec)
        wanted_images = {
            c["name"]: c["image"]
            for c in template.get("initContainers", []) + template["containers"]
        }
        images = {
            c["name"]: c["image"] for c in spec.get("initContainers", []) + spec["containers"]
        }
        # The API canonicalizes whole CPU quantities, so compare numeric bounds
        # and the complete container layout rather than textual quantities.
        if (
            pod["metadata"].get("deletionTimestamp")
            or spec.get("ephemeralContainers")
            or role not in resources.roles_for(controller)
            or node != f"{target.name}-{role}"
            or key in evidence
            or images != wanted_images
            or any(
                c.get("imagePullPolicy") != "Never"
                for c in [*spec.get("initContainers", []), *spec["containers"]]
            )
            or resources.normalized(actual) != resources.normalized(accounted[controller])
        ):
            raise ValueError("Installed Cilium Pod resources, placement or image selection drifted")
        verify_pod_authority(template, spec)
        evidence[key] = {
            "uid": pod["metadata"]["uid"],
            "containerEvidence": completed_containers(pod, actual, staged[node]),
        }
    if len(evidence) != count_expected:
        raise ValueError("Cilium rollout did not create exactly the expected placed Pods")
    return evidence


def fixture_pods(target: Target, documents: list[dict], staged: dict) -> dict:
    expected = {reference(item): item for item in documents if item["kind"] == "Pod"}
    output = {}
    for item in list_resources(target, "pods"):
        key = reference(item)
        if key not in expected:
            continue
        spec = item["spec"]
        template = expected[key]["spec"]
        role = template["nodeSelector"][resources.ROLE]
        bounds = resources.pod_bounds(spec)
        if (
            item["metadata"].get("deletionTimestamp")
            or owner_uid(item) is not None
            or spec.get("nodeName") != f"{target.name}-{role}"
            or [(c["name"], c["image"]) for c in spec["containers"]]
            != [(c["name"], c["image"]) for c in template["containers"]]
            or spec.get("ephemeralContainers")
            or any(c.get("imagePullPolicy") != "Never" for c in spec["containers"])
            or resources.normalized(bounds) != resources.normalized(resources.pod_bounds(template))
        ):
            raise ValueError("Installed fixture Pod drifted from the reviewed bounded manifest")
        verify_pod_authority(template, spec)
        output[f"{key[2]}/{key[1]}"] = {
            "uid": item["metadata"]["uid"],
            "containerEvidence": completed_containers(item, bounds, staged[spec["nodeName"]]),
        }
    if len(output) != len(expected):
        raise ValueError("Incomplete network fixture placement or lifecycle evidence")
    return output


def guarded_foundation(
    target: Target, receipt: dict, installed: set[str], fixture_uids: set[str]
) -> None:
    report = foundation(target, installed, fixture_uids, receipt["networkSelection"])
    receipt["foundation"] = report
    check_foundation_bounds(report["systemRequestsByRole"], report["systemLimitsByRole"])


def record_peaks(
    target: Target, journals: list[dict], receipt: dict, stage: str, predicted: dict
) -> None:
    """Bind measured whole-node peaks and CPU counters to the original node lifetime."""
    metrics = minimal_bootstrap.node_metrics(target, journals)
    receipt.setdefault("kernelEvidenceByStage", {})[stage] = metrics
    previous = receipt["kernelEvidenceByStage"]["pre-cilium"]
    maxima = receipt.setdefault("nodePeakCeilingBytes", {})
    for role, bound in predicted.items():
        node = f"{target.name}-{role}"
        memory = load_profile(name=target.profile)["nodes"][role]["memoryBytes"]
        non_pod = memory - bound["schedulerEnvelope"]["memoryBytes"]
        ceiling = non_pod + bound["activeMemoryLimitBytes"]
        maxima[node] = max(maxima.get(node, 0), ceiling)
        if any(
            metrics[node][key] != previous[node][key]
            for key in ("startedAt", "cgroupIdentity", "id")
        ):
            raise ValueError("Node lifetime changed during Cilium qualification")
        if metrics[node]["memoryPeakBytes"] > maxima[node]:
            raise ValueError(
                f"Measured {role} lifetime peak exceeds the accounted Cilium stage ceiling"
            )


def install(
    target: Target, journals: list[dict], receipt: dict, path: Path, headroom: Callable[[], None]
) -> tuple[dict, dict, list[dict]]:
    with failure.phase("network-assets", "missing-prerequisite"):
        raw_pins = RELEASE_FILE.read_bytes()
        if minimal_bootstrap.source_digest(network_release=raw_pins) != receipt["artifactDigest"]:
            raise ValueError(
                "Network release inventory or executable source changed after admission"
            )
        pins = selection(raw_pins)
        chart, crds = download_bounded(raw_pins, timeout=runtime.remaining_timeout(180))
        shared, documents = render(
            target, chart, crds, release=pins, timeout=runtime.remaining_timeout(60)
        )
        fixtures = workloads(target, images=pins["probes"])
        bounds = resources.rendered_bounds(target, documents)
        controllers = resources.controllers(documents)
        receipt["networkSelection"] = controllers["cilium"]["metadata"]["labels"][SELECTION_LABEL]
        receipt["artifactPins"] = pins
        receipt["resourceEnvelope"] = bounds
    with failure.phase("image-admission", "missing-prerequisite"):
        headroom()
        staged = cached_images(target, journals, controllers, fixtures)
        receipt["cachedImages"] = staged
    with failure.phase("foundation-admission", "resource-admission-refused"):
        network.require_fresh_cni(target)
        guarded_foundation(target, receipt, set(), set())
        record_peaks(target, journals, receipt, "pre-cilium", resources.stage_fit(target, {}))
        save_receipt(path, receipt)
    with failure.phase("network-apis", "dependency-failure"):
        headroom()
        network.install_apis(target, shared, documents, fresh=True)
        network.apply_objects(
            target,
            [
                item
                for item in documents
                if item["kind"] not in {"Namespace", "DaemonSet", "Deployment", "GatewayClass"}
            ],
        )
    installed: dict[str, dict] = {}
    pod_evidence: dict = {}
    for name in resources.CONTROLLERS:
        with failure.phase(f"startup-{name}", "resource-admission-refused"):
            headroom()
            guarded_foundation(target, receipt, set(installed), set())
            if (
                installed
                and controller_pods(target, installed, bounds["pods"], staged) != pod_evidence
            ):
                raise ValueError("Previous Cilium instance changed before the next startup stage")
            item = controllers[name]
            network.apply_objects(target, [item])
            network.wait_controller(target, item)
            installed[name] = item
            following = controller_pods(target, installed, bounds["pods"], staged)
            if any(following.get(key) != value for key, value in pod_evidence.items()):
                raise ValueError("Previous Cilium instance changed during startup")
            pod_evidence = following
            receipt["controllerPods"] = pod_evidence
            record_peaks(target, journals, receipt, name, bounds["startupByController"][name])
            receipt["lastCompletedPhase"] = f"startup-{name}"
            save_receipt(path, receipt)
    with failure.phase("network-readiness", "dependency-failure"):
        headroom()
        network.apply_objects(
            target, [item for item in documents if item["kind"] == "GatewayClass"]
        )
        network.observe(target, shared, documents)
        receipt["nodeAllocatable"] = runtime.read_readiness(target, wait=True)
        kube(
            target,
            ["-n", "kube-system", "rollout", "status", "deployment/coredns", "--timeout=60s"],
        )
        kube(
            target,
            [
                "-n",
                "local-path-storage",
                "rollout",
                "status",
                "deployment/local-path-provisioner",
                "--timeout=60s",
            ],
        )
    return controllers, bounds["pods"], fixtures


def qualify(target: Target, hostname: str, request: dict, expected_host_id: str) -> dict:
    """Install and qualify one explicitly authorized, freshly bootstrapped target."""
    if (
        target.profile != MINIMAL_PROFILE
        or target.https_port is None
        or not target.consumer.startswith("qual-")
    ):
        raise ValueError(
            "Bounded Cilium qualification requires a minimal-demo qual- target and HTTPS listener"
        )
    network_qualification.validate_hostname(hostname)
    with runtime.deadline(900), minimal_bootstrap.network_operation(target):
        prior, journals = minimal_bootstrap.network_bootstrap(target, request, expected_host_id)
        path = reserve_receipt(target)
        receipt = {
            **identity(target),
            "incarnation": runtime.incarnation(target),
            "artifactDigest": prior["artifactDigest"],
            "sourceProvenance": prior["sourceProvenance"],
            "nodeImage": prior["nodeImage"],
            "toolVersions": prior["toolVersions"],
            "networkAdmissionDigest": host_admission.digest(prior["networkAdmission"]),
            "startedAt": int(time.time()),
            "lastCompletedPhase": "bootstrap-verified",
            "outcome": "running",
            "installedNetworkQualified": False,
            "installedAcceptance": False,
        }
        save_receipt(path, receipt)
        # The enclosing node cgroups independently keep full lifetime peaks.
        # This sampler only records physical host headroom during the new stage.
        floor = [host_observation.mem_available()]
        stopped = threading.Event()

        def sample() -> None:
            while not stopped.wait(0.5):
                try:
                    floor.append(host_observation.mem_available())
                except (OSError, ValueError):
                    floor.append(0)

        sampler = threading.Thread(target=sample, daemon=True)

        def headroom() -> None:
            floor.append(host_observation.mem_available())
            if min(floor) < 4 * host_admission.GIB:
                raise failure.Failure(
                    {"category": "resource-admission-refused", "phase": "physical-host-floor"}
                )
            receipt["physicalDiskFreeBytes"] = disk_floor(
                request, expected_host_id, prior["networkAdmission"]
            )

        sampler.start()
        try:
            run_qualification(target, hostname, journals, receipt, path, headroom)
            with failure.phase("completion", "resource-admission-refused"):
                stopped.set()
                sampler.join(timeout=2)
                headroom()
                if sampler.is_alive():
                    raise ValueError(
                        "Physical host memory floor was not preserved during Cilium qualification"
                    )
                if (
                    host_observation.host_id() != expected_host_id
                    or minimal_bootstrap.source_digest() != prior["artifactDigest"]
                    or runtime.incarnation(target) != receipt["incarnation"]
                ):
                    raise ValueError("Source, host or installed Cilium incarnation changed")
                receipt["toolingEvidence"] = minimal_bootstrap.tooling_metrics(request)
                runtime.remaining_timeout()
            receipt.update(
                outcome="qualified", lastCompletedPhase="completion", installedNetworkQualified=True
            )
            return receipt
        except (Exception, KeyboardInterrupt) as error:
            safe = failure.evidence(error, "network-qualification", "dependency-failure")
            receipt.update(outcome="failed", failure=safe)
            if isinstance(error, failure.Failure):
                raise
            raise failure.Failure(safe) from None
        finally:
            stopped.set()
            sampler.join(timeout=2)
            receipt["sampledHostMemoryFloorBytes"] = min(floor)
            receipt["hostMemorySamplingIntervalSeconds"] = 0.5
            receipt["finishedAt"] = int(time.time())
            save_receipt(path, receipt)


def run_qualification(
    target: Target,
    hostname: str,
    journals: list[dict],
    receipt: dict,
    path: Path,
    headroom: Callable[[], None],
) -> None:
    controllers, accounted, documents = install(target, journals, receipt, path, headroom)
    staged = receipt["cachedImages"]
    with failure.phase("fixture-admission", "resource-admission-refused"):
        headroom()
        fixture_bounds: dict[str, list[dict]] = {role: [] for role in resources.ROLES}
        for item in documents:
            if item["kind"] == "Pod":
                spec = item["spec"]
                fixture_bounds[spec["nodeSelector"][resources.ROLE]].append(
                    resources.pod_bounds(spec)
                )
        receipt["fixtureResourceEnvelope"] = {
            "steady": resources.stage_fit(target, accounted, fixtures=fixture_bounds),
            "oneReplacementByController": {
                name: resources.stage_fit(
                    target, accounted, next_controller=name, fixtures=fixture_bounds
                )
                for name in resources.CONTROLLERS
            },
        }
        guarded_foundation(target, receipt, set(controllers), set())
        if controller_pods(target, controllers, accounted, staged) != receipt["controllerPods"]:
            raise ValueError("Cilium instance changed before traffic fixture admission")
        network_qualification.create_fixtures(target, documents=documents)
        fixtures = fixture_pods(target, documents, staged)
        receipt["fixturePods"] = fixtures
        receipt["lastCompletedPhase"] = "fixture-admission"
        save_receipt(path, receipt)
    with failure.phase("allowed-denied-traffic", "dependency-failure"):
        headroom()
        receipt["networkEvidence"] = network_qualification.traffic(target)
        receipt["lastCompletedPhase"] = "allowed-denied-traffic"
        save_receipt(path, receipt)
    with failure.phase("gateway-and-policy", "dependency-failure"):
        headroom()
        receipt["tlsEvidence"] = network_qualification.tls(target, hostname)
        receipt["lastCompletedPhase"] = "gateway-and-policy"
        save_receipt(path, receipt)
    with failure.phase("installed-budget", "resource-admission-refused"):
        guarded_foundation(
            target, receipt, set(controllers), {item["uid"] for item in fixtures.values()}
        )
        if (
            controller_pods(target, controllers, accounted, staged) != receipt["controllerPods"]
            or fixture_pods(target, documents, staged) != fixtures
        ):
            raise ValueError(
                "Controller or fixture changed during allowed/denied traffic qualification"
            )
        runtime.read_readiness(target)
        check_failure_events(target)
        record_peaks(
            target,
            journals,
            receipt,
            "fixture-traffic",
            receipt["fixtureResourceEnvelope"]["steady"],
        )


def cached_images(
    target: Target, journals: list[dict], controllers: dict[str, dict], fixtures: list[dict]
) -> dict:
    """Identify preloaded immutable image content on each verified node before any CNI writes."""
    return staged_images(target, journals, resources.required_images(controllers, fixtures))


def staged_images(target: Target, journals: list[dict], images: dict[str, list[str]]) -> dict:
    evidence = {}
    for journal in journals:
        node = journal["name"]
        evidence[node] = {}
        for image in sorted(images[journal["role"]]):
            response = runtime.command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "exec",
                    journal["id"],
                    "crictl",
                    "--image-endpoint",
                    "unix:///run/containerd/containerd.sock",
                    "inspecti",
                    "-o",
                    "json",
                    image,
                ],
            )
            observed = json.loads(response)["status"]
            repository, digest = image.split("@", 1)
            expected = repository.rsplit(":", 1)[0] + "@" + digest
            if expected not in observed.get("repoDigests", []) or not re.fullmatch(
                r"sha256:[a-f0-9]{64}", observed.get("id", "")
            ):
                raise ValueError(
                    f"Exact immutable image must already be staged on the {journal['role']}"
                )
            evidence[node][image] = {"configDigest": observed["id"], "repoDigest": expected}
    return evidence


def disk_floor(request: dict, host_id: str, decision: dict) -> dict[str, int]:
    available = {}
    for account in request["disk"]:
        path = host_observation.ordinary_path(account["path"])
        filesystem = host_admission.digest([host_id, path.stat().st_dev])
        if filesystem not in available:
            status = os.statvfs(path)
            available[filesystem] = status.f_bavail * status.f_frsize
    if set(available) != {store["filesystemId"] for store in decision["filesystems"]}:
        raise ValueError("Network disk charge moved to a different observed filesystem")
    if min(available.values()) < 10 * host_admission.GIB:
        raise ValueError("Physical disk free space fell below the reviewed floor")
    return available


def check_failure_events(target: Target) -> None:
    failures = {"Evicted", "BackOff", "OOMKilling", "Preempted", "UnexpectedAdmissionError"}
    if any(event.get("reason") in failures for event in list_resources(target, "events")):
        raise ValueError(
            "Eviction, runtime failure or unexpected Pod restart occurred during qualification"
        )
