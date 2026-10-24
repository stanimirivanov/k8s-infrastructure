"""Offline controls for disposable native scheduler qualification and failure retention."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import capacity_qualification as qualification
from scripts import consumer_capacity
from scripts.consumer_profile import LABEL, Target
from scripts.node_capacity import envelope, quantity


class CapacityQualificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = Target(
            "qual-capacity-test",
            "local-engine",
            Path(temporary.name).resolve(),
            "isolated-argo-local",
        )
        self.owner: dict = {
            "incarnation": "original-cluster",
            "nonce": "original-claim",
            "nodeIds": {
                self.target.name + "-control-plane": "a" * 64,
                self.target.name + "-worker": "b" * 64,
            },
        }
        self.nodes: dict[str, dict] = {
            role: {
                "metadata": {"uid": role + "-uid"},
                "status": {
                    "capacity": {"cpu": "4", "memory": "16Gi"},
                    "allocatable": {"cpu": "1750m", "memory": "3484Mi", "pods": "110"},
                },
            }
            for role in ("control-plane", "worker")
        }
        self.pods: dict[str, dict] = {}
        self.namespace_exists = False
        self.events = []

    def boundaries(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ("health", "owned"):
            stack.enter_context(
                patch.object(qualification.lifecycle, name, return_value=self.owner)
            )
        stack.enter_context(
            patch.object(qualification.capacity, "observations", return_value=self.nodes)
        )
        stack.enter_context(patch.object(qualification.capacity, "preflight_workloads"))
        stack.enter_context(patch.object(qualification.capacity, "wait_allocatable"))
        reconcile = stack.enter_context(
            patch.object(qualification.capacity, "configure", return_value={"restartedNodes": []})
        )
        stack.enter_context(patch.object(qualification, "kube", side_effect=self.kube))
        process = stack.enter_context(
            patch.object(qualification.runtime, "command", side_effect=self.command)
        )
        return reconcile, process

    def command(self, target, args, *, input_data=None):
        if args[0] == "docker":
            self.events.append(("restart", args[-4]))
        else:
            path = args[args.index("--raw") + 1]
            name = path.rsplit("/", 1)[-1]
            assert isinstance(input_data, bytes)
            self.assertEqual(
                json.loads(input_data)["preconditions"]["uid"], self.pods[name]["metadata"]["uid"]
            )
            self.events.append(("delete", name))
            del self.pods[name]
        return ""

    def kube(self, target, args, documents=None):
        if documents:
            value = copy.deepcopy(documents[0])
            if value["kind"] == "Namespace":
                self.namespace_exists = True
                value["metadata"]["uid"] = "namespace-uid"
            else:
                name = value["metadata"]["name"]
                role = value["spec"]["nodeSelector"]["k8s-infrastructure.io/role"]
                value["metadata"]["uid"] = name + "-uid"
                if name.endswith("positive"):
                    value["spec"]["nodeName"] = f"{target.name}-{role}"
                    value["status"] = {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    }
                else:
                    value["status"] = {
                        "phase": "Pending",
                        "conditions": [
                            {
                                "type": "PodScheduled",
                                "status": "False",
                                "reason": "Unschedulable",
                                "message": "Insufficient " + name.rsplit("-", 1)[-1],
                            }
                        ],
                    }
                self.pods[name] = value
                self.events.append(("create", name))
            return json.dumps(value)
        if "namespace" in args:
            return (
                json.dumps({"metadata": {"uid": "namespace-uid"}}) if self.namespace_exists else ""
            )
        if "get" in args:
            return json.dumps(self.pods[args[args.index("pod") + 1]])
        if "wait" in args:
            return ""
        raise AssertionError(args)

    def test_preview_is_pure_and_cli_requires_explicit_execution(self):
        with (
            patch.object(
                qualification.runtime, "command", side_effect=AssertionError("live process")
            ),
            patch.object(
                qualification.lifecycle, "owned", side_effect=AssertionError("private state")
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = qualification.preview(self.target)
            self.assertEqual(result["execution"], "not-performed")
            self.assertEqual(result["maximumConcurrentPods"], 3)
            self.assertEqual(
                consumer_capacity.main(
                    [
                        "qualify",
                        "--consumer",
                        self.target.consumer,
                        "--profile",
                        self.target.profile,
                        "--docker-context",
                        self.target.docker_context,
                        "--state-root",
                        str(self.target.state_root),
                    ]
                ),
                0,
            )
        self.assertIn("not-performed", output.getvalue())

    def test_retained_consumer_is_refused_before_state_or_process_access(self):
        target = Target("fleetiq", "local-engine", self.target.state_root, self.target.profile)
        with (
            patch.object(qualification.lifecycle, "health") as health,
            self.assertRaisesRegex(ValueError, "disposable"),
        ):
            qualification.qualify(target)
        health.assert_not_called()

    def test_probe_contracts_bound_resources_and_prevent_preemption(self):
        limits = envelope({"cpus": 2, "memoryBytes": 4 * 1024**3})
        for role in self.nodes:
            for probe in ("positive", "cpu", "memory"):
                value = qualification.pod(self.target, role, probe)
                spec = value["spec"]
                self.assertNotIn("nodeName", spec)
                self.assertEqual(spec["preemptionPolicy"], "Never")
                self.assertEqual(spec["nodeSelector"][LABEL], self.target.consumer)
                container = spec["containers"][0]
                self.assertIn("@sha256:", container["image"])
                self.assertFalse(spec["automountServiceAccountToken"])
                requests = container["resources"]["requests"]
                if probe == "cpu":
                    self.assertGreater(quantity(requests["cpu"], cpu=True), limits["cpuMillicores"])
                if probe == "memory":
                    self.assertGreater(quantity(requests["memory"]), limits["memoryBytes"])

    def test_complete_proof_restarts_only_original_ids_and_uid_deletes_probes(self):
        reconcile, process = self.boundaries()
        result = qualification.qualify(self.target)
        self.assertEqual(result["incarnation"], self.owner["incarnation"])
        self.assertEqual(result["reconciliation"], "idempotent")
        self.assertEqual(len(result["schedulerEvidence"]), 2)
        self.assertEqual(reconcile.call_count, 2)
        restarts = [call.args[1] for call in process.call_args_list if call.args[1][0] == "docker"]
        self.assertEqual([args[-4] for args in restarts], list(self.owner["nodeIds"].values()))
        self.assertEqual(len([event for event in self.events if event[0] == "delete"]), 6)
        self.assertEqual(self.pods, {})
        self.assertTrue(self.namespace_exists)

    def test_existing_namespace_and_consumer_workloads_refuse_before_restart(self):
        for mode in ("namespace", "workloads"):
            with self.subTest(mode=mode):
                self.namespace_exists = mode == "namespace"
                _, process = self.boundaries()
                with (
                    patch.object(
                        qualification.capacity,
                        "preflight_workloads",
                        side_effect=ValueError("consumer Pods") if mode == "workloads" else None,
                    ),
                    self.assertRaises(ValueError),
                ):
                    qualification.qualify(self.target)
                process.assert_not_called()

    def test_changed_owner_node_uid_and_capacity_are_refused(self):
        self.boundaries()
        uids = {role: value["metadata"]["uid"] for role, value in self.nodes.items()}
        with self.assertRaisesRegex(ValueError, "owner changed"):
            qualification.guard(self.target, {**self.owner, "nonce": "other"}, uids)
        with self.assertRaisesRegex(ValueError, "node UID changed"):
            qualification.guard(self.target, self.owner, {**uids, "worker": "other"})
        self.nodes["worker"]["status"]["allocatable"]["memory"] = "16Gi"
        with self.assertRaises(ValueError):
            qualification.guard(self.target, self.owner, uids)

    def test_api_failure_and_missing_scheduler_condition_never_pass(self):
        with (
            patch.object(qualification, "observe_pod", side_effect=ValueError("API unavailable")),
            self.assertRaisesRegex(ValueError, "API unavailable"),
        ):
            qualification.wait_probe(self.target, "worker-cpu", "uid", "worker", "cpu")
        pending = {
            "metadata": {"uid": "uid"},
            "spec": {},
            "status": {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "SchedulingGated",
                        "message": "Insufficient cpu",
                    }
                ],
            },
        }
        with (
            patch.object(qualification, "observe_pod", return_value=pending),
            patch.object(qualification.time, "monotonic", side_effect=[0, 121]),
            self.assertRaisesRegex(ValueError, "120s"),
        ):
            qualification.wait_probe(self.target, "worker-cpu", "uid", "worker", "cpu")

    def test_wrong_resource_missing_phase_and_image_failure_are_not_scheduler_proof(self):
        condition = {
            "type": "PodScheduled",
            "status": "False",
            "reason": "Unschedulable",
            "message": "Insufficient cpu",
        }
        for status in (
            {"phase": "Pending", "conditions": [{**condition, "message": "Insufficient memory"}]},
            {"conditions": [condition]},
            {
                "phase": "Pending",
                "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
            },
        ):
            with (
                self.subTest(status=status),
                patch.object(
                    qualification, "observe_pod", return_value={"spec": {}, "status": status}
                ),
                patch.object(qualification.time, "monotonic", side_effect=[0, 121]),
                self.assertRaisesRegex(ValueError, "120s"),
            ):
                qualification.wait_probe(self.target, "worker-cpu", "uid", "worker", "cpu")

    def test_positive_control_must_be_ready_on_selected_node(self):
        for spec, status in (
            ({"nodeName": "foreign-node"}, {"conditions": [{"type": "Ready", "status": "True"}]}),
            (
                {"nodeName": self.target.name + "-worker"},
                {"conditions": [{"type": "Ready", "status": "False"}]},
            ),
        ):
            with (
                self.subTest(spec=spec, status=status),
                patch.object(
                    qualification, "observe_pod", return_value={"spec": spec, "status": status}
                ),
                patch.object(qualification.time, "monotonic", side_effect=[0, 121]),
                self.assertRaisesRegex(ValueError, "120s"),
            ):
                qualification.wait_probe(
                    self.target, "worker-positive", "uid", "worker", "positive"
                )

    def test_bound_negative_pod_or_replaced_fixture_never_passes(self):
        with (
            patch.object(
                qualification,
                "observe_pod",
                return_value={"spec": {"nodeName": "node"}, "status": {"phase": "Pending"}},
            ),
            self.assertRaisesRegex(ValueError, "bound or ran"),
        ):
            qualification.wait_probe(self.target, "worker-cpu", "uid", "worker", "cpu")
        with (
            patch.object(
                qualification, "kube", return_value=json.dumps({"metadata": {"uid": "replacement"}})
            ),
            self.assertRaisesRegex(ValueError, "identity changed"),
        ):
            qualification.observe_pod(self.target, "namespace", "worker-cpu", "uid")

    def test_restart_or_probe_failure_retains_state_and_never_cleans_up(self):
        for phase in ("restart", "probe"):
            with self.subTest(phase=phase):
                self.namespace_exists = False
                self.pods.clear()
                self.events.clear()
                self.boundaries()
                if phase == "restart":
                    boundary = patch.object(
                        qualification.runtime, "command", side_effect=ValueError("restart failed")
                    )
                else:
                    boundary = patch.object(
                        qualification, "wait_probe", side_effect=ValueError("probe failed")
                    )
                with boundary, self.assertRaises(ValueError):
                    qualification.qualify(self.target)
                self.assertFalse(any(event[0] == "delete" for event in self.events))
                self.assertEqual(bool(self.pods), phase == "probe")

    def test_namespace_replacement_and_non_idempotent_repair_refuse(self):
        self.boundaries()
        with (
            patch.object(
                qualification, "kube", return_value=json.dumps({"metadata": {"uid": "replacement"}})
            ),
            self.assertRaisesRegex(ValueError, "namespace identity"),
        ):
            qualification.check_namespace(self.target, "namespace", "original")
        with (
            patch.object(
                qualification.capacity, "configure", return_value={"restartedNodes": ["worker"]}
            ),
            self.assertRaisesRegex(ValueError, "not idempotent"),
        ):
            qualification.qualify(self.target)
        self.assertFalse(self.namespace_exists)
