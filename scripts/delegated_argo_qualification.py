"""Explicit neutral live qualification: real API authorization, admission and execution."""

from __future__ import annotations

import copy
import json
import time

from .consumer_profile import Target
from .delegated_argo_config import Delegation, selection
from .delegated_argo_policy import resource
from .delegated_argo_runtime import health, kube

FIXTURE = "provider-qualification-v1"
WORKFLOW = "provider-i02-qualification"


def wait_workflow(target: Target, delegation: Delegation) -> None:
    """Stop on a terminal failure instead of spending the whole success deadline."""
    deadline = time.monotonic() + 300
    while True:
        phase = kube(
            target,
            [
                "-n",
                delegation.managed,
                "get",
                "workflow",
                WORKFLOW,
                "-o",
                "jsonpath={.status.phase}",
            ],
        ).strip()
        if phase == "Succeeded":
            return
        if phase in {"Failed", "Error"}:
            raise ValueError(f"Neutral workflow ended {phase}; inspect retained fixture")
        if time.monotonic() >= deadline:
            raise ValueError("Neutral workflow did not succeed within 300s; fixture retained")
        time.sleep(2)


def access(target: Target, user: str, namespace: str, group: str, name: str, verb: str) -> bool:
    review = resource(
        "SubjectAccessReview",
        "",
        None,
        spec={
            "user": user,
            "resourceAttributes": {
                "namespace": namespace,
                "group": group,
                "resource": name,
                "verb": verb,
            },
        },
    )
    review["apiVersion"] = "authorization.k8s.io/v1"
    review.pop("metadata")
    result = json.loads(kube(target, ["create", "-f", "-", "-o", "json"], [review]))
    status = result.get("status", result.get("items", [{}])[0].get("status", {}))
    if status.get("evaluationError") or not isinstance(status.get("allowed"), bool):
        raise ValueError("Authorization evaluation incomplete; no qualification pass")
    return status["allowed"]


def permission_checks(target: Target, delegation: Delegation) -> int:
    ns = delegation.managed
    cases = [
        (ns, "release-releaser", ns, "argoproj.io", "workflows", "create", True),
        (ns, "release-releaser", ns, "argoproj.io", "workflowtemplates", "create", False),
        (ns, "release-releaser", ns, "", "pods", "create", False),
        (ns, "release-releaser", ns, "", "secrets", "get", False),
        (ns, "release-releaser", "kube-system", "argoproj.io", "workflows", "create", False),
        (ns, "release-approver", ns, "argoproj.io", "workflows", "patch", True),
        (ns, "release-approver", ns, "argoproj.io", "workflows", "create", False),
        (ns, "release-viewer", ns, "argoproj.io", "workflows", "get", True),
        (ns, "release-viewer", ns, "argoproj.io", "workflows", "create", False),
        (ns, "release-executor", ns, "argoproj.io", "workflowtaskresults", "create", True),
        (ns, "release-executor", ns, "", "secrets", "get", False),
        (ns, "release-executor", ns, "", "pods", "create", False),
        ("kube-system", "generic-garbage-collector", ns, "argoproj.io", "workflows", "patch", True),
        (
            delegation.controllers,
            "argo",
            delegation.workload_namespaces[0],
            "",
            "secrets",
            "get",
            False,
        ),
        (delegation.controllers, "argo", "kube-system", "", "secrets", "get", False),
        (
            delegation.gitops,
            "argocd-application-controller",
            delegation.workload_namespaces[0],
            "apps",
            "deployments",
            "create",
            True,
        ),
        (
            delegation.gitops,
            "argocd-application-controller",
            delegation.workload_namespaces[0],
            "rbac.authorization.k8s.io",
            "rolebindings",
            "create",
            False,
        ),
        (
            delegation.gitops,
            "argocd-application-controller",
            "perfeng-system",
            "",
            "secrets",
            "get",
            False,
        ),
    ]
    for account_ns, account, namespace, group, name, verb, expected in cases:
        if (
            access(
                target,
                f"system:serviceaccount:{account_ns}:{account}",
                namespace,
                group,
                name,
                verb,
            )
            != expected
        ):
            raise ValueError("Delegated authorization positive/negative qualification failed")
    return len(cases)


def submission(delegation: Delegation) -> dict:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "name": WORKFLOW,
            "namespace": delegation.managed,
            "labels": {"workflows.argoproj.io/controller-instanceid": delegation.consumer},
            "annotations": {
                "k8s-infrastructure.io/submitted-by": f"system:serviceaccount:{delegation.managed}:release-releaser"
            },
        },
        "spec": {
            "workflowTemplateRef": {"name": FIXTURE},
            "arguments": {
                "parameters": [{"name": "release-digest", "value": "sha256:" + "1" * 64}]
            },
            "suspend": True,
        },
    }


def admission_checks(target: Target, delegation: Delegation) -> int:
    user = f"system:serviceaccount:{delegation.managed}:release-releaser"
    args = [f"--as={user}", "create", "--dry-run=server", "-f", "-", "-o", "name"]
    valid = submission(delegation)
    mutations = [
        {"entrypoint": "unapproved"},
        {"serviceAccountName": "argo"},
        {"podSpecPatch": '{"hostNetwork":true}'},
        {"volumes": [{"name": "escape", "hostPath": {"path": "/"}}]},
        {
            "templates": [
                {"name": "escape", "container": {"image": "unapproved", "command": ["sh"]}}
            ]
        },
        {"workflowTemplateRef": {"name": "unapproved"}},
        {"workflowTemplateRef": {"name": FIXTURE, "clusterScope": True}},
        {"suspend": False},
        {"arguments": {"parameters": [{"name": "release-digest", "value": "unreviewed"}]}},
        {
            "arguments": {
                "parameters": [
                    {
                        "name": "release-digest",
                        "valueFrom": {"configMapKeyRef": {"name": "escape", "key": "value"}},
                    }
                ]
            }
        },
        {"archiveLogs": False},
        {
            "artifactGC": {
                "strategy": "OnWorkflowCompletion",
                "podSpecPatch": '{"hostNetwork":true}',
            }
        },
    ]
    for change in mutations:
        candidate = copy.deepcopy(valid)
        candidate["spec"].update(change)
        try:
            kube(target, args, [candidate])
        except ValueError:
            # A working positive control after each rejection prevents a broken
            # connection, missing API or unavailable cluster from counting as denial.
            kube(target, args, [valid])
        else:
            raise ValueError("Submission override was accepted; no qualification pass")
    return len(mutations)


def qualify(target: Target, delegation: Delegation) -> dict:
    """Creates only the named neutral fixture; failures retain it for inspection."""
    if FIXTURE not in delegation.approved_templates:
        raise ValueError(
            "Qualify requires explicit provider-qualification-v1 in the reviewed template list"
        )
    result = health(target, delegation)
    permissions = permission_checks(target, delegation)
    denials = admission_checks(target, delegation)
    template = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "WorkflowTemplate",
        "metadata": {
            "name": FIXTURE,
            "namespace": delegation.managed,
            "labels": {"workflows.argoproj.io/controller-instanceid": delegation.consumer},
        },
        "spec": {
            "entrypoint": "probe",
            "serviceAccountName": "release-executor",
            "activeDeadlineSeconds": 300,
            "arguments": {"parameters": [{"name": "release-digest"}]},
            "templates": [
                {
                    "name": "probe",
                    "container": {
                        "image": f"quay.io/argoproj/argocli:{selection()['workflows']['version']}",
                        "command": ["argo", "version"],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "250m", "memory": "256Mi"},
                        },
                    },
                }
            ],
        },
    }
    # create, never apply/adopt/overwrite a consumer's template or workflow.
    kube(target, ["create", "-f", "-"], [template])
    user = f"system:serviceaccount:{delegation.managed}:release-releaser"
    kube(target, [f"--as={user}", "create", "-f", "-"], [submission(delegation)])
    kube(
        target,
        ["-n", delegation.controllers, "rollout", "restart", "deployment/workflow-controller"],
    )
    kube(
        target,
        [
            "-n",
            delegation.controllers,
            "rollout",
            "status",
            "deployment/workflow-controller",
            "--timeout=300s",
        ],
    )
    approver = f"system:serviceaccount:{delegation.managed}:release-approver"
    # Reject an approval that attempts to replace the reviewed release request.
    try:
        kube(
            target,
            [
                f"--as={approver}",
                "-n",
                delegation.managed,
                "patch",
                "workflow",
                WORKFLOW,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"suspend": False, "entrypoint": "escape"}}),
            ],
        )
    except ValueError:
        kube(
            target,
            [
                f"--as={approver}",
                "-n",
                delegation.managed,
                "patch",
                "workflow",
                WORKFLOW,
                "--type=merge",
                "-p",
                json.dumps(
                    {
                        "spec": {"suspend": False},
                        "metadata": {
                            "annotations": {"k8s-infrastructure.io/approved-by": approver}
                        },
                    }
                ),
            ],
        )
    else:
        raise ValueError("Approval changed the request; no qualification pass")
    wait_workflow(target, delegation)
    accounts = kube(
        target,
        [
            "-n",
            delegation.managed,
            "get",
            "pods",
            "-l",
            f"workflows.argoproj.io/workflow={WORKFLOW}",
            "-o",
            "jsonpath={.items[*].spec.serviceAccountName}",
        ],
    )
    if set(accounts.split()) != {"release-executor"}:
        raise ValueError("Neutral workflow did not execute under the minimal identity")
    completed = json.loads(
        kube(target, ["-n", delegation.managed, "get", "workflow", WORKFLOW, "-o", "json"])
    )
    annotations = completed["metadata"].get("annotations", {})
    if (
        annotations.get("k8s-infrastructure.io/submitted-by") != user
        or annotations.get("k8s-infrastructure.io/approved-by") != approver
    ):
        raise ValueError("Authenticated submission/approval identity evidence is incomplete")
    # Recovery authority must not become permission to edit release requests.
    collector = "system:serviceaccount:kube-system:generic-garbage-collector"
    try:
        kube(
            target,
            [
                f"--as={collector}",
                "-n",
                delegation.managed,
                "patch",
                "workflow",
                WORKFLOW,
                "--dry-run=server",
                "--type=merge",
                "-p",
                json.dumps({"spec": {"entrypoint": "escape"}}),
            ],
        )
    except ValueError:
        kube(target, ["-n", delegation.managed, "get", "workflow", WORKFLOW, "-o", "name"])
    else:
        raise ValueError("Garbage collector changed a release request; no qualification pass")
    return {
        **result,
        "liveAuthorizationAndExecution": "observed",
        "authorizationCases": permissions,
        "admissionOverrideDenials": denials + 2,
        "controllerRestart": "observed",
        "fixtureWorkflow": WORKFLOW,
        "evidenceRetention": "cluster-local only; consumer durable evidence remains required",
    }
