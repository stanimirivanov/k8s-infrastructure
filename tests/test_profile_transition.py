"""Retained transition proofs with real private files and fake native boundaries."""

from __future__ import annotations

import base64
import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import consumer_capacity as capacity
from scripts import consumer_lifecycle as lifecycle
from scripts import consumer_runtime as native
from scripts import consumer_state as owner
from scripts import consumer_transition as cli
from scripts import profile_transition_model as model
from scripts import profile_transition_private as private
from scripts import profile_transition_runtime as transition
from scripts import profile_transition_state as store
from scripts.consumer_profile import LABEL, Target, identity, load_profile
from scripts.node_capacity import envelope, kubelet_configuration, verify_allocatable

REVISION = "c" * 40
INCARNATION = "e8a7c276-73ff-4cee-bae6-867ac7ace49a"
OPERATION = "a" * 32
OLD_MACHINE = {"cpu": "4", "memory": "16Gi"}
NEW_MACHINE = {"cpu": "8", "memory": "32Gi"}
BASE = {
    "kind": "KubeletConfiguration",
    "apiVersion": "kubelet.config.k8s.io/v1beta1",
    "cgroupDriver": "systemd",
    "privateMarker": "private-config-never-print",
}


class TransitionFixture(unittest.TestCase):
    CONSUMER = "neutral"
    SUCCESSOR_PROFILE = model.SUCCESSOR
    CURRENT_MACHINE = NEW_MACHINE

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = Target(self.CONSUMER, "local-engine", self.root, model.PREDECESSOR, 18443)
        self.successor = model.paired(self.target, successor_profile=self.SUCCESSOR_PROFILE)
        self.configs = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.statuses: dict[str, dict[str, Any]] = {}
        self.backups = {}
        self.events = []
        self.failure = None
        request_nodes = {}
        for index, role in enumerate(model.ROLES, 1):
            name = f"{self.target.name}-{role}"
            node_id = str(index) * 64
            uid = f"00000000-0000-4000-8000-{index:012d}"
            budget = load_profile(name=self.target.profile)["nodes"][role]
            self.configs[node_id] = model.encoded(kubelet_configuration(BASE, OLD_MACHINE, budget))
            self.containers[name] = {
                "id": node_id,
                "role": role,
                "cpu": budget["cpus"] * 10**9,
                "memory": budget["memoryBytes"],
                "pids": 2048,
                "swap": budget["memoryBytes"],
            }
            self.statuses[role] = {
                "metadata": {
                    "name": name,
                    "uid": uid,
                    "labels": {
                        LABEL: self.target.consumer,
                        "k8s-infrastructure.io/role": role,
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                    },
                },
                "status": {
                    # Node status includes resources outside the reviewed CPU/memory pair.
                    "capacity": {
                        **self.CURRENT_MACHINE,
                        "ephemeral-storage": "1055762868Ki",
                        "hugepages-1Gi": "0",
                        "hugepages-2Mi": "0",
                        "pods": "110",
                    },
                    "nodeInfo": {"kubeletVersion": "v1.35.0"},
                    "allocatable": {"cpu": "5750m", "memory": "19Gi", "pods": "110"},
                    "conditions": [
                        {"type": k, "status": v}
                        for k, v in (
                            ("Ready", "True"),
                            ("MemoryPressure", "False"),
                            ("DiskPressure", "False"),
                            ("PIDPressure", "False"),
                        )
                    ],
                },
            }
            request_nodes[role] = {
                "containerId": node_id,
                "uid": uid,
                "oldConfigSha256": model.digest(self.configs[node_id]),
                "oldMachineCapacity": copy.deepcopy(OLD_MACHINE),
                "newMachineCapacity": copy.deepcopy(self.CURRENT_MACHINE),
            }
        self.request: dict[str, Any] = {
            "format": model.FORMAT,
            "operationId": OPERATION,
            "providerRevision": REVISION,
            "predecessorDigest": identity(self.target)["profileDigest"],
            "successorDigest": identity(self.successor)["profileDigest"],
            "incarnation": INCARNATION,
            "nodes": request_nodes,
        }
        record = owner.reserve(self.target, "unix:///engine.sock")
        record.update(
            phase="ready",
            incarnation=INCARNATION,
            nodeIds={name: n["id"] for name, n in self.containers.items()},
        )
        owner.save(self.target, record)
        self.target.path("kubeconfig").write_bytes(b"private-access-fixture\r\n")
        self.original_owner = self.target.path("owner.json").read_bytes()
        self.original_kind = self.target.path("kind.yaml").read_bytes()
        self.original_access = self.target.path("kubeconfig").read_bytes()
        stack = ExitStack()
        self.addCleanup(stack.close)
        patches = (
            patch.object(native, "prerequisites", return_value="unix:///engine.sock"),
            patch.object(native, "engine", return_value="unix:///engine.sock"),
            patch.object(native, "clusters", return_value=[self.target.name]),
            patch.object(native, "incarnation", return_value=INCARNATION),
            patch.object(native, "verify_context"),
            patch.object(
                native, "node_identity", side_effect=lambda _: copy.deepcopy(self.containers)
            ),
            patch.object(native, "command", side_effect=self.command),
            patch.object(native, "read_readiness", side_effect=self.readiness),
            patch.object(
                lifecycle,
                "source_identity",
                return_value={"providerRevision": REVISION, "providerDirty": False},
            ),
            patch.object(
                capacity, "observations", side_effect=lambda _: copy.deepcopy(self.statuses)
            ),
            patch.object(capacity, "preflight_workloads"),
            patch.object(capacity, "read_configuration", side_effect=self.config),
            patch.object(private, "read_configuration", side_effect=self.config),
            patch.object(private, "execute", side_effect=self.private_command),
            patch.object(
                transition,
                "foundation",
                return_value=([["Namespace", "", "kube-system", INCARNATION, 0]], True),
            ),
        )
        for selected in patches:
            stack.enter_context(selected)

    def config(self, _target, node_id):
        content = self.configs[node_id]
        return model.digest(content), json.loads(content)

    def interrupt(self, point):
        if self.failure == point:
            self.failure = None
            raise ValueError("injected private-config-never-print interruption")

    def command(self, _target, args, **_kwargs):
        node_id = args[-1] if "update" in args else args[args.index("exec") + 1]
        role = next(
            role for role, n in self.request["nodes"].items() if n["containerId"] == node_id
        )
        self.events.append(("cap" if "update" in args else "restart", role))
        if "update" in args:
            budget = load_profile(name=self.successor.profile)["nodes"][role]
            self.containers[f"{self.target.name}-{role}"].update(
                cpu=budget["cpus"] * 10**9, memory=budget["memoryBytes"], swap=budget["memoryBytes"]
            )
            self.interrupt(f"cap-{role}")
        elif args[-3:] == ["systemctl", "restart", "kubelet"]:
            expected = envelope(load_profile(name=self.successor.profile)["nodes"][role])
            self.statuses[role]["status"]["allocatable"].update(
                cpu=f"{expected['cpuMillicores']}m", memory=str(expected["memoryBytes"])
            )
            self.interrupt(f"restart-{role}")
        else:
            raise AssertionError("Unexpected native mutation")
        return ""

    def private_command(self, _target, node_id, script, operation, *arguments, data=None):
        self.assertEqual(operation, OPERATION)
        role = next(r for r, n in self.request["nodes"].items() if n["containerId"] == node_id)
        if script == private.REPAIR_HASH:
            return model.digest(b"original retained repair backup") + "  original\n"
        elif script == private.PREFLIGHT:
            if node_id in self.backups:
                raise ValueError("stale versioned backup")
        elif script == private.BACKUP:
            self.backups.setdefault(node_id, self.configs[node_id])
            self.events.append(("backup", role))
            self.interrupt(f"backup-{role}")
        elif script == private.VERIFY_PATHS:
            if model.digest(self.backups[node_id]) != arguments[0]:
                raise ValueError("backup drift")
        elif script == private.READ_BACKUP:
            content = self.backups[node_id]
            return f"{model.digest(content)}  backup\n{base64.b64encode(content).decode()}"
        elif script == private.WRITE:
            assert data is not None
            self.configs[node_id] = data
            self.events.append(("write", role))
            self.interrupt(f"write-{role}")
        else:
            raise AssertionError("Unexpected private script")
        return ""

    def readiness(self, target, **_kwargs):
        return native.check_readiness(target, {"items": list(self.statuses.values())})

    def run_transition(self, action="apply"):
        return transition.execute(self.target, self.request, action)


class TransitionTests(TransitionFixture):
    def test_success_preserves_identity_access_original_policy_and_backups(self):
        result = self.run_transition()
        self.assertEqual(result["profile"], model.SUCCESSOR)
        self.assertEqual(result["liveFullInstallationQualification"], "pending")
        self.assertEqual(
            self.events,
            [
                ("backup", "control-plane"),
                ("backup", "worker"),
                ("cap", "control-plane"),
                ("write", "control-plane"),
                ("restart", "control-plane"),
                ("cap", "worker"),
                ("write", "worker"),
                ("restart", "worker"),
            ],
        )
        completed = store.load(self.target, self.request)
        self.assertEqual(completed["phase"], "complete")
        self.assertEqual(completed["predecessorOwner"].encode(), self.original_owner)
        self.assertEqual(
            owner.load(self.successor)["nodeIds"], json.loads(self.original_owner)["nodeIds"]
        )
        self.assertEqual(self.target.path("kind.yaml").read_bytes(), self.original_kind)
        self.assertEqual(self.target.path("kubeconfig").read_bytes(), self.original_access)
        for role, node in self.request["nodes"].items():
            old = json.loads(self.backups[node["containerId"]])
            new = json.loads(self.configs[node["containerId"]])
            self.assertEqual(new["privateMarker"], BASE["privateMarker"])
            self.assertEqual(old["systemReserved"]["cpu"], "2000m")
            verify_allocatable(
                self.statuses[role]["status"],
                load_profile(name=self.successor.profile)["nodes"][role],
            )

    def test_partial_native_boundaries_require_explicit_recovery(self):
        # A separate fixture for each boundary also proves later-node failure never
        # causes the earlier verified node to be resized/restarted again.
        for point in (
            "backup-control-plane",
            "cap-control-plane",
            "write-control-plane",
            "restart-control-plane",
            "cap-worker",
            "write-worker",
            "restart-worker",
        ):
            with self.subTest(point=point):
                fixture = TransitionTests(methodName="runTest")
                fixture.setUp()
                try:
                    fixture.failure = point
                    with self.assertRaises(ValueError):
                        fixture.run_transition()
                    with self.assertRaises(ValueError):
                        owner.load(fixture.target)
                    with self.assertRaises(ValueError):
                        owner.load(fixture.successor)
                    with self.assertRaisesRegex(ValueError, "Existing transition"):
                        fixture.run_transition()
                    fixture.run_transition("recover")
                    self.assertEqual(
                        store.load(fixture.target, fixture.request)["phase"], "complete"
                    )
                finally:
                    fixture.doCleanups()

    def test_every_journal_intention_and_commit_boundary_can_explicitly_recover(self):
        boundaries = (
            ("backups", "pending"),
            ("nodes", "pending"),
            ("nodes", "cap-intent"),
            ("nodes", "write-intent"),
            ("nodes", "restart-intent"),
            ("nodes", "verified"),
            ("finalizing", "verified"),
            ("complete", "verified"),
        )
        for phase, stage in boundaries:
            with self.subTest(phase=phase, stage=stage):
                fixture = TransitionTests(methodName="runTest")
                fixture.setUp()
                save = store.save
                triggered = False

                def interrupted(target, journal, save=save, phase=phase, stage=stage):
                    nonlocal triggered
                    save(target, journal)
                    if (
                        not triggered
                        and journal["phase"] == phase
                        and journal["stages"]["control-plane"] == stage
                    ):
                        triggered = True
                        raise ValueError("interrupted journal boundary")

                try:
                    with (
                        patch.object(store, "save", side_effect=interrupted),
                        self.assertRaises(ValueError),
                    ):
                        fixture.run_transition()
                    fixture.run_transition("recover")
                    self.assertEqual(
                        store.load(fixture.target, fixture.request)["phase"], "complete"
                    )
                finally:
                    fixture.doCleanups()

    def test_owner_replacement_interruption_recovers_without_repeating_node_changes(self):
        replace = store.replace

        def interrupted(path, data, expected):
            replace(path, data, expected)
            if path == self.target.path("owner.json"):
                raise ValueError("interrupted after ownership replacement")

        with patch.object(store, "replace", side_effect=interrupted), self.assertRaises(ValueError):
            self.run_transition()
        self.assertEqual(store.load(self.target, self.request)["phase"], "finalizing")
        with self.assertRaises(ValueError):
            owner.load(self.successor)
        before = list(self.events)
        self.run_transition("recover")
        self.assertEqual(self.events, before)
        owner.load(self.successor)
        self.run_transition("recover")
        self.assertEqual(self.events, before)

    def test_policy_drift_or_later_node_preflight_failure_has_no_live_mutations(self):
        node_id = self.request["nodes"]["worker"]["containerId"]
        self.backups[node_id] = b"stale"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_transition()
        self.assertEqual(self.events, [])
        self.assertFalse(self.target.path("transition.json").exists())
        del self.backups[node_id]
        config = json.loads(self.configs[node_id])
        config["systemReserved"]["cpu"] = "1000m"
        self.configs[node_id] = model.encoded(config)
        self.request["nodes"]["worker"]["oldConfigSha256"] = model.digest(self.configs[node_id])
        with self.assertRaisesRegex(ValueError, "Foreign"):
            self.run_transition()
        self.assertEqual(self.events, [])

    def test_identity_capacity_workload_and_source_drift_fail_before_journal(self):
        changes = (
            patch.object(native, "incarnation", return_value="different"),
            patch.object(native, "engine", return_value="unix:///other.sock"),
            patch.object(
                lifecycle,
                "source_identity",
                return_value={"providerRevision": REVISION, "providerDirty": True},
            ),
            patch.object(
                capacity, "preflight_workloads", side_effect=ValueError("consumer activated")
            ),
            patch.object(transition, "foundation", return_value=([], False)),
        )
        for selected in changes:
            with selected, self.assertRaises(ValueError):
                self.run_transition()
            self.assertFalse(self.target.path("transition.json").exists())
        self.statuses["worker"]["status"]["capacity"] = {"cpu": "8", "memory": "30Gi"}
        with self.assertRaises(ValueError):
            self.run_transition()
        self.assertEqual(self.events, [])

    def test_recovery_refuses_request_changed_identity_config_caps_or_foundation(self):
        self.failure = "cap-control-plane"
        with self.assertRaises(ValueError):
            self.run_transition()
        original = copy.deepcopy(self.request)
        self.request["operationId"] = "b" * 32
        with self.assertRaises(ValueError):
            self.run_transition("recover")
        self.request = original
        node_id = self.request["nodes"]["control-plane"]["containerId"]
        before = self.configs[node_id]
        self.configs[node_id] = b"foreign"
        with self.assertRaises(ValueError):
            self.run_transition("recover")
        self.configs[node_id] = before
        name = f"{self.target.name}-control-plane"
        self.containers[name]["memory"] = 7 * 1024**3
        with self.assertRaises(ValueError):
            self.run_transition("recover")
        self.containers[name]["memory"] = 6 * 1024**3
        with (
            patch.object(transition, "foundation", return_value=([["changed"]], True)),
            self.assertRaises(ValueError),
        ):
            self.run_transition("recover")
        self.assertEqual(self.target.path("owner.json").read_bytes(), self.original_owner)

    def test_inspection_of_pending_operation_is_read_only_and_verified_node_drift_stops_worker(
        self,
    ):
        self.failure = "cap-worker"
        with self.assertRaises(ValueError):
            self.run_transition()
        before = self.target.path("transition.json").read_bytes()
        events = list(self.events)
        result = self.run_transition("inspect")
        self.assertEqual(result["phase"], "nodes")
        self.assertEqual(self.target.path("transition.json").read_bytes(), before)
        self.assertEqual(self.events, events)
        self.statuses["control-plane"]["status"]["allocatable"]["cpu"] = "3500m"
        with self.assertRaises(ValueError):
            self.run_transition("recover")
        self.assertEqual(self.events, events)

    def test_initial_cordon_or_blocking_taint_refuses_before_changes(self):
        for spec in (
            {"unschedulable": True},
            {"taints": [{"key": "external-maintenance", "effect": "NoSchedule"}]},
        ):
            self.statuses["worker"]["spec"] = spec
            with self.assertRaises(ValueError):
                self.run_transition()
            self.assertFalse(self.target.path("transition.json").exists())
            self.assertEqual(self.events, [])

    def test_os_lock_excludes_overlapping_execution_and_releases_without_deleting_file(self):
        with store.locked(self.target):
            with self.assertRaises(ValueError):
                with store.locked(self.target):
                    self.fail("overlapping operation acquired lock")
        with store.locked(self.target):
            pass
        self.assertTrue(self.target.path("transition.lock").exists())

    def test_preview_is_offline_and_failure_diagnostics_do_not_quote_private_data(self):
        common = [
            "--consumer",
            "neutral",
            "--docker-context",
            "local-engine",
            "--state-root",
            str(self.root),
            "--https-port",
            "18443",
        ]
        with (
            patch.object(store, "read") as read,
            patch.object(native, "command") as command,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["recover", *common]), 0)
        self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")
        read.assert_not_called()
        command.assert_not_called()
        request_path = self.root / "request.json"
        request_path.write_bytes(model.encoded(self.request))
        self.failure = "cap-control-plane"
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                cli.main(
                    [
                        "apply",
                        *common,
                        "--request",
                        str(request_path),
                        "--execute",
                        "--confirm-quiesced",
                        self.target.name,
                    ]
                ),
                1,
            )
        self.assertNotIn("private-config-never-print", output.getvalue())

    def test_request_contract_and_exact_byte_history(self):
        for key, value in (
            ("providerRevision", "main"),
            ("operationId", "../../unsafe"),
            ("successorDigest", self.request["predecessorDigest"]),
            ("incarnation", ""),
        ):
            changed = copy.deepcopy(self.request)
            changed[key] = value
            with self.assertRaises(ValueError):
                model.validate_request(self.target, changed)
        original = (
            json.dumps(json.loads(self.original_owner), indent=2).replace("\n", "\r\n").encode()
        )
        self.target.path("owner.json").write_bytes(original)
        self.run_transition()
        self.assertEqual(
            store.load(self.target, self.request)["predecessorOwner"].encode(), original
        )
        record = owner.load(self.successor)
        record["phase"] = "deleting"
        owner.save(self.successor, record)
        self.assertEqual(owner.load(self.successor)["phase"], "deleting")

    def test_live_capacity_normalizes_equivalent_quantities_without_mutating_observations(self):
        for node in self.statuses.values():
            node["status"]["capacity"].update(cpu="8000m", memory="33554432Ki")
        before = copy.deepcopy(self.statuses)
        result = self.run_transition("inspect")
        self.assertTrue(result["readyForApproval"])
        self.assertEqual(self.statuses, before)
        self.assertEqual(self.events, [])
        self.assertFalse(self.target.path("transition.json").exists())

    def test_invalid_or_changed_live_capacity_refuses_before_journal_or_mutation(self):
        invalid = (None, [], {}, {"cpu": "8"}, {"memory": "32Gi"})
        for key in ("cpu", "memory"):
            for value in (None, 8, "", "invalid", "0", "-1", "NaN"):
                invalid += ({**NEW_MACHINE, key: value},)
        invalid += ({**NEW_MACHINE, "cpu": "7"}, {**NEW_MACHINE, "memory": "31Gi"})
        for role in model.ROLES:
            original = copy.deepcopy(self.statuses[role]["status"]["capacity"])
            for value in invalid:
                with self.subTest(role=role, capacity=value):
                    self.statuses[role]["status"]["capacity"] = value
                    with self.assertRaises(ValueError):
                        self.run_transition()
                    self.assertEqual(self.events, [])
                    self.assertFalse(self.target.path("transition.json").exists())
                    self.assertEqual(
                        self.target.path("owner.json").read_bytes(), self.original_owner
                    )
            self.statuses[role]["status"]["capacity"] = original

    def test_reviewed_capacity_remains_strict_for_both_nodes_and_observation_periods(self):
        for role in model.ROLES:
            for period in ("oldMachineCapacity", "newMachineCapacity"):
                for extra in ("pods", "ephemeral-storage", "hugepages-1Gi", "hugepages-2Mi"):
                    with self.subTest(role=role, period=period, extra=extra):
                        changed = copy.deepcopy(self.request)
                        changed["nodes"][role][period][extra] = "0"
                        with self.assertRaisesRegex(ValueError, "Explicit CPU/memory"):
                            transition.execute(self.target, changed, "inspect")
                        self.assertEqual(self.events, [])
                        self.assertFalse(self.target.path("transition.json").exists())

    def test_read_only_inspect_and_unfinished_guard_do_not_change_owner(self):
        result = self.run_transition("inspect")
        self.assertTrue(result["readyForApproval"])
        self.assertFalse(self.target.path("transition.json").exists())
        self.assertEqual(self.events, [])
        for action in (lifecycle.health, lifecycle.ensure):
            self.target.path("transition.json").write_text(
                '{"format":"isolated-profile-transition.v1","phase":"nodes"}'
            )
            with self.assertRaises(ValueError):
                action(self.target)
        self.assertEqual(self.target.path("owner.json").read_bytes(), self.original_owner)


class FoundationPolicyTests(unittest.TestCase):
    def test_required_inventory_and_ready_signals_are_not_inferred_from_empty_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Target("neutral", "local-engine", Path(directory), model.PREDECESSOR, 18443)
            resources: list[dict[str, Any]] = [
                {"kind": "Namespace", "metadata": {"name": "kube-system", "uid": INCARNATION}}
            ]
            controllers = (
                ("Deployment", "kube-system", "coredns"),
                ("DaemonSet", "kube-system", "cilium"),
                ("DaemonSet", "kube-system", "cilium-envoy"),
                ("Deployment", "kube-system", "cilium-operator"),
                ("Deployment", "local-path-storage", "local-path-provisioner"),
            )
            for kind, namespace, name in controllers:
                resources.append(
                    {
                        "kind": kind,
                        "metadata": {
                            "name": name,
                            "namespace": namespace,
                            "uid": name + "-uid",
                            "generation": 1,
                        },
                        "spec": {"replicas": 1},
                        "status": {
                            "observedGeneration": 1,
                            "readyReplicas": 1,
                            "desiredNumberScheduled": 2,
                            "numberReady": 2,
                        },
                    }
                )
            with patch.object(
                native, "kubectl", return_value=json.dumps({"items": resources})
            ) as command:
                snapshot, ready = transition.foundation(target)
                self.assertTrue(ready)
                self.assertEqual(len(snapshot), 6)
                self.assertNotIn("secrets", command.call_args.args[1])
            for mutation in (
                [],
                resources[:-1],
                [
                    *resources,
                    {
                        "kind": "PersistentVolume",
                        "metadata": {"name": "retained", "uid": "retained-uid"},
                    },
                ],
            ):
                with (
                    patch.object(native, "kubectl", return_value=json.dumps({"items": mutation})),
                    self.assertRaises(ValueError),
                ):
                    transition.foundation(target)
            resources[1]["status"]["readyReplicas"] = 0
            with patch.object(native, "kubectl", return_value=json.dumps({"items": resources})):
                self.assertFalse(transition.foundation(target)[1])
