"""Fence new Workflow submissions in the exact minimal-demo release namespace."""

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


def documents_for(delegation: Delegation) -> list[dict]:
    name = policy_name(delegation)
    return stamp(
        [
            resource("ConfigMap", FENCE_CONFIG, delegation.controllers, data={"mode": "closed"}),
            resource(
                "ValidatingAdmissionPolicy",
                name,
                None,
                spec={
                    "failurePolicy": "Fail",
                    "paramKind": {"apiVersion": "v1", "kind": "ConfigMap"},
                    "matchConstraints": {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": delegation.managed}
                        },
                        "resourceRules": [
                            {
                                "apiGroups": ["argoproj.io"],
                                "apiVersions": ["v1alpha1"],
                                "operations": ["CREATE"],
                                "resources": ["workflows"],
                            }
                        ],
                    },
                    "validations": [
                        {
                            "expression": "params != null && has(params.data) && 'mode' in params.data && params.data['mode'] == 'open'",
                            "message": "Minimal-demo Argo lifecycle has closed Workflow submission.",
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
        ],
        delegation,
    )


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
                or data["mode"] not in {"open", "closed"}
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
    return read_fence(target, delegation)["data"]["mode"] == "closed"


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
    deadline = time.monotonic() + ACTIVATION_SECONDS
    while True:
        try:
            kube(target, command, [workflow], request_timeout=3)
        except CommandFailure as error:
            if error.denial != expected:
                raise
            if closed:
                return
        else:
            if not closed:
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("Argo Workflow fence admission did not reach requested mode")
        time.sleep(0.25)


def set_closed(
    target: Target, delegation: Delegation, closed: bool, *, expected_uid: str | None = None
) -> None:
    before = read_fence(target, delegation)
    if expected_uid is not None and before["metadata"]["uid"] != expected_uid:
        raise ValueError("Argo lifecycle Workflow fence UID differs from receipt")
    mode = "closed" if closed else "open"
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
    after = read_fence(target, delegation)
    if after["metadata"]["uid"] != before["metadata"]["uid"] or after["data"]["mode"] != mode:
        raise ValueError("Argo lifecycle Workflow fence identity or mode changed")
