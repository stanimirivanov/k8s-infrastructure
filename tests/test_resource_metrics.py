from __future__ import annotations

import base64
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from scripts import kubelet_serving, metrics_certificates, metrics_chart, resource_metrics

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def node() -> dict:
    return {
        "metadata": {
            "name": "perfeng-local-worker",
            "uid": "node-uid",
            "creationTimestamp": (NOW - timedelta(days=1)).isoformat(),
        },
        "status": {
            "addresses": [
                {"type": "Hostname", "address": "perfeng-local-worker"},
                {"type": "InternalIP", "address": "172.18.0.3"},
            ]
        },
    }


def request(
    *,
    dns: str = "perfeng-local-worker",
    address: str = "172.18.0.3",
    uri: bool = False,
    key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | None = None,
) -> dict:
    import ipaddress

    names = [x509.DNSName(dns), x509.IPAddress(ipaddress.ip_address(address))]
    if uri:
        names.append(x509.UniformResourceIdentifier("spiffe://unapproved"))
    key = key or ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "system:nodes"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "system:node:perfeng-local-worker"),
                ]
            )
        )
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    return {
        "apiVersion": "certificates.k8s.io/v1",
        "kind": "CertificateSigningRequest",
        "metadata": {
            "name": "csr-reviewed",
            "uid": "csr-uid",
            "resourceVersion": "4",
            "creationTimestamp": NOW.isoformat(),
        },
        "spec": {
            "signerName": "kubernetes.io/kubelet-serving",
            "username": "system:node:perfeng-local-worker",
            "groups": ["system:nodes", "system:authenticated"],
            "usages": ["digital signature", "server auth"],
            "request": base64.b64encode(csr.public_bytes(serialization.Encoding.PEM)).decode(),
        },
    }


class CertificateTests(unittest.TestCase):
    def test_both_supported_serving_usage_sets_work_for_ec_and_rsa_keys(self) -> None:
        for key in (
            ec.generate_private_key(ec.SECP256R1()),
            rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ):
            selected = request(key=key)
            for usages in (
                ["digital signature", "server auth"],
                ["digital signature", "key encipherment", "server auth"],
                ["server auth", "digital signature"],
                ["server auth", "key encipherment", "digital signature"],
            ):
                with self.subTest(key=type(key).__name__, usages=usages):
                    selected["spec"]["usages"] = usages
                    metrics_certificates.review_request(selected, node(), NOW)

    def test_unsupported_or_malformed_usages_never_reach_approval(self) -> None:
        selected = request()
        for usages in (
            None,
            "digital signature,server auth",
            [],
            [None, "server auth"],
            ["server auth"],
            ["digital signature"],
            ["digital signature", "key encipherment"],
            ["digital signature", "server auth", "client auth"],
            ["digital signature", "server auth", "cert sign"],
            ["digital signature", "server auth", "unexpected"],
            ["digital signature", "server auth", "server auth"],
        ):
            selected["spec"]["usages"] = usages
            with (
                self.subTest(usages=usages),
                patch("scripts.local_kind.run") as run,
                self.assertRaisesRegex(ValueError, "usages"),
            ):
                kubelet_serving.approve(["kubectl"], selected, node(), NOW, lambda: None)
            run.assert_not_called()

    def test_inspection_reports_specific_refusal_without_request_payloads(self) -> None:
        cases = []
        for field, value, reason in (
            ("groups", ["system:masters"], "groups"),
            ("usages", ["client auth"], "usages"),
            ("request", "untrusted-payload-not-for-output", "encoding"),
        ):
            selected = request()
            selected["spec"][field] = value
            cases.append((selected, reason))
        for when, reason in (
            (NOW - timedelta(hours=2), "one-hour"),
            (NOW + timedelta(seconds=1), "future"),
            (NOW - timedelta(days=2), "node incarnation"),
        ):
            selected = request()
            selected["metadata"]["creationTimestamp"] = when.isoformat()
            cases.append((selected, reason))
        denied = request()
        denied["status"] = {"conditions": [{"type": "Denied", "status": "True"}]}
        cases.append((denied, "Denied"))
        deleting = request()
        deleting["metadata"]["deletionTimestamp"] = NOW.isoformat()
        cases.append((deleting, "deleted"))
        cases.extend([(request(dns="foreign-node"), "SANs"), (request(uri=True), "SANs")])
        for selected, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    metrics_certificates.review_request(selected, node(), NOW)
                with redirect_stdout(io.StringIO()) as output:
                    resource_metrics.csr_nodes({"items": [selected]}, [node()], NOW)
                self.assertIn("REFUSED", output.getvalue())
                self.assertIn(reason, output.getvalue())
                self.assertNotIn(selected["spec"]["request"], output.getvalue())

    def test_unrelated_requests_are_not_inspected_but_cannot_be_approved(self) -> None:
        for field, value, reason in (
            ("signerName", "wrong-signer", "signer"),
            ("username", "system:node:other", "username"),
        ):
            selected = request()
            selected["spec"][field] = value
            with self.subTest(field=field), patch("scripts.local_kind.run") as run:
                with self.assertRaisesRegex(ValueError, reason):
                    kubelet_serving.approve(["kubectl"], selected, node(), NOW, lambda: None)
                with redirect_stdout(io.StringIO()) as output:
                    resource_metrics.csr_nodes({"items": [selected]}, [node()], NOW)
                self.assertEqual(output.getvalue(), "")
                run.assert_not_called()

    def test_unexpected_parser_details_are_not_printed_during_inspection(self) -> None:
        selected = request()
        with (
            patch(
                "scripts.metrics_certificates.review_request",
                side_effect=ValueError("untrusted-library-error-not-for-output"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            resource_metrics.csr_nodes({"items": [selected]}, [node()], NOW)
        self.assertIn("malformed or unsupported CSR/node data", output.getvalue())
        self.assertNotIn("untrusted-library-error-not-for-output", output.getvalue())
        self.assertNotIn(selected["spec"]["request"], output.getvalue())

    def test_usage_acceptance_does_not_extend_the_timestamp_review_window(self) -> None:
        selected = request()
        for stamp, reason in (
            (None, "timestamp"),
            ("invalid", "timestamp"),
            (NOW.replace(tzinfo=None).isoformat(), "timezone"),
            ((NOW - timedelta(hours=1, microseconds=1)).isoformat(), "one-hour"),
        ):
            selected["metadata"]["creationTimestamp"] = stamp
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, reason):
                metrics_certificates.review_request(selected, node(), NOW)
        selected["metadata"]["creationTimestamp"] = (NOW - timedelta(hours=1)).isoformat()
        metrics_certificates.review_request(selected, node(), NOW)

    def test_serving_keys_stay_in_secret_and_public_handoff_is_reusable(self) -> None:
        secret = metrics_certificates.serving_secret("monitoring", NOW)
        values = metrics_certificates.public_values(secret, "monitoring", NOW)
        self.assertEqual(set(values), {"tls"})
        self.assertEqual(values["tls"]["secretName"], metrics_certificates.SECRET)
        self.assertNotIn(secret["data"]["tls.key"], json.dumps(values))
        self.assertEqual(values, metrics_certificates.public_values(secret, "monitoring", NOW))
        for mutation in ("owner", "expiry", "key", "namespace", "immutable"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(secret)
                when = NOW
                namespace = "monitoring"
                if mutation == "owner":
                    changed["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "foreign"
                elif mutation == "expiry":
                    when += timedelta(days=360)
                elif mutation == "key":
                    changed["data"]["tls.key"] = metrics_certificates.serving_secret(
                        "monitoring", NOW
                    )["data"]["tls.key"]
                elif mutation == "namespace":
                    namespace = "other"
                else:
                    changed["immutable"] = False
                with self.assertRaises(ValueError):
                    metrics_certificates.public_values(changed, namespace, when)

    def test_csr_authority_addresses_and_freshness_are_closed(self) -> None:
        metrics_certificates.review_request(request(), node(), NOW)
        cases = [request(dns="another-node"), request(address="172.18.0.99"), request(uri=True)]
        for field, value in [
            ("signerName", "kubernetes.io/kube-apiserver-client"),
            ("username", "system:node:other"),
            ("usages", ["server auth", "client auth"]),
            ("groups", ["system:masters"]),
        ]:
            changed = request()
            changed["spec"][field] = value
            cases.append(changed)
        expired = request()
        expired["metadata"]["creationTimestamp"] = (NOW - timedelta(hours=2)).isoformat()
        cases.append(expired)
        denied = request()
        denied["status"] = {"conditions": [{"type": "Denied", "status": "True"}]}
        cases.append(denied)
        for index, changed in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                metrics_certificates.review_request(changed, node(), NOW)

    def test_ambiguous_payloads_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical"):
            metrics_certificates.decode("Zh==")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            resource_metrics.document('{"metadata": {}, "metadata": {}}')
        for extra in ("kind: Other\n", "1: ambiguous\n"):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(ValueError, "Duplicate|non-string"),
            ):
                kubelet_serving.bootstrap_configuration(RolloutTests.configuration() + extra, "")

    def test_approval_is_single_request_fenced_and_idempotent(self) -> None:
        selected = request()
        with patch("scripts.local_kind.run", return_value="") as run:
            kubelet_serving.approve(["kubectl"], selected, node(), NOW, lambda: None)
        command = run.call_args.args[0]
        self.assertIn(
            "/apis/certificates.k8s.io/v1/certificatesigningrequests/csr-reviewed/approval", command
        )
        body = json.loads(run.call_args.kwargs["input_data"])
        self.assertEqual(body["metadata"]["uid"], "csr-uid")
        self.assertEqual(body["metadata"]["resourceVersion"], "4")
        self.assertEqual(selected.get("status"), None)
        with patch("scripts.local_kind.run") as run:
            with self.assertRaisesRegex(ValueError, "changed"):
                kubelet_serving.approve(
                    ["kubectl"],
                    selected,
                    node(),
                    NOW,
                    lambda: (_ for _ in ()).throw(ValueError("identity changed")),
                )
        run.assert_not_called()
        with patch("scripts.local_kind.run") as run:
            kubelet_serving.approve(["kubectl"], body, node(), NOW, lambda: None)
        run.assert_not_called()

    def test_kubelet_change_preserves_other_policy_and_rejects_flag_overrides(self) -> None:
        original = {
            "apiVersion": "kubelet.config.k8s.io/v1beta1",
            "kind": "KubeletConfiguration",
            "authentication": {"anonymous": {"enabled": False}},
            "tlsCertFile": "/old.crt",
            "tlsPrivateKeyFile": "/old.key",
        }
        updated = yaml.safe_load(
            kubelet_serving.bootstrap_configuration(yaml.safe_dump(original), "")
        )
        self.assertEqual(
            updated,
            {k: v for k, v in original.items() if k not in {"tlsCertFile", "tlsPrivateKeyFile"}}
            | {"serverTLSBootstrap": True},
        )
        with self.assertRaisesRegex(ValueError, "overrides"):
            kubelet_serving.bootstrap_configuration(
                yaml.safe_dump(original), "--tls-cert-file=/override"
            )

    def test_preview_and_missing_confirmation_cannot_read_or_write_cluster(self) -> None:
        with (
            patch("sys.argv", ["resource_metrics", "enable-serving"]),
            patch("scripts.local_kind.run") as run,
        ):
            self.assertEqual(resource_metrics.main(), 0)
        run.assert_not_called()
        with patch("scripts.local_kind.verify_local_docker") as verify:
            with self.assertRaisesRegex(ValueError, "confirm"):
                resource_metrics.execute("enable-serving", Path("."), "monitoring")
        verify.assert_not_called()

    def test_secret_is_created_only_through_private_stdin_and_reused(self) -> None:
        secret = metrics_certificates.serving_secret("monitoring", NOW)
        root = Path.cwd()
        values = iter(["", ""])
        with (
            patch("scripts.local_kind.normalize_state_root", return_value=root),
            patch("scripts.local_kind.verify_local_docker"),
            patch("scripts.local_kind.verify_context"),
            patch("scripts.resource_metrics.inventory", return_value=("cluster", [node()])),
            patch("scripts.local_kind.run", side_effect=lambda *_a, **_kw: next(values)) as run,
            patch("scripts.metrics_certificates.serving_secret", return_value=secret),
            patch("scripts.resource_metrics.datetime") as clock,
        ):
            clock.now.return_value = NOW
            with redirect_stdout(io.StringIO()) as output:
                resource_metrics.execute("serving-tls", root, "monitoring", confirm="perfeng-local")
        body = run.call_args.kwargs["input_data"]
        self.assertEqual(json.loads(body)["data"], secret["data"])
        self.assertNotIn(secret["data"]["tls.key"], output.getvalue())
        self.assertNotIn(secret["data"]["tls.key"], " ".join(run.call_args.args[0]))

    def test_existing_secret_is_reused_without_issuance_or_write(self) -> None:
        secret = metrics_certificates.serving_secret("monitoring", NOW)
        root = Path.cwd()
        with (
            patch("scripts.local_kind.normalize_state_root", return_value=root),
            patch("scripts.local_kind.verify_local_docker"),
            patch("scripts.local_kind.verify_context"),
            patch("scripts.resource_metrics.inventory", return_value=("cluster", [node()])),
            patch("scripts.local_kind.run", return_value=json.dumps(secret)) as run,
            patch("scripts.metrics_certificates.serving_secret") as issue,
            patch("scripts.resource_metrics.datetime") as clock,
            redirect_stdout(io.StringIO()) as output,
        ):
            clock.now.return_value = NOW
            resource_metrics.execute("serving-tls", root, "monitoring", confirm="perfeng-local")
        issue.assert_not_called()
        run.assert_called_once()
        self.assertNotIn("input_data", run.call_args.kwargs)
        self.assertNotIn(secret["data"]["tls.key"], output.getvalue())


class RolloutTests(unittest.TestCase):
    @staticmethod
    def configuration() -> str:
        return yaml.safe_dump(
            {
                "kind": "KubeletConfiguration",
                "apiVersion": "kubelet.config.k8s.io/v1beta1",
                "tlsCertFile": "/old.crt",
                "tlsPrivateKeyFile": "/old.key",
            }
        )

    def test_rollout_uses_container_ids_preserves_backup_and_never_approves(self) -> None:
        content = self.configuration()
        prepared = kubelet_serving.bootstrap_configuration(content, "")
        import hashlib

        digest = hashlib.sha256(prepared).hexdigest()
        identity = "c" * 64

        def response(command: list[str], **kwargs: Any) -> str:
            if command[-1] == kubelet_serving.CONFIG and "cat" in command:
                return content
            if command[-1].endswith("/proxy/configz"):
                return json.dumps({"kubeletconfig": {}})
            if "sha256sum" in command:
                return digest + "  " + kubelet_serving.CONFIG
            return ""

        with (
            patch("scripts.kubelet_serving.container_id", return_value=identity),
            patch("scripts.local_kind.run", side_effect=response) as run,
        ):
            kubelet_serving.enable(["kubectl"], [node()], lambda: None)
        writes = [call for call in run.call_args_list if "-i" in call.args[0]]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].kwargs["input_data"], prepared)
        self.assertIn(kubelet_serving.BACKUP, writes[0].args[0][6])
        self.assertTrue(any("restart" in call.args[0] for call in run.call_args_list))
        for call in run.call_args_list:
            command = call.args[0]
            if command[:2] == ["docker", "exec"]:
                self.assertIn(identity, command)
            self.assertNotIn("approve", command)

    def test_prevalidation_and_container_replacement_prevent_writes(self) -> None:
        content = self.configuration()
        for failure in ("flags", "replacement"):
            with self.subTest(failure=failure):
                values = [content, "--tls-cert-file=/override" if failure == "flags" else ""]
                with (
                    patch("scripts.kubelet_serving.container_id", side_effect=["c" * 64, "d" * 64]),
                    patch("scripts.local_kind.run", side_effect=values) as run,
                ):
                    with self.assertRaises(ValueError):
                        kubelet_serving.enable(["kubectl"], [node()], lambda: None)
                self.assertFalse(
                    any(
                        "-i" in call.args[0] or "restart" in call.args[0]
                        for call in run.call_args_list
                    )
                )

    def test_address_change_or_node_recreation_invalidates_review(self) -> None:
        original = node()
        changed = copy.deepcopy(original)
        changed["status"]["addresses"][1]["address"] = "172.18.0.99"
        self.assertNotEqual(
            resource_metrics.identities("cluster", [original]),
            resource_metrics.identities("cluster", [changed]),
        )
        recreated = copy.deepcopy(original)
        recreated["metadata"]["creationTimestamp"] = (NOW + timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            metrics_certificates.review_request(request(), recreated, NOW)

    def test_interrupted_write_stops_without_restart_rollback_or_next_node(self) -> None:
        content = self.configuration()
        other = copy.deepcopy(node())
        other["metadata"]["name"] = "perfeng-local-worker2"

        def response(command: list[str], **kwargs: Any) -> str:
            if "-i" in command:
                raise ValueError("Interrupted node write; inspect retained backup")
            if command[-1] == kubelet_serving.CONFIG and "cat" in command:
                return content
            if command[-1].endswith("/proxy/configz"):
                return json.dumps({"kubeletconfig": {}})
            return ""

        with (
            patch("scripts.kubelet_serving.container_id", return_value="c" * 64),
            patch("scripts.local_kind.run", side_effect=response) as run,
            self.assertRaisesRegex(ValueError, "Interrupted"),
        ):
            kubelet_serving.enable(["kubectl"], [node(), other], lambda: None)
        self.assertEqual(sum("-i" in call.args[0] for call in run.call_args_list), 1)
        self.assertFalse(any("restart" in call.args[0] for call in run.call_args_list))
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "-i"])


class ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.objects = metrics_chart.validate()

    def test_aggregation_render_matches_api_omission_of_default_false(self) -> None:
        api = next(o for o in self.objects if o["kind"] == "APIService")
        self.assertNotIn("insecureSkipTLSVerify", api["spec"])
        self.assertIs(api["spec"].get("insecureSkipTLSVerify", False), False)
        self.assertTrue(api["spec"]["caBundle"])

    def test_render_validator_refuses_reintroduced_aggregation_flags(self) -> None:
        for value in (False, True, None, 0, "false"):
            with self.subTest(value=value):
                objects = copy.deepcopy(self.objects)
                api = next(o for o in objects if o["kind"] == "APIService")
                api["spec"]["insecureSkipTLSVerify"] = value
                with self.assertRaisesRegex(ValueError, "aggregation"):
                    metrics_chart.validate_rendered(objects)

    def test_rejects_privilege_or_tls_regressions(self) -> None:
        for failure in (
            "scrape-rbac",
            "delegation",
            "aggregation",
            "missing-ca",
            "kubelet",
            "privileged",
            "extra-secret",
        ):
            with self.subTest(failure=failure):
                objects = copy.deepcopy(self.objects)
                by_kind = {o["kind"]: o for o in objects}
                if failure == "scrape-rbac":
                    by_kind["ClusterRole"]["rules"][0]["verbs"].append("create")
                elif failure == "delegation":
                    binding = next(
                        o
                        for o in objects
                        if o["metadata"]["name"] == "substrate-metrics-server-auth-delegator"
                    )
                    binding["roleRef"]["name"] = "cluster-admin"
                elif failure == "aggregation":
                    by_kind["APIService"]["spec"]["insecureSkipTLSVerify"] = True
                elif failure == "missing-ca":
                    del by_kind["APIService"]["spec"]["caBundle"]
                elif failure == "kubelet":
                    by_kind["Deployment"]["spec"]["template"]["spec"]["containers"][0][
                        "args"
                    ].append("--kubelet-insecure-tls")
                elif failure == "privileged":
                    by_kind["Deployment"]["spec"]["template"]["spec"]["hostNetwork"] = True
                else:
                    objects.append({"kind": "Secret", "metadata": {"name": "generated"}})
                with self.assertRaises(ValueError):
                    metrics_chart.validate_rendered(objects)

    def test_schema_rejects_missing_tls_and_unknown_overrides(self) -> None:
        for values in (
            {},
            {"args": ["--kubelet-insecure-tls"]},
            {"tls": {"secretName": "selected", "caBundle": ""}},
        ):
            import subprocess

            completed = subprocess.run(
                ["helm", "template", "resource-metrics", str(metrics_chart.CHART), "-f", "-"],
                input=yaml.safe_dump(values),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_health_requires_actual_available_verified_fresh_metrics(self) -> None:
        api = copy.deepcopy(next(o for o in self.objects if o["kind"] == "APIService"))
        secret = metrics_certificates.serving_secret("monitoring", NOW)
        api["spec"]["caBundle"] = metrics_certificates.public_values(secret, "monitoring", NOW)[
            "tls"
        ]["caBundle"]
        api["status"] = {"conditions": [{"type": "Available", "status": "True"}]}
        metrics: dict[str, Any] = {
            "apiVersion": "metrics.k8s.io/v1beta1",
            "kind": "NodeMetricsList",
            "items": [
                {
                    "metadata": {"name": "perfeng-local-worker"},
                    "timestamp": NOW.isoformat(),
                    "window": "15s",
                    "usage": {"cpu": "1m", "memory": "1Mi"},
                }
            ],
        }
        resource_metrics.validate_health(api, metrics, [node()], "monitoring", NOW)
        metrics["items"][0]["window"] = "1m0s"
        resource_metrics.validate_health(api, metrics, [node()], "monitoring", NOW)
        api["spec"]["insecureSkipTLSVerify"] = False
        resource_metrics.validate_health(api, metrics, [node()], "monitoring", NOW)
        del api["spec"]["insecureSkipTLSVerify"]
        resource_metrics.validate_health(api, metrics, [node()], "monitoring", NOW)
        for failure in ("missing", "stale", "future", "window", "insecure"):
            with self.subTest(failure=failure):
                changed = copy.deepcopy(metrics)
                service = copy.deepcopy(api)
                if failure == "missing":
                    changed["items"] = []
                elif failure == "stale":
                    changed["items"][0]["timestamp"] = (NOW - timedelta(minutes=3)).isoformat()
                elif failure == "future":
                    changed["items"][0]["timestamp"] = (NOW + timedelta(seconds=1)).isoformat()
                elif failure == "window":
                    changed["items"][0]["window"] = "61s"
                else:
                    service["spec"]["insecureSkipTLSVerify"] = True
                with self.assertRaises(ValueError):
                    resource_metrics.validate_health(service, changed, [node()], "monitoring", NOW)
