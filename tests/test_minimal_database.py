"""Public staged database qualification behavior with synthetic Kubernetes responses."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.consumer_profile import Target
from tests.test_database import DatabaseTests


class MinimalDatabaseTests(unittest.TestCase):
    def test_staged_envelope_accounts_retained_network_fixtures(self):
        from scripts import database_assets, minimal_database_resources

        target = Target("qual-db-test", "local-engine", Path("/tmp"), "minimal-demo", 18443)
        groups = database_assets.render(target, DatabaseTests().sources())
        report = minimal_database_resources.rendered_bounds(target, groups)
        worker = report["startupByController"]["barman-cloud"]["worker"]
        # 100m system + 200m Cilium + 20m fixtures + 300m database.
        self.assertEqual(worker["cpuRequestedMillicores"], 620)
        # 256 system + 896 Cilium + 256 fixtures + 1024 database.
        self.assertEqual(worker["activeMemoryLimitBytes"], 2432 * 1024**2)
        self.assertEqual(report["oneReplacementByController"]["cert-manager"]["worker"], worker)

    def test_replacement_starts_only_after_old_pod_and_replica_drain(self):
        from scripts.minimal_database import restart_controller

        target = Target("qual-db-test", "local-engine", Path("/tmp"), "minimal-demo", 18443)
        deployment = {"metadata": {"name": "barman-cloud", "namespace": "cnpg-system"}}
        desired = 1
        observations = []

        def api(_target, args, *unused):
            nonlocal desired
            observations.append(args[2:4])
            if args[2:4] == ["get", "deployment"]:
                return json.dumps(
                    {
                        "metadata": {
                            "uid": "deployment",
                            "resourceVersion": str(len(observations)),
                        },
                        "spec": {"replicas": desired},
                    }
                )
            if args[2:4] == ["patch", "deployment"]:
                changes = json.loads(args[-1])
                self.assertIn(
                    {"op": "test", "path": "/metadata/uid", "value": "deployment"}, changes
                )
                desired = changes[-1]["value"]
                return ""
            if args[2:4] == ["get", "replicasets"]:
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "uid": "replica",
                                    "ownerReferences": [{"uid": "deployment", "controller": True}],
                                },
                                "spec": {"replicas": 0},
                            }
                        ]
                    }
                )
            self.assertEqual(args[2:4], ["get", "pods"])
            pod = {"metadata": {"ownerReferences": [{"uid": "replica", "controller": True}]}}
            return json.dumps({"items": [pod] if observations.count(["get", "pods"]) == 1 else []})

        with patch("scripts.minimal_database.kube", side_effect=api), patch("time.sleep"):
            restart_controller(
                target, deployment, "deployment", lambda: observations.append("drained")
            )
        self.assertEqual(observations.count(["patch", "deployment"]), 2)
        self.assertEqual(observations.count(["get", "pods"]), 2)
        self.assertLess(observations.index("drained"), len(observations) - 1)
        self.assertEqual(desired, 1)
