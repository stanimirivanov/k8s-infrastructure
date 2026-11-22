"""Offline minimal-demo namespace authority and one reviewed Job attempt."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass

from . import database_policy, host_admission
from .consumer_profile import LABEL, MINIMAL_PROFILE, Target
from .delegated_argo_policy import binding, resource, rule
from .network_resources import EXTRA_MEMORY, pod_bounds

RECONCILER = "demo-reconciler"
ROUTINE_RUNTIME = "demo-routine-runtime"
ROUTINE_LABEL = "k8s-infrastructure.io/demo-routine"
ROUTINE_CLAIM_PREFIX = "demo-ordinary-"
NAMESPACE_LABEL = "kubernetes.io/metadata.name"
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
    bounds = pod_bounds(pod)
    memory_request, memory_limit = (
        (128, 256) if operation.namespace == namespaces(target)[0] else (192, 384)
    )
    mib = 1024**2
    if (
        bounds["effectiveRequests"]["cpu"] > 100
        or bounds["effectiveLimits"]["cpu"] > 1000
        or bounds["steadyRequests"]["memory"] > memory_request * mib
        or bounds["steadyLimits"]["memory"] > memory_limit * mib
        or bounds["effectiveLimits"]["memory"] > memory_limit * mib + EXTRA_MEMORY
    ):
        raise ValueError(
            "Reviewed Job exceeds its namespace allocation or initialization allowance"
        )
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
    namespace: str | None = None,
    object_labels: dict | None = None,
) -> list[dict]:
    match: dict = {}
    if namespace:
        match["namespaceSelector"] = {"matchLabels": {NAMESPACE_LABEL: namespace}}
    constraints = {
        **match,
        "resourceRules": [
            {
                "apiGroups": [group],
                "apiVersions": ["v1"],
                "resources": resources,
                "operations": list(verbs),
                "scope": "Namespaced",
            }
        ],
    }
    spec = {
        "failurePolicy": "Fail",
        "matchConstraints": constraints,
        "matchConditions": [{"name": "reviewed-boundary", "expression": condition}],
        "validations": [{"expression": expr, "message": message} for expr, message in validations],
    }
    binding_spec: dict = {"policyName": name, "validationActions": ["Deny", "Audit"]}
    if object_labels:
        match["objectSelector"] = {"matchLabels": object_labels}
    if match:
        binding_spec["matchResources"] = match
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
    # Kubernetes treats map-style lists as unordered for CEL object equality.
    # Preserve order separately where runtime semantics depend on it.
    podspec = "object.spec.template.spec"
    orders = [(podspec, "volumes", "name", operation.pod.get("volumes", []))]
    for group in ("containers", "initContainers"):
        for index, container in enumerate(operation.pod.get(group, [])):
            ref = f"{podspec}.{group}[{index}]"
            for field, key in (("env", "name"), ("volumeMounts", "mountPath")):
                orders.append((ref, field, key, container.get(field, [])))
    order = " && ".join(
        f"(has({ref}.{field}) ? {ref}.{field}.map(item, item.{key}) : []) == "
        + json.dumps([item[key] for item in values])
        for ref, field, key, values in orders
    )
    name = f"{target.consumer}-{operation.launcher}-job"
    launcher = f"request.userInfo.username == '{username(operations_namespace(target), operation.launcher)}'"
    marked = (
        f"has(object.metadata.labels) && '{ATTEMPT_LABEL}' in object.metadata.labels"
        f" && object.metadata.labels['{ATTEMPT_LABEL}'] == '{operation.launcher}'"
    )
    # The API server resolves params before matchConditions. Restrict actual
    # resource matching, and require the launcher to use the selected marker.
    return policy(
        name + "-marker",
        "batch",
        ["jobs"],
        ("CREATE",),
        launcher,
        [(marked, "Demo launcher must submit its reviewed attempt marker")],
        namespace=operation.namespace,
    ) + policy(
        name,
        "batch",
        ["jobs"],
        ("CREATE",),
        marked,
        [
            (
                f"request.namespace == '{operation.namespace}' && object.metadata.name == '{operation.name}'",
                "Demo launcher may create only its reviewed Job attempt",
            ),
            (envelope, "Demo Job must use the reviewed controller and bounded execution"),
            (metadata, "Demo Job metadata must contain only the reviewed attempt and system keys"),
            (
                "params != null && dyn(object.spec.template.spec) == dyn(params.template.spec)"
                f" && ({order})",
                "Demo Job PodSpec must exactly match the reviewed template",
            ),
        ],
        parameter={"name": operation.launcher, "namespace": operation.namespace},
        namespace=operation.namespace,
        object_labels={ATTEMPT_LABEL: operation.launcher},
    )


def routine_policy(target: Target) -> list[dict]:
    spaces = namespaces(target)
    actor = username(operations_namespace(target), RECONCILER)
    condition = (
        f"request.namespace in {json.dumps(list(spaces))} && request.userInfo.username == '{actor}'"
    )
    required_label = (
        f"'{ROUTINE_LABEL}' in {{ref}}.labels && {{ref}}.labels['{ROUTINE_LABEL}'] == 'true'"
    )

    def marked(ref: str) -> str:
        return f"(has({ref}.labels) && {required_label.format(ref=ref)})"

    own = (
        f"(request.operation == 'CREATE' || {marked('oldObject.metadata')})"
        f" && (request.operation == 'DELETE' || {marked('object.metadata')})"
    )
    documents = policy(
        f"{target.consumer}-demo-routine-config",
        "",
        ["configmaps", "services"],
        (*OPERATIONS, "DELETE"),
        condition,
        [(own, "Demo reconciler may change only routine-owned configuration and Services")],
    )
    documents += policy(
        f"{target.consumer}-demo-routine-claim",
        "",
        ["persistentvolumeclaims"],
        ("CREATE",),
        condition,
        [
            (
                f"object.metadata.name.startsWith('{ROUTINE_CLAIM_PREFIX}')",
                "Demo reconciler must reserve ordinary claim names",
            )
        ],
    )
    pod = "object.spec.template.spec"
    mount = (
        "has(v.emptyDir) || has(v.configMap) || has(v.downwardAPI)"
        " || (has(v.persistentVolumeClaim) && "
        f"v.persistentVolumeClaim.claimName.startsWith('{ROUTINE_CLAIM_PREFIX}'))"
    )
    refs = (
        "(!has(c.env) || c.env.all(e, !has(e.valueFrom) || !has(e.valueFrom.secretKeyRef)))"
        " && (!has(c.envFrom) || c.envFrom.all(e, !has(e.secretRef)))"
        " && (!has(c.ports) || c.ports.all(p, !has(p.hostPort) || p.hostPort == 0))"
    )
    worker = json.dumps({LABEL: target.consumer, "k8s-infrastructure.io/role": "worker"})
    scope = (
        f"has({pod}.serviceAccountName) && {pod}.serviceAccountName == '{ROUTINE_RUNTIME}'"
        f" && (!has({pod}.serviceAccount) || {pod}.serviceAccount == '{ROUTINE_RUNTIME}')"
        f" && has({pod}.automountServiceAccountToken) && {pod}.automountServiceAccountToken == false"
        f" && !has({pod}.imagePullSecrets) && !has({pod}.runtimeClassName) && !has({pod}.resourceClaims)"
        f" && (!has({pod}.volumes) || {pod}.volumes.all(v, {mount}))"
        f" && {pod}.containers.all(c, {refs})"
        f" && !has({pod}.nodeName) && has({pod}.nodeSelector) && {pod}.nodeSelector == {worker}"
        f" && {pod}.schedulerName == 'default-scheduler' && !has({pod}.priorityClassName) && !has({pod}.hostAliases)"
        f" && {marked('object.spec.template.metadata')}"
        " && has(object.spec.selector.matchLabels)"
        f" && '{ROUTINE_LABEL}' in object.spec.selector.matchLabels"
        f" && object.spec.selector.matchLabels['{ROUTINE_LABEL}'] == 'true'"
        " && !has(object.spec.template.metadata.annotations)"
        " && !has(object.metadata.ownerReferences) && !has(object.metadata.finalizers)"
        " && !has(object.spec.template.metadata.ownerReferences)"
        " && !has(object.spec.template.metadata.finalizers)"
    )
    # Bound the delegated shape to one container and one replica at steady state.
    # Credential-owning operators account for their separately reviewed init/transitions.
    compute = (
        "object.spec.replicas >= 0 && object.spec.replicas <= 1"
        f" && size({pod}.containers) == 1"
        f" && !has({pod}.initContainers) && !has({pod}.ephemeralContainers)"
        f" && !has(dyn({pod}).resources) && !has({pod}.overhead)"
    )
    for field, value, maximum in (
        ("requests", "cpu", "'100m'"),
        ("requests", "memory", f"request.namespace == '{spaces[0]}' ? '256Mi' : '512Mi'"),
        ("limits", "cpu", "'1'"),
        ("limits", "memory", f"request.namespace == '{spaces[0]}' ? '512Mi' : '1Gi'"),
    ):
        values = f"dyn({pod}.containers[0].resources).{field}"
        compute += (
            f" && has({values}) && size({values}) == 2 && '{value}' in {values}"
            f" && quantity({values}['{value}']).sign() > 0"
            f" && !quantity({values}['{value}']).isGreaterThan(quantity({maximum}))"
        )
    for kind, rollout in (
        ("deployments", "object.spec.strategy.type == 'Recreate'"),
        ("statefulsets", "!has(object.spec.volumeClaimTemplates)"),
    ):
        documents += policy(
            f"{target.consumer}-demo-routine-{kind}",
            "apps",
            [kind],
            (*OPERATIONS, "DELETE"),
            condition,
            [
                (own, "Demo reconciler may change only routine-owned controllers"),
                (
                    f"request.operation == 'DELETE' || ({scope})",
                    "Demo routine workload cannot access controller or Secret credentials",
                ),
                (
                    f"request.operation == 'DELETE' || ({compute} && {rollout})",
                    "Demo routine workload must fit its bounded single-replica allocation",
                ),
            ],
        )
    return documents


def routine_quota(application: bool) -> dict:
    # Local namespace ceilings include all Pods and the permitted transient
    # memory allowance. The consumer still serializes phases across namespaces.
    controllers, pods, cpu_request, cpu_limit, memory_request, memory_limit = (
        (10, 10, "650m", "7250m", "1792Mi", "3Gi")
        if application
        else (3, 3, "175m", "1500m", "1216Mi", "1920Mi")
    )
    return {
        "count/deployments.apps": str(controllers),
        "count/statefulsets.apps": str(controllers),
        "pods": str(pods),
        "requests.cpu": cpu_request,
        "limits.cpu": cpu_limit,
        "requests.memory": memory_request,
        "limits.memory": memory_limit,
    }


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
                ["configmaps", "services"],
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
                    ["get", "list", "watch"],
                )
            )
        else:
            documents.extend(
                [
                    resource(
                        "ServiceAccount", ROUTINE_RUNTIME, ns, automountServiceAccountToken=False
                    ),
                    resource(
                        "ResourceQuota",
                        "demo-routine-controllers",
                        ns,
                        spec={"hard": routine_quota(ns == spaces[0])},
                    ),
                ]
            )
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
    documents += routine_policy(target)
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
                    f"{target.consumer}-{operation.launcher}-job-marker",
                }
                else "base",
            }
        )
    return documents
