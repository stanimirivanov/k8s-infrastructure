from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from scripts import minimal_delegation
from scripts.consumer_profile import LABEL, MINIMAL_PROFILE, Target
from scripts.minimal_delegation_policy import (
    ATTEMPT_LABEL,
    OWNER_LABEL,
    RECONCILER,
    load_operation,
    render,
)


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
        scope = {"matchLabels": {"kubernetes.io/metadata.name": "qual-demo-apps"}}
        self.assertEqual(
            binding["spec"]["matchResources"],
            {
                "namespaceSelector": scope,
                "objectSelector": {"matchLabels": {ATTEMPT_LABEL: operation.launcher}},
            },
        )
        policy = next(
            obj
            for obj in objects
            if obj["kind"] == "ValidatingAdmissionPolicy"
            and obj["metadata"]["name"].endswith("-job")
        )
        self.assertEqual(policy["spec"]["matchConstraints"]["namespaceSelector"], scope)

    def test_routine_roles_leave_secret_and_cloudnativepg_writes_to_the_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            objects = render(target)
        roles = {
            obj["metadata"]["namespace"]: obj["rules"]
            for obj in objects
            if obj["kind"] == "Role" and obj["metadata"]["name"] == RECONCILER
        }
        for rules in roles.values():
            self.assertFalse(any("secrets" in rule["resources"] for rule in rules))
        database = next(r for r in roles["qual-demo-data"] if r["resources"] == ["clusters"])
        self.assertEqual(database["verbs"], ["get", "list", "watch"])
        for ns in ("qual-demo-apps", "qual-demo-demo-support"):
            workloads = next(r for r in roles[ns] if r["apiGroups"] == ["apps"])
            self.assertIn("create", workloads["verbs"])
            self.assertEqual(workloads["resources"], ["deployments", "statefulsets"])

    def test_configure_refuses_an_existing_attempt_grant_in_another_workload_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            operation = minimal_delegation.qualification_operation(
                target, "probe@sha256:" + "1" * 64
            )
            old = "demo-launch-" + "2" * 24

            def cluster(
                _target: Target, args: list[str], documents: list[dict] | None = None
            ) -> str:
                self.assertIsNone(documents)
                if args[:4] == ["-n", "qual-demo-data", "get", "rolebindings"]:
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "metadata": {
                                        "name": old,
                                        "labels": {
                                            LABEL: target.consumer,
                                            OWNER_LABEL: old,
                                        },
                                    }
                                }
                            ]
                        }
                    )
                return '{"items": []}'

            with (
                patch.object(minimal_delegation, "kube", side_effect=cluster),
                patch.object(minimal_delegation, "check_prerequisites"),
                self.assertRaisesRegex(ValueError, "Another reviewed attempt"),
            ):
                minimal_delegation.configure(target, operation)

    def test_configure_refuses_an_incomplete_job_after_its_grant_was_withdrawn(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            operation = minimal_delegation.qualification_operation(
                target, "probe@sha256:" + "1" * 64
            )

            def cluster(_target: Target, args: list[str]) -> str:
                if args[:4] == ["-n", "qual-demo-data", "get", "jobs"]:
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "metadata": {
                                        "name": "old-attempt",
                                        "labels": {
                                            ATTEMPT_LABEL: "demo-launch-" + "2" * 24,
                                        },
                                    },
                                    "status": {
                                        "conditions": [
                                            {"type": "SuccessCriteriaMet", "status": "True"}
                                        ]
                                    },
                                }
                            ]
                        }
                    )
                return '{"items": []}'

            with (
                patch.object(minimal_delegation, "kube", side_effect=cluster),
                patch.object(minimal_delegation, "check_prerequisites"),
                self.assertRaisesRegex(ValueError, "active or its outcome is incomplete"),
            ):
                minimal_delegation.configure(target, operation)

    def test_configure_observes_an_existing_exact_job_without_recreating_it_or_its_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            operation = minimal_delegation.qualification_operation(
                target, "probe@sha256:" + "1" * 64
            )
            job = operation.job()
            uid = "b3be3cd3-9b8e-4681-9132-ce9e9a78526f"
            job["metadata"]["uid"] = uid
            spec = job["spec"]
            spec.update(
                suspend=False,
                manualSelector=False,
                completionMode="NonIndexed",
                selector={"matchLabels": {"batch.kubernetes.io/controller-uid": uid}},
            )
            spec["template"]["metadata"]["labels"].update(
                {
                    "batch.kubernetes.io/controller-uid": uid,
                    "batch.kubernetes.io/job-name": operation.name,
                    "controller-uid": uid,
                    "job-name": operation.name,
                }
            )
            spec["template"]["spec"]["serviceAccount"] = operation.runtime

            def cluster(_target: Target, args: list[str]) -> str:
                if args[:5] == ["-n", operation.namespace, "get", "job", operation.name]:
                    return json.dumps(job)
                return '{"items": []}'

            with (
                patch.object(minimal_delegation, "kube", side_effect=cluster),
                patch.object(minimal_delegation, "check_prerequisites"),
                patch.object(minimal_delegation.database_runtime, "inventory", return_value={}),
                patch.object(minimal_delegation, "inspect"),
                patch.object(minimal_delegation, "install_group") as install,
                patch.object(minimal_delegation.probe, "prove_routine_admission"),
                patch.object(minimal_delegation.probe, "denied") as denied,
                patch.object(minimal_delegation.probe, "create") as create,
            ):
                minimal_delegation.configure(target, operation)
                self.assertFalse(
                    any(
                        item["kind"] == "RoleBinding"
                        and item["metadata"]["name"] == operation.launcher
                        for call in install.call_args_list
                        for item in call.args[1]
                    )
                )
                self.assertFalse(
                    any(call.args[1]["kind"] == "Job" for call in denied.call_args_list)
                )
                create.assert_not_called()

                install.reset_mock()
                job["spec"]["template"]["spec"]["containers"][0]["args"] = ["different-command"]
                with self.assertRaisesRegex(ValueError, "differs from the exact reviewed attempt"):
                    minimal_delegation.configure(target, operation)
                install.assert_not_called()

    def test_configure_records_last_completed_phase_and_withholds_private_failure_details(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            target.directory.mkdir()

            def fail(
                _target: Target, _operation: object, *, checkpoint: Callable[[str], None]
            ) -> None:
                checkpoint("controls-installed")
                raise ValueError("private raw detail")

            with (
                patch.object(minimal_delegation, "configure", side_effect=fail),
                self.assertRaisesRegex(ValueError, "dependency-failure: delegation-configure"),
            ):
                minimal_delegation.configure_recorded(target, None, "test-incarnation")
            (path,) = target.directory.glob("*.delegation-configure-*.json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            evidence = path.read_text()
            self.assertNotIn("private raw detail", evidence)
            data = json.loads(evidence)
            self.assertEqual(data["lastCompletedPhase"], "controls-installed")
            self.assertEqual(data["outcome"], "failed")

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
