"""Disposable controller probes at the Kubernetes command boundary."""

import json
import tempfile
import unittest
from copy import deepcopy
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
        drift = ""
        dry_runs = 0

        def kube(target, args, documents=None, **kwargs):
            nonlocal dry_runs
            self.assertEqual(target, self.target)
            if documents is None:
                self.assertEqual(args[0], "get")
                self.assertIn("webhookconfiguration/", args[1])
                verb = "mutate" if "mutating" in args[1] else "validate"
                return json.dumps(
                    {
                        "webhooks": [
                            {
                                "name": ("mcluster" if "mutating" in args[1] else "vcluster")
                                + ".cnpg.io",
                                "failurePolicy": "Fail",
                                "sideEffects": "None",
                                "objectSelector": {"matchLabels": {"test": "only"}}
                                if drift == "selector"
                                else {},
                                "clientConfig": {
                                    "service": {
                                        "namespace": "cnpg-system",
                                        "name": "cnpg-webhook-service",
                                        "path": "/wrong"
                                        if drift == "path"
                                        else f"/{verb}-postgresql-cnpg-io-v1-cluster",
                                        "port": 8443 if drift == "port" else 443,
                                    },
                                    "caBundle": "bXktY2VydGlmaWNhdGU=",
                                },
                                "rules": [
                                    {
                                        "apiGroups": ["postgresql.cnpg.io"],
                                        "apiVersions": ["v1"],
                                        "operations": ["CREATE"],
                                        "resources": ["clusters"],
                                        "scope": "Cluster" if drift == "scope" else "*",
                                    }
                                ],
                            }
                        ]
                    }
                )
            self.assertEqual(args[0], "create")
            self.assertIn("--dry-run=server", args)
            dry_runs += 1
            document = documents[0]
            self.assertEqual(document["kind"], "Cluster")
            self.assertEqual(document["spec"]["imageName"], postgres)
            return json.dumps(document)

        with patch.object(probes, "kube", side_effect=kube):
            result = probes.cnpg_admission(self.target, postgres_image=postgres)
            for drift in ("selector", "scope", "path", "port"):
                with (
                    self.subTest(drift=drift),
                    self.assertRaisesRegex(ValueError, "authority or admission rules"),
                ):
                    probes.cnpg_admission(self.target, postgres_image=postgres)
        self.assertEqual(result, {"outcome": "server-dry-run-accepted", "resource": "Cluster"})
        self.assertEqual(dry_runs, 1)

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

    def test_plugin_discovery_requires_the_reviewed_service_and_current_worker_endpoint(self):
        from scripts import database_assets, database_runtime
        from scripts import minimal_database_probes as probes

        expected: dict = {
            "kind": "Service",
            "metadata": {
                "name": "barman-cloud",
                "namespace": "cnpg-system",
                "labels": {
                    **database_assets.labels(self.target),
                    "cnpg.io/pluginName": "barman-cloud.cloudnative-pg.io",
                },
                "annotations": {
                    "cnpg.io/pluginClientSecret": "barman-cloud-client-tls",
                    "cnpg.io/pluginServerSecret": "barman-cloud-server-tls",
                    "cnpg.io/pluginPort": "9090",
                },
            },
            "spec": {
                "selector": {"app": "barman-cloud"},
                "ports": [{"port": 9090, "protocol": "TCP", "targetPort": 9090}],
            },
        }
        actual = deepcopy(expected)
        actual["metadata"]["uid"] = "plugin-service"
        worker = f"{self.target.name}-worker"
        endpoint = {
            "nodeName": worker,
            "targetRef": {"kind": "Pod", "namespace": "cnpg-system", "uid": "new-pod"},
            "conditions": {"ready": True},
        }
        slice_ = {
            "metadata": {
                "uid": "plugin-endpoints",
                "ownerReferences": [{"kind": "Service", "uid": "plugin-service"}],
            },
            "endpoints": [endpoint],
            "ports": [{"protocol": "TCP", "port": 9090}],
        }
        with (
            patch.object(database_runtime, "kube", return_value=json.dumps(actual)),
            patch.object(probes, "kube", return_value=json.dumps({"items": [slice_]})),
        ):
            self.assertEqual(
                probes.barman_service_evidence(self.target, expected, "new-pod", worker),
                {
                    "serviceUID": "plugin-service",
                    "endpointSliceUID": "plugin-endpoints",
                    "podUID": "new-pod",
                    "port": 9090,
                },
            )
        with (
            patch.object(database_runtime, "kube", return_value=json.dumps(actual)),
            patch.object(probes, "kube", return_value=json.dumps({"items": [slice_]})),
            patch.object(probes.time, "monotonic", side_effect=[0, 91]),
            self.assertRaisesRegex(TimeoutError, "current Barman Pod"),
        ):
            probes.barman_service_evidence(self.target, expected, "replacement-pod", worker)
        actual["metadata"]["annotations"]["cnpg.io/pluginPort"] = "9191"
        with (
            patch.object(database_runtime, "kube", return_value=json.dumps(actual)),
            self.assertRaisesRegex(ValueError, "plugin Service differs"),
        ):
            probes.barman_service_evidence(self.target, expected, "new-pod", worker)

    def test_recovery_certificate_uses_a_fresh_secret_and_create(self):
        from scripts import minimal_database_probes as probes

        recovery = probes.recovery_certificate(self.certificate)
        self.assertNotEqual(recovery["metadata"]["name"], self.certificate["metadata"]["name"])
        self.assertNotEqual(recovery["spec"]["secretName"], self.certificate["spec"]["secretName"])
        self.assertEqual(recovery["spec"]["issuerRef"], self.certificate["spec"]["issuerRef"])
        with patch.object(probes, "kube") as kube:
            probes.create_recovery_certificate(self.target, recovery)
        kube.assert_called_once_with(self.target, ["create", "-f", "-"], [recovery])
