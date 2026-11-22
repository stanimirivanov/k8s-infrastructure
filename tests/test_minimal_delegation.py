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
