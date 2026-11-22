"""Witness fresh CA injector work without changing any effective admission rules."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

from . import consumer_runtime
from .consumer_profile import LABEL, MINIMAL_PROFILE, Target
from .kubernetes import kube

INJECT_APISERVER_CA = "cert-manager.io/inject-apiserver-ca"


def probe_ca_injection(target: Target) -> dict:
    """Require a new inert admission object to acquire the exact public API server CA."""
    if target.profile != MINIMAL_PROFILE or not target.consumer.startswith("qual-"):
        raise ValueError("CA injection probe requires a disposable minimal-demo target")
    with consumer_runtime.deadline(90):
        config = json.loads(
            kube(
                target, ["-n", "cert-manager", "get", "configmap", "kube-root-ca.crt", "-o", "json"]
            )
        )
        expected = config.get("data", {}).get("ca.crt", "").encode()
        if not expected:
            raise ValueError("Public API server CA is unavailable")

        name = f"{target.consumer}-ca-injection-{secrets.token_hex(6)}"
        endpoint = {"namespace": "cert-manager", "name": "cert-manager-webhook", "port": 443}
        document = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingWebhookConfiguration",
            "metadata": {
                "name": name,
                "annotations": {INJECT_APISERVER_CA: "true"},
                "labels": {LABEL: target.consumer},
            },
            "webhooks": [
                {
                    "name": "ca-probe.k8s-infrastructure.io",
                    "rules": [],
                    "sideEffects": "None",
                    "admissionReviewVersions": ["v1"],
                    "failurePolicy": "Ignore",
                    "clientConfig": {"service": endpoint},
                }
            ],
        }
        created = json.loads(kube(target, ["create", "-f", "-", "-o", "json"], [document]))
        if created.get("kind") == "List" and len(created["items"]) == 1:
            created = created["items"][0]
        uid = created["metadata"].get("uid")
        if not uid or _bundle(created, name, uid, target.consumer, endpoint):
            raise ValueError("New CA injection target did not start with an empty bundle")

        while True:
            item = json.loads(
                kube(target, ["get", "validatingwebhookconfiguration", name, "-o", "json"])
            )
            bundle = _bundle(item, name, uid, target.consumer, endpoint)
            if bundle:
                if base64.b64decode(bundle, validate=True) != expected:
                    raise ValueError("Injected bundle differs from the public API server CA")
                return {
                    "name": name,
                    "uid": uid,
                    "bundleSha256": hashlib.sha256(expected).hexdigest(),
                }
            time.sleep(1)


def _bundle(item: dict, name: str, uid: str, consumer: str, endpoint: dict) -> str:
    meta = item["metadata"]
    hooks = item.get("webhooks", [])
    if (
        item.get("kind") != "ValidatingWebhookConfiguration"
        or meta.get("name") != name
        or meta.get("uid") != uid
        or meta.get("deletionTimestamp")
        or meta.get("labels", {}).get(LABEL) != consumer
        or meta.get("annotations", {}).get(INJECT_APISERVER_CA) != "true"
        or len(hooks) != 1
    ):
        raise ValueError("Owned CA injection target changed")
    hook = hooks[0]
    client = hook["clientConfig"]
    if (
        hook.get("name") != "ca-probe.k8s-infrastructure.io"
        or hook.get("rules")
        or hook.get("failurePolicy") != "Ignore"
        or client.get("service") != endpoint
        or client.get("url")
    ):
        raise ValueError("CA injection target gained admission authority")
    return client.get("caBundle", "")
