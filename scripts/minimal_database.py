"""Explicit, staged database-controller qualification on a disposable minimal-demo target."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from . import bootstrap_failure as failure
from . import consumer_runtime as runtime
from . import (
    database_assets,
    database_runtime,
    host_admission,
    host_observation,
    minimal_bootstrap,
    network_assets,
    network_fixtures,
)
from . import minimal_database_resources as resources
from . import minimal_network as network
from . import network_resources as pod_resources
from .consumer_profile import MINIMAL_PROFILE, Target, identity
from .consumer_state import read_bounded
from .kubernetes import contains, kube, reference
from .minimal_network import owner_uid
from .network_runtime import comparable_spec, verify_pod_template


def pod_spec(manifest: dict) -> dict:
    return manifest["spec"]["template"]["spec"]


def check_pod(manifest: dict, pod: dict) -> dict:
    """Check the complete API-admitted container population before using its bounds."""
    template = pod_spec(manifest)
    actual = pod["spec"]
    layout = ("containers", "initContainers")
    bounds = pod_resources.pod_bounds(actual)
    if (
        actual.get("ephemeralContainers")
        or actual.get("nodeSelector") != template["nodeSelector"]
        or any(
            [(c["name"], c["image"], c.get("imagePullPolicy")) for c in actual.get(group, [])]
            != [(c["name"], c["image"], c.get("imagePullPolicy")) for c in template.get(group, [])]
            for group in layout
        )
        or pod_resources.normalized(bounds)
        != pod_resources.normalized(pod_resources.pod_bounds(template))
    ):
        raise ValueError(
            "Admitted database Pod differs from finite placement, image or resource inventory"
        )
    network.verify_pod_authority(template, actual)
    return bounds


def admit_pod(target: Target, manifest: dict) -> dict:
    template = manifest["spec"]["template"]
    namespace, name = manifest["metadata"]["namespace"], manifest["metadata"]["name"]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            **deepcopy(template.get("metadata", {})),
            "name": name + "-qualification-admission",
            "namespace": namespace,
        },
        "spec": pod_spec(manifest),
    }
    result = json.loads(
        kube(target, ["create", "--dry-run=server", "-f", "-", "-o", "json"], [pod])
    )
    items = result["items"] if result.get("kind") == "List" else [result]
    if len(items) != 1 or reference(items[0]) != reference(pod):
        raise ValueError("Database Pod admission returned an unexpected resource")
    return check_pod(manifest, items[0])


def deployment_evidence(
    target: Target,
    manifest: dict,
    actual: dict | None,
    replicas: list[dict],
    pods: list[dict],
    staged: dict,
) -> tuple[str, dict]:
    name = manifest["metadata"]["name"]
    if actual is None or not contains(
        comparable_spec(manifest, minimal=True), comparable_spec(actual, minimal=True)
    ):
        raise ValueError("Admitted database Deployment drifted from its rendered specification")
    verify_pod_template(manifest, actual)
    deployment_uid = actual["metadata"]["uid"]
    owners = {item["metadata"]["uid"] for item in replicas if owner_uid(item) == deployment_uid}
    pods = [pod for pod in pods if owner_uid(pod) in owners]
    worker = f"{target.name}-worker"
    if (
        len(pods) != 1
        or pods[0]["spec"].get("nodeName") != worker
        or pods[0]["metadata"].get("deletionTimestamp")
    ):
        raise ValueError(f"Database Deployment {name} has unexpected placement or Pod overlap")
    pod = pods[0]
    bounds = check_pod(manifest, pod)
    return deployment_uid, {
        "uid": pod["metadata"]["uid"],
        "node": worker,
        "bounds": bounds,
        "containerEvidence": network.completed_containers(pod, bounds, staged[worker]),
    }


def wait_deployment(target: Target, manifest: dict) -> None:
    metadata = manifest["metadata"]
    kube(
        target,
        [
            "-n",
            metadata["namespace"],
            "rollout",
            "status",
            "deployment/" + metadata["name"],
            "--timeout=120s",
        ],
        request_timeout=120,
    )


def install_support(target: Target, group: list[dict]) -> None:
    schemas = [item for item in group if item["kind"] == "CustomResourceDefinition"]
    for item in schemas:
        database_runtime.apply_objects(target, [item])
        kube(
            target,
            [
                "wait",
                "--for=condition=Established",
                "crd/" + item["metadata"]["name"],
                "--timeout=90s",
            ],
            request_timeout=90,
        )
    database_runtime.apply_objects(target, [item for item in group if item["kind"] == "Namespace"])
    database_runtime.apply_objects(
        target,
        [
            item
            for item in group
            if item["kind"] in database_runtime.BUILTINS
            and item["kind"] not in {"Namespace", "CustomResourceDefinition", "Deployment"}
        ],
    )


def restart_controller(
    target: Target,
    manifest: dict,
    deployment_uid: str,
    pod_uid: str,
    after_drain: Callable[[], None],
) -> None:
    """Scale one exact Deployment, wait for termination, then resume the same object."""
    name, namespace = manifest["metadata"]["name"], manifest["metadata"]["namespace"]
    prefix = ["-n", namespace]

    def scale(previous: int, requested: int) -> None:
        actual = json.loads(kube(target, [*prefix, "get", "deployment", name, "-o", "json"]))
        if (
            actual["metadata"].get("deletionTimestamp")
            or actual["metadata"]["uid"] != deployment_uid
        ):
            raise ValueError("Database Deployment identity changed before scaling")
        changes = [
            {"op": "test", "path": "/metadata/uid", "value": deployment_uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": actual["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/replicas", "value": previous},
            {"op": "replace", "path": "/spec/replicas", "value": requested},
        ]
        kube(
            target,
            [*prefix, "patch", "deployment", name, "--type=json", "--patch", json.dumps(changes)],
        )

    scale(1, 0)
    with runtime.deadline(90):
        while True:
            runtime.remaining_timeout()
            replicas = [
                item
                for item in json.loads(kube(target, [*prefix, "get", "replicasets", "-o", "json"]))[
                    "items"
                ]
                if owner_uid(item) == deployment_uid
            ]
            owners = {item["metadata"]["uid"] for item in replicas}
            pods = json.loads(kube(target, [*prefix, "get", "pods", "-o", "json"]))["items"]
            if not any(item["spec"]["replicas"] for item in replicas) and not any(
                pod["metadata"]["uid"] == pod_uid or owner_uid(pod) in owners for pod in pods
            ):
                break
            time.sleep(1)
    after_drain()
    scale(0, 1)


class Qualification:
    def __init__(
        self,
        target: Target,
        journals: list[dict],
        receipt: dict,
        path: Path,
        previous: dict,
        headroom: Callable[[], None],
    ) -> None:
        self.target = target
        self.journals = journals
        self.receipt = receipt
        self.path = path
        self.previous = previous
        self.headroom = headroom
        self.active: dict[str, tuple[str, dict]] = {}
        self.cilium: dict = {}
        self.fixtures: list[dict] = []
        self.bounds: dict = {}
        self.manifests: dict = {}
        self.staged: dict = {}

    def publish(self, phase: str, *, drained: str | None = None) -> None:
        self.receipt["lastCompletedPhase"] = phase
        self.receipt["controllerPods"] = {
            name: {"deploymentUID": uid, **pod}
            for name, (uid, pod) in self.active.items()
            if name != drained
        }
        network.save_receipt(self.path, self.receipt)

    def inspect(self, names: set[str]) -> dict[str, tuple[str, dict]]:
        expected = [self.manifests[name] for name in sorted(names)]
        found = database_runtime.inventory(self.target, expected)
        replicas = network.list_resources(self.target, "replicasets")
        pods = network.list_resources(self.target, "pods")
        return {
            item["metadata"]["name"]: deployment_evidence(
                self.target, item, found.get(reference(item)), replicas, pods, self.staged
            )
            for item in expected
        }

    def checkpoint(self, *, drained: str | None = None) -> None:
        self.headroom()
        previous = self.previous
        target = self.target
        fixture_uids = {pod["uid"] for pod in previous["fixturePods"].values()}
        foundation = network.foundation(
            target,
            set(self.cilium),
            fixture_uids,
            previous["networkSelection"],
            additional={
                reference(self.manifests[name]): item[0] for name, item in self.active.items()
            },
        )
        network.check_foundation_bounds(
            foundation["systemRequestsByRole"], foundation["systemLimitsByRole"]
        )
        if (
            foundation != previous["foundation"]
            or network.controller_pods(
                target, self.cilium, previous["resourceEnvelope"]["pods"], previous["cachedImages"]
            )
            != previous["controllerPods"]
            or network.fixture_pods(target, self.fixtures, previous["cachedImages"])
            != previous["fixturePods"]
            or runtime.read_readiness(target) != previous["nodeAllocatable"]
            or self.inspect(set(self.active) - {drained})
            != {name: item for name, item in self.active.items() if name != drained}
        ):
            raise ValueError(
                "Database continuation found a changed foundation, network fixture or controller"
            )
        network.check_failure_events(target)
        self.receipt["nodeAllocatable"] = previous["nodeAllocatable"]

    def peak(self, phase: str, budget: dict) -> None:
        network.record_peaks(self.target, self.journals, self.receipt, phase, budget)

    def start(self, name: str) -> None:
        with failure.phase(f"startup-{name}", "dependency-failure"):
            manifest = self.manifests[name]
            self.checkpoint()
            self.receipt.setdefault("admittedPodBoundsByStage", {})[f"startup-{name}"] = admit_pod(
                self.target, manifest
            )
            database_runtime.apply_objects(self.target, [manifest])
            wait_deployment(self.target, manifest)
            self.active[name] = self.inspect({name})[name]
            self.checkpoint()
            self.peak(f"startup-{name}", self.bounds["startupByController"][name])
            self.publish(f"startup-{name}")

    def restart(self, name: str) -> None:
        with failure.phase(f"restart-{name}", "dependency-failure"):
            manifest = self.manifests[name]
            self.checkpoint()
            uid, prior = self.active[name]
            self.receipt.setdefault("admittedPodBoundsByStage", {})[f"restart-{name}"] = admit_pod(
                self.target, manifest
            )

            def drained() -> None:
                self.checkpoint(drained=name)
                self.receipt.setdefault("replacements", {})[name] = {
                    "deploymentUID": uid,
                    "oldPodUID": prior["uid"],
                    "oldPodTerminated": True,
                }
                self.publish(f"drained-{name}", drained=name)

            restart_controller(self.target, manifest, uid, prior["uid"], drained)
            wait_deployment(self.target, manifest)
            replacement_uid, replacement = self.inspect({name})[name]
            if replacement_uid != uid or replacement["uid"] == prior["uid"]:
                raise ValueError("Database controller did not replace only its Pod")
            self.active[name] = replacement_uid, replacement
            self.checkpoint()
            self.peak(f"restart-{name}", self.bounds["oneReplacementByController"][name])
            self.receipt["replacements"][name]["newPodUID"] = replacement["uid"]
            self.publish(f"restart-{name}")

    def run(self) -> None:
        from . import minimal_database_probes as probes

        target, prior, receipt = self.target, self.previous, self.receipt
        with failure.phase("release-assets", "missing-prerequisite"):
            database_bytes = database_assets.RELEASE_FILE.read_bytes()
            network_bytes = network_assets.RELEASE_FILE.read_bytes()
            if (
                minimal_bootstrap.source_digest(
                    database_release=database_bytes,
                    network_release=network_bytes,
                )
                != receipt["artifactDigest"]
            ):
                raise ValueError("Release selections or source changed after cold admission")
            release = database_assets.selection(database_bytes)
            network_release = network_assets.selection(network_bytes)
            if (
                network_release != prior["artifactPins"]
                or network_assets.labels(target, release=network_release)[
                    network_assets.SELECTION_LABEL
                ]
                != prior["networkSelection"]
            ):
                raise ValueError("Network qualification artifact selection changed")
            assets = database_assets.download_bounded(
                database_bytes, timeout=runtime.remaining_timeout(180)
            )
            chart, apis = network_assets.download_bounded(
                network_bytes, timeout=runtime.remaining_timeout(180)
            )
            _, documents = network_assets.render(
                target, chart, apis, release=network_release, timeout=runtime.remaining_timeout(60)
            )
            self.cilium = pod_resources.controllers(documents)
            self.fixtures = network_fixtures.workloads(target, images=network_release["probes"])
            if pod_resources.rendered_bounds(target, documents) != prior["resourceEnvelope"]:
                raise ValueError("Network qualification resource selection changed")
            groups = database_assets.render(target, assets, release=release)
            custom = {
                (group, *reference(item)): item
                for group, items in groups.items()
                for item in items
                if item["kind"] not in database_runtime.BUILTINS
            }
            if set(custom) != {
                ("barman", "Issuer", "selfsigned-issuer", "cnpg-system"),
                ("barman", "Certificate", "barman-cloud-client", "cnpg-system"),
                ("barman", "Certificate", "barman-cloud-server", "cnpg-system"),
            }:
                raise ValueError("Unexpected database custom resource inventory")
            issuer = custom["barman", "Issuer", "selfsigned-issuer", "cnpg-system"]
            server = custom["barman", "Certificate", "barman-cloud-server", "cnpg-system"]
            certificates = [
                custom["barman", "Certificate", "barman-cloud-client", "cnpg-system"],
                server,
            ]
            self.manifests = resources.controllers(groups)
            self.bounds = resources.rendered_bounds(
                target, groups, cilium=prior["resourceEnvelope"]["pods"], fixtures=self.fixtures
            )
            storage = database_assets.storage_class(target, release=release)
            receipt.update(databaseArtifactPins=release, resourceEnvelope=self.bounds)
            self.publish("release-assets")
        with failure.phase("controller-admission", "resource-admission-refused"):
            images = resources.required_images(groups)
            self.staged = network.staged_images(target, self.journals, images)
            self.checkpoint()
            builtins = [
                item
                for group in groups.values()
                for item in group
                if item["kind"] in database_runtime.BUILTINS
            ] + [storage]
            if database_runtime.inventory(target, builtins):
                raise ValueError("Existing database resources prevent a disposable qualification")
            receipt["storageDriver"] = database_runtime.verify_driver(
                target, assets["kind-storage"]
            )
            self.peak(
                "database-admission",
                resources.stage_fit(
                    target,
                    prior["resourceEnvelope"]["pods"],
                    self.fixtures,
                    {},
                ),
            )
            receipt["cachedImages"] = self.staged
            self.publish("controller-admission")
        with failure.phase("certificate-apis", "dependency-failure"):
            self.checkpoint()
            install_support(target, groups["cert-manager"])
        for name in resources.STARTUP[:3]:
            self.start(name)
        with failure.phase("certificate-issuance", "dependency-failure"):
            self.checkpoint()
            install_support(target, groups["cnpg"])
            install_support(target, groups["barman"])
            receipt["certificateAdmission"] = probes.certificate_admission(target, issuer)
            database_runtime.apply_objects(target, [issuer])
            receipt["issuer"] = probes.issuer_evidence(target, issuer)
            database_runtime.apply_objects(target, certificates)
            receipt["certificates"] = probes.certificate_evidence(target, certificates)
            self.publish("certificate-issuance")
        self.start("cnpg-controller-manager")
        with failure.phase("database-api-readiness", "dependency-failure"):
            self.checkpoint()
            database_runtime.apply_objects(target, [storage])
            receipt["cnpgAdmission"] = probes.cnpg_admission(
                target, postgres_image=release["images"]["postgres"]
            )
            self.publish("database-api-readiness")
        self.start("barman-cloud")
        for name in resources.STARTUP:
            self.restart(name)
            with failure.phase(f"recovery-{name}", "dependency-failure"):
                receipt["replacements"][name]["certificateAdmission"] = (
                    probes.certificate_admission(target, issuer)
                )
                receipt["replacements"][name]["cnpgAdmission"] = probes.cnpg_admission(
                    target, postgres_image=release["images"]["postgres"]
                )
                current = probes.certificate_evidence(target, certificates)
                if (
                    current != receipt["certificates"]
                    or probes.issuer_evidence(target, issuer) != receipt["issuer"]
                ):
                    raise ValueError(
                        "Original Barman Certificates or Issuer changed on controller restart"
                    )
                if name == "cert-manager":
                    certificate = probes.recovery_certificate(server)
                    probes.create_recovery_certificate(target, certificate)
                    receipt["postRestartCertificate"] = probes.certificate_evidence(
                        target, [certificate]
                    )
                self.checkpoint()
                self.peak(f"recovery-{name}", self.bounds["steady"])
                self.publish(f"recovery-{name}")
        with failure.phase("completion", "resource-admission-refused"):
            self.checkpoint()
            if len(database_runtime.inventory(target, builtins)) != len(builtins):
                raise ValueError(
                    "Database capability lost a required resource during qualification"
                )
            self.peak("completion", self.bounds["steady"])


def qualify(target: Target, request: dict, expected_host_id: str) -> dict:
    """Continue one successful, unchanged minimal-demo network qualification."""
    if (
        target.profile != MINIMAL_PROFILE
        or target.https_port is None
        or not target.consumer.startswith("qual-")
    ):
        raise ValueError(
            "Database qualification requires a minimal-demo qual- target and HTTPS listener"
        )
    with runtime.deadline(1200), minimal_bootstrap.network_operation(target):
        with failure.phase("network-continuation", "identity-mismatch"):
            # This checks the original bootstrap receipt, journals, physical stores,
            # cgroups and request; future image and retained-payload growth is zero.
            cold, journals = minimal_bootstrap.network_bootstrap(target, request, expected_host_id)
            previous = json.loads(
                read_bounded(
                    target.directory / f"{target.name}.network-qualification.json",
                    1024**2,
                )
            )
            incarnation = runtime.incarnation(target)
            if (
                any(previous.get(key) != value for key, value in identity(target).items())
                or previous.get("incarnation") != incarnation
                or previous.get("artifactDigest") != cold["artifactDigest"]
                or previous.get("sourceProvenance") != cold["sourceProvenance"]
                or previous.get("nodeImage") != cold["nodeImage"]
                or previous.get("toolVersions") != cold["toolVersions"]
                or previous.get("outcome") != "qualified"
                or previous.get("lastCompletedPhase") != "completion"
                or previous.get("installedNetworkQualified") is not True
                or not previous.get("fixturePods")
                or not previous.get("controllerPods")
            ):
                raise ValueError(
                    "Exact successful network qualification with its retained fixtures required"
                )
            path = network.reserve_receipt(target, database=True)
        receipt = {
            **identity(target),
            "incarnation": incarnation,
            "artifactDigest": cold["artifactDigest"],
            "sourceProvenance": cold["sourceProvenance"],
            "nodeImage": cold["nodeImage"],
            "toolVersions": cold["toolVersions"],
            "networkReceiptDigest": host_admission.digest(previous),
            "networkSelection": previous["networkSelection"],
            "networkFixturePodUIDs": {
                name: pod["uid"] for name, pod in previous["fixturePods"].items()
            },
            "hostAdmission": cold["networkAdmission"],
            "kernelEvidenceByStage": {
                "pre-cilium": previous["kernelEvidenceByStage"]["pre-cilium"],
                "qualified-network": previous["kernelEvidenceByStage"]["fixture-traffic"],
            },
            "nodePeakCeilingBytes": dict(previous["nodePeakCeilingBytes"]),
            "startedAt": int(time.time()),
            "lastCompletedPhase": "network-verified",
            "outcome": "running",
            "installedDatabaseControllersQualified": False,
            "installedAcceptance": False,
        }
        network.save_receipt(path, receipt)
        floor: list[int] = []
        stopped = threading.Event()

        def sample_memory() -> None:
            try:
                floor.append(host_observation.mem_available())
            except (OSError, ValueError):
                floor.append(0)

        def sample() -> None:
            while not stopped.wait(0.5):
                sample_memory()

        def headroom() -> None:
            sample_memory()
            if min(floor) < 4 * host_admission.GIB:
                raise ValueError(
                    "Physical host memory floor was not preserved during database qualification"
                )
            receipt["physicalDiskFreeBytes"] = network.disk_floor(
                request, expected_host_id, cold["networkAdmission"]
            )

        sample_memory()
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            Qualification(target, journals, receipt, path, previous, headroom).run()
            with failure.phase("completion", "resource-admission-refused"):
                stopped.set()
                sampler.join(timeout=2)
                headroom()
                if (
                    sampler.is_alive()
                    or host_observation.host_id() != expected_host_id
                    or minimal_bootstrap.source_digest() != receipt["artifactDigest"]
                    or runtime.incarnation(target) != incarnation
                ):
                    raise ValueError("Source, host or database target incarnation changed")
                receipt["toolingEvidence"] = minimal_bootstrap.tooling_metrics(request)
                runtime.remaining_timeout()
            receipt.update(
                outcome="qualified",
                lastCompletedPhase="completion",
                installedDatabaseControllersQualified=True,
            )
            return receipt
        except (Exception, KeyboardInterrupt) as error:
            safe = failure.evidence(error, "database-qualification", "dependency-failure")
            receipt.update(outcome="failed", failure=safe)
            raise failure.Failure(safe) from None
        finally:
            stopped.set()
            sampler.join(timeout=2)
            receipt["sampledHostMemoryFloorBytes"] = min(floor)
            receipt["hostMemorySamplingIntervalSeconds"] = 0.5
            receipt["finishedAt"] = int(time.time())
            network.save_receipt(path, receipt)
