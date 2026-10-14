"""Render and validate the project-neutral resource metrics capability offline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/resource-metrics"
IMAGE = "registry.k8s.io/metrics-server/metrics-server:v0.9.0@sha256:d9862115e7c7881280d3d75ca26bda8ffc0fc213315979575bf23ce9826205c0"


def helm(arguments: list[str]) -> str:
    """Invoke bounded offline Helm; never install or contact a cluster."""
    try:
        result = subprocess.run(
            ["helm", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Helm v4.1.4 is required for resource-metrics rendering") from error
    if result.returncode:
        raise ValueError("Offline resource-metrics Helm validation failed: " + result.stderr[:600])
    return result.stdout


def validate_rendered(objects: list[dict[str, Any]]) -> None:
    """Reject extra authority, TLS bypass, secret generation and unsafe workload shape."""
    expected = {
        ("ServiceAccount", "metrics-server"),
        ("Deployment", "metrics-server"),
        ("Service", "metrics-server"),
        ("APIService", "v1beta1.metrics.k8s.io"),
        ("ClusterRole", "substrate-metrics-server"),
        ("ClusterRoleBinding", "substrate-metrics-server"),
        ("ClusterRoleBinding", "substrate-metrics-server-auth-delegator"),
        ("RoleBinding", "substrate-metrics-server-auth-reader"),
    }
    by_key = {(o["kind"], o["metadata"]["name"]): o for o in objects}
    if set(by_key) != expected or len(objects) != len(expected):
        raise ValueError("Unexpected resource-metrics objects, duplication or generated secrets")
    deployment = by_key["Deployment", "metrics-server"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if (
        len(pod["containers"]) != 1
        or pod.get("initContainers")
        or pod.get("hostNetwork")
        or pod.get("hostPID")
        or pod.get("hostIPC")
        or pod.get("ephemeralContainers")
        or container.get("env")
        or container.get("envFrom")
        or container["image"] != IMAGE
        or container.get("command")
        or container["securityContext"]
        != {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        or pod["serviceAccountName"] != "metrics-server"
        or pod["securityContext"]
        != {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        or container["args"]
        != [
            "--secure-port=10250",
            "--tls-cert-file=/serving/tls.crt",
            "--tls-private-key-file=/serving/tls.key",
            "--cert-dir=/tmp",
            "--kubelet-certificate-authority=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname",
            "--kubelet-use-node-status-port",
            "--metric-resolution=15s",
        ]
    ):
        raise ValueError("Resource-metrics workload or verified TLS policy differs")
    if (
        container["resources"]
        != {
            "requests": {"cpu": "100m", "memory": "200Mi", "ephemeral-storage": "16Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "64Mi"},
        }
        or pod["volumes"]
        != [
            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
            {
                "name": "serving",
                "secret": {"secretName": "fixture-not-for-deployment", "defaultMode": 288},
            },
        ]
        or container["volumeMounts"]
        != [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "serving", "mountPath": "/serving", "readOnly": True},
        ]
    ):
        raise ValueError("Resource-metrics budgets or private certificate mounts differ")
    api = by_key["APIService", "v1beta1.metrics.k8s.io"]["spec"]
    if (
        api["insecureSkipTLSVerify"] is not False
        or not api.get("caBundle")
        or api["service"] != {"name": "metrics-server", "namespace": "monitoring", "port": 443}
    ):
        raise ValueError("Metrics API aggregation must verify its serving CA")
    rules = by_key["ClusterRole", "substrate-metrics-server"]["rules"]
    if rules != [
        {"apiGroups": [""], "resources": ["nodes/metrics"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["nodes", "pods"], "verbs": ["get", "list", "watch"]},
    ]:
        raise ValueError("Metrics Server scrape authority differs")
    for name, role in [
        ("substrate-metrics-server", "substrate-metrics-server"),
        ("substrate-metrics-server-auth-delegator", "system:auth-delegator"),
    ]:
        binding = by_key["ClusterRoleBinding", name]
        if binding["roleRef"] != {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": role,
        } or binding["subjects"] != [
            {"kind": "ServiceAccount", "name": "metrics-server", "namespace": "monitoring"}
        ]:
            raise ValueError("Metrics Server authentication binding differs")
    binding = by_key["RoleBinding", "substrate-metrics-server-auth-reader"]
    if (
        binding["metadata"]["namespace"] != "kube-system"
        or binding["roleRef"]
        != {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "extension-apiserver-authentication-reader",
        }
        or binding["subjects"]
        != [{"kind": "ServiceAccount", "name": "metrics-server", "namespace": "monitoring"}]
    ):
        raise ValueError("Metrics Server request-header authentication binding differs")


def validate() -> list[dict[str, Any]]:
    """Check pinned release identity, strict lint and rendered behavior offline."""
    release = json.loads((ROOT / "addons/resource-metrics/release.json").read_text())
    if release != {
        "schemaVersion": 1,
        "name": "metrics-server",
        "version": "0.9.0",
        "source": "https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.9.0",
        "image": IMAGE,
        "minimumKubernetesVersion": "1.34.0",
        "chart": "charts/resource-metrics",
        "license": "Apache-2.0",
    }:
        raise ValueError("Resource metrics release selection differs")
    values = str(ROOT / "addons/resource-metrics/render-values.yaml")
    helm(["lint", str(CHART), "--strict", "-f", values])
    output = helm(
        [
            "template",
            "resource-metrics",
            str(CHART),
            "--namespace",
            "monitoring",
            "--kube-version",
            "1.35.0",
            "-f",
            values,
        ]
    )
    objects = list(yaml.safe_load_all(output))
    validate_rendered(objects)
    return objects


if __name__ == "__main__":
    validate()
    print(
        "Verified resource-metrics chart, two-way TLS verification and least-privilege bindings offline."
    )
