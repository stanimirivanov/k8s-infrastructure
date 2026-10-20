"""Native neutral persistence/retention proof; never a consumer backup/restore drill."""

from __future__ import annotations

import copy
import json
import time
import uuid

from .consumer_profile import LABEL, Target
from .consumer_runtime import command
from .database_assets import STORAGE_CLASS, download, render, selection
from .database_policy import configure
from .database_runtime import apply_objects, health, inventory
from .kubernetes import kube, reference
from .network_assets import selection as network_selection


def pvc(namespace: str, name: str, **spec) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "storageClassName": STORAGE_CLASS,
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
            **spec,
        },
    }


def probe_pod(target: Target, namespace: str, claim: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "retention-probe", "namespace": namespace},
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "nodeSelector": {LABEL: target.consumer, "k8s-infrastructure.io/role": "worker"},
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": network_selection()["probes"]["client"],
                    "command": ["sh", "-c", "sleep 3600"],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "200m", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                }
            ],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }


def cluster(target: Target, namespace: str) -> dict:
    return {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": {
            "name": "neutral-postgres",
            "namespace": namespace,
            "annotations": {"argocd.argoproj.io/sync-options": "Prune=false,Delete=false"},
        },
        "spec": {
            "instances": 1,
            "imageName": selection()["images"]["postgres"],
            "storage": {"size": "1Gi", "storageClass": STORAGE_CLASS},
            "affinity": {
                "nodeSelector": {LABEL: target.consumer, "k8s-infrastructure.io/role": "worker"}
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "768Mi"},
            },
        },
    }


def uid_delete(target: Target, item: dict) -> None:
    """UID preconditions prevent deleting a replacement between inspection and write."""
    plurals = {"Pod": "pods", "Deployment": "deployments", "ConfigMap": "configmaps"}
    metadata = item["metadata"]
    prefix = "/api/v1" if item["apiVersion"] == "v1" else "/apis/" + item["apiVersion"]
    path = (
        prefix
        + "/namespaces/"
        + metadata["namespace"]
        + "/"
        + plurals[item["kind"]]
        + "/"
        + metadata["name"]
    )
    command(
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
                "preconditions": {"uid": metadata["uid"]},
                "propagationPolicy": "Foreground",
            }
        ).encode(),
    )


def get(target: Target, namespace: str, kind: str, name: str) -> dict:
    raw = kube(target, ["-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json"])
    return json.loads(raw) if raw.strip() else {}


def primary(target: Target, namespace: str, *, different_uid: str | None = None) -> str:
    deadline = time.monotonic() + 300
    while True:
        status = get(target, namespace, "clusters.postgresql.cnpg.io", "neutral-postgres").get(
            "status", {}
        )
        name = status.get("currentPrimary")
        if name and status.get("readyInstances") == 1:
            pod = get(target, namespace, "pod", name)
            if (
                pod
                and pod["metadata"]["uid"] != different_uid
                and any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                )
            ):
                return name
        if time.monotonic() >= deadline:
            raise ValueError("Neutral PostgreSQL did not reach native readiness")
        time.sleep(2)


def sql(target: Target, namespace: str, pod: str, statement: str) -> str:
    return kube(
        target,
        [
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            "postgres",
            "--",
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            statement,
        ],
    ).strip()


def admission(target: Target, namespace: str) -> None:
    valid = pvc(namespace, "admission-probe")
    args = ["create", "--dry-run=server", "-f", "-", "-o", "name"]
    for size in ("1Gi", "2Gi", "3Gi", "4Gi"):
        accepted = copy.deepcopy(valid)
        accepted["spec"]["resources"]["requests"]["storage"] = size
        kube(target, args, [accepted])
    for spec in (
        {"storageClassName": "standard"},
        {"accessModes": ["ReadWriteMany"]},
        {"resources": {"requests": {"storage": "5Gi"}}},
        {"resources": {"requests": {"storage": "512Mi"}}},
        {"resources": {"requests": {"storage": "1536Mi"}}},
    ):
        invalid = copy.deepcopy(valid)
        invalid["spec"].update(spec)
        try:
            kube(target, args, [invalid])
        except ValueError:
            kube(target, args, [valid])  # A broken API cannot count as rejection.
        else:
            raise ValueError("Unsafe storage request was admitted")
    store = {
        "apiVersion": "barmancloud.cnpg.io/v1",
        "kind": "ObjectStore",
        "metadata": {"name": "neutral-api-probe", "namespace": namespace},
        "spec": {"configuration": {"destinationPath": "s3://neutral-not-created/"}},
    }
    kube(target, args, [store])  # API-only: never create a bucket or test credentials implicitly.


def retention(target: Target, namespace: str, marker: str) -> dict:
    owner = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "neutral-consumer-instance", "namespace": namespace},
        "data": {"fixture": "retention-only"},
    }
    kube(target, ["create", "-f", "-"], [owner])
    actual_owner = get(target, namespace, "configmap", owner["metadata"]["name"])
    claim = pvc(namespace, "retention-data")
    claim["metadata"]["ownerReferences"] = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "name": owner["metadata"]["name"],
            "uid": actual_owner["metadata"]["uid"],
        }
    ]
    kube(target, ["create", "-f", "-"], [claim, probe_pod(target, namespace, "retention-data")])
    kube(
        target,
        ["-n", namespace, "wait", "--for=condition=Ready", "pod/retention-probe", "--timeout=180s"],
    )
    kube(
        target,
        [
            "-n",
            namespace,
            "exec",
            "retention-probe",
            "--",
            "sh",
            "-c",
            'printf "%s" "$1" > /data/marker; sync',
            "--",
            marker,
        ],
    )
    old = get(target, namespace, "pvc", "retention-data")
    volume = json.loads(kube(target, ["get", "pv", old["spec"]["volumeName"], "-o", "json"]))
    if volume["spec"]["persistentVolumeReclaimPolicy"] != "Retain":
        raise ValueError("Native PV is not retained")
    uid_delete(target, get(target, namespace, "pod", "retention-probe"))
    kube(target, ["-n", namespace, "wait", "--for=delete", "pod/retention-probe", "--timeout=120s"])
    uid_delete(target, actual_owner)
    kube(target, ["-n", namespace, "wait", "--for=delete", "pvc/retention-data", "--timeout=120s"])
    kube(
        target,
        [
            "wait",
            "--for=jsonpath={.status.phase}=Released",
            "pv/" + volume["metadata"]["name"],
            "--timeout=90s",
        ],
    )
    # Explicit disposable-fixture recovery, not automatic production adoption.
    # Test immutable PV identity and the exact old claim UID at the API server.
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": volume["metadata"]["uid"]},
        {"op": "test", "path": "/spec/claimRef/uid", "value": old["metadata"]["uid"]},
        {"op": "remove", "path": "/spec/claimRef"},
    ]
    kube(
        target, ["patch", "pv", volume["metadata"]["name"], "--type=json", "-p", json.dumps(patch)]
    )
    kube(
        target,
        ["create", "-f", "-"],
        [
            pvc(namespace, "retained-read", volumeName=volume["metadata"]["name"]),
            probe_pod(target, namespace, "retained-read"),
        ],
    )
    kube(
        target,
        ["-n", namespace, "wait", "--for=condition=Ready", "pod/retention-probe", "--timeout=180s"],
    )
    if (
        kube(
            target, ["-n", namespace, "exec", "retention-probe", "--", "cat", "/data/marker"]
        ).strip()
        != marker
    ):
        raise ValueError("Retained PV lost its neutral marker")
    return {
        "ownerAndPVCDeletion": "retained data read after explicit UID-checked rebind",
        "volumeUID": volume["metadata"]["uid"],
    }


def qualify(target: Target) -> dict:
    result = health(target)
    namespace = target.consumer + "-database-qual"
    if get(target, namespace, "namespace", namespace):
        raise ValueError("Neutral database namespace exists; inspect before retry")
    kube(
        target,
        ["create", "-f", "-"],
        [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "labels": {
                        LABEL: target.consumer,
                        "app.kubernetes.io/managed-by": target.consumer,
                    },
                },
            }
        ],
    )
    configure(target, (namespace,))
    admission(target, namespace)
    print(
        "Storage admission and ObjectStore API checks passed; provisioning neutral PostgreSQL...",
        flush=True,
    )
    kube(target, ["create", "-f", "-"], [cluster(target, namespace)])
    name = primary(target, namespace)
    marker = str(uuid.uuid4())
    sql(
        target,
        namespace,
        name,
        "CREATE TABLE neutral_persistence(marker text PRIMARY KEY); INSERT INTO neutral_persistence VALUES ('"
        + marker
        + "');",
    )
    old_pod = get(target, namespace, "pod", name)
    uid_delete(target, old_pod)
    name = primary(target, namespace, different_uid=old_pod["metadata"]["uid"])
    if sql(target, namespace, name, "SELECT marker FROM neutral_persistence;") != marker:
        raise ValueError("PostgreSQL restart lost neutral data")
    print(
        "PostgreSQL restart persistence passed; checking controller-independent data...", flush=True
    )
    groups = render(target, download())
    operator = next(d for d in groups["cnpg"] if d["kind"] == "Deployment")
    actual = inventory(target, [operator])[reference(operator)]
    uid_delete(target, actual)
    kube(
        target,
        [
            "-n",
            "cnpg-system",
            "wait",
            "--for=delete",
            "deployment/" + operator["metadata"]["name"],
            "--timeout=180s",
        ],
    )
    if sql(target, namespace, name, "SELECT marker FROM neutral_persistence;") != marker:
        raise ValueError("Controller removal lost neutral data")
    apply_objects(target, [operator])
    kube(
        target,
        [
            "-n",
            "cnpg-system",
            "rollout",
            "status",
            "deployment/" + operator["metadata"]["name"],
            "--timeout=300s",
        ],
    )
    retained = retention(target, namespace, marker)
    final = health(target)
    return {
        **result,
        "controllerReadiness": final["controllerReadiness"],
        "dataRetention": "observed",
        "postgresRestartPersistence": "observed",
        "operatorRemovalPersistence": "observed",
        "retentionEvidence": retained,
        "pluginAPI": "native ObjectStore server dry-run and plugin certificate/controller readiness; backup execution unqualified",
        "fixtureRetention": "retained for inspection; only explicitly confirmed whole-target disposal destroys data",
    }
