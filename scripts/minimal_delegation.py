"""Preview, install or qualify minimal-demo delegation on one explicit owned target."""

from __future__ import annotations

import argparse
import json
import os
import time
from copy import deepcopy
from pathlib import Path

from . import bootstrap_failure as failure
from . import (
    consumer_lifecycle,
    database_assets,
    database_runtime,
    host_admission,
    host_observation,
    minimal_bootstrap,
    minimal_network,
)
from . import consumer_runtime as runtime
from . import (
    minimal_delegation_probe as probe,
)
from .consumer_profile import LABEL, MINIMAL_PROFILE, Target, identity, load_profile
from .consumer_state import read_bounded
from .delegated_argo_policy import resource
from .kubernetes import kube, reference
from .minimal_delegation_policy import (
    Operation,
    load_operation,
    namespaces,
    operations_namespace,
    render,
)
from .network_resources import pod_bounds, quantity
from .node_capacity import envelope

SECURITY = {
    "pod-security.kubernetes.io/enforce": "baseline",
    "pod-security.kubernetes.io/enforce-version": "v1.35",
}


def render_namespaces(target: Target) -> list[dict]:
    documents = [resource("Namespace", ns, None) for ns in namespaces(target)]
    for item in documents:
        item["metadata"]["labels"] = {LABEL: target.consumer, **SECURITY}
    return documents


def check_prerequisites(target: Target) -> None:
    desired = database_assets.storage_class(target)
    actual = json.loads(
        kube(target, ["get", "storageclass", desired["metadata"]["name"], "-o", "json"])
    )
    for field in ("provisioner", "reclaimPolicy", "volumeBindingMode", "allowVolumeExpansion"):
        if desired[field] != actual.get(field):
            raise ValueError("Expected provider local-retained provisioning and Retain semantics")
    if any(actual.get(key) for key in ("parameters", "mountOptions", "allowedTopologies")) or any(
        actual["metadata"].get("annotations", {}).get(key) == "true"
        for key in (
            "storageclass.kubernetes.io/is-default-class",
            "storageclass.beta.kubernetes.io/is-default-class",
        )
    ):
        raise ValueError(
            "Retained storage exposure and placement options differ from provider contract"
        )
    for item in render_namespaces(target):
        name = item["metadata"]["name"]
        found = json.loads(kube(target, ["get", "namespace", name, "-o", "json"]))
        if (
            found["metadata"].get("deletionTimestamp")
            or found["status"]["phase"] != "Active"
            or any(
                found["metadata"].get("labels", {}).get(key) != value
                for key, value in item["metadata"]["labels"].items()
            )
        ):
            raise ValueError(
                "Expected existing consumer-owned baseline app, data and support namespaces"
            )


def canonical_template(template: dict) -> dict:
    """Account only for Kubernetes 1.35 defaults on the finite reviewed PodSpec."""
    pod = deepcopy(template["spec"])
    pod.setdefault("dnsPolicy", "ClusterFirst")
    pod.setdefault("schedulerName", "default-scheduler")
    alias = pod.pop("serviceAccount", None)
    if alias is not None and alias != pod["serviceAccountName"]:
        raise ValueError("Unexpected deprecated ServiceAccount alias in approved template")
    if pod.get("initContainers") == []:
        pod.pop("initContainers")
    if pod.get("volumes") == []:
        pod.pop("volumes")
    for volume in pod.get("volumes", []):
        for source in ("configMap", "secret"):
            if source in volume:
                volume[source].setdefault("defaultMode", 420)
    for item in [*pod["containers"], *pod.get("initContainers", [])]:
        item.setdefault("terminationMessagePath", "/dev/termination-log")
        item.setdefault("terminationMessagePolicy", "File")
        for values in item["resources"].values():
            for key, value in values.items():
                values[key] = quantity(value, cpu=key == "cpu")
    metadata = deepcopy(template.get("metadata", {}))
    if metadata.get("creationTimestamp") is None:
        metadata.pop("creationTimestamp", None)
    return {"spec": pod, "metadata": metadata}


def canonical_policy(kind: str, spec: dict) -> dict:
    value = deepcopy(spec)
    if kind == "ValidatingAdmissionPolicy":
        match = value["matchConstraints"]
        if match.get("matchPolicy") == "Equivalent":
            match.pop("matchPolicy")
        for field in ("namespaceSelector", "objectSelector", "excludeResourceRules"):
            if match.get(field) in ({}, []):
                match.pop(field)
    return value


def check_document(desired: dict, actual: dict, *, type_checked: bool = True) -> None:
    kind = desired["kind"]
    for key in ("rules", "subjects", "roleRef", "automountServiceAccountToken"):
        if key in desired and actual.get(key) != desired[key]:
            raise ValueError("Demo authority differs from reviewed identity and permissions")
    if kind == "ServiceAccount" and (actual.get("secrets") or actual.get("imagePullSecrets")):
        raise ValueError("Demo ServiceAccount has unreviewed credentials")
    if kind == "PodTemplate" and canonical_template(desired["template"]) != canonical_template(
        actual["template"]
    ):
        raise ValueError("Approved demo PodTemplate drifted")
    if kind in {
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "ResourceQuota",
    } and (canonical_policy(kind, desired["spec"]) != canonical_policy(kind, actual["spec"])):
        raise ValueError("Demo admission or quota scope drifted")
    if kind == "ValidatingAdmissionPolicy" and type_checked:
        status = actual.get("status", {})
        if (
            status.get("observedGeneration") != actual["metadata"]["generation"]
            or "typeChecking" not in status
        ):
            raise ValueError("Demo admission type checking pending")
        if status["typeChecking"].get("expressionWarnings"):
            raise ValueError("Demo admission has a Kubernetes CEL type warning")


def inspect(target: Target, documents: list[dict], *, type_checked: bool = True) -> dict:
    known = database_runtime.inventory(target, documents)
    for item in documents:
        found = known.get(reference(item))
        if found is None:
            raise ValueError("Expected demo capability object is missing")
        check_document(item, found, type_checked=type_checked)
    return known


def install_group(target: Target, documents: list[dict]) -> None:
    found = database_runtime.inventory(target, documents)
    for item in documents:
        actual = found.get(reference(item))
        if actual:
            check_document(item, actual, type_checked=False)
        else:
            kube(target, ["create", "--field-manager=k8s-infrastructure-demo", "-f", "-"], [item])


def configure(target: Target, operation: Operation | None) -> None:
    check_prerequisites(target)
    objects = render(target, operation)
    preflight = database_runtime.inventory(target, objects)
    for item in objects:
        current = preflight.get(reference(item))
        if current:
            check_document(item, current, type_checked=False)
    namespace = [item for item in objects if item["kind"] == "Namespace"]
    bindings = [item for item in objects if item["kind"] == "RoleBinding"]
    policies = [item for item in objects if item not in [*namespace, *bindings]]
    install_group(target, namespace)
    install_group(target, policies)
    with runtime.deadline(60):
        while True:
            try:
                inspect(target, policies)
                break
            except ValueError as error:
                if str(error) != "Demo admission type checking pending":
                    raise
                time.sleep(1)
        install_group(target, bindings)
        if operation:
            actor = probe.as_account(operations_namespace(target), operation.launcher)
            probe.create(target, operation.job(), actor)
            altered = operation.job()
            altered["spec"]["template"]["spec"]["serviceAccountName"] = "default"
            probe.denied(
                target, altered, actor, ("admission", f"{target.consumer}-{operation.launcher}-job")
            )
    inspect(target, objects)


def qualification_operation(target: Target, image: str) -> Operation:
    return load_operation(
        target,
        {
            "namespace": namespaces(target)[0],
            "name": "delegation-job-probe",
            "deadlineSeconds": 90,
            "podSpec": {
                "securityContext": {"runAsUser": 65532, "runAsGroup": 65532},
                "containers": [
                    {
                        "name": "probe",
                        "image": image,
                        "command": ["python", "-I", "-c", "pass"],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "100m", "memory": "64Mi"},
                        },
                        "securityContext": {"readOnlyRootFilesystem": True},
                    }
                ],
            },
        },
    )


def worker_headroom(target: Target, database: dict, network: dict) -> dict:
    expected = {
        *[item["uid"] for item in database["controllerPods"].values()],
        *[item["uid"] for item in network["fixturePods"].values()],
        *[item["uid"] for item in network["controllerPods"].values()],
        *[item["uid"] for item in network["foundation"]["podInventory"]],
    }
    pods = minimal_network.list_resources(target, "pods")
    if {item["metadata"]["uid"] for item in pods} != expected or any(
        item["metadata"].get("deletionTimestamp") or item["status"].get("phase") != "Running"
        for item in pods
    ):
        raise ValueError("Resident Pod inventory changed since database qualification")
    worker = target.name + "-worker"
    active = [pod_bounds(item["spec"]) for item in pods if item["spec"].get("nodeName") == worker]
    # Provisioning can overlap a claim consumer's start. Two probe Pods may
    # overlap during locking. Charge both probe Pods and the helper together.
    cpu = 125 + sum(item["effectiveRequests"]["cpu"] for item in active)
    memory = 192 * 1024**2 + sum(item["effectiveLimits"]["memory"] for item in active)
    limit = envelope(load_profile(name=target.profile)["nodes"]["worker"])
    if cpu > limit["cpuMillicores"] or memory > limit["memoryBytes"]:
        raise ValueError("Retained proof exceeds the existing worker envelope")
    return {"workerCpuRequestsMillicores": cpu, "workerMemoryLimitsBytes": memory}


def exact_receipt(target: Target, suffix: str, artifact: dict, incarnation: str) -> dict:
    path = target.directory / f"{target.name}.{suffix}-qualification.json"
    value = json.loads(read_bounded(path, 1024**2))
    if (
        any(value.get(key) != field for key, field in identity(target).items())
        or any(
            value.get(key) != artifact[key]
            for key in ("artifactDigest", "sourceProvenance", "nodeImage", "toolVersions")
        )
        or value.get("incarnation") != incarnation
        or value.get("outcome") != "qualified"
        or value.get("lastCompletedPhase") != "completion"
    ):
        raise ValueError("Exact successful predecessor qualification receipt required")
    return value


def probe_images() -> tuple[str, str]:
    from .minimal_delegation_storage import PROBE_IMAGE

    return PROBE_IMAGE, database_assets.selection()["images"]["kind-helper"]


def qualify(target: Target, request: dict, host_id: str) -> dict:
    from .minimal_delegation_storage import qualify_storage

    if not target.consumer.startswith("qual-"):
        raise ValueError("Demo delegation qualification requires a disposable qual- target")
    with runtime.deadline(900), minimal_bootstrap.network_operation(target):
        cold, journals = minimal_bootstrap.network_bootstrap(
            target, request, host_id, retained_growth_bytes=host_admission.GIB
        )
        incarnation = runtime.incarnation(target)
        database = exact_receipt(target, "database", cold, incarnation)
        network = exact_receipt(target, "network", cold, incarnation)
        if (
            database.get("installedDatabaseControllersQualified") is not True
            or network.get("installedNetworkQualified") is not True
            or database.get("networkReceiptDigest") != host_admission.digest(network)
            or runtime.read_readiness(target) != network["nodeAllocatable"]
        ):
            raise ValueError("Predecessor installed evidence is incomplete or changed")
        images = probe_images()
        operation = qualification_operation(target, images[0])
        cached = minimal_network.staged_images(
            target, journals, {"control-plane": [], "worker": list(images)}
        )
        bounds = worker_headroom(target, database, network)
        foreign = f"{target.consumer}-demo-foreign-control"
        spaces = [*render_namespaces(target), resource("Namespace", foreign, None)]
        if database_runtime.inventory(
            target, [*spaces, *render(target, operation)]
        ) or minimal_network.list_resources(target, "persistentvolumeclaims,persistentvolumes"):
            raise ValueError(
                "Existing demo fixtures or retained claims refuse disposable qualification"
            )
        path = target.directory / f"{target.name}.delegation-qualification.json"
        with path.open("x", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
        receipt = {
            **identity(target),
            **{
                key: cold[key]
                for key in ("artifactDigest", "sourceProvenance", "nodeImage", "toolVersions")
            },
            "incarnation": incarnation,
            "databaseReceiptDigest": host_admission.digest(database),
            "operationDigest": operation.digest,
            "namespaceRoles": list(namespaces(target)),
            "cachedImages": cached,
            "hostAdmission": cold["networkAdmission"],
            "resourceEnvelope": bounds,
            "lastCompletedPhase": "predecessor",
            "outcome": "running",
            "installedDelegationQualified": False,
            "installedAcceptance": False,
        }

        def publish(phase: str) -> None:
            receipt["lastCompletedPhase"] = phase
            minimal_network.save_receipt(path, receipt)

        publish("predecessor")
        try:
            with failure.phase("delegation", "dependency-failure"):
                install_group(target, spaces)
                sentinel = probe.create(
                    target,
                    resource("ConfigMap", "unrelated", foreign, data={"marker": "unchanged"}),
                    [],
                    dry_run=False,
                )
                configure(target, operation)
                publish("delegation")
            with failure.phase("authorization-and-admission", "dependency-failure"):
                receipt["nativeControls"] = probe.prove(target, operation, foreign)
                publish("authorization-and-admission")
            with failure.phase("retained-filesystem", "dependency-failure"):
                minimal_network.disk_floor(request, host_id, cold["networkAdmission"])
                receipt["retainedFilesystem"] = qualify_storage(target, operation.namespace)
                publish("retained-filesystem")
            with failure.phase("completion", "identity-mismatch"):
                observed = json.loads(
                    kube(target, ["-n", foreign, "get", "configmap", "unrelated", "-o", "json"])
                )
                if (
                    observed["metadata"]["uid"] != sentinel["metadata"]["uid"]
                    or observed["metadata"]["resourceVersion"]
                    != sentinel["metadata"]["resourceVersion"]
                    or observed["data"] != sentinel["data"]
                    or runtime.incarnation(target) != incarnation
                    or host_observation.host_id() != host_id
                    or minimal_bootstrap.source_digest() != cold["artifactDigest"]
                ):
                    raise ValueError("Unrelated resource, target identity or source changed")
                inspect(target, render(target, operation))
                receipt["finalHostDiskFreeBytes"] = minimal_network.disk_floor(
                    request, host_id, cold["networkAdmission"]
                )
                receipt["toolingEvidence"] = minimal_bootstrap.tooling_metrics(request)
                receipt["unrelatedConfigMapUID"] = observed["metadata"]["uid"]
                receipt.update(outcome="qualified", installedDelegationQualified=True)
                publish("completion")
            return receipt
        except (Exception, KeyboardInterrupt) as error:
            safe = failure.evidence(error, "delegation-qualification", "dependency-failure")
            receipt.update(outcome="failed", failure=safe)
            minimal_network.save_receipt(path, receipt)
            raise failure.Failure(safe) from None


def preview(target: Target, action: str, operation: Operation | None) -> dict:
    objects = render(target, operation)
    return {
        **identity(target),
        "action": action,
        "execution": "not-performed",
        "workloadNamespaces": list(namespaces(target)),
        "operationsNamespace": operations_namespace(target),
        "operation": {
            "name": operation.name,
            "digest": operation.digest,
            "namespace": operation.namespace,
            "launcher": operation.launcher,
        }
        if operation
        else None,
        "resources": [reference(item) for item in objects],
        "workerProbeImages": list(probe_images()) if action == "qualify" else [],
        "installedAcceptance": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "health", "qualify"))
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--https-port", required=True, type=int)
    parser.add_argument("--operation", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--confirm-target")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = Target(
            args.consumer, args.docker_context, args.state_root, MINIMAL_PROFILE, args.https_port
        )
        operation = (
            load_operation(target, json.loads(read_bounded(args.operation)))
            if args.operation
            else None
        )
        if args.action == "qualify" and (operation or not target.consumer.startswith("qual-")):
            raise ValueError(
                "Qualification uses a disposable qual- target and a built-in neutral attempt"
            )
        if not args.execute:
            result = preview(target, args.action, operation)
        else:
            if args.confirm_target != target.name:
                raise ValueError("Explicit confirmation must match the selected owned target")
            if args.action == "qualify":
                if not args.request or not args.host_id:
                    raise ValueError(
                        "Installed qualification requires original admission request and current host ID"
                    )
                result = qualify(target, json.loads(read_bounded(args.request)), args.host_id)
            else:
                with runtime.deadline(240), minimal_bootstrap.network_operation(target):
                    owned = consumer_lifecycle.owned(target, require_ready=False)
                    if args.action == "configure":
                        configure(target, operation)
                    else:
                        check_prerequisites(target)
                        inspect(target, render(target, operation))
                    result = {
                        **identity(target),
                        "incarnation": owned["incarnation"],
                        "outcome": "observed",
                        "operationDigest": operation.digest if operation else None,
                    }
        print(json.dumps(result, indent=2))
        return 0
    except failure.Failure as error:
        print(
            json.dumps(
                {
                    "failure": error.evidence,
                    "receipt": "Inspect the selected target's private delegation receipt.",
                }
            )
        )
    except (OSError, TypeError, KeyError, json.JSONDecodeError):
        print("Demo delegation refused or incomplete. Private details withheld.")
    except ValueError as error:
        print(f"Demo delegation refused or incomplete. {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
