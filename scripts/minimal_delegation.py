"""Preview, install or qualify minimal-demo delegation on one explicit owned target."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
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
    ATTEMPT_LABEL,
    JOB_LABELS,
    OWNER_LABEL,
    RECONCILER,
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
    actual = database_runtime.inventory(target, [desired]).get(reference(desired))
    if actual is None:
        raise ValueError("Expected existing provider local-retained StorageClass")
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
        for key in ("args", "command", "env", "envFrom", "volumeMounts"):
            if item.get(key) == []:
                item.pop(key)
        for env in item.get("env", []):
            reference_value = env.get("valueFrom", {}).get("fieldRef")
            if reference_value is not None:
                reference_value.setdefault("apiVersion", "v1")
        for values in item["resources"].values():
            for key, value in values.items():
                values[key] = quantity(value, cpu=key == "cpu")
    metadata = deepcopy(template.get("metadata", {}))
    if metadata.get("creationTimestamp") is None:
        metadata.pop("creationTimestamp", None)
    return {"spec": pod, "metadata": metadata}


def canonical_policy(kind: str, spec: dict) -> dict:
    value = deepcopy(spec)
    match_key = {
        "ValidatingAdmissionPolicy": "matchConstraints",
        "ValidatingAdmissionPolicyBinding": "matchResources",
    }.get(kind)
    if match_key and match_key in value:
        match = value[match_key]
        if match.get("matchPolicy") == "Equivalent":
            match.pop("matchPolicy")
        for field in (
            "namespaceSelector",
            "objectSelector",
            "excludeResourceRules",
            "resourceRules",
        ):
            if match.get(field) in ({}, []):
                match.pop(field)
        if not match:
            value.pop(match_key)
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
    if kind == "ResourceQuota" and type_checked:
        status = actual.get("status", {})
        hard = desired["spec"]["hard"]
        if status.get("hard") != hard or not hard.keys() <= status.get("used", {}).keys():
            raise ValueError("Demo quota accounting pending")
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


def inspect_existing_job(operation: Operation, actual: dict) -> None:
    wanted = operation.job()
    meta, expected_meta = actual["metadata"], wanted["metadata"]
    uid = meta.get("uid")
    if (
        not uid
        or meta.get("deletionTimestamp")
        or any(
            meta.get(key)
            for key in ("annotations", "ownerReferences", "finalizers", "generateName")
        )
        or any(meta.get(key) != expected_meta[key] for key in ("name", "namespace", "labels"))
    ):
        raise ValueError("Existing Job does not have the reviewed attempt identity")
    actual_spec, expected_spec = deepcopy(actual["spec"]), wanted["spec"]
    expected_spec.update(
        suspend=False,
        manualSelector=False,
        completionMode="NonIndexed",
        selector={"matchLabels": {JOB_LABELS[0]: uid}},
    )
    actual_template = actual_spec.pop("template")
    expected_template = expected_spec.pop("template")
    expected_template["metadata"]["labels"].update(
        dict(zip(JOB_LABELS, (uid, operation.name, uid, operation.name), strict=True))
    )
    if actual_spec != expected_spec or canonical_template(actual_template) != canonical_template(
        expected_template
    ):
        raise ValueError("Existing Job differs from the exact reviewed attempt")


def preflight_attempt(target: Target, operation: Operation) -> bool:
    attempt_name = re.compile(r"demo-launch-[0-9a-f]{24}").fullmatch
    binding_names = {operation.launcher}
    if operation.observer:
        binding_names.add(operation.observer_binding)
    for ns in namespaces(target):
        grants = json.loads(
            kube(
                target,
                [
                    "-n",
                    ns,
                    "get",
                    "rolebindings",
                    "-o",
                    "json",
                ],
            )
        )["items"]
        for grant in grants:
            meta = grant["metadata"]
            labels = meta.get("labels", {})
            owner = labels.get(OWNER_LABEL)
            owned = labels.get(LABEL) == target.consumer and owner not in {None, "base"}
            delegated = any(
                subject["kind"] == "ServiceAccount"
                and subject.get("namespace") == operations_namespace(target)
                and attempt_name(subject["name"])
                for subject in grant.get("subjects", [])
            )
            if not (owned or delegated or attempt_name(meta["name"].removesuffix("-observer"))):
                continue
            if (
                owner != operation.launcher
                or meta["name"] not in binding_names
                or ns != operation.namespace
                or meta.get("deletionTimestamp")
            ):
                raise ValueError(
                    "Another reviewed attempt grant is still present; withdraw it before configuring a new attempt"
                )
        jobs = json.loads(
            kube(target, ["-n", ns, "get", "jobs", "-l", ATTEMPT_LABEL, "-o", "json"])
        )["items"]
        for job in jobs:
            meta = job["metadata"]
            marker = meta["labels"][ATTEMPT_LABEL]
            if not attempt_name(marker):
                continue
            if (ns, meta["name"], marker) == (
                operation.namespace,
                operation.name,
                operation.launcher,
            ):
                continue
            status = job.get("status", {})
            if (
                marker == operation.launcher
                or meta.get("deletionTimestamp")
                or status.get("active", 0) > 0
                or status.get("terminating", 0) > 0
                or not any(
                    condition["type"] in {"Complete", "Failed"} and condition["status"] == "True"
                    for condition in status.get("conditions", [])
                )
            ):
                raise ValueError("Another owned Job is active or its outcome is incomplete")
    return attempt_job(target, operation) is not None


def attempt_job(target: Target, operation: Operation) -> dict | None:
    raw = kube(
        target,
        [
            "-n",
            operation.namespace,
            "get",
            "job",
            operation.name,
            "--ignore-not-found",
            "-o",
            "json",
        ],
    )
    if not raw.strip():
        return None
    job = json.loads(raw)
    inspect_existing_job(operation, job)
    return job


def configure(
    target: Target,
    operation: Operation | None,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> None:
    if operation:
        try:
            withdrawal_path(target, operation).lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("Attempt has recorded withdrawal; configure a fresh reviewed attempt")
    check_prerequisites(target)
    existing_job = preflight_attempt(target, operation) if operation else False
    objects = render(target, operation)
    if operation and operation.observer and operation.observer["kind"] == "ServiceAccount":
        account = operation.observer
        raw = kube(
            target,
            ["-n", account["namespace"], "get", "serviceaccount", account["name"], "-o", "json"],
        )
        if json.loads(raw)["metadata"].get("deletionTimestamp"):
            raise ValueError("Reviewed deployment observer ServiceAccount is terminating")
    preflight = database_runtime.inventory(target, objects)
    base = render(target) if operation else objects
    bindings = [item for item in objects if item["kind"] == "RoleBinding"]
    attempt_grants = [
        item for item in bindings if item["metadata"]["labels"][OWNER_LABEL] != "base"
    ]
    # A partially removed policy must not be silently repaired with its grants
    # still active. Withdraw the affected binding before resuming.
    if any(reference(item) in preflight for item in base if item["kind"] == "RoleBinding") and any(
        reference(item) not in preflight for item in base if item["kind"] != "RoleBinding"
    ):
        raise ValueError("Existing base grant has incomplete controls; withdraw it before recovery")
    if any(reference(item) in preflight for item in attempt_grants) and any(
        reference(item) not in preflight for item in objects if item["kind"] != "RoleBinding"
    ):
        raise ValueError(
            "Existing attempt grant has incomplete controls; withdraw it before recovery"
        )
    for item in objects:
        current = preflight.get(reference(item))
        if current:
            check_document(item, current, type_checked=False)
    namespaces_to_install = [item for item in objects if item["kind"] == "Namespace"]
    policies = [item for item in objects if item not in [*namespaces_to_install, *bindings]]
    if checkpoint:
        checkpoint("preflight")
    install_group(target, namespaces_to_install)
    if checkpoint:
        checkpoint("operations-namespace")
    install_group(target, policies)
    if checkpoint:
        checkpoint("controls-installed")
    with runtime.deadline(60):
        while True:
            try:
                inspect(target, policies)
                break
            except ValueError as error:
                if str(error) not in {
                    "Demo admission type checking pending",
                    "Demo quota accounting pending",
                }:
                    raise
                time.sleep(1)
        # Only dry runs use the provider's authorization to reach the same
        # admission rules before the scoped RoleBindings become effective.
        privilege = "--as-group=system:masters"
        reconciler = [*probe.as_account(operations_namespace(target), RECONCILER), privilege]
        probe.prove_routine_admission(target, reconciler)
        if operation and not existing_job:
            actor = [*probe.as_account(operations_namespace(target), operation.launcher), privilege]
            altered = operation.job()
            altered["spec"]["template"]["spec"]["serviceAccountName"] = "default"
            expected = ("admission", f"{target.consumer}-{operation.launcher}-job")
            probe.denied(target, altered, actor, expected)
            probe.assert_job_marker_required(target, operation, actor)
            probe.create(target, operation.job(), actor)
        supports = namespaces(target)[2]
        service = resource(
            "Service",
            "delegation-admission-control",
            supports,
            spec={
                "type": "NodePort",
                "ports": [{"port": 443}],
            },
        )
        probe.denied(target, service, [], ("admission", f"{target.consumer}-demo-service"))
        volume = probe.claim(supports, "delegation-admission-control", "5Gi")
        probe.denied(target, volume, [], ("admission", f"{target.consumer}-database-storage-pvc"))
        if checkpoint:
            checkpoint("controls-verified")
        for grant in bindings:
            grant_key = reference(grant)
            if existing_job and grant in attempt_grants and grant_key not in preflight:
                continue
            install_group(target, [grant])
            if checkpoint:
                kind, name, namespace = reference(grant)
                checkpoint(f"{kind}/{namespace}/{name}")


def configure_recorded(target: Target, operation: Operation | None, incarnation: str) -> dict:
    attempt = operation.launcher if operation else "base"
    descriptor, filename = tempfile.mkstemp(
        prefix=f"{target.name}.delegation-configure-{attempt}-",
        suffix=".json",
        dir=target.directory,
    )
    os.close(descriptor)
    path = Path(filename)
    receipt = {
        **identity(target),
        "incarnation": incarnation,
        "operationDigest": operation.digest if operation else None,
        **observer_handoff(operation),
        "lastCompletedPhase": "target-verified",
        "outcome": "running",
    }

    def checkpoint(phase: str) -> None:
        receipt["lastCompletedPhase"] = phase
        minimal_network.save_receipt(path, receipt)

    checkpoint("target-verified")
    try:
        configure(target, operation, checkpoint=checkpoint)
        receipt["outcome"] = "configured"
        checkpoint("completion")
    except (Exception, KeyboardInterrupt) as error:
        safe = failure.evidence(error, "delegation-configure", "dependency-failure")
        receipt.update(outcome="failed", failure=safe)
        minimal_network.save_receipt(path, receipt)
        raise failure.Failure(safe) from None
    return receipt


def withdrawal_path(target: Target, operation: Operation) -> Path:
    return target.directory / f"{target.name}.delegation-withdraw-{operation.launcher}.json"


def attempt_resources(target: Target, operation: Operation) -> list[dict]:
    return [
        item
        for item in render(target, operation)
        if item["metadata"]["labels"][OWNER_LABEL] == operation.launcher
    ]


def withdrawal_job(target: Target, operation: Operation) -> dict:
    job = attempt_job(target, operation)
    uid = job["metadata"]["uid"] if job else None
    status = job.get("status", {}) if job else {}
    if job and (
        status.get("active", 0)
        or status.get("terminating", 0)
        or any((status.get("uncountedTerminatedPods") or {}).values())
        or not any(
            condition["type"] in {"Complete", "Failed"} and condition["status"] == "True"
            for condition in status.get("conditions", [])
        )
    ):
        raise ValueError("Attempt must be unused or terminal before withdrawal")
    pods = json.loads(kube(target, ["-n", operation.namespace, "get", "pods", "-o", "json"]))[
        "items"
    ]
    pod_uids = []
    for pod in pods:
        meta = pod["metadata"]
        owned = uid is not None and any(
            owner.get("controller") is True
            and (owner.get("apiVersion"), owner.get("kind"), owner.get("name"), owner.get("uid"))
            == ("batch/v1", "Job", operation.name, uid)
            for owner in meta.get("ownerReferences", [])
        )
        if not owned and meta.get("labels", {}).get(ATTEMPT_LABEL) != operation.launcher:
            continue
        if (
            not owned
            or meta.get("deletionTimestamp")
            or pod["status"].get("phase") not in {"Succeeded", "Failed"}
        ):
            raise ValueError("Attempt Pods are still active or their ownership is incomplete")
        pod_uids.append(meta["uid"])
    return {"jobUID": uid, "podUIDs": sorted(pod_uids)}


def delete_attempt_resource(target: Target, item: dict) -> None:
    plurals = {
        "RoleBinding": "rolebindings",
        "ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
        "ValidatingAdmissionPolicy": "validatingadmissionpolicies",
        "PodTemplate": "podtemplates",
        "Role": "roles",
        "ServiceAccount": "serviceaccounts",
    }
    kind, name, ns = reference(item)
    path = "/api/v1" if item["apiVersion"] == "v1" else f"/apis/{item['apiVersion']}"
    if ns:
        path += f"/namespaces/{ns}"
    path += f"/{plurals[kind]}/{name}"
    runtime.command(
        target,
        [
            "kubectl",
            "--kubeconfig",
            str(target.path("kubeconfig")),
            "--context",
            target.context,
            "--request-timeout=30s",
            "delete",
            "--raw",
            path,
            "-f",
            "-",
        ],
        input_data=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {key: item["metadata"][key] for key in ("uid", "resourceVersion")},
            }
        ).encode(),
    )


def withdraw(target: Target, operation: Operation, incarnation: str) -> dict:
    """Retire one reviewed attempt under the caller's existing provider lock."""
    path = withdrawal_path(target, operation)
    own = {
        **identity(target),
        "incarnation": incarnation,
        "operationDigest": operation.digest,
        **observer_handoff(operation),
    }
    try:
        receipt = json.loads(read_bounded(path))
    except FileNotFoundError:
        receipt = None
    if receipt is not None and (
        any(receipt.get(key) != value for key, value in own.items())
        or receipt.get("outcome") not in {"withdrawing", "withdrawn"}
    ):
        raise ValueError("Withdrawal receipt does not match the reviewed attempt and target")
    desired = attempt_resources(target, operation)
    present = database_runtime.inventory(target, desired)
    for item in desired:
        actual = present.get(reference(item))
        if actual is not None:
            check_document(item, actual, type_checked=False)
    uids = {
        "/".join(part for part in key if part): item["metadata"]["uid"]
        for key, item in present.items()
    }
    if receipt is not None and any(
        receipt["resourceUIDs"].get(key) != uid for key, uid in uids.items()
    ):
        raise ValueError("Attempt resource was replaced after withdrawal began")
    if receipt is not None and receipt["outcome"] == "withdrawn":
        if present:
            raise ValueError("Withdrawn attempt resources reappeared")
        return receipt
    observed = withdrawal_job(target, operation)
    if receipt is None:
        if not present:
            raise ValueError("No reviewed attempt resources exist to withdraw")
        receipt = {**own, **observed, "resourceUIDs": uids, "outcome": "withdrawing"}
        minimal_network.save_receipt(path, receipt)
    elif receipt["jobUID"] is not None and receipt["jobUID"] != observed["jobUID"]:
        raise ValueError("Reviewed Job identity changed after withdrawal began")
    launcher_key = "RoleBinding", operation.launcher, operation.namespace
    if launcher_key in present:
        delete_attempt_resource(target, present.pop(launcher_key))
    launcher = probe.as_account(operations_namespace(target), operation.launcher)
    with runtime.deadline(20):
        while probe.allowed(target, launcher, "create", "jobs.batch", operation.namespace):
            time.sleep(1)
    if withdrawal_job(target, operation) != observed:
        raise ValueError("Attempt changed during withdrawal; inspect its outcome before retrying")
    receipt.update(observed)
    minimal_network.save_receipt(path, receipt)
    # Removing the admission bindings before their parameter keeps other
    # flows free of missing-parameter errors.
    for kind in (
        "RoleBinding",
        "ValidatingAdmissionPolicyBinding",
        "ValidatingAdmissionPolicy",
        "PodTemplate",
        "Role",
        "ServiceAccount",
    ):
        for key, item in present.items():
            if key[0] == kind:
                delete_attempt_resource(target, item)
    if database_runtime.inventory(target, desired):
        raise ValueError("Attempt withdrawal is incomplete; inspect and retry")
    receipt["outcome"] = "withdrawn"
    minimal_network.save_receipt(path, receipt)
    return receipt


def qualification_operation(
    target: Target, image: str, *, observer: dict | None = None
) -> Operation:
    return load_operation(
        target,
        {
            "namespace": namespaces(target)[0],
            "name": "delegation-job-probe",
            "deadlineSeconds": 90,
            **({"observer": observer} if observer else {}),
            "podSpec": {
                "securityContext": {"runAsUser": 65532, "runAsGroup": 65532},
                "containers": [
                    {
                        "name": "probe",
                        "image": image,
                        "command": ["python", "-I", "-c", "pass"],
                        "env": [
                            {"name": "FIRST", "value": "control"},
                            {"name": "NEXT", "value": "$(FIRST)"},
                        ],
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
    fixture_uids = {item["uid"] for item in network["fixturePods"].values()}
    cilium = network["controllerPods"]
    controllers = database["controllerPods"]
    deployment_namespaces = {
        "cert-manager": "cert-manager",
        "cert-manager-cainjector": "cert-manager",
        "cert-manager-webhook": "cert-manager",
        "cnpg-controller-manager": "cnpg-system",
        "barman-cloud": "cnpg-system",
    }
    found = minimal_network.foundation(
        target,
        {key.split("/")[0] for key in cilium},
        fixture_uids,
        network["networkSelection"],
        additional={
            ("Deployment", key, deployment_namespaces[key]): item["deploymentUID"]
            for key, item in controllers.items()
        },
    )
    if found != network["foundation"]:
        raise ValueError("Disposable foundation changed since the predecessor qualification")
    expected = fixture_uids | {item["uid"] for item in [*controllers.values(), *cilium.values()]}
    pods = minimal_network.list_resources(target, "pods")
    observed = {item["metadata"]["uid"] for item in pods}
    if (
        not expected <= observed
        or len(observed - expected) != len(found["podInventory"])
        or any(
            item["metadata"].get("deletionTimestamp") or item["status"].get("phase") != "Running"
            for item in pods
        )
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


def verify_storage_helper(target: Target, database: dict) -> None:
    expected = database["storageDriver"]
    actual = json.loads(
        kube(
            target,
            [
                "-n",
                "local-path-storage",
                "get",
                "configmap",
                "local-path-config",
                "-o",
                "json",
            ],
        )
    )
    if (
        actual["metadata"].get("deletionTimestamp")
        or actual["metadata"]["uid"] != expected.get("configurationUID")
        or host_admission.digest(actual["data"]) != expected.get("configurationDigest")
    ):
        raise ValueError("The qualified local-path helper configuration changed")


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
        foreign = f"{target.consumer}-demo-foreign-control"
        observer_account = "delegation-job-observer"
        operation = qualification_operation(
            target,
            images[0],
            observer={"kind": "ServiceAccount", "name": observer_account, "namespace": foreign},
        )
        cached = minimal_network.staged_images(
            target, journals, {"control-plane": [], "worker": list(images)}
        )
        bounds = worker_headroom(target, database, network)
        spaces = [*render_namespaces(target), resource("Namespace", foreign, None)]
        if database_runtime.inventory(
            target, [*spaces, *render(target, operation)]
        ) or minimal_network.list_resources(target, "persistentvolumeclaims,persistentvolumes"):
            raise ValueError(
                "Existing demo fixtures or retained claims refuse disposable qualification"
            )
        path = minimal_network.reserve_receipt(target, capability="delegation")
        receipt = {
            **identity(target),
            **{
                key: cold[key]
                for key in ("artifactDigest", "sourceProvenance", "nodeImage", "toolVersions")
            },
            "incarnation": incarnation,
            "databaseReceiptDigest": host_admission.digest(database),
            "operationDigest": operation.digest,
            **observer_handoff(operation),
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
                probe.create(
                    target,
                    resource(
                        "ServiceAccount",
                        observer_account,
                        foreign,
                        automountServiceAccountToken=False,
                    ),
                    [],
                    dry_run=False,
                )
                configure(target, operation)
                publish("delegation")
            with failure.phase("authorization-and-admission", "dependency-failure"):
                receipt["nativeControls"] = probe.prove(target, operation, foreign)
                probe.prove_observer_scope(target, operation, foreign)
                receipt["nativeControls"]["neutralJob"] = probe.run_job(target, operation)
                publish("authorization-and-admission")
            with failure.phase("attempt-withdrawal", "dependency-failure"):
                withdrawal = withdraw(target, operation, incarnation)
                probe.prove_observer_revoked(target, operation)
                receipt["nativeControls"]["attemptWithdrawal"] = {
                    "jobUID": withdrawal["jobUID"],
                    "observerAccessRevoked": True,
                }
                if withdrawal["jobUID"] != receipt["nativeControls"]["neutralJob"]["jobUID"]:
                    raise ValueError("Withdrawal did not retain the observer's exact Job identity")
                publish("attempt-withdrawal")
            with failure.phase("retained-filesystem", "dependency-failure"):
                minimal_network.disk_floor(request, host_id, cold["networkAdmission"])
                verify_storage_helper(target, database)
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
                inspect(target, render(target))
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
    objects = (
        attempt_resources(target, operation)
        if action == "withdraw" and operation
        else render(target, operation)
    )
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
            **observer_handoff(operation),
        }
        if operation
        else None,
        "resources": [reference(item) for item in objects],
        "workerProbeImages": list(probe_images()) if action == "qualify" else [],
        "installedAcceptance": False,
    }


def observer_handoff(operation: Operation | None) -> dict:
    if operation and operation.observer:
        return {
            "observer": {"subject": operation.observer, "roleBinding": operation.observer_binding}
        }
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "health", "qualify", "job", "withdraw"))
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
        if args.action == "withdraw" and operation is None:
            raise ValueError("Withdrawal requires the reviewed operation file")
        if args.action == "job":
            if args.execute or operation is None:
                raise ValueError(
                    "Job rendering requires a reviewed operation file and stays offline"
                )
            result = operation.job()
        elif not args.execute:
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
                        result = configure_recorded(target, operation, owned["incarnation"])
                    elif args.action == "withdraw" and operation is not None:
                        result = withdraw(target, operation, owned["incarnation"])
                    else:
                        check_prerequisites(target)
                        inspect(target, render(target, operation))
                        result = {
                            **identity(target),
                            "incarnation": owned["incarnation"],
                            "outcome": "observed",
                            "operationDigest": operation.digest if operation else None,
                            **observer_handoff(operation),
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
