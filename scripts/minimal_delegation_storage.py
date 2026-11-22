"""Disposable local-retained binding and simulator filesystem prerequisites."""

from __future__ import annotations

import json
import secrets
from pathlib import Path, PurePosixPath

from . import bootstrap_failure as failure
from .consumer_profile import LABEL, MINIMAL_PROFILE, Target
from .database_assets import STORAGE_CLASS
from .database_qualification import pvc
from .kubernetes import contains, kube, reference
from .minimal_network import verify_pod_authority
from .network_resources import normalized, pod_bounds

PROBE_IMAGE = (
    "docker.io/library/python:3.12.16-slim-bookworm@"
    "sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4"
)


def validate_binding(claim: dict, volume: dict, pod: dict, claim_uid: str, worker: str) -> dict:
    metadata, spec = claim["metadata"], volume["spec"]
    source = PurePosixPath(spec.get("hostPath", {}).get("path", ""))
    root = PurePosixPath("/var/local-path-provisioner")
    claim_ref = spec.get("claimRef", {})
    if (
        metadata.get("uid") != claim_uid
        or metadata.get("deletionTimestamp")
        or volume["metadata"].get("deletionTimestamp")
        or not volume["metadata"].get("uid")
        or claim["status"].get("phase") != "Bound"
        or volume["status"].get("phase") != "Bound"
        or claim["spec"].get("volumeName") != volume["metadata"]["name"]
        or claim["spec"].get("storageClassName") != STORAGE_CLASS
        or spec.get("storageClassName") != STORAGE_CLASS
        or spec.get("persistentVolumeReclaimPolicy") != "Retain"
        or spec.get("accessModes") != ["ReadWriteOnce"]
        or claim["spec"].get("accessModes") != ["ReadWriteOnce"]
        or spec.get("volumeMode", "Filesystem") != "Filesystem"
        or spec.get("capacity", {}).get("storage") != "1Gi"
        or not source.is_relative_to(root)
        or source == root
        or ".." in source.parts
        or any(claim_ref.get(key) != metadata[key] for key in ("uid", "name", "namespace"))
        or spec.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms")
        != [
            {
                "matchExpressions": [
                    {"key": "kubernetes.io/hostname", "operator": "In", "values": [worker]}
                ]
            }
        ]
        or pod["spec"].get("nodeName") != worker
        or not pod["metadata"].get("uid")
    ):
        raise ValueError("Local-retained claim/PV Retain identity or worker placement differs")
    return {
        "namespace": metadata["namespace"],
        "claimName": metadata["name"],
        "claimUID": claim_uid,
        "volumeUID": volume["metadata"]["uid"],
        "storageClass": STORAGE_CLASS,
        "retention": "Retain",
        "node": worker,
    }


def _pod(target: Target, namespace: str, claim: str, action: str, source: str, marker: str) -> dict:
    spec: dict = {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "activeDeadlineSeconds": 300 if action == "writer" else 120,
        "terminationGracePeriodSeconds": 5,
        "nodeSelector": {LABEL: target.consumer, "k8s-infrastructure.io/role": "worker"},
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "filesystem",
                "image": PROBE_IMAGE,
                "imagePullPolicy": "Never",
                "command": ["python", "-I", "-B", "-c", source, action, marker],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "32Mi"},
                    "limits": {"cpu": "200m", "memory": "64Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [{"name": "state", "mountPath": "/state"}],
                **(
                    {
                        "readinessProbe": {
                            "exec": {
                                "command": ["/usr/bin/test", "-f", "/state/qualification/ready"]
                            },
                            "periodSeconds": 1,
                        }
                    }
                    if action == "writer"
                    else {}
                ),
            }
        ],
        "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": claim}}],
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"{claim}-{action}",
            "namespace": namespace,
            "labels": {LABEL: target.consumer},
        },
        "spec": spec,
    }


def _admitted(target: Target, document: dict, *, dry_run: bool) -> dict:
    args = ["create", *(["--dry-run=server"] if dry_run else []), "-f", "-", "-o", "json"]
    item = json.loads(kube(target, args, [document]))
    if item.get("kind") == "List" and len(item["items"]) == 1:
        item = item["items"][0]
    if reference(item) != reference(document) or (not dry_run and not item["metadata"].get("uid")):
        raise ValueError("Neutral storage resource creation identity differs")
    if document["kind"] == "Pod":
        wanted, actual = document["spec"], item["spec"]
        if (
            not contains(wanted, actual)
            or normalized(pod_bounds(wanted)) != normalized(pod_bounds(actual))
            or actual.get("nodeName")
            or actual.get("ephemeralContainers")
        ):
            raise ValueError("Unexpected neutral storage Pod admission")
        verify_pod_authority(wanted, actual)
    return item


def _create(target: Target, document: dict) -> str:
    if document["kind"] == "Pod":
        _admitted(target, document, dry_run=True)
    return _admitted(target, document, dry_run=False)["metadata"]["uid"]


def _get(target: Target, namespace: str, kind: str, name: str) -> dict:
    return json.loads(kube(target, ["-n", namespace, "get", kind, name, "-o", "json"]))


def _wait_pod(
    target: Target, namespace: str, name: str, uid: str, worker: str, condition: str
) -> dict:
    kube(
        target,
        ["-n", namespace, "wait", "--for=" + condition, f"pod/{name}", "--timeout=90s"],
        request_timeout=90,
    )
    pod = _get(target, namespace, "pod", name)
    status = pod["status"].get("containerStatuses", [])
    if (
        pod["metadata"].get("uid") != uid
        or pod["metadata"].get("deletionTimestamp")
        or pod["spec"].get("nodeName") != worker
        or pod["spec"]["containers"][0]["image"] != PROBE_IMAGE
        or len(status) != 1
        or status[0].get("restartCount") != 0
    ):
        raise ValueError("Neutral filesystem Pod identity or readiness differs")
    if condition != "condition=Ready" and (
        pod["status"].get("phase") != "Succeeded"
        or status[0].get("state", {}).get("terminated", {}).get("exitCode") != 0
    ):
        raise ValueError("Neutral filesystem Pod did not terminate successfully")
    return pod


def qualify_storage(target: Target, application_namespace: str) -> dict:
    """Verify one exact worker-local claim under the caller's authorized operation deadline."""
    if (
        target.profile != MINIMAL_PROFILE
        or not target.consumer.startswith("qual-")
        or application_namespace != target.consumer + "-apps"
    ):
        raise ValueError("Filesystem probe requires the disposable minimal-demo app namespace")
    namespace = application_namespace
    worker = target.name + "-worker"
    marker = secrets.token_hex(12)
    name = "filesystem-qualification-" + marker
    source = (
        Path(__file__).with_name("minimal_delegation_storage_worker.py").read_text(encoding="utf-8")
    )
    succeeded = "jsonpath={.status.phase}=Succeeded"

    with failure.phase("storage-local-retained-binding", "dependency-failure"):
        claim_uid = _create(target, pvc(namespace, name))
        writer = _pod(target, namespace, name, "writer", source, marker)
        writer_name = writer["metadata"]["name"]
        writer_uid = _create(target, writer)
        running = _wait_pod(target, namespace, writer_name, writer_uid, worker, "condition=Ready")
        claim = _get(target, namespace, "persistentvolumeclaim", name)
        bound_name = claim["spec"]["volumeName"]
        volume = _get(target, namespace, "persistentvolume", bound_name)
        binding = validate_binding(claim, volume, running, claim_uid, worker)

    with failure.phase("storage-single-writer", "dependency-failure"):
        contender = _pod(target, namespace, name, "contender", source, marker)
        contender_uid = _create(target, contender)
        _wait_pod(
            target, namespace, contender["metadata"]["name"], contender_uid, worker, succeeded
        )
        _wait_pod(target, namespace, writer_name, writer_uid, worker, "condition=Ready")
        kube(
            target,
            [
                "-n",
                namespace,
                "exec",
                writer_name,
                "-c",
                "filesystem",
                "--",
                "python",
                "-I",
                "-B",
                "-c",
                source,
                "release",
                marker,
            ],
        )
        _wait_pod(target, namespace, writer_name, writer_uid, worker, succeeded)

    with failure.phase("storage-replacement-durability", "dependency-failure"):
        replacement = _pod(target, namespace, name, "replacement", source, marker)
        replacement_uid = _create(target, replacement)
        completed = _wait_pod(
            target, namespace, replacement["metadata"]["name"], replacement_uid, worker, succeeded
        )
        final = validate_binding(
            _get(target, namespace, "persistentvolumeclaim", name),
            _get(target, namespace, "persistentvolume", bound_name),
            completed,
            claim_uid,
            worker,
        )
        if final != binding:
            raise ValueError("Neutral retained binding changed during Pod replacement")
        return {
            **binding,
            "filesystem": "ext4",
            "writerUID": writer_uid,
            "contenderUID": contender_uid,
            "replacementUID": replacement_uid,
            "singleWriter": "SQLite and POSIX locks rejected concurrent acquisition",
            "checkpoint": "replacement read state after file fsync, rename and directory fsync",
            "fixtureRetention": "PVC, PV and Pods retained for inspection",
        }
