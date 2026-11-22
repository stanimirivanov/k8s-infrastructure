"""Fence Workflow submissions and GitOps desired-state writes during restoration."""

from __future__ import annotations

import json
import secrets
import time

from .consumer_profile import Target
from .delegated_argo_config import Delegation
from .delegated_argo_policy import resource, stamp
from .kubernetes import contains, kube, reference
from .local_kind import CommandFailure

FENCE_CONFIG = "argo-lifecycle-fence"
ACTIVATION_SECONDS = 30


def policy_name(delegation: Delegation) -> str:
    return f"{delegation.consumer}-argo-lifecycle-fence"


def gitops_policy_name(delegation: Delegation) -> str:
    return f"{delegation.consumer}-argo-restore-gitops"


def documents_for(delegation: Delegation) -> list[dict]:
    documents = [
        resource("ConfigMap", FENCE_CONFIG, delegation.controllers, data={"mode": "closed"})
    ]
    for name, namespace, operations, resources, allowed in (
        (policy_name(delegation), delegation.managed, ["CREATE"], ["workflows"], ["open"]),
        (
            gitops_policy_name(delegation),
            delegation.gitops,
            ["CREATE", "UPDATE", "DELETE"],
            ["applications", "applicationsets"],
            ["closed", "open"],
        ),
    ):
        documents.extend(
            [
                resource(
                    "ValidatingAdmissionPolicy",
                    name,
                    None,
                    spec={
                        "failurePolicy": "Fail",
                        "paramKind": {"apiVersion": "v1", "kind": "ConfigMap"},
                        "matchConstraints": {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": namespace}
                            },
                            "resourceRules": [
                                {
                                    "apiGroups": ["argoproj.io"],
                                    "apiVersions": ["v1alpha1"],
                                    "operations": operations,
                                    "resources": resources,
                                }
                            ],
                        },
                        "validations": [
                            {
                                "expression": "params != null && has(params.data) && 'mode' in params.data && params.data['mode'] in "
                                + json.dumps(allowed),
                                "message": "Minimal-demo Argo lifecycle has fenced this operation.",
                            }
                        ],
                    },
                ),
                resource(
                    "ValidatingAdmissionPolicyBinding",
                    name,
                    None,
                    spec={
                        "policyName": name,
                        "validationActions": ["Deny", "Audit"],
                        "paramRef": {
                            "name": FENCE_CONFIG,
                            "namespace": delegation.controllers,
                            "parameterNotFoundAction": "Deny",
                        },
                    },
                ),
            ]
        )
    return stamp(documents, delegation)


def read_fence(target: Target, delegation: Delegation) -> dict:
    from .delegated_argo_runtime import read_resources, verify_ownership, verify_security

    expected = documents_for(delegation)
    found = read_resources(target, expected)
    for desired in expected:
        actual = found.get(reference(desired))
        if actual is None:
            raise ValueError("Argo lifecycle Workflow fence is missing")
        verify_ownership(desired, actual)
        if desired["kind"] == "ConfigMap":
            data = actual.get("data", {})
            if (
                set(data) != {"mode"}
                or data["mode"] not in {"open", "closed", "restoring"}
                or actual.get("immutable")
                or actual.get("binaryData")
            ):
                raise ValueError("Argo lifecycle Workflow fence mode differs")
        else:
            verify_security(desired, actual)
            if not contains(desired["spec"], actual["spec"]):
                raise ValueError("Argo lifecycle Workflow fence policy drifted")
            if desired["kind"] == "ValidatingAdmissionPolicy":
                match = actual["spec"]["matchConstraints"]
                if match.get("excludeResourceRules") or len(match["resourceRules"]) != 1:
                    raise ValueError("Argo lifecycle Workflow fence has unreviewed exclusions")
    return found[reference(expected[0])]


def is_closed(target: Target, delegation: Delegation) -> bool:
    return read_fence(target, delegation)["data"]["mode"] != "open"


def confirm_admission(target: Target, delegation: Delegation, closed: bool) -> None:
    """Exercise the real release identity without creating a Workload or Workflow."""
    ns = delegation.managed
    actor = f"system:serviceaccount:{ns}:release-releaser"
    workflow = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "name": f"argo-fence-probe-{secrets.token_hex(8)}",
            "namespace": ns,
            "labels": {"workflows.argoproj.io/controller-instanceid": delegation.consumer},
            "annotations": {"k8s-infrastructure.io/submitted-by": actor},
        },
        "spec": {
            "workflowTemplateRef": {"name": delegation.approved_templates[0]},
            "arguments": {
                "parameters": [{"name": "release-digest", "value": "sha256:" + "0" * 64}]
            },
            "suspend": True,
        },
    }
    command = [
        f"--as={actor}",
        "--as-group=system:authenticated",
        "--as-group=system:serviceaccounts",
        f"--as-group=system:serviceaccounts:{ns}",
        "create",
        "--dry-run=server",
        "-f",
        "-",
    ]
    expected = ("admission", policy_name(delegation))
    await_admission(target, command, workflow, expected, closed)


def confirm_gitops_frozen(target: Target, delegation: Delegation) -> None:
    application_spec = {
        "project": "default",
        "source": {"repoURL": "https://example.invalid/fence-probe", "path": "."},
        "destination": {"server": "https://kubernetes.default.svc"},
    }
    for kind, spec in (
        ("Application", application_spec),
        (
            "ApplicationSet",
            {
                "generators": [{"list": {"elements": [{"name": "probe"}]}}],
                "template": {"metadata": {"name": "probe"}, "spec": application_spec},
            },
        ),
    ):
        probe = resource(
            kind,
            f"argo-fence-probe-{secrets.token_hex(8)}",
            delegation.gitops,
            apiVersion="argoproj.io/v1alpha1",
            spec=spec,
        )
        await_admission(
            target,
            ["create", "--dry-run=server", "-f", "-"],
            probe,
            ("admission", gitops_policy_name(delegation)),
            True,
        )


def await_admission(
    target: Target, command: list[str], document: dict, expected: tuple[str, str], denied: bool
) -> None:
    deadline = time.monotonic() + ACTIVATION_SECONDS
    while True:
        try:
            kube(target, command, [document], request_timeout=3)
        except CommandFailure as error:
            if error.denial != expected:
                raise
            if denied:
                return
        else:
            if not denied:
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("Argo lifecycle fence admission did not reach requested mode")
        time.sleep(0.25)


def set_closed(
    target: Target,
    delegation: Delegation,
    closed: bool,
    *,
    expected_uid: str | None = None,
    freeze_gitops: bool = False,
) -> None:
    before = read_fence(target, delegation)
    if expected_uid is not None and before["metadata"]["uid"] != expected_uid:
        raise ValueError("Argo lifecycle Workflow fence UID differs from receipt")
    if freeze_gitops and not closed:
        raise ValueError("Workflow submission must stay closed while restoring GitOps")
    mode = "restoring" if freeze_gitops else "closed" if closed else "open"
    prior = before["data"]["mode"]
    if prior != mode:
        metadata = before["metadata"]
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": metadata["resourceVersion"],
            },
            {"op": "test", "path": "/data/mode", "value": prior},
            {"op": "replace", "path": "/data/mode", "value": mode},
        ]
        kube(
            target,
            [
                "-n",
                delegation.controllers,
                "patch",
                "configmap",
                FENCE_CONFIG,
                "--type=json",
                "-p",
                json.dumps(patch),
            ],
        )
    confirm_admission(target, delegation, closed)
    if freeze_gitops:
        confirm_gitops_frozen(target, delegation)
    after = read_fence(target, delegation)
    if after["metadata"]["uid"] != before["metadata"]["uid"] or after["data"]["mode"] != mode:
        raise ValueError("Argo lifecycle Workflow fence identity or mode changed")
