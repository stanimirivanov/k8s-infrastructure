"""Native admission and issuance evidence for the disposable database gate."""

from __future__ import annotations

import copy
import json
import re
import secrets
import time
from datetime import datetime

from .consumer_profile import Target
from .consumer_runtime import remaining_timeout
from .database_assets import MANAGER
from .database_qualification import cluster
from .kubernetes import contains, kube, reference
from .network_qualification import wait_condition

RECOVERY_NAME = "minimal-demo-certificate-restart-probe"
CERT_VALIDATION_LABEL = "cert-manager.io/disable-validation"


def _webhook_ready(
    target: Target,
    *,
    configuration: str,
    name: str,
    service: tuple[str, str],
    api_group: str,
    resource: str,
    operations: set[str],
    namespace_selector: dict | None = None,
) -> None:
    """Wait for the exact fail-closed webhook trust before probing its API."""
    deadline = time.monotonic() + remaining_timeout(90)
    while True:
        item = json.loads(kube(target, ["get", configuration, "-o", "json"]))
        matching = [hook for hook in item.get("webhooks", []) if hook.get("name") == name]
        if len(matching) != 1:
            raise ValueError("Reviewed database webhook is missing or ambiguous")
        hook = matching[0]
        endpoint = hook.get("clientConfig", {}).get("service", {})
        if (
            (endpoint.get("namespace"), endpoint.get("name")) != service
            or hook.get("failurePolicy") != "Fail"
            or hook.get("sideEffects") not in {"None", "NoneOnDryRun"}
            or hook.get("namespaceSelector", {}) != (namespace_selector or {})
            or hook.get("objectSelector")
            or hook.get("matchConditions")
            or not any(
                (api_group in rule.get("apiGroups", []) or "*" in rule.get("apiGroups", []))
                and ("v1" in rule.get("apiVersions", []) or "*" in rule.get("apiVersions", []))
                and bool({resource, "*", "*/*"} & set(rule.get("resources", [])))
                and (
                    operations <= set(rule.get("operations", []))
                    or "*" in rule.get("operations", [])
                )
                for rule in hook.get("rules", [])
            )
        ):
            raise ValueError("Database webhook authority or admission rules differ")
        if hook["clientConfig"].get("caBundle"):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Database webhook trust did not become ready")
        time.sleep(1)


def _server_probe(target: Target, document: dict, args: list[str]) -> dict:
    raw = json.loads(kube(target, [*args, "--dry-run=server", "-f", "-", "-o", "json"], [document]))
    items = raw["items"] if raw.get("kind") == "List" else [raw]
    if len(items) != 1 or reference(items[0]) != reference(document):
        raise ValueError("Database admission returned a different resource")
    return {"outcome": "server-dry-run-accepted", "resource": document["kind"]}


def certificate_admission(target: Target, issuer: dict) -> dict:
    """Exercise live cert-manager admission without persisting an Issuer."""
    namespace = json.loads(
        kube(target, ["get", "namespace", issuer["metadata"]["namespace"], "-o", "json"])
    )
    if namespace["metadata"].get("labels", {}).get(CERT_VALIDATION_LABEL) == "true":
        raise ValueError("Certificate probe namespace bypasses cert-manager validation")
    for webhook_type in ("mutating", "validating"):
        # The pinned mutator handles CertificateRequests produced by issuance;
        # server dry-run exercises the separate validating path for Issuers.
        mutating = webhook_type == "mutating"
        _webhook_ready(
            target,
            configuration=f"{webhook_type}webhookconfiguration/cert-manager-webhook",
            name="webhook.cert-manager.io",
            service=("cert-manager", "cert-manager-webhook"),
            api_group="cert-manager.io",
            resource="certificaterequests" if mutating else "issuers",
            operations={"CREATE"} if mutating else {"CREATE", "UPDATE"},
            namespace_selector=None
            if mutating
            else {
                "matchExpressions": [
                    {"key": CERT_VALIDATION_LABEL, "operator": "NotIn", "values": ["true"]}
                ]
            },
        )
    return _server_probe(target, issuer, ["apply", "--server-side", f"--field-manager={MANAGER}"])


def cnpg_admission(target: Target, *, postgres_image: str | None = None) -> dict:
    """Exercise the CNPG webhook using a neutral Cluster that never persists."""
    for webhook_type in ("mutating", "validating"):
        _webhook_ready(
            target,
            configuration=f"{webhook_type}webhookconfiguration/cnpg-{webhook_type}-webhook-configuration",
            name=f"{webhook_type[0]}cluster.cnpg.io",
            service=("cnpg-system", "cnpg-webhook-service"),
            api_group="postgresql.cnpg.io",
            resource="clusters",
            operations={"CREATE"},
        )
    document = cluster(target, "cnpg-system")
    if postgres_image is not None:
        document["spec"]["imageName"] = postgres_image
    return _server_probe(target, document, ["create"])


def _ready(target: Target, document: dict) -> dict:
    kind, name, namespace = reference(document)
    resource = f"{kind.lower()}.cert-manager.io"
    if namespace is None:
        raise ValueError("Database certificate evidence requires an explicit namespace")
    wait_condition(target, namespace, resource, name, "Ready", "True")
    item = json.loads(kube(target, ["-n", namespace, "get", resource, name, "-o", "json"]))
    if reference(item) != (kind, name, namespace) or item["metadata"].get("deletionTimestamp"):
        raise ValueError("Database certificate evidence changed identity")
    meta, status = item["metadata"], item["status"]
    generation = meta.get("generation")
    ready = [
        condition for condition in status.get("conditions", []) if condition.get("type") == "Ready"
    ]
    if (
        not meta.get("uid")
        or type(generation) is not int
        or generation <= 0
        or len(ready) != 1
        or (ready[0].get("status") != "True" or ready[0].get("observedGeneration") != generation)
    ):
        raise ValueError("Database certificate evidence does not show the current Ready generation")
    return item


def issuer_evidence(target: Target, issuer: dict) -> dict:
    actual = _ready(target, issuer)
    if not contains(issuer["spec"], actual["spec"]):
        raise ValueError("Database issuer configuration differs")
    return {"uid": actual["metadata"]["uid"], "generation": actual["metadata"]["generation"]}


def certificate_evidence(target: Target, certificates: list[dict]) -> dict:
    evidence = {}
    for expected in certificates:
        actual = _ready(target, expected)
        wanted, observed = expected["spec"], actual["spec"]
        fields = ("commonName", "dnsNames", "issuerRef", "secretName", "usages")
        if any(wanted.get(field) != observed.get(field) for field in fields) or observed.get(
            "isCA", False
        ) is not wanted.get("isCA", False):
            raise ValueError("Database Certificate selection differs")
        meta, status = actual["metadata"], actual["status"]
        start, end = (datetime.fromisoformat(status[field]) for field in ("notBefore", "notAfter"))
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or end <= start
            or type(status.get("revision")) is not int
            or status["revision"] < 1
        ):
            raise ValueError("Database Certificate issuance revision or validity is missing")
        evidence[f"{meta['namespace']}/{meta['name']}"] = {
            "uid": meta["uid"],
            "generation": meta["generation"],
            "revision": status["revision"],
            "notBefore": start.isoformat(),
            "notAfter": end.isoformat(),
        }
    return evidence


def recovery_certificate(server_certificate: dict) -> dict:
    """Ask the pinned issuer for a distinct post-restart Certificate object."""
    result = copy.deepcopy(server_certificate)
    name = f"{RECOVERY_NAME}-{secrets.token_hex(6)}"
    result["metadata"]["name"] = name
    result["spec"].update(commonName=name, dnsNames=[name], secretName=f"{name}-tls")
    return result


def create_recovery_certificate(target: Target, certificate: dict) -> None:
    if not re.fullmatch(
        f"{RECOVERY_NAME}-[a-f0-9]{{12}}", certificate.get("metadata", {}).get("name", "")
    ):
        raise ValueError("Unexpected database restart Certificate identity")
    kube(target, ["create", "-f", "-"], [certificate])
