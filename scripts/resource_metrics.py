"""Operate verified-TLS prerequisites without deploying or reconciling Metrics Server."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm

from . import kubelet_serving, local_kind, metrics_certificates

ROOT = Path(__file__).resolve().parents[1]
NODE_NAMES = {"perfeng-local-control-plane", "perfeng-local-worker", "perfeng-local-worker2"}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def document(output: str) -> dict[str, Any]:
    """Bound decoded API objects and refuse non-object responses."""
    if len(output) > 1 << 20:
        raise ValueError("Resource metrics response exceeds its bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate resource metrics API field")
            result[key] = value
        return result

    value = json.loads(output, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Resource metrics response must be an object")
    return value


def inventory(kube: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """Read the named cluster incarnation and exact Ready node cohort."""
    cluster = document(
        local_kind.run([*kube, "get", "namespace", "kube-system", "-o", "json"], timeout=15)
    )
    uid = str(UUID(cluster["metadata"]["uid"]))
    values = document(local_kind.run([*kube, "get", "nodes", "-o", "json"], timeout=15))
    local_kind.check_nodes(values)
    nodes = values["items"]
    if {node["metadata"]["name"] for node in nodes} != NODE_NAMES or len(
        {str(UUID(node["metadata"]["uid"])) for node in nodes}
    ) != 3:
        raise ValueError("Unexpected local node identity")
    return uid, sorted(nodes, key=lambda n: n["metadata"]["name"])


def identities(uid: str, nodes: list[dict[str, Any]]) -> tuple[str, str]:
    """Freeze node incarnations and SAN address inputs before an approval or write."""
    return uid, json.dumps(
        [
            (
                n["metadata"]["name"],
                n["metadata"]["uid"],
                sorted((a["type"], a["address"]) for a in n["status"]["addresses"]),
            )
            for n in nodes
        ],
        sort_keys=True,
    )


def csr_nodes(requests: dict[str, Any], nodes: list[dict[str, Any]], now: datetime) -> None:
    """Print only reviewed serving requests, never approving a collection."""
    items = requests.get("items")
    if not isinstance(items, list) or len(items) > 256:
        raise ValueError("CSR inventory exceeds its bound")
    by_user = {f"system:node:{n['metadata']['name']}": n for n in nodes}
    for request in items:
        spec = request.get("spec", {})
        if (
            spec.get("signerName") != "kubernetes.io/kubelet-serving"
            or spec.get("username") not in by_user
        ):
            continue
        node = by_user[spec["username"]]
        try:
            metrics_certificates.review_request(request, node, now)
        except metrics_certificates.CSRRefusal as error:
            print(f"CSR {request['metadata']['name']}: REFUSED; {error}.")
            continue
        except (ValueError, KeyError, TypeError, x509.ExtensionNotFound, UnsupportedAlgorithm):
            print(
                f"CSR {request['metadata']['name']}: REFUSED; malformed or unsupported CSR/node data. "
                "Request payload and external error details withheld."
            )
            continue
        print(
            json.dumps(
                {
                    "request": request["metadata"]["name"],
                    "node": node["metadata"]["name"],
                    "nodeUid": node["metadata"]["uid"],
                    "addresses": node["status"]["addresses"],
                    "conditions": [
                        c["type"] for c in request.get("status", {}).get("conditions", [])
                    ],
                }
            )
        )


def window_seconds(value: Any) -> float:
    """Read a bounded nonnegative Go duration as returned by the Metrics API."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("Invalid metrics window")
    parts = re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise ValueError("Invalid metrics window")
    factors = {
        "ns": 1e-9,
        "us": 1e-6,
        "µs": 1e-6,
        "μs": 1e-6,
        "ms": 1e-3,
        "s": 1,
        "m": 60,
        "h": 3600,
    }
    return sum(float(number) * factors[unit] for number, unit in parts)


def validate_health(
    api: dict[str, Any],
    metrics: dict[str, Any],
    nodes: list[dict[str, Any]],
    namespace: str,
    now: datetime,
) -> None:
    """Require verified aggregation and fresh node usage, not benchmark eligibility."""
    spec = api["spec"]
    if (
        api["metadata"]["name"] != "v1beta1.metrics.k8s.io"
        or spec.get("insecureSkipTLSVerify", False) is not False
        or spec.get("service") != {"name": "metrics-server", "namespace": namespace, "port": 443}
        or not any(
            c.get("type") == "Available" and c.get("status") == "True"
            for c in api.get("status", {}).get("conditions", [])
        )
    ):
        raise ValueError("Metrics API is unavailable or its verified-TLS service binding differs")
    ca = x509.load_pem_x509_certificate(metrics_certificates.decode(spec["caBundle"]))
    if (
        not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or not ca.not_valid_before_utc <= now < ca.not_valid_after_utc
    ):
        raise ValueError("Metrics API serving CA is invalid or expired")
    items = metrics.get("items")
    expected = {node["metadata"]["name"] for node in nodes}
    if (
        metrics.get("apiVersion") != "metrics.k8s.io/v1beta1"
        or metrics.get("kind") != "NodeMetricsList"
        or not isinstance(items, list)
        or len(items) != len(expected)
    ):
        raise ValueError("Metrics API did not return the full node inventory")
    seen = set()
    for item in items:
        name = item["metadata"]["name"]
        stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        window = window_seconds(item["window"])
        if (
            name not in expected
            or name in seen
            or stamp.tzinfo is None
            or not 0 <= (now - stamp).total_seconds() <= 120
            or not 0 < window <= 60
            or not item.get("usage", {}).get("cpu")
            or not item.get("usage", {}).get("memory")
        ):
            raise ValueError("Node metrics are missing, duplicated, invalid, stale or future-dated")
        seen.add(name)


def execute(
    action: str,
    root: Path,
    namespace: str,
    *,
    confirm: str | None = None,
    request_name: str | None = None,
    node_name: str | None = None,
) -> None:
    """Run one explicit operation after verifying Docker, kind and immutable identities."""
    mutations = {"enable-serving", "approve", "serving-tls"}
    if (
        action not in mutations | {"inspect", "health"}
        or NAME.fullmatch(namespace) is None
        or len(namespace) > 63
    ):
        raise ValueError("Invalid resource metrics action or namespace")
    if action in mutations and confirm != local_kind.NAME:
        raise ValueError("Mutation requires --confirm perfeng-local")
    if action == "approve" and (
        not isinstance(request_name, str)
        or NAME.fullmatch(request_name) is None
        or node_name not in NODE_NAMES
    ):
        raise ValueError("Approval requires one exact --request and --node")
    root = local_kind.normalize_state_root(root)
    local_kind.verify_local_docker()
    local_kind.verify_context(root)
    kube = [
        "kubectl",
        "--kubeconfig",
        str(local_kind.kubeconfig(root)),
        "--context",
        local_kind.CONTEXT,
        "--namespace",
        namespace,
        "--request-timeout=10s",
    ]
    uid, nodes = inventory(kube)
    expected = identities(uid, nodes)

    def guard() -> None:
        local_kind.verify_context(root)
        current_uid, current_nodes = inventory(kube)
        if identities(current_uid, current_nodes) != expected:
            raise ValueError("Cluster/node incarnation changed; no further writes allowed")

    now = datetime.now(UTC)
    print(f"Resource metrics phase: {action} (dedicated cluster verified).", flush=True)
    if action == "enable-serving":
        kubelet_serving.enable(kube, nodes, guard)
    elif action == "inspect":
        csr_nodes(
            document(local_kind.run([*kube, "get", "csr", "-o", "json"], timeout=15)), nodes, now
        )
    elif action == "approve":
        request = document(
            local_kind.run([*kube, "get", "csr", str(request_name), "-o", "json"], timeout=15)
        )
        if request["metadata"].get("name") != request_name:
            raise ValueError("CSR name differs from the explicitly selected request")
        node = next(n for n in nodes if n["metadata"]["name"] == node_name)
        kubelet_serving.approve(kube, request, node, now, guard)
    elif action == "serving-tls":
        output = local_kind.run(
            [
                *kube,
                "get",
                "secret",
                metrics_certificates.SECRET,
                "--ignore-not-found",
                "-o",
                "json",
            ],
            timeout=15,
        )
        if output.strip():
            secret = document(output)
        else:
            secret = metrics_certificates.serving_secret(namespace, now)
            guard()
            local_kind.run(
                [*kube, "create", "-f", "-"], input_data=json.dumps(secret).encode(), timeout=20
            )
        values = metrics_certificates.public_values(secret, namespace, now)
        guard()
        print("Public Helm values only; no private key follows:")
        print(json.dumps(values, indent=2))
    else:
        print("Checking verified APIService availability and node metrics...", flush=True)
        api = document(
            local_kind.run(
                [*kube, "get", "apiservice", "v1beta1.metrics.k8s.io", "-o", "json"], timeout=15
            )
        )
        metrics = document(
            local_kind.run(
                [*kube, "get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes"], timeout=15
            )
        )
        validate_health(api, metrics, nodes, namespace, datetime.now(UTC))
        guard()
        print(
            "Verified resource metrics for all three local nodes; run consumer qualification next."
        )


def main() -> int:
    """Preview offline by default; require execution and separate mutation confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["enable-serving", "inspect", "approve", "serving-tls", "health"]
    )
    parser.add_argument("--state-root", type=Path, default=ROOT)
    parser.add_argument("--namespace", default="monitoring")
    parser.add_argument("--request")
    parser.add_argument("--node")
    parser.add_argument("--confirm")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(
            f"Preview only: {args.action}. No cluster reads, keys, writes or deployment performed."
        )
        return 0
    try:
        execute(
            args.action,
            args.state_root,
            args.namespace,
            confirm=args.confirm,
            request_name=args.request,
            node_name=args.node,
        )
    except ValueError as error:
        print(f"Resource metrics {args.action} error: {str(error)[:400]}")
        return 1
    except (
        OSError,
        KeyError,
        TypeError,
        x509.ExtensionNotFound,
        InvalidSignature,
        UnsupportedAlgorithm,
        yaml.YAMLError,
    ):
        print(
            f"Resource metrics {args.action} failed: invalid prerequisite or certificate material. Private output withheld."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
