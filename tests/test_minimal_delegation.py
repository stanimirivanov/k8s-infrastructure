from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.consumer_profile import MINIMAL_PROFILE, Target
from scripts.minimal_delegation_policy import load_operation, render


class MinimalDelegationTests(unittest.TestCase):
    def test_launcher_receives_one_named_attempt_without_runtime_or_reconciler_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            operation = load_operation(
                target,
                {
                    "namespace": "qual-demo-apps",
                    "name": "simulate-attempt-1",
                    "deadlineSeconds": 300,
                    "podSpec": {
                        "containers": [
                            {
                                "name": "run",
                                "image": "registry.example.test/probe@sha256:" + "1" * 64,
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "100m", "memory": "128Mi"},
                                },
                            }
                        ]
                    },
                },
            )
            objects = render(target, operation)
        launcher = next(
            obj
            for obj in objects
            if obj["kind"] == "Role" and obj["metadata"]["name"] == operation.launcher
        )
        self.assertEqual(
            launcher["rules"],
            [
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]},
                {
                    "apiGroups": ["batch"],
                    "resources": ["jobs"],
                    "verbs": ["get"],
                    "resourceNames": ["simulate-attempt-1"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["podtemplates"],
                    "verbs": ["get"],
                    "resourceNames": [operation.launcher],
                },
            ],
        )
        template = next(obj for obj in objects if obj["kind"] == "PodTemplate")
        self.assertEqual(template["template"]["spec"]["serviceAccountName"], operation.runtime)
        self.assertFalse(template["template"]["spec"]["automountServiceAccountToken"])
        self.assertTrue(
            all(
                subject["name"] != operation.runtime
                for obj in objects
                if obj["kind"] == "RoleBinding"
                for subject in obj["subjects"]
            )
        )
        binding = next(
            obj
            for obj in objects
            if obj["kind"] == "ValidatingAdmissionPolicyBinding"
            and obj["metadata"]["name"].endswith("-job")
        )
        self.assertEqual(binding["spec"]["paramRef"]["parameterNotFoundAction"], "Deny")
        self.assertEqual(binding["spec"]["paramRef"]["name"], operation.launcher)

    def test_demo_delegation_is_not_rendered_for_existing_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("example", "default", Path(directory), "isolated-network-local", 18443)
            with self.assertRaisesRegex(ValueError, "minimal-demo"):
                render(target)

    def test_cli_job_uses_exact_reviewed_document_without_target_access(self):
        import contextlib
        import io
        import json
        from unittest.mock import patch

        from scripts.minimal_delegation import main

        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "operation.json"
            spec.write_text(
                json.dumps(
                    {
                        "name": "manual-attempt-01",
                        "namespace": "qual-demo-data",
                        "deadlineSeconds": 90,
                        "podSpec": {
                            "containers": [
                                {
                                    "name": "run",
                                    "image": "registry.example.test/probe@sha256:" + "0" * 64,
                                    "command": ["probe", "--execute-one"],
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "192Mi"},
                                        "limits": {"cpu": "1000m", "memory": "384Mi"},
                                    },
                                }
                            ]
                        },
                    }
                )
            )
            args = [
                "job",
                "--consumer",
                "qual-demo",
                "--docker-context",
                "default",
                "--state-root",
                directory,
                "--https-port",
                "18443",
                "--operation",
                str(spec),
            ]
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                patch(
                    "scripts.consumer_runtime.command", side_effect=AssertionError("live access")
                ),
            ):
                self.assertEqual(main(args), 0)
            job = json.loads(output.getvalue())
            pod = job["spec"]["template"]["spec"]
            self.assertEqual(job["metadata"]["name"], "manual-attempt-01")
            self.assertEqual(job["metadata"]["namespace"], "qual-demo-data")
            self.assertEqual(job["spec"]["backoffLimit"], 0)
            self.assertEqual(job["spec"]["activeDeadlineSeconds"], 90)
            self.assertEqual(pod["nodeSelector"]["k8s-infrastructure.io/role"], "worker")
            self.assertEqual(pod["containers"][0]["command"], ["probe", "--execute-one"])

            oversized = json.loads(spec.read_text())
            oversized["podSpec"]["containers"][0]["resources"]["requests"]["memory"] = "193Mi"
            spec.write_text(json.dumps(oversized))
            with contextlib.redirect_stdout(io.StringIO()) as refusal:
                self.assertEqual(main(args), 1)
            self.assertIn("exceeds its namespace allocation", refusal.getvalue())
