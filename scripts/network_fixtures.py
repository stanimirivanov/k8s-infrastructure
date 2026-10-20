"""Neutral consumer-owned network fixtures; never FleetIQ application desired state."""

from __future__ import annotations

from .consumer_profile import LABEL, Target
from .network_assets import selection


def resource(kind: str, name: str, namespace: str | None, **fields) -> dict:
    group = {
        "NetworkPolicy": "networking.k8s.io/v1",
        "CiliumNetworkPolicy": "cilium.io/v2",
        "Gateway": "gateway.networking.k8s.io/v1",
        "HTTPRoute": "gateway.networking.k8s.io/v1",
    }.get(kind, "v1")
    return {
        "apiVersion": group,
        "kind": kind,
        "metadata": {"name": name, **({"namespace": namespace} if namespace else {})},
        **fields,
    }


def namespaces(target: Target) -> tuple[str, str]:
    return f"{target.consumer}-net-a", f"{target.consumer}-net-b"


def workloads(target: Target) -> list[dict]:
    a, b = namespaces(target)
    images = selection()["probes"]
    documents = []
    for namespace, identity, role in [
        (a, f"{target.consumer}-a", "worker"),
        (b, f"{target.consumer}-b", "control-plane"),
    ]:
        ns = resource("Namespace", namespace, None)
        ns["metadata"]["labels"] = {
            "app.kubernetes.io/managed-by": target.consumer,
            LABEL: identity,
        }
        documents.append(ns)
        for name in ("echo", "client"):
            container = {
                "name": name,
                "image": images["server" if name == "echo" else "client"],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "32Mi"},
                    "limits": {"cpu": "200m", "memory": "128Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
            if name == "client":
                container["command"] = ["sleep", "3600"]
            else:
                container["readinessProbe"] = {
                    "httpGet": {"path": "/status/200", "port": 8080},
                    "initialDelaySeconds": 1,
                }
            pod = resource(
                "Pod",
                name,
                namespace,
                spec={
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "nodeSelector": {LABEL: target.consumer, "k8s-infrastructure.io/role": role},
                    "tolerations": [
                        {
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [container],
                    "restartPolicy": "Never",
                },
            )
            pod["metadata"]["labels"] = {"app": name}
            documents.append(pod)
        documents.append(
            resource(
                "Service",
                "echo",
                namespace,
                spec={"selector": {"app": "echo"}, "ports": [{"port": 8080, "targetPort": 8080}]},
            )
        )
    return documents


def policies(target: Target) -> list[dict]:
    a, _ = namespaces(target)
    return [
        resource(
            "NetworkPolicy",
            "echo-same-consumer",
            a,
            spec={
                "podSelector": {"matchLabels": {"app": "echo"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": {"app": "client"}}}],
                        "ports": [{"port": 8080, "protocol": "TCP"}],
                    }
                ],
            },
        ),
        resource(
            "CiliumNetworkPolicy",
            "client-required-egress",
            a,
            spec={
                "endpointSelector": {"matchLabels": {"app": "client"}},
                "egress": [
                    {
                        "toEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": a,
                                    "k8s:app": "echo",
                                }
                            }
                        ],
                        "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
                    },
                    {
                        "toEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": "kube-system",
                                    "k8s:k8s-app": "kube-dns",
                                }
                            }
                        ],
                        "toPorts": [
                            {
                                "ports": [{"port": "53", "protocol": "ANY"}],
                                "rules": {"dns": [{"matchPattern": "*"}]},
                            }
                        ],
                    },
                    {
                        "toFQDNs": [
                            {"matchName": "raw.githubusercontent.com"},
                            {"matchName": "registry-1.docker.io"},
                        ],
                        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
                    },
                ],
            },
        ),
        resource(
            "CiliumNetworkPolicy",
            "echo-gateway",
            a,
            spec={
                "endpointSelector": {"matchLabels": {"app": "echo"}},
                "ingress": [{"fromEntities": ["ingress"]}],
            },
        ),
    ]


def routes(target: Target, hostname: str) -> list[dict]:
    a, b = namespaces(target)
    gateway = resource(
        "Gateway",
        "neutral-https",
        a,
        spec={
            "gatewayClassName": "cilium",
            "listeners": [
                {
                    "name": "https",
                    "hostname": hostname,
                    "port": 8443,
                    "protocol": "HTTPS",
                    "tls": {
                        "mode": "Terminate",
                        "certificateRefs": [{"kind": "Secret", "name": "neutral-ingress-tls"}],
                    },
                    "allowedRoutes": {"namespaces": {"from": "Same"}},
                }
            ],
        },
    )
    rule = {
        "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
        "backendRefs": [{"name": "echo", "port": 8080}],
    }
    return [
        gateway,
        resource(
            "HTTPRoute",
            "neutral-echo",
            a,
            spec={
                "parentRefs": [{"name": "neutral-https"}],
                "hostnames": [hostname],
                "rules": [rule],
            },
        ),
        resource(
            "HTTPRoute",
            "foreign-attachment",
            b,
            spec={
                "parentRefs": [{"name": "neutral-https", "namespace": a}],
                "hostnames": [hostname],
                "rules": [rule],
            },
        ),
    ]
