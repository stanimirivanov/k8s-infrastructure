"""Validate kubelet serving CSRs and issue isolated Metrics Server TLS material."""

from __future__ import annotations

import base64
import ipaddress
from datetime import datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OWNER = "k8s-infrastructure"
SECRET = "substrate-resource-metrics-tls"
CA_ANNOTATION = "k8s-infrastructure.io/serving-ca"


def decode(value: str) -> bytes:
    """Decode bounded, canonical base64 without accepting ignored characters."""
    if not isinstance(value, str) or not 1 <= len(value) <= 32768:
        raise ValueError("Invalid certificate payload size")
    decoded = base64.b64decode(value, validate=True)
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("Certificate base64 encoding is not canonical")
    return decoded


def review_request(request: dict[str, Any], node: dict[str, Any], now: datetime) -> None:
    """Require an authenticated, fresh server-only CSR for the current node's addresses.

    This validates public request bytes only. It never approves a CSR or establishes
    ownership of the live node; the caller must recheck node UID before an API write.
    """
    name = node["metadata"]["name"]
    spec = request["spec"]
    created = datetime.fromisoformat(
        request["metadata"]["creationTimestamp"].replace("Z", "+00:00")
    )
    node_created = datetime.fromisoformat(
        node["metadata"]["creationTimestamp"].replace("Z", "+00:00")
    )
    if (
        spec.get("signerName") != "kubernetes.io/kubelet-serving"
        or spec.get("username") != f"system:node:{name}"
        or set(spec.get("groups", [])) != {"system:nodes", "system:authenticated"}
        or set(spec.get("usages", [])) != {"digital signature", "key encipherment", "server auth"}
        or created.tzinfo is None
        or node_created.tzinfo is None
        or created < node_created
        or not timedelta() <= now - created <= timedelta(hours=1)
        or request["metadata"].get("deletionTimestamp") is not None
        or any(c.get("type") == "Denied" for c in request.get("status", {}).get("conditions", []))
    ):
        raise ValueError("CSR identity, freshness or server-only authority differs")
    csr = x509.load_pem_x509_csr(decode(spec["request"]))
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "system:nodes"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"system:node:{name}"),
        ]
    )
    key = csr.public_key()
    strong = (isinstance(key, rsa.RSAPublicKey) and 2048 <= key.key_size <= 8192) or (
        isinstance(key, ec.EllipticCurvePublicKey)
        and isinstance(key.curve, (ec.SECP256R1, ec.SECP384R1, ec.SECP521R1))
    )
    if not strong or csr.subject != subject or not csr.is_signature_valid:
        raise ValueError("CSR subject, signature or public key differs")
    if len(csr.extensions) != 1:
        raise ValueError("CSR must request only node address SANs")
    sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    addresses = node["status"]["addresses"]
    dns = {
        a["address"] for a in addresses if a["type"] in {"Hostname", "InternalDNS", "ExternalDNS"}
    }
    ips = {
        ipaddress.ip_address(a["address"])
        for a in addresses
        if a["type"] in {"InternalIP", "ExternalIP"}
    }
    requested_dns = sans.get_values_for_type(x509.DNSName)
    requested_ips = sans.get_values_for_type(x509.IPAddress)
    if (
        not requested_ips
        or len(sans) != len(requested_dns) + len(requested_ips)
        or len(set(requested_dns)) != len(requested_dns)
        or len(set(requested_ips)) != len(requested_ips)
        or not set(requested_dns) <= dns
        or set(requested_ips) != ips
    ):
        raise ValueError("CSR SANs differ from the current node addresses")


def serving_secret(namespace: str, now: datetime) -> dict[str, Any]:
    """Issue one serving leaf/CA entirely in memory; discard the CA signing key.

    The returned Secret contains a private leaf key and must travel only through
    private stdin. Only its CA annotation is a public consumer handoff value.
    """
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "substrate-resource-metrics")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=366))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "metrics-server")])
    leaf = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(f"metrics-server.{namespace}.svc"),
                    x509.DNSName(f"metrics-server.{namespace}.svc.cluster.local"),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def encode(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": SECRET,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": OWNER},
            "annotations": {CA_ANNOTATION: encode(ca.public_bytes(serialization.Encoding.PEM))},
        },
        "type": "kubernetes.io/tls",
        "immutable": True,
        "data": {
            "tls.crt": encode(leaf.public_bytes(serialization.Encoding.PEM)),
            "tls.key": encode(
                leaf_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            ),
        },
    }


def public_values(secret: dict[str, Any], namespace: str, now: datetime) -> dict[str, Any]:
    """Verify owned existing TLS material and return only non-secret Helm values.

    Reuse never rotates keys. Expiry within seven days fails with an explicit
    rotation requirement; changing the public CA also requires GitOps review.
    """
    metadata = secret["metadata"]
    if (
        metadata.get("name") != SECRET
        or metadata.get("namespace") != namespace
        or metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != OWNER
        or metadata.get("deletionTimestamp") is not None
        or secret.get("immutable") is not True
        or secret.get("type") != "kubernetes.io/tls"
        or set(secret["data"]) != {"tls.crt", "tls.key"}
    ):
        raise ValueError("Existing Metrics Server TLS Secret has unexpected ownership")
    bundle = metadata["annotations"][CA_ANNOTATION]
    ca = x509.load_pem_x509_certificate(decode(bundle))
    leaf = x509.load_pem_x509_certificate(decode(secret["data"]["tls.crt"]))
    key = serialization.load_pem_private_key(decode(secret["data"]["tls.key"]), password=None)
    leaf.verify_directly_issued_by(ca)
    if (
        leaf.public_key() != key.public_key()
        or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or not ca.not_valid_before_utc <= now < ca.not_valid_after_utc - timedelta(days=7)
        or not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc - timedelta(days=7)
        or leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        != x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
        or set(
            leaf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
        )
        != {f"metrics-server.{namespace}.svc", f"metrics-server.{namespace}.svc.cluster.local"}
    ):
        raise ValueError(
            "TLS certificate/key/CA, service identity or validity differs; review rotation"
        )
    return {"tls": {"secretName": SECRET, "caBundle": bundle}}
