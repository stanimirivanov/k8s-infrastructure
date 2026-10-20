"""Explicit public trust, in-memory neutral certificates and verified loopback HTTPS."""

from __future__ import annotations

import base64
import http.client
import re
import socket
import ssl
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def validate_hostname(hostname: str) -> str:
    if (
        len(hostname) > 253
        or not hostname
        or any(
            len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in hostname.split(".")
        )
    ):
        raise ValueError(
            "Ingress hostname must be an explicit lowercase DNS name without scheme/port"
        )
    return hostname


def fixture_certificate(namespace: str, hostname: str) -> tuple[str, dict]:
    """Issue only a neutral test leaf; the root signing key never leaves this function.

    The returned leaf Secret must travel through private stdin, never logs or a
    handoff. Production certificate lifecycle remains consumer-owned.
    """
    validate_hostname(hostname)
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "neutral-network-qualification")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=12))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "neutral-ingress-tls", "namespace": namespace},
        "type": "kubernetes.io/tls",
        "data": {
            "tls.crt": base64.b64encode(leaf.public_bytes(serialization.Encoding.PEM)).decode(),
            "tls.key": base64.b64encode(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            ).decode(),
        },
    }
    return ca.public_bytes(serialization.Encoding.PEM).decode(), secret


def request(
    context: ssl.SSLContext,
    hostname: str,
    port: int,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Connect only to the selected loopback listener; verify the supplied DNS SNI.

    No resolver/hosts-file or trust-store mutation is necessary. The HTTP Host
    and Origin retain the external host port for same-origin browser sessions.
    """
    validate_hostname(hostname)
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("Ingress verification requires trusted certificates and hostname checking")
    fields = {"Host": f"{hostname}:{port}", "Connection": "close", **(headers or {})}
    wire = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname=hostname) as channel:
            channel.sendall(wire.encode("ascii"))
            response = http.client.HTTPResponse(channel)
            response.begin()
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("Neutral HTTPS response exceeded the bounded evidence size")
            return response.status, response.getheaders(), body


def rejection(context: ssl.SSLContext, hostname: str, port: int, *, wrong_host: bool) -> None:
    try:
        request(context, hostname, port, "/anything")
    except ssl.SSLCertVerificationError as error:
        expected = {62} if wrong_host else {18, 19, 20, 21}
        if error.verify_code not in expected:
            raise ValueError("TLS failed for an unexpected verification reason") from None
    else:
        raise ValueError("Untrusted or wrong-host TLS unexpectedly succeeded")


def wrong_host_rejection(
    context: ssl.SSLContext, hostname: str, port: int, *, selected_hostname: str
) -> str:
    """Prove wrong-host denial, including an SNI-only Gateway's handshake refusal.

    Envoy closes TLS without presenting a certificate when no SNI filter chain
    matches. Accept only that specific EOF between verified positive controls;
    timeouts, resets, other TLS errors and an unavailable selected host fail.
    The returned mode distinguishes SNI rejection from certificate verification.
    """
    if hostname == selected_hostname:
        raise ValueError("Wrong-host qualification requires a distinct hostname")
    if request(context, selected_hostname, port, "/anything")[0] != 200:
        raise ValueError("Wrong-host TLS lacks a verified positive control")
    mode = "certificate-hostname-mismatch"
    try:
        rejection(context, hostname, port, wrong_host=True)
    except ssl.SSLEOFError as error:
        if error.reason != "UNEXPECTED_EOF_WHILE_READING":
            raise
        mode = "unrecognized-sni-handshake"
    if request(context, selected_hostname, port, "/anything")[0] != 200:
        raise ValueError("Wrong-host TLS lacks a verified positive control")
    return mode
