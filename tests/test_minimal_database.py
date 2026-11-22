"""Public staged database qualification behavior with synthetic Kubernetes responses."""

import json
import unittest
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.consumer_profile import Target, identity
from tests import test_database


class MinimalDatabaseTests(unittest.TestCase):
    def test_failed_network_receipt_refuses_database_writes_and_preserves_receipt(self):
        from scripts import bootstrap_failure, minimal_database

        with TemporaryDirectory() as directory:
            target = Target("qual-db-test", "local-engine", Path(directory), "minimal-demo", 18443)
            target.directory.mkdir()
            path = target.directory / f"{target.name}.network-qualification.json"
            cold = {
                "artifactDigest": "sha256:" + "a" * 64,
                "sourceProvenance": {},
                "nodeImage": "pinned",
                "toolVersions": {},
            }
            previous = {
                **identity(target),
                **cold,
                "incarnation": "owned",
                "outcome": "failed",
                "lastCompletedPhase": "fixture-admission",
                "installedNetworkQualified": False,
            }
            path.write_text(json.dumps(previous))
            with (
                patch.object(
                    minimal_database.minimal_bootstrap,
                    "network_operation",
                    return_value=nullcontext(),
                ),
                patch.object(
                    minimal_database.minimal_bootstrap, "network_bootstrap", return_value=(cold, [])
                ),
                patch.object(minimal_database.runtime, "incarnation", return_value="owned"),
                patch.object(minimal_database.database_runtime, "apply_objects") as apply,
                self.assertRaises(bootstrap_failure.Failure) as refused,
            ):
                minimal_database.qualify(target, {}, "boot")
            self.assertEqual(refused.exception.evidence["phase"], "network-continuation")
            self.assertEqual(json.loads(path.read_text()), previous)
            self.assertEqual(list(target.directory.iterdir()), [path])
            apply.assert_not_called()

    def test_staged_envelope_accounts_retained_network_fixtures(self):
        from scripts import database_assets, minimal_database_resources

        target = Target("qual-db-test", "local-engine", Path("/tmp"), "minimal-demo", 18443)
        groups = database_assets.render(target, test_database.DatabaseTests().sources())
        report = minimal_database_resources.rendered_bounds(target, groups)
        worker = report["startupByController"]["barman-cloud"]["worker"]
        # 100m system + 200m Cilium + 20m fixtures + 300m database.
        self.assertEqual(worker["cpuRequestedMillicores"], 620)
        # 256 system + 896 Cilium + 256 fixtures + 1024 database.
        self.assertEqual(worker["activeMemoryLimitBytes"], 2432 * 1024**2)
        self.assertEqual(report["oneReplacementByController"]["cert-manager"]["worker"], worker)

    def test_initializer_peak_uses_only_the_reviewed_extra_memory(self):
        from scripts import database_assets, minimal_database_resources

        target = Target("qual-db-test", "local-engine", Path("/tmp"), "minimal-demo", 18443)
        groups = database_assets.render(target, test_database.DatabaseTests().sources())
        barman = next(item for item in groups["barman"] if item["kind"] == "Deployment")
        budget = {"cpu": "200m", "memory": "640Mi"}
        barman["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "setup", "resources": {"requests": budget, "limits": budget}}
        ]
        report = minimal_database_resources.rendered_bounds(target, groups)
        replacement = report["oneReplacementByController"]["barman-cloud"]["worker"]
        self.assertEqual(replacement["activeMemoryLimitBytes"], 2944 * 1024**2)
        self.assertEqual(replacement["cpuRequestedMillicores"], 770)
        budget["memory"] = "641Mi"
        with self.assertRaisesRegex(ValueError, "initialization exceeds"):
            minimal_database_resources.rendered_bounds(target, groups)

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
            pod = {"metadata": {"uid": "old-pod"}}
            return json.dumps({"items": [pod] if observations.count(["get", "pods"]) == 1 else []})

        with patch("scripts.minimal_database.kube", side_effect=api), patch("time.sleep"):
            restart_controller(
                target, deployment, "deployment", "old-pod", lambda: observations.append("drained")
            )
        self.assertEqual(observations.count(["patch", "deployment"]), 2)
        self.assertEqual(observations.count(["get", "pods"]), 2)
        self.assertLess(observations.index("drained"), len(observations) - 1)
        self.assertEqual(desired, 1)

    def test_controller_admission_normalizes_api_quantities_and_refuses_generated_sidecar(self):
        from scripts import database_assets
        from scripts.minimal_database import admit_pod

        target = Target("qual-db-test", "local-engine", Path("/tmp"), "minimal-demo", 18443)
        groups = database_assets.render(target, test_database.DatabaseTests().sources())
        deployment = next(item for item in groups["barman"] if item["kind"] == "Deployment")

        def api(_target, args, documents):
            self.assertEqual(args, ["create", "--dry-run=server", "-f", "-", "-o", "json"])
            (pod,) = deepcopy(documents)
            limits = pod["spec"]["containers"][0]["resources"]["limits"]
            limits.update(cpu="0.5", memory="134217728")
            if injected:
                pod["spec"]["initContainers"] = [
                    {"name": "generated-sidecar", "restartPolicy": "Always", "image": "foreign"}
                ]
            return json.dumps(pod)

        injected = False
        with patch("scripts.minimal_database.kube", side_effect=api):
            self.assertEqual(admit_pod(target, deployment)["effectiveLimits"]["memory"], 134217728)
            injected = True
            with self.assertRaisesRegex(ValueError, "lacks finite"):
                admit_pod(target, deployment)
