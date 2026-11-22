"""Native impersonated admission, authorization and quota controls for demo delegation."""

from __future__ import annotations

import json
from copy import deepcopy

from . import consumer_runtime as runtime
from .consumer_profile import LABEL, Target
from .database_qualification import cluster, pvc, uid_delete
from .delegated_argo_policy import resource
from .kubernetes import kube
from .local_kind import CommandFailure
from .minimal_delegation_policy import (
    ATTEMPT_LABEL,
    RECONCILER,
    ROUTINE_CLAIM_PREFIX,
    ROUTINE_LABEL,
    ROUTINE_RUNTIME,
    Operation,
    namespaces,
    operations_namespace,
    username,
)
from .minimal_network import owner_uid


def as_account(namespace: str, account: str) -> list[str]:
    return [
        "--as=" + username(namespace, account),
        "--as-group=system:authenticated",
        "--as-group=system:serviceaccounts",
        "--as-group=system:serviceaccounts:" + namespace,
    ]


def decision(
    target: Target, actor: list[str], verb: str, kind: str, namespace: str | None, *, yes: bool
) -> None:
    attrs = {"verb": verb, "resource": kind.split(".")[0]}
    if "." in kind:
        attrs["group"] = kind.split(".", 1)[1]
    if "/" in attrs["resource"]:
        attrs["resource"], attrs["subresource"] = attrs["resource"].split("/", 1)
    if namespace:
        attrs["namespace"] = namespace
    review = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": actor[0].removeprefix("--as="),
            "groups": [group.removeprefix("--as-group=") for group in actor[1:]],
            "resourceAttributes": attrs,
        },
    }
    result = runtime.command(
        target,
        [
            "kubectl",
            "--kubeconfig",
            str(target.path("kubeconfig")),
            "--context",
            target.context,
            "--request-timeout=30s",
            "create",
            "-f",
            "-",
            "-o",
            "json",
        ],
        input_data=json.dumps(review).encode(),
    )
    status = json.loads(result)["status"]
    if status.get("evaluationError") or status["allowed"] is not yes:
        raise ValueError("Native demo SubjectAccessReview differs from expected authorization")


def create(target: Target, document: dict, actor: list[str], *, dry_run: bool = True) -> dict:
    raw = kube(
        target,
        [
            *actor,
            "create",
            *(["--dry-run=server"] if dry_run else []),
            "-f",
            "-",
            "-o",
            "json",
        ],
        [document],
    )
    response = json.loads(raw)
    return response["items"][0] if response["kind"] == "List" else response


def denied(target: Target, document: dict, actor: list[str], expected: tuple[str, str]) -> None:
    try:
        create(target, document, actor)
    except CommandFailure as error:
        if error.denial != expected:
            raise ValueError(
                "Native demo rejection did not come from the expected control"
            ) from None
    else:
        raise ValueError("Native demo negative control was admitted")


def pvc_delete(target: Target, item: dict) -> None:
    metadata = item["metadata"]
    namespace, name = metadata["namespace"], metadata["name"]
    current = json.loads(kube(target, ["-n", namespace, "get", "pvc", name, "-o", "json"]))
    if (
        current["metadata"]["uid"] != metadata["uid"]
        or current.get("status", {}).get("phase", "Pending") != "Pending"
        or current["spec"].get("volumeName")
        or current["metadata"].get("annotations", {}).get("volume.kubernetes.io/selected-node")
    ):
        raise ValueError(
            "Quota fixture acquired a binding or changed identity; retained for inspection"
        )
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
            f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}",
            "-f",
            "-",
        ],
        input_data=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {
                    "uid": metadata["uid"],
                    "resourceVersion": current["metadata"]["resourceVersion"],
                },
            }
        ).encode(),
    )


def claim(namespace: str, name: str, size: str, **spec) -> dict:
    document = pvc(namespace, name, **spec)
    document["spec"]["resources"]["requests"]["storage"] = size
    return document


def prove_storage(target: Target, reconciler: list[str]) -> dict:
    apps, data, support = namespaces(target)
    for ns in (apps, data, support):
        for size in ("1Gi", "4Gi"):
            create(target, claim(ns, ROUTINE_CLAIM_PREFIX + "size-control", size), reconciler)
    for invalid in ("512Mi", "5Gi", "1500Mi"):
        denied(
            target,
            claim(support, ROUTINE_CLAIM_PREFIX + "size-control", invalid),
            reconciler,
            ("admission", f"{target.consumer}-database-storage-pvc"),
        )
    denied(
        target,
        claim(support, ROUTINE_CLAIM_PREFIX + "size-control", "1Gi", storageClassName="standard"),
        reconciler,
        ("admission", f"{target.consumer}-database-storage-pvc"),
    )
    denied(
        target,
        claim(support, ROUTINE_CLAIM_PREFIX + "size-control", "1Gi", accessModes=["ReadWriteMany"]),
        reconciler,
        ("admission", f"{target.consumer}-database-storage-pvc"),
    )
    denied(
        target,
        claim(support, ROUTINE_CLAIM_PREFIX + "size-control", "1Gi", volumeName="foreign-retained"),
        reconciler,
        ("admission", f"{target.consumer}-demo-volume"),
    )
    for ns, count, size, reason in ((data, 4, "1Gi", "count"), (support, 2, "4Gi", "aggregate")):
        batch = []
        try:
            for index in range(count):
                batch.append(
                    create(
                        target,
                        claim(ns, f"{ROUTINE_CLAIM_PREFIX}{reason}-{index}", size),
                        reconciler,
                        dry_run=False,
                    )
                )
            denied(
                target,
                claim(ns, f"{ROUTINE_CLAIM_PREFIX}{reason}-extra", "1Gi"),
                reconciler,
                ("quota", "database-storage"),
            )
        finally:
            for item in batch:
                pvc_delete(target, item)
    return {"wholeGiB": "1–4", "maximumClaims": 4, "maximumRequestedGiB": 8}


def assert_job_marker_required(target: Target, operation: Operation, actor: list[str]) -> None:
    unmarked = operation.job()
    unmarked["metadata"].pop("labels")
    # Job defaulting copies PodTemplate labels onto an otherwise unlabeled Job.
    unmarked["spec"]["template"]["metadata"]["labels"].pop(ATTEMPT_LABEL)
    denied(
        target,
        unmarked,
        actor,
        ("admission", f"{target.consumer}-{operation.launcher}-job-marker"),
    )


def assert_unrelated_jobs(target: Target, operation: Operation, foreign: str) -> None:
    # The foreign Job deliberately retains the marker so the immutable namespace
    # selector is tested independently from the object selector.
    unrelated = operation.job()
    unrelated["metadata"].update(name="delegation-unrelated-job", namespace=foreign)
    unrelated["spec"]["template"]["spec"]["serviceAccountName"] = "default"
    create(target, unrelated, [])
    unrelated["metadata"].update(namespace=operation.namespace)
    unrelated["metadata"].pop("labels")
    unrelated["spec"]["template"]["metadata"]["labels"].pop(ATTEMPT_LABEL)
    create(target, unrelated, [])


def routine_controller(target: Target, workload: str, *, name: str) -> dict:
    namespace = namespaces(target)[0]
    labels = {ROUTINE_LABEL: "true", "app": name}
    template = {
        "metadata": {"labels": labels},
        "spec": {
            "serviceAccountName": ROUTINE_RUNTIME,
            "automountServiceAccountToken": False,
            "nodeSelector": {LABEL: target.consumer, "k8s-infrastructure.io/role": "worker"},
            "containers": [
                {
                    "name": "probe",
                    "image": "probe.invalid/control-only",
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "100m", "memory": "128Mi"},
                    },
                }
            ],
        },
    }
    kind = "Deployment" if workload == "deployments" else "StatefulSet"
    controller = resource(
        kind,
        name,
        namespace,
        spec={
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": template,
            **(
                {"strategy": {"type": "Recreate"}}
                if kind == "Deployment"
                else {"serviceName": name}
            ),
        },
    )
    controller["apiVersion"] = "apps/v1"
    controller["metadata"]["labels"] = labels
    return controller


def prove_routine_admission(target: Target, reconciler: list[str]) -> None:
    for workload in ("deployments", "statefulsets"):
        controller = routine_controller(target, workload, name="delegation-routine-control")
        expected = ("admission", f"{target.consumer}-demo-routine-{workload}")
        create(target, controller, reconciler)
        altered = deepcopy(controller)
        altered["spec"]["replicas"] = 2
        denied(target, altered, reconciler, expected)
        altered = deepcopy(controller)
        altered["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = (
            "513Mi"
        )
        denied(target, altered, reconciler, expected)
        ordinary = deepcopy(controller)
        ordinary["spec"]["template"]["spec"]["volumes"] = [
            {
                "name": "data",
                "persistentVolumeClaim": {"claimName": ROUTINE_CLAIM_PREFIX + "fixture"},
            }
        ]
        create(target, ordinary, reconciler)
        for field, replacement in (
            ("serviceAccountName", "credential-operator"),
            ("automountServiceAccountToken", True),
            ("imagePullSecrets", [{"name": "credential-operator"}]),
            ("volumes", [{"name": "credentials", "secret": {"secretName": "operator"}}]),
            (
                "volumes",
                [
                    {
                        "name": "credentials",
                        "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]},
                    }
                ],
            ),
            (
                "volumes",
                [{"name": "data", "persistentVolumeClaim": {"claimName": "operator-data"}}],
            ),
        ):
            altered = deepcopy(controller)
            altered["spec"]["template"]["spec"][field] = replacement
            denied(target, altered, reconciler, expected)
        for field, value in (
            (
                "env",
                [
                    {
                        "name": "KEY",
                        "valueFrom": {"secretKeyRef": {"name": "operator", "key": "key"}},
                    }
                ],
            ),
            ("envFrom", [{"secretRef": {"name": "operator"}}]),
        ):
            altered = deepcopy(controller)
            altered["spec"]["template"]["spec"]["containers"][0][field] = value
            denied(target, altered, reconciler, expected)


def prove_operator_authority(target: Target, reconciler: list[str]) -> None:
    namespace = namespaces(target)[1]
    database = cluster(target, namespace)
    create(target, database, [])
    denied(target, database, reconciler, ("authorization", "forbidden"))
    protected = routine_controller(target, "deployments", name="delegation-operator-control")
    protected["metadata"].pop("labels")
    protected["spec"]["replicas"] = 0
    protected["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "operator-placeholder"}}
    ]
    created = create(target, protected, [], dry_run=False)
    try:
        for verb, extra in (
            (
                "patch",
                [
                    "--type=merge",
                    "-p",
                    json.dumps(
                        {
                            "metadata": {"labels": {ROUTINE_LABEL: "true"}},
                            "spec": {
                                "template": routine_controller(
                                    target, "deployments", name=created["metadata"]["name"]
                                )["spec"]["template"]
                            },
                        }
                    ),
                ],
            ),
            ("delete", []),
        ):
            try:
                kube(
                    target,
                    [
                        *reconciler,
                        "-n",
                        created["metadata"]["namespace"],
                        verb,
                        "deployment",
                        created["metadata"]["name"],
                        "--dry-run=server",
                        *extra,
                    ],
                )
            except CommandFailure as error:
                if error.denial != ("admission", f"{target.consumer}-demo-routine-deployments"):
                    raise ValueError(
                        "Native operator fixture rejection did not come from admission"
                    ) from None
            else:
                raise ValueError("Demo reconciler could change a credential-operator controller")
    finally:
        uid_delete(target, created)


def prove(target: Target, operation: Operation, foreign: str) -> dict:
    ops = operations_namespace(target)
    actor = as_account(ops, operation.launcher)
    reconciler = as_account(ops, RECONCILER)
    pod = as_account(operation.namespace, operation.runtime)
    ns = operation.namespace
    for tested, verb, kind, scope, allowed in [
        (actor, "create", "jobs.batch", ns, True),
        (actor, "create", "jobs.batch", foreign, False),
        (actor, "patch", "jobs.batch", ns, False),
        (actor, "delete", "jobs.batch", ns, False),
        (actor, "create", "cronjobs.batch", ns, False),
        (actor, "create", "pods", ns, False),
        (actor, "create", "pods/exec", ns, False),
        (actor, "get", "pods/log", ns, False),
        (actor, "get", "secrets", ns, False),
        (actor, "create", "serviceaccounts/token", ops, False),
        (actor, "create", "persistentvolumeclaims", ns, False),
        (actor, "create", "rolebindings.rbac.authorization.k8s.io", ns, False),
        (actor, "create", "namespaces", None, False),
        (pod, "create", "jobs.batch", ns, False),
        (pod, "get", "secrets", ns, False),
        (reconciler, "create", "persistentvolumeclaims", namespaces(target)[2], True),
        (reconciler, "create", "configmaps", foreign, False),
        (reconciler, "create", "jobs.batch", ns, False),
        (reconciler, "create", "pods", ns, False),
        (reconciler, "get", "secrets", ns, False),
        (reconciler, "create", "secrets", ns, False),
        (reconciler, "patch", "secrets", ns, False),
        (reconciler, "get", "clusters.postgresql.cnpg.io", namespaces(target)[1], True),
        (reconciler, "create", "clusters.postgresql.cnpg.io", namespaces(target)[1], False),
        (reconciler, "patch", "clusters.postgresql.cnpg.io", namespaces(target)[1], False),
        (as_account(namespaces(target)[0], ROUTINE_RUNTIME), "get", "secrets", ns, False),
        (reconciler, "create", "serviceaccounts/token", ops, False),
        (reconciler, "update", "podtemplates", ns, False),
        (reconciler, "delete", "persistentvolumeclaims", ns, False),
        (reconciler, "create", "clusterroles.rbac.authorization.k8s.io", None, False),
    ]:
        decision(target, tested, verb, kind, scope, yes=allowed)
    expected = ("admission", f"{target.consumer}-{operation.launcher}-job")
    # The positive is the exact same principal, namespace and API entry point
    # as the altered attempts. Any compile/defaulting mismatch fails the gate.
    create(target, operation.job(), actor)
    assert_unrelated_jobs(target, operation, foreign)
    assert_job_marker_required(target, operation, actor)
    prove_routine_admission(target, reconciler)
    prove_operator_authority(target, reconciler)
    for field, replacement in {
        "serviceAccountName": "default",
        "automountServiceAccountToken": True,
        "nodeSelector": {"kubernetes.io/hostname": target.name + "-control-plane"},
        "volumes": [{"name": "other", "secret": {"secretName": "foreign-secret"}}],
    }.items():
        altered = operation.job()
        altered["spec"]["template"]["spec"][field] = replacement
        denied(target, altered, actor, expected)
    for volume_source in (
        {"hostPath": {"path": "/var"}},
        {"persistentVolumeClaim": {"claimName": "foreign-claim"}},
        {"projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}},
    ):
        altered = operation.job()
        altered["spec"]["template"]["spec"]["volumes"] = [{"name": "other", **volume_source}]
        denied(target, altered, actor, expected)
    altered = operation.job()
    altered["spec"]["template"]["spec"]["containers"][0]["args"] = ["unreviewed-command"]
    denied(target, altered, actor, expected)
    altered = operation.job()
    altered["spec"]["template"]["spec"]["containers"][0]["env"].reverse()
    denied(target, altered, actor, expected)
    altered = operation.job()
    altered["metadata"]["name"] = "different-attempt"
    denied(target, altered, actor, expected)
    altered = operation.job()
    altered["spec"]["parallelism"] = 2
    denied(target, altered, actor, expected)
    placement = resource("Pod", "delegation-placement-control", ns, spec=deepcopy(operation.pod))
    placement["spec"]["volumes"] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": "delegation-placement-control"}}
    ]
    create(target, placement, [])
    unpinned = deepcopy(placement)
    unpinned["spec"].pop("nodeSelector")
    denied(target, unpinned, [], ("admission", f"{target.consumer}-demo-placement"))
    service = resource(
        "Service",
        "delegation-private",
        ns,
        spec={
            "type": "ClusterIP",
            "ports": [{"port": 443}],
            "selector": {"probe": "isolated"},
        },
    )
    service["metadata"]["labels"] = {ROUTINE_LABEL: "true"}
    create(target, service, reconciler)
    exposed = deepcopy(service)
    exposed["spec"]["type"] = "NodePort"
    denied(target, exposed, reconciler, ("admission", f"{target.consumer}-demo-service"))
    denied(
        target,
        resource("ConfigMap", "unrelated", foreign, data={"marker": "unchanged"}),
        reconciler,
        ("authorization", "forbidden"),
    )
    token = resource(
        "Secret",
        "delegation-token-control",
        ns,
        type="kubernetes.io/service-account-token",
    )
    token["metadata"]["annotations"] = {"kubernetes.io/service-account.name": operation.runtime}
    denied(target, token, reconciler, ("authorization", "forbidden"))
    denied(target, token, [], ("admission", f"{target.consumer}-demo-secret"))
    return {
        "jobAdmission": "approved, altered and unrelated attempts exercised",
        "routineControllerAdmission": "ordinary work admitted and credential references refused",
        "storageQuota": prove_storage(target, reconciler),
    }


def run_job(target: Target, operation: Operation) -> dict:
    ns = operation.namespace
    actor = as_account(operations_namespace(target), operation.launcher)
    job = create(target, operation.job(), actor, dry_run=False)
    uid = job["metadata"]["uid"]
    kube(
        target,
        ["-n", ns, "wait", "--for=condition=Complete", f"job/{operation.name}", "--timeout=100s"],
        request_timeout=100,
    )
    direct = json.loads(
        kube(target, [*actor, "-n", ns, "get", "job", operation.name, "-o", "json"])
    )
    items = json.loads(
        kube(
            target,
            [
                "-n",
                ns,
                "get",
                "pods",
                "-l",
                f"batch.kubernetes.io/controller-uid={uid}",
                "-o",
                "json",
            ],
        )
    )["items"]
    if (
        len(items) != 1
        or owner_uid(items[0]) != uid
        or direct["metadata"]["uid"] != uid
        or direct["status"].get("succeeded") != 1
        or direct["status"].get("failed", 0) != 0
    ):
        raise ValueError("Reviewed Job did not complete once with its exact owner")
    pod = items[0]
    spec, status = pod["spec"], pod["status"]
    wanted = operation.pod["containers"][0]
    if (
        spec.get("nodeName") != target.name + "-worker"
        or spec.get("serviceAccountName") != operation.runtime
        or spec.get("automountServiceAccountToken") is not False
        or spec.get("volumes")
        or spec.get("initContainers")
        or spec.get("ephemeralContainers")
        or len(spec["containers"]) != 1
        or any(
            spec["containers"][0].get(key) != wanted[key]
            for key in ("name", "image", "imagePullPolicy", "command", "resources")
        )
        or status.get("phase") != "Succeeded"
        or len(status.get("containerStatuses", [])) != 1
        or status["containerStatuses"][0].get("restartCount") != 0
        or status["containerStatuses"][0]["state"]["terminated"]["exitCode"] != 0
    ):
        raise ValueError("Qualified Job Pod differs from the exact bounded operation")
    return {
        "jobUID": uid,
        "podUID": pod["metadata"]["uid"],
        "node": spec["nodeName"],
        "exitCode": 0,
    }
