"""Disposable controller probes at the Kubernetes command boundary."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.consumer_profile import Target


class MinimalDatabaseProbesTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.target = Target("qual-db", "default", Path(state.name), "minimal-demo", 18443)
        self.certificate: dict = {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"namespace": "cnpg-system", "name": "barman-cloud-server"},
            "spec": {
                "commonName": "barman-cloud",
                "issuerRef": {
                    "group": "cert-manager.io",
                    "kind": "Issuer",
                    "name": "selfsigned-issuer",
                },
                "dnsNames": ["barman-cloud"],
                "usages": ["server auth"],
                "secretName": "barman-cloud-server-tls",
            },
        }

    def test_native_admission_requires_server_dry_run_for_the_frozen_image(self):
        from scripts import minimal_database_probes as probes

        postgres = "ghcr.io/cloudnative-pg/postgresql:18.2@sha256:" + "a" * 64
        bypass = False

        def kube(target, args, documents=None, **kwargs):
            self.assertEqual(target, self.target)
            if documents is None:
                self.assertEqual(args[0], "get")
                self.assertIn("webhookconfiguration/", args[1])
                return json.dumps(
                    {
                        "webhooks": [
                            {
                                "name": ("mcluster" if "mutating" in args[1] else "vcluster")
                                + ".cnpg.io",
                                "failurePolicy": "Fail",
                                "sideEffects": "None",
                                "objectSelector": {"matchLabels": {"test": "only"}}
                                if bypass
                                else {},
                                "clientConfig": {
                                    "service": {
                                        "namespace": "cnpg-system",
                                        "name": "cnpg-webhook-service",
                                    },
                                    "caBundle": "bXktY2VydGlmaWNhdGU=",
                                },
                                "rules": [
                                    {
                                        "apiGroups": ["postgresql.cnpg.io"],
                                        "apiVersions": ["v1"],
                                        "operations": ["CREATE"],
                                        "resources": ["clusters"],
                                    }
                                ],
                            }
                        ]
                    }
                )
            self.assertEqual(args[0], "create")
            self.assertIn("--dry-run=server", args)
            document = documents[0]
            self.assertEqual(document["kind"], "Cluster")
            self.assertEqual(document["spec"]["imageName"], postgres)
            return json.dumps(document)

        with patch.object(probes, "kube", side_effect=kube):
            result = probes.cnpg_admission(self.target, postgres_image=postgres)
            bypass = True
            with self.assertRaisesRegex(ValueError, "authority or admission rules"):
                probes.cnpg_admission(self.target, postgres_image=postgres)
        self.assertEqual(result, {"outcome": "server-dry-run-accepted", "resource": "Cluster"})

    def test_certificate_evidence_refuses_a_stale_ready_condition(self):
        from scripts import minimal_database_probes as probes

        issued = {
            **self.certificate,
            "metadata": {**self.certificate["metadata"], "uid": "certificate-uid", "generation": 2},
            "status": {
                "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}],
                "revision": 1,
                "notBefore": "2026-11-22T10:00:00Z",
                "notAfter": "2026-12-22T10:00:00Z",
            },
        }
        with (
            patch.object(probes, "wait_condition"),
            patch.object(probes, "kube", return_value=json.dumps(issued)),
            self.assertRaisesRegex(ValueError, "generation"),
        ):
            probes.certificate_evidence(self.target, [self.certificate])

    def test_recovery_certificate_uses_a_fresh_secret_and_create(self):
        from scripts import minimal_database_probes as probes

        recovery = probes.recovery_certificate(self.certificate)
        self.assertNotEqual(recovery["metadata"]["name"], self.certificate["metadata"]["name"])
        self.assertNotEqual(recovery["spec"]["secretName"], self.certificate["spec"]["secretName"])
        self.assertEqual(recovery["spec"]["issuerRef"], self.certificate["spec"]["issuerRef"])
        with patch.object(probes, "kube") as kube:
            probes.create_recovery_certificate(self.target, recovery)
        kube.assert_called_once_with(self.target, ["create", "-f", "-"], [recovery])
