from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import minimal_delegation
from scripts.consumer_profile import LABEL, MINIMAL_PROFILE, Target
from scripts.local_kind import CommandFailure
from scripts.minimal_delegation_policy import (
    ATTEMPT_LABEL,
    OWNER_LABEL,
    RECONCILER,
    load_operation,
    render,
)


class MinimalDelegationTests(unittest.TestCase):
    def test_neutral_job_uses_observer_uid_from_the_pre_submit_list_version(self):
        target = Target("qual-demo", "default", Path("/tmp"), MINIMAL_PROFILE, 18443)
        operation = minimal_delegation.qualification_operation(
            target,
            "probe@sha256:" + "1" * 64,
            observer={"kind": "ServiceAccount", "namespace": "deployment", "name": "observer"},
        )
        submitted = False
        observed_uid = "server-assigned-uid"
        job = operation.job()
        job["metadata"]["uid"] = observed_uid
        job["status"] = {"succeeded": 1}
        actions = []
        observer = "--as=system:serviceaccount:deployment:observer"

        def api(_target, args, documents=None, **_kwargs):
            nonlocal submitted
            if "--raw" in args:
                self.assertIn(observer, args)
                uri = urlsplit(args[args.index("--raw") + 1])
                self.assertTrue(
                    uri.path.startswith(f"/apis/batch/v1/namespaces/{operation.namespace}/jobs")
                )
                if uri.path.endswith("/" + operation.name):
                    self.assertTrue(submitted)
                    actions.append("get")
                    return json.dumps(job)
                query = parse_qs(uri.query)
                self.assertEqual(query["fieldSelector"], [f"metadata.name={operation.name}"])
                if query.get("watch") == ["true"]:
                    self.assertTrue(submitted)
                    self.assertEqual(query["resourceVersion"], ["401"])
                    actions.append("watch")
                    return json.dumps({"type": "ADDED", "object": job}) + "\n"
                self.assertFalse(submitted)
                actions.append("list")
                return '{"kind":"JobList","metadata":{"resourceVersion":"401"},"items":[]}'
            if "create" in args:
                self.assertFalse(submitted)
                self.assertNotIn(observer, args)
                self.assertEqual(documents, [operation.job()])
                submitted = True
                actions.append("submit")
                self.assertEqual(args[-2:], ["-o", "name"])
                return "job.batch/delegation-job-probe\n"
            if "wait" in args:
                return ""
            if "job" in args:
                return json.dumps(job)
            self.assertIn(f"batch.kubernetes.io/controller-uid={observed_uid}", args)
            pod = {
                "metadata": {
                    "uid": "exact-pod",
                    "ownerReferences": [{"controller": True, "uid": observed_uid}],
                },
                "spec": {**operation.pod, "nodeName": target.name + "-worker"},
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {"restartCount": 0, "state": {"terminated": {"exitCode": 0}}}
                    ],
                },
            }
            return json.dumps({"items": [pod]})

        with patch.object(minimal_delegation.probe, "kube", side_effect=api):
            evidence = minimal_delegation.probe.run_job(target, operation)
        self.assertEqual(actions, ["list", "submit", "watch", "get"])
        self.assertEqual(evidence["jobUID"], observed_uid)
        self.assertEqual(evidence["observer"]["baselineResourceVersion"], "401")

    def test_observer_scope_requires_typed_denial_for_the_exact_raw_request(self):
        target = Target("qual-demo", "default", Path("/tmp"), MINIMAL_PROFILE, 18443)
        operation = minimal_delegation.qualification_operation(
            target,
            "probe@sha256:" + "1" * 64,
            observer={"kind": "ServiceAccount", "namespace": "deployment", "name": "observer"},
        )
        reviews = []

        def access_review(_target, _args, *, input_data):
            reviews.append(json.loads(input_data))
            return '{"status": {"allowed": true}}'

        forbidden = CommandFailure("kubectl refused", denial=("authorization", "forbidden"))

        def raw_request(_target, args, **_kwargs):
            self.assertIn(
                f"/apis/batch/v1/namespaces/{operation.namespace}/jobs/other-delegation-job",
                args,
            )
            raise forbidden

        with (
            patch.object(minimal_delegation.probe, "kube", side_effect=raw_request),
            patch.object(minimal_delegation.runtime, "command", side_effect=access_review),
            self.assertRaisesRegex(ValueError, "SubjectAccessReview"),
        ):
            minimal_delegation.probe.prove_observer_scope(target, operation, "foreign")
        self.assertEqual(
            [review["spec"]["resourceAttributes"] for review in reviews],
            [
                {
                    "verb": "get",
                    "resource": "jobs",
                    "group": "batch",
                    "namespace": operation.namespace,
                    "name": "other-delegation-job",
                }
            ],
        )

    def test_observer_revocation_checks_the_selector_scope_and_refuses_a_timed_out_watch(self):
        target = Target("qual-demo", "default", Path("/tmp"), MINIMAL_PROFILE, 18443)
        operation = minimal_delegation.qualification_operation(
            target,
            "probe@sha256:" + "1" * 64,
            observer={"kind": "ServiceAccount", "namespace": "deployment", "name": "observer"},
        )
        scopes = []
        watch_timeout = False

        def access_review(_target, _args, *, input_data):
            scopes.append(json.loads(input_data)["spec"]["resourceAttributes"])
            return '{"status": {"allowed": false}}'

        def raw_request(_target, args, **_kwargs):
            url = urlsplit(args[args.index("--raw") + 1])
            options = parse_qs(url.query)
            if options:
                self.assertEqual(options["fieldSelector"], [f"metadata.name={operation.name}"])
            if options.get("watch") == ["true"] and watch_timeout:
                raise CommandFailure("watch deadline exhausted", timed_out=True)
            raise CommandFailure("kubectl refused", denial=("authorization", "forbidden"))

        with (
            patch.object(minimal_delegation.probe, "kube", side_effect=raw_request),
            patch.object(minimal_delegation.runtime, "command", side_effect=access_review),
        ):
            minimal_delegation.probe.prove_observer_revoked(target, operation)
            self.assertEqual([scope["verb"] for scope in scopes], ["get", "get", "list", "watch"])
            self.assertTrue(
                all(
                    scope["name"] == operation.name
                    and scope["namespace"] == operation.namespace
                    and scope["resource"] == "jobs"
                    and scope["group"] == "batch"
                    for scope in scopes
                )
            )
            watch_timeout = True
            scopes.clear()
            with self.assertRaisesRegex(CommandFailure, "watch deadline exhausted"):
                minimal_delegation.probe.prove_observer_revoked(target, operation)
            self.assertEqual([scope["verb"] for scope in scopes], ["get", "get", "list"])

    def test_withdrawal_refuses_an_inflight_job_and_does_not_regrant_an_unused_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            target.directory.mkdir()
            operation = minimal_delegation.qualification_operation(
                target,
                "probe@sha256:" + "1" * 64,
                observer={"kind": "ServiceAccount", "namespace": "deployment", "name": "observer"},
            )
            job = operation.job()
            uid = "b3be3cd3-9b8e-4681-9132-ce9e9a78526f"
            job["metadata"]["uid"] = uid
            job["spec"].update(
                suspend=False,
                manualSelector=False,
                completionMode="NonIndexed",
                selector={"matchLabels": {"batch.kubernetes.io/controller-uid": uid}},
            )
            job["spec"]["template"]["metadata"]["labels"].update(
                {
                    "batch.kubernetes.io/controller-uid": uid,
                    "batch.kubernetes.io/job-name": operation.name,
                    "controller-uid": uid,
                    "job-name": operation.name,
                }
            )
            job_raw = json.dumps(job)
            objects = {}
            for item in render(target, operation):
                key = minimal_delegation.reference(item)
                item["metadata"].update(
                    uid="uid-" + "-".join(x for x in key if x), resourceVersion="99"
                )
                objects[key] = item
            deleted = []
            interrupt = True
            path = minimal_delegation.withdrawal_path(target, operation)

            def delete(_target, args, *, input_data):
                nonlocal interrupt, job_raw
                endpoint = args[args.index("--raw") + 1]
                preconditions = json.loads(input_data)["preconditions"]
                found = next(
                    key
                    for key, item in objects.items()
                    if endpoint.endswith("/" + key[1])
                    and item["metadata"]["uid"] == preconditions["uid"]
                )
                self.assertEqual(preconditions["resourceVersion"], "99")
                deleted.append(found)
                objects.pop(found)
                if found[:2] == ("RoleBinding", operation.observer_binding) and interrupt:
                    interrupt = False
                    raise OSError("interrupted after observer binding deletion")
                if found == ("RoleBinding", operation.launcher, operation.namespace):
                    job_raw = json.dumps(job)
                return "{}"

            def cluster(_target, args):
                if args[:5] == ["-n", operation.namespace, "get", "job", operation.name]:
                    return job_raw
                if args[:4] == ["-n", operation.namespace, "get", "pods"]:
                    return '{"items": []}'
                self.fail(args)

            with (
                patch.object(minimal_delegation, "kube", side_effect=cluster),
                patch.object(minimal_delegation, "check_prerequisites"),
                patch.object(
                    minimal_delegation.database_runtime,
                    "inventory",
                    side_effect=lambda _, desired: {
                        key: objects[key]
                        for item in desired
                        if (key := minimal_delegation.reference(item)) in objects
                    },
                ),
                patch.object(minimal_delegation.probe, "allowed", return_value=False),
                patch.object(minimal_delegation.runtime, "command", side_effect=delete),
            ):
                with self.assertRaisesRegex(ValueError, "unused or terminal"):
                    minimal_delegation.withdraw(target, operation, "incarnation")
                self.assertEqual(deleted, [])
                job_raw = ""
                with self.assertRaisesRegex(ValueError, "unused or terminal"):
                    minimal_delegation.withdraw(target, operation, "incarnation")
                self.assertEqual(
                    json.loads(path.read_text())["lastCompletedPhase"], "launcher-revoked"
                )
                with self.assertRaisesRegex(ValueError, "withdrawal"):
                    minimal_delegation.configure(target, operation)

                job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
                job_raw = json.dumps(job)
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    minimal_delegation.withdraw(target, operation, "incarnation")
                partial = json.loads(path.read_text())
                self.assertEqual(partial["lastCompletedPhase"], "launcher-revoked")
                self.assertIsNone(partial["jobUID"])
                key = "RoleBinding", operation.observer_binding, operation.namespace
                original_binding = objects[key]

                # Model the provider accounting for the late Job and the operator
                # removing it with UID-scoped cleanup before resuming withdrawal.
                job_raw = ""
                with self.assertRaisesRegex(OSError, "interrupted after observer binding deletion"):
                    minimal_delegation.withdraw(target, operation, "incarnation")
                self.assertNotIn(key, objects)
                interrupted = path.read_text()
                partial = json.loads(interrupted)
                self.assertEqual(partial["lastCompletedPhase"], "execution-rechecked")
                partial["lastCompletedPhase"] = "unknown"
                path.write_text(json.dumps(partial))
                with self.assertRaisesRegex(ValueError, "Withdrawal receipt does not match"):
                    minimal_delegation.withdraw(target, operation, "incarnation")
                path.write_text(interrupted)
                objects[key] = original_binding
                original_binding["metadata"]["uid"] = "replacement-uid"
                with self.assertRaisesRegex(ValueError, "replaced"):
                    minimal_delegation.withdraw(target, operation, "incarnation")
                self.assertIn(key, objects)
                objects.pop(key)
                receipt = minimal_delegation.withdraw(target, operation, "incarnation")
                self.assertEqual(receipt["outcome"], "withdrawn")
                self.assertEqual(receipt["lastCompletedPhase"], "completion")
                self.assertIsNone(receipt["jobUID"])
                self.assertEqual(
                    deleted,
                    [
                        ("RoleBinding", operation.launcher, operation.namespace),
                        ("RoleBinding", operation.observer_binding, operation.namespace),
                    ],
                )
                for kind in ("ValidatingAdmissionPolicyBinding", "PodTemplate", "ServiceAccount"):
                    self.assertTrue(
                        any(
                            item["kind"] == kind
                            and item["metadata"]["labels"][OWNER_LABEL] == operation.launcher
                            for item in objects.values()
                        )
                    )
                self.assertEqual(
                    minimal_delegation.withdraw(target, operation, "incarnation"), receipt
                )
                with self.assertRaisesRegex(ValueError, "withdrawal"):
                    minimal_delegation.configure(target, operation)

    def test_observer_is_opt_in_and_exact_job_authority_is_bound_to_the_review(self):
        target = Target("qual-demo", "default", Path("/tmp"), MINIMAL_PROFILE, 18443)
        reviewed = {
            "namespace": "qual-demo-apps",
            "name": "neutral-attempt",
            "deadlineSeconds": 60,
            "podSpec": {
                "containers": [
                    {
                        "name": "run",
                        "image": "probe@sha256:" + "1" * 64,
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "100m", "memory": "64Mi"},
                        },
                    }
                ]
            },
        }
        subject = {"kind": "ServiceAccount", "namespace": "deployment", "name": "observer"}
        legacy = load_operation(target, reviewed)
        operation = load_operation(target, {**reviewed, "observer": subject})
        self.assertNotEqual(operation.digest, legacy.digest)
        self.assertEqual(
            legacy.digest, "sha256:0710ee191080bee6da77edf3c4f949230f3b1095ed499018a43a55013cca2f5b"
        )
        name = operation.launcher + "-observer"
        added = [item for item in render(target, operation) if item["metadata"]["name"] == name]
        self.assertEqual(len(added), 2)
        role, grant = added
        for item in added:
            self.assertEqual(item["metadata"]["namespace"], "qual-demo-apps")
            self.assertEqual(item["metadata"]["labels"][OWNER_LABEL], operation.launcher)
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": ["batch"],
                    "resources": ["jobs"],
                    "verbs": ["get", "list", "watch"],
                    "resourceNames": ["neutral-attempt"],
                }
            ],
        )
        self.assertEqual(grant["subjects"], [subject])
        self.assertEqual(grant["roleRef"]["name"], name)
        self.assertFalse(
            any(item["metadata"]["name"].endswith("-observer") for item in render(target, legacy))
        )
        self.assertEqual(
            minimal_delegation.preview(target, "configure", operation)["operation"]["observer"],
            {"subject": subject, "roleBinding": name},
        )
        self.assertEqual(
            minimal_delegation.preview(target, "withdraw", operation)["resources"],
            [
                ("RoleBinding", name, operation.namespace),
                ("RoleBinding", operation.launcher, operation.namespace),
            ],
        )

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

    def test_configure_refuses_to_hand_off_a_used_attempt_without_the_observer_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-demo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            operation = minimal_delegation.qualification_operation(
                target,
                "probe@sha256:" + "1" * 64,
                observer={"kind": "User", "name": "deployment-observer"},
            )
            with (
                patch.object(minimal_delegation, "check_prerequisites"),
                patch.object(minimal_delegation, "preflight_attempt", return_value=True),
                patch.object(minimal_delegation.database_runtime, "inventory", return_value={}),
                patch.object(minimal_delegation, "inspect"),
                patch.object(minimal_delegation, "install_group") as install,
                patch.object(minimal_delegation.probe, "prove_routine_admission"),
                patch.object(minimal_delegation.probe, "denied"),
                self.assertRaisesRegex(ValueError, "observer binding"),
            ):
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
