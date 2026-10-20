"""Real SSL trust/hostname tests against an ephemeral loopback server, not a cluster."""

import base64
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.network_tls import (
    fixture_certificate,
    rejection,
    request,
    validate_hostname,
    wrong_host_rejection,
)


class NetworkTLSTests(unittest.TestCase):
    def test_sni_eof_requires_both_verified_positive_controls(self):
        context = ssl.create_default_context()
        eof = ssl.SSLEOFError(8, "neutral EOF")
        eof.reason = "UNEXPECTED_EOF_WHILE_READING"
        positive = (200, [], b"OK")
        with patch("scripts.network_tls.request", side_effect=[positive, eof, positive]) as send:
            self.assertEqual(
                wrong_host_rejection(
                    context, "wrong.localhost", 18443, selected_hostname="right.localhost"
                ),
                "unrecognized-sni-handshake",
            )
            self.assertEqual(
                [call.args[1] for call in send.call_args_list],
                ["right.localhost", "wrong.localhost", "right.localhost"],
            )
        for results in (
            [eof],
            [positive, eof, eof],
            [positive, TimeoutError()],
            [positive, ConnectionResetError()],
            [positive, eof, (503, [], b"")],
        ):
            with (
                self.subTest(results=len(results)),
                patch("scripts.network_tls.request", side_effect=results),
                self.assertRaises((OSError, ValueError)),
            ):
                wrong_host_rejection(
                    context, "wrong.localhost", 18443, selected_hostname="right.localhost"
                )
        with patch("scripts.network_tls.request") as send, self.assertRaises(ValueError):
            wrong_host_rejection(
                context, "right.localhost", 18443, selected_hostname="right.localhost"
            )
        send.assert_not_called()

    def test_hostname_rejects_header_and_origin_injection(self):
        for name in (
            "https://app.localhost",
            "app.localhost:8443",
            "app.localhost\r\nInjected: true",
            "UPPER.localhost",
            "",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_hostname(name)

    def test_real_verified_tls_trusted_untrusted_and_wrong_host(self):
        ca, secret = fixture_certificate("neutral-test", "neutral.localhost")
        self.assertEqual(set(secret["data"]), {"tls.crt", "tls.key"})
        self.assertNotIn("PRIVATE", ca)
        # Windows may spend significant time reading the system trust store.
        # Prepare both contexts before starting the bounded server so that
        # unrelated trust-store discovery cannot exhaust its accept window.
        trusted = ssl.create_default_context(cadata=ca)
        untrusted = ssl.create_default_context()
        with tempfile.TemporaryDirectory(prefix="neutral-tls-") as directory:
            root = Path(directory)
            for name, field in (("leaf.pem", "tls.crt"), ("leaf.key", "tls.key")):
                path = root / name
                path.write_bytes(base64.b64decode(secret["data"][field]))
                path.chmod(0o600)
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(root / "leaf.pem", root / "leaf.key")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(10)
                port = listener.getsockname()[1]
                failures = []

                def serve():
                    try:
                        for _ in range(3):
                            channel, _ = listener.accept()
                            with channel:
                                try:
                                    with server.wrap_socket(channel, server_side=True) as tls:
                                        tls.recv(4096)
                                        tls.sendall(
                                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                                        )
                                except ssl.SSLError:
                                    pass  # Expected client certificate/hostname rejection.
                    except Exception as error:
                        failures.append(type(error).__name__)

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                self.assertEqual(request(trusted, "neutral.localhost", port, "/")[0], 200)
                rejection(untrusted, "neutral.localhost", port, wrong_host=False)
                rejection(trusted, "other.localhost", port, wrong_host=True)
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
