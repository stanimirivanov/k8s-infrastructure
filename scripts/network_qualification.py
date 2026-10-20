"""Explicit live traffic/TLS qualification; installed policies alone are not evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import ssl
import time
from urllib.parse import quote

from .consumer_profile import Target
from .kubernetes import kube
from .network_assets import selection
from .network_fixtures import namespaces, policies, routes, workloads
from .network_runtime import health, require_profile
from .network_tls import (
    fixture_certificate,
    rejection,
    request,
    validate_hostname,
    wrong_host_rejection,
)

# Constant shell program, public URL passed as a separate argument. A successful
# kubectl exec is distinct from curl's network result, so API/RBAC failures can
# never masquerade as policy denials. Neither requests nor responses carry keys.
CURL_PROBE = 'http=$(curl --ipv4 --connect-timeout 3 --max-time 5 --silent --output /dev/null --write-out "%{http_code}" "$1"); code=$?; printf "%s %s" "$code" "$http"'


def probe(target: Target, namespace: str, url: str) -> tuple[int, int]:
    output = kube(
        target, ["-n", namespace, "exec", "client", "--", "sh", "-c", CURL_PROBE, "--", url]
    )
    fields = output.strip().split()
    if len(fields) != 2 or any(not value.isdecimal() for value in fields):
        raise ValueError("Incomplete native curl result; no network qualification pass")
    return int(fields[0]), int(fields[1])


def expect_probe(target: Target, namespace: str, url: str, expected: tuple[int, int]) -> None:
    deadline = time.monotonic() + 60
    while True:
        actual = probe(target, namespace, url)
        if actual == expected:
            return
        if time.monotonic() >= deadline:
            raise ValueError(
                f"Native network boundary in {namespace} did not converge: "
                f"expected curl/HTTP {expected}, observed {actual}"
            )
        time.sleep(2)


def wait_condition(
    target: Target, namespace: str, kind: str, name: str, condition: str, expected: str
) -> None:
    deadline = time.monotonic() + 120
    while True:
        item = json.loads(kube(target, ["-n", namespace, "get", kind, name, "-o", "json"]))
        states = item.get("status", {}).get("conditions", [])
        if kind == "httproute":
            states = [
                state
                for parent in item.get("status", {}).get("parents", [])
                for state in parent.get("conditions", [])
            ]
        matching = [state for state in states if state["type"] == condition]
        if matching and all(
            state.get("status") == expected
            and state.get("observedGeneration") == item["metadata"]["generation"]
            for state in matching
        ):
            return
        if time.monotonic() >= deadline:
            raise ValueError(f"Neutral {kind} {condition} did not reach {expected}")
        time.sleep(2)


def create_fixtures(target: Target) -> None:
    documents = workloads(target)
    spaces = [item for item in documents if item["kind"] == "Namespace"]
    for item in spaces:
        if kube(
            target,
            [
                "get",
                "namespace",
                item["metadata"]["name"],
                "--ignore-not-found",
                "-o",
                "jsonpath={.metadata.uid}",
            ],
        ).strip():
            raise ValueError(
                "Qualification namespace exists; inspect retained fixtures before retry"
            )
    uids = {}
    for item in spaces:
        output = json.loads(kube(target, ["create", "-f", "-", "-o", "json"], [item]))
        created = output.get("items", [output])[0]
        uids[item["metadata"]["name"]] = created["metadata"]["uid"]
    for name, uid in uids.items():
        if (
            kube(target, ["get", "namespace", name, "-o", "jsonpath={.metadata.uid}"]).strip()
            != uid
        ):
            raise ValueError(
                "Neutral namespace identity changed; refusing foreign fixture creation"
            )
    kube(target, ["create", "-f", "-"], [item for item in documents if item["kind"] != "Namespace"])
    for namespace in namespaces(target):
        kube(
            target,
            [
                "-n",
                namespace,
                "wait",
                "--for=condition=Ready",
                "pod/echo",
                "pod/client",
                "--timeout=300s",
            ],
        )


def traffic(target: Target) -> dict:
    a, b = namespaces(target)
    ips = {}
    for ns in (a, b):
        address = kube(
            target, ["-n", ns, "get", "service", "echo", "-o", "jsonpath={.spec.clusterIP}"]
        ).strip()
        ips[ns] = str(ipaddress.IPv4Address(address))
    urls = {ns: f"http://{ip}:8080/status/200" for ns, ip in ips.items()}
    # Prove both paths healthy before isolation; denial cannot mean dead backend.
    for source in (a, b):
        for url in urls.values():
            expect_probe(target, source, url, (0, 200))
    kube(target, ["create", "-f", "-"], policies(target))
    expect_probe(target, a, urls[a], (0, 200))
    expect_probe(target, b, urls[a], (28, 0))  # ingress from another consumer
    expect_probe(target, a, urls[b], (28, 0))  # egress to another consumer
    expect_probe(target, b, urls[b], (0, 200))
    expect_probe(target, a, f"http://echo.{a}.svc.cluster.local:8080/status/200", (0, 200))
    expect_probe(target, a, selection()["ciliumCRDs"][0]["url"], (0, 200))
    expect_probe(target, a, "https://registry-1.docker.io/v2/", (0, 401))
    expect_probe(target, a, urls[a], (0, 200))
    return {
        "crossConsumerIngress": "denied",
        "crossConsumerEgress": "denied",
        "clusterDNS": "observed",
        "providerAndRegistryEgress": "observed",
        "positiveControls": "observed before and after policy",
    }


def one_header(headers: dict, name: str) -> str:
    values = [value for key, value in headers.items() if key.casefold() == name.casefold()]
    if len(values) != 1:
        raise ValueError("Neutral HTTPS echo omitted or duplicated a required header")
    value = values[0]
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str):
        raise ValueError("Neutral HTTPS echo returned an ambiguous header")
    return value


def tls(target: Target, hostname: str) -> dict:
    validate_hostname(hostname)
    port = require_profile(target)
    a, b = namespaces(target)
    ca, secret = fixture_certificate(a, hostname)
    kube(target, ["create", "-f", "-"], [secret])
    del secret
    kube(target, ["create", "-f", "-"], routes(target, hostname))
    wait_condition(target, a, "gateway", "neutral-https", "Programmed", "True")
    wait_condition(target, a, "httproute", "neutral-echo", "Accepted", "True")
    wait_condition(target, b, "httproute", "foreign-attachment", "Accepted", "False")
    context = ssl.create_default_context(cadata=ca)
    origin = f"https://{hostname}:{port}"
    deadline = time.monotonic() + 120
    while True:
        try:
            status, _, body = request(
                context,
                hostname,
                port,
                "/anything",
                {"Origin": origin, "Cookie": "network-probe=neutral"},
            )
            if status == 200:
                break
        except (OSError, ssl.SSLError):
            pass
        if time.monotonic() >= deadline:
            raise ValueError("Trusted neutral HTTPS route did not become ready")
        time.sleep(2)
    headers = json.loads(body)["headers"]
    for key, value in {
        "Host": f"{hostname}:{port}",
        "Origin": origin,
        "Cookie": "network-probe=neutral",
    }.items():
        if one_header(headers, key) != value:
            raise ValueError(f"Gateway changed neutral browser {key} semantics")
    cookie = "network-probe=neutral; Secure; HttpOnly; SameSite=Lax"
    status, response_headers, _ = request(
        context,
        hostname,
        port,
        "/response-headers?Set-Cookie=" + quote(cookie, safe=""),
    )
    if status != 200 or not any(
        key.casefold() == "set-cookie" and value == cookie for key, value in response_headers
    ):
        raise ValueError("Gateway changed the neutral secure session cookie")
    rejection(ssl.create_default_context(), hostname, port, wrong_host=False)
    wrong_host_mode = wrong_host_rejection(
        context, "wrong." + hostname, port, selected_hostname=hostname
    )
    if request(context, hostname, port, "/anything", {"Host": "unselected.invalid"})[0] != 404:
        raise ValueError("Gateway routed an unselected HTTP hostname")
    return {
        "origin": origin,
        "trustedTLS": "observed",
        "untrustedTLS": "denied",
        "wrongHostTLS": "denied",
        "wrongHostRejection": wrong_host_mode,
        "foreignRouteAttachment": "denied",
        "hostOriginAndCookies": "preserved",
        "publicTrustSHA256": hashlib.sha256(ca.encode()).hexdigest(),
        "hostTrustStoreChanged": False,
        "CAKeyExported": False,
    }


def qualify(target: Target, hostname: str) -> dict:
    result = health(target)
    print("Networking health passed; creating neutral qualification fixtures...", flush=True)
    create_fixtures(target)
    print("Neutral fixtures ready; qualifying traffic, DNS and external HTTPS...", flush=True)
    network = traffic(target)
    print(
        "Traffic qualification passed; qualifying trusted ingress and negative controls...",
        flush=True,
    )
    secure = tls(target, hostname)
    return {
        **result,
        "trafficAndTLS": "observed",
        "networkEvidence": network,
        "tlsEvidence": secure,
        "fixtureRetention": "retained for inspection; whole-target disposal remains explicit",
    }
