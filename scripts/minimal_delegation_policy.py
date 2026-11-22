"""Offline minimal-demo namespace authority and one reviewed Job attempt."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass

from . import database_policy, host_admission
from .consumer_profile import LABEL, MINIMAL_PROFILE, Target
from .delegated_argo_policy import binding, resource, rule
from .network_resources import pod_bounds

RECONCILER = "demo-reconciler"
ATTEMPT_LABEL = "k8s-infrastructure.io/demo-attempt"
OWNER_LABEL = "k8s-infrastructure.io/demo-delegation"
JOB_LABELS = (
    "batch.kubernetes.io/controller-uid",
    "batch.kubernetes.io/job-name",
    "controller-uid",
    "job-name",
)
OPERATIONS = ("CREATE", "UPDATE")
POD_FIELDS = {
    "containers",
    "initContainers",
    "volumes",
    "securityContext",
    "enableServiceLinks",
}
CONTAINER_FIELDS = {
    "name",
    "image",
    "imagePullPolicy",
    "command",
    "args",
    "env",
    "envFrom",
    "resources",
    "securityContext",
    "volumeMounts",
    "workingDir",
}


def namespaces(target: Target) -> tuple[str, str, str]:
    if target.profile != MINIMAL_PROFILE or target.https_port is None:
        raise ValueError("Demo delegation requires a minimal-demo target with HTTPS listener")
    consumer = target.consumer
    return f"{consumer}-apps", f"{consumer}-data", f"{consumer}-demo-support"


def operations_namespace(target: Target) -> str:
    return f"{target.consumer}-demo-operations"


def username(namespace: str, name: str) -> str:
    return f"system:serviceaccount:{namespace}:{name}"


@dataclass(frozen=True)
class Operation:
    name: str
    namespace: str
    deadline: int
    pod: dict
    digest: str

    @property
    def launcher(self) -> str:
        return "demo-launch-" + self.digest.removeprefix("sha256:")[:24]

    @property
    def runtime(self) -> str:
        return self.launcher.replace("launch", "run", 1)

    def job(self) -> dict:
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "labels": {ATTEMPT_LABEL: self.launcher},
            },
            "spec": {
                "parallelism": 1,
                "completions": 1,
                "backoffLimit": 0,
                "activeDeadlineSeconds": self.deadline,
                "podReplacementPolicy": "Failed",
                "template": {
                    "metadata": {"labels": {ATTEMPT_LABEL: self.launcher}},
                    "spec": deepcopy(self.pod),
                },
            },
        }


def load_operation(target: Target, value: dict) -> Operation:
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "namespace", "deadlineSeconds", "podSpec"}
        or not isinstance(value["name"], str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,48}[a-z0-9]", value["name"])
        or value["namespace"] not in namespaces(target)[:2]
        or type(value["deadlineSeconds"]) is not int
        or not 1 <= value["deadlineSeconds"] <= 900
        or not isinstance(value["podSpec"], dict)
        or not set(value["podSpec"]) <= POD_FIELDS
    ):
        raise ValueError(
            "Select an explicit app/data Job attempt, 1-900 second deadline and reviewed PodSpec"
        )
    pod = deepcopy(value["podSpec"])
    containers, init = pod.get("containers"), pod.get("initContainers", [])
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or not isinstance(init, list)
        or len(init) > 1
    ):
        raise ValueError("Reviewed Job requires one main container and at most one initializer")
    for container in [*containers, *init]:
        validate_container(container)
    volumes = pod.get("volumes", [])
    if not isinstance(volumes, list) or any(
        not isinstance(volume, dict)
        or len(volume) != 2
        or "name" not in volume
        or not set(volume) <= {"name", "configMap", "secret", "persistentVolumeClaim", "emptyDir"}
        for volume in volumes
    ):
        raise ValueError("Reviewed Job volumes must be explicit ConfigMap, Secret, PVC or emptyDir")
    security = pod.get("securityContext", {})
    if (
        not isinstance(security, dict)
        or not set(security)
        <= {
            "runAsUser",
            "runAsGroup",
            "fsGroup",
            "fsGroupChangePolicy",
        }
        or any(security.get(key, 1) in {0, None} for key in ("runAsUser", "runAsGroup", "fsGroup"))
    ):
        raise ValueError("Reviewed Job Pod security context must use unprivileged IDs")
    digest = host_admission.digest({"consumer": target.consumer, **value})
    operation = Operation(value["name"], value["namespace"], value["deadlineSeconds"], pod, digest)
    pod.update(
        serviceAccountName=operation.runtime,
        automountServiceAccountToken=False,
        enableServiceLinks=False,
        restartPolicy="Never",
        terminationGracePeriodSeconds=60,
        nodeSelector={LABEL: target.consumer, "k8s-infrastructure.io/role": "worker"},
        securityContext={
            **security,
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    )
    pod_bounds(pod)
    return operation


def validate_container(container: dict) -> None:
    if (
        not isinstance(container, dict)
        or not set(container) <= CONTAINER_FIELDS
        or not isinstance(container.get("image"), str)
        or not re.fullmatch(r"[a-zA-Z0-9/_.:-]+@sha256:[a-f0-9]{64}", container["image"])
        or container.get("imagePullPolicy", "Never") != "Never"
    ):
        raise ValueError("Reviewed Job containers must use a staged immutable image")
    resources = container.get("resources", {})
    if (
        not isinstance(resources, dict)
        or set(resources) != {"requests", "limits"}
        or any(
            not isinstance(values, dict)
            or set(values) != {"cpu", "memory"}
            or not all(
                isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*(?:m|Mi|Gi)?", value)
                for value in values.values()
            )
            for values in resources.values()
        )
    ):
        raise ValueError(
            "Reviewed Job containers require finite CPU and memory requests and limits"
        )
    security = container.get("securityContext", {})
    if (
        not isinstance(security, dict)
        or not set(security)
        <= {
            "runAsUser",
            "runAsGroup",
            "readOnlyRootFilesystem",
        }
        or any(security.get(key, 1) in {0, None} for key in ("runAsUser", "runAsGroup"))
    ):
        raise ValueError(
            "Reviewed Job containers may only set unprivileged IDs and filesystem mode"
        )
    container.update(
        imagePullPolicy="Never",
        securityContext={
            **security,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    )


def policy(
    name: str,
    group: str,
    resources: list[str],
    verbs: tuple[str, ...],
    condition: str,
    validations: list[tuple[str, str]],
    *,
    parameter: dict | None = None,
) -> list[dict]:
    spec = {
        "failurePolicy": "Fail",
        "matchConstraints": {
            "resourceRules": [
                {
                    "apiGroups": [group],
                    "apiVersions": ["v1"],
                    "resources": resources,
                    "operations": list(verbs),
                    "scope": "Namespaced",
                }
            ],
        },
        "matchConditions": [{"name": "reviewed-boundary", "expression": condition}],
        "validations": [{"expression": expr, "message": message} for expr, message in validations],
    }
    binding_spec: dict = {"policyName": name, "validationActions": ["Deny", "Audit"]}
    if parameter:
        spec["paramKind"] = {"apiVersion": "v1", "kind": "PodTemplate"}
        binding_spec["paramRef"] = {**parameter, "parameterNotFoundAction": "Deny"}
    return [
        resource("ValidatingAdmissionPolicy", name, None, spec=spec),
        resource("ValidatingAdmissionPolicyBinding", name, None, spec=binding_spec),
    ]


def job_policy(target: Target, operation: Operation) -> list[dict]:
    # The Job admission sees Kubernetes' auto-generated controller labels.
    # All other template metadata must be exactly the reviewed attempt label.
    marker = json.dumps({ATTEMPT_LABEL: operation.launcher})
    template_labels = "object.spec.template.metadata.labels"
    allowed = json.dumps([ATTEMPT_LABEL, *JOB_LABELS])
    envelope = (
        f"object.spec.activeDeadlineSeconds == {operation.deadline}"
        " && object.spec.parallelism == 1 && object.spec.completions == 1"
        " && object.spec.backoffLimit == 0 && object.spec.suspend == false"
        " && object.spec.manualSelector == false && object.spec.completionMode == 'NonIndexed'"
        " && object.spec.podReplacementPolicy == 'Failed'"
        " && !has(object.spec.selector.matchExpressions)"
        " && size(object.spec.selector.matchLabels) == 1"
        " && 'batch.kubernetes.io/controller-uid' in object.spec.selector.matchLabels"
    )
    for field in (
        "managedBy",
        "podFailurePolicy",
        "successPolicy",
        "backoffLimitPerIndex",
        "maxFailedIndexes",
        "ttlSecondsAfterFinished",
    ):
        envelope += f" && !has(object.spec.{field})"
    metadata = (
        f"object.metadata.labels == {marker}"
        f" && {template_labels}.all(k, k in {allowed})"
        f" && {template_labels}['{ATTEMPT_LABEL}'] == '{operation.launcher}'"
    )
    for path in ("object.metadata", "object.spec.template.metadata"):
        for field in ("annotations", "ownerReferences", "finalizers", "generateName"):
            metadata += f" && !has({path}.{field})"
    return policy(
        f"{target.consumer}-{operation.launcher}-job",
        "batch",
        ["jobs"],
        ("CREATE",),
        f"request.userInfo.username == '{username(operations_namespace(target), operation.launcher)}'"
        f" || (has(object.metadata.labels) && '{ATTEMPT_LABEL}' in object.metadata.labels"
        f" && object.metadata.labels['{ATTEMPT_LABEL}'] == '{operation.launcher}')",
        [
            (
                f"request.namespace == '{operation.namespace}' && object.metadata.name == '{operation.name}'",
                "Demo launcher may create only its reviewed Job attempt",
            ),
            (envelope, "Demo Job must use the reviewed controller and bounded execution"),
            (metadata, "Demo Job metadata must contain only the reviewed attempt and system keys"),
            (
                "params != null && dyn(object.spec.template.spec) == dyn(params.template.spec)",
                "Demo Job PodSpec must exactly match the reviewed template",
            ),
        ],
        parameter={"name": operation.launcher, "namespace": operation.namespace},
    )


def render(target: Target, operation: Operation | None = None) -> list[dict]:
    spaces = namespaces(target)
    ops = operations_namespace(target)
    documents = database_policy.objects(target, spaces)
    documents.extend(
        [
            resource("Namespace", ops, None),
            resource("ServiceAccount", RECONCILER, ops, automountServiceAccountToken=False),
        ]
    )
    for ns in spaces:
        rules = [
            rule(
                "",
                ["configmaps", "secrets", "services"],
                ["get", "list", "watch", "create", "update", "patch", "delete"],
            ),
            rule("", ["persistentvolumeclaims"], ["get", "list", "watch", "create"]),
            rule("", ["pods", "events"], ["get", "list", "watch"]),
        ]
        if ns == spaces[1]:
            rules.append(
                rule(
                    "postgresql.cnpg.io",
                    ["clusters"],
                    ["get", "list", "watch", "create", "update", "patch"],
                )
            )
        else:
            rules.append(
                rule(
                    "apps",
                    ["deployments", "statefulsets"],
                    ["get", "list", "watch", "create", "update", "patch", "delete"],
                )
            )
        documents.extend(
            [
                resource("Role", RECONCILER, ns, rules=rules),
                binding(RECONCILER, ns, RECONCILER, RECONCILER, ops),
            ]
        )
    documents += policy(
        f"{target.consumer}-demo-service",
        "",
        ["services"],
        OPERATIONS,
        f"request.namespace in {json.dumps(list(spaces))}",
        [
            (
                "object.spec.type == 'ClusterIP' && !has(object.spec.externalIPs) && "
                "!has(object.spec.externalName) && object.spec.ports.all(p, !has(p.nodePort) || p.nodePort == 0)",
                "Demo desired state may expose only private ClusterIP Services",
            )
        ],
    )
    documents += policy(
        f"{target.consumer}-demo-secret",
        "",
        ["secrets"],
        OPERATIONS,
        f"request.namespace in {json.dumps(list(spaces))}",
        [
            (
                "object.type != 'kubernetes.io/service-account-token'",
                "Demo namespaces use no legacy ServiceAccount token Secrets",
            )
        ],
    )
    documents += policy(
        f"{target.consumer}-demo-volume",
        "",
        ["persistentvolumeclaims"],
        ("CREATE",),
        f"request.namespace in {json.dumps(list(spaces))}",
        [
            (
                "!has(object.spec.volumeName) && !has(object.spec.selector)"
                " && !has(object.spec.dataSource) && !has(object.spec.dataSourceRef)"
                " && (!has(object.metadata.annotations) || object.metadata.annotations.all(k, k != 'volumeType' && k != 'volume.kubernetes.io/selected-node'))"
                " && (!has(object.spec.volumeMode) || object.spec.volumeMode == 'Filesystem')",
                "Demo claims require fresh node-local filesystem provisioning",
            )
        ],
    )
    documents += policy(
        f"{target.consumer}-demo-placement",
        "",
        ["pods"],
        ("CREATE",),
        f"request.namespace in {json.dumps(list(spaces))}",
        [
            (
                "!has(object.spec.volumes) || !object.spec.volumes.exists(v, has(v.persistentVolumeClaim))"
                " || (!has(object.spec.nodeName) && has(object.spec.nodeSelector)"
                f" && '{LABEL}' in object.spec.nodeSelector && object.spec.nodeSelector['{LABEL}'] == '{target.consumer}'"
                " && 'k8s-infrastructure.io/role' in object.spec.nodeSelector"
                " && object.spec.nodeSelector['k8s-infrastructure.io/role'] == 'worker')",
                "Demo retained PVC consumers require the owned worker and scheduler binding",
            )
        ],
    )
    if operation:
        ns, actor = operation.namespace, operation.launcher
        documents.extend(
            [
                resource("ServiceAccount", actor, ops, automountServiceAccountToken=False),
                resource(
                    "ServiceAccount", operation.runtime, ns, automountServiceAccountToken=False
                ),
                resource("PodTemplate", actor, ns, template={"spec": deepcopy(operation.pod)}),
                *job_policy(target, operation),
                resource(
                    "Role",
                    actor,
                    ns,
                    rules=[
                        rule("batch", ["jobs"], ["create"]),
                        rule("batch", ["jobs"], ["get"], resourceNames=[operation.name]),
                        rule("", ["podtemplates"], ["get"], resourceNames=[actor]),
                    ],
                ),
                binding(actor, ns, actor, actor, ops),
            ]
        )
    for document in documents:
        document["metadata"].setdefault("labels", {}).update(
            {
                LABEL: target.consumer,
                OWNER_LABEL: operation.launcher
                if operation
                and document["metadata"]["name"]
                in {
                    operation.launcher,
                    operation.runtime,
                    f"{target.consumer}-{operation.launcher}-job",
                }
                else "base",
            }
        )
    return documents
