"""Regression controls for host-inflated kind scheduling capacity and repair safety."""

import base64
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import consumer_capacity as capacity
from scripts import consumer_runtime as runtime
from scripts.consumer_profile import LABEL, Target
from scripts.node_capacity import (
    envelope,
    kubelet_configuration,
    pod_requests,
    quantity,
    verify_allocatable,
)

BUDGET = {"cpus": 2, "memoryBytes": 4 * 1024**3}
MACHINE = {"cpu": "4", "memory": "16Gi"}
CONFIG = {
    "kind": "KubeletConfiguration",
    "apiVersion": "kubelet.config.k8s.io/v1beta1",
    "cgroupDriver": "systemd",
    "staticPodPath": "/etc/kubernetes/manifests",
}
STATUS = {"capacity": MACHINE, "allocatable": {"cpu": "1750m", "memory": "3484Mi", "pods": "110"}}


class CapacityPolicyTests(unittest.TestCase):
    def test_exact_resource_units_and_malformed_observations(self):
        self.assertEqual(quantity("16155204Ki"), 16542928896)
        self.assertEqual(quantity("1.75", cpu=True), 1750)
        self.assertEqual(quantity("1750000000n", cpu=True), 1750)
        for value in [True, None, "", "NaN", "-1", "0", "1e3", "1GiB", "9" * 41, "0.1"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                quantity(value)
        with self.assertRaises(ValueError):
            quantity("1n", cpu=True)

    def test_reservations_exclude_host_gap_and_preserve_unrelated_config(self):
        desired = kubelet_configuration(CONFIG, MACHINE, BUDGET)
        self.assertEqual(desired["systemReserved"], {"cpu": "2000m", "memory": str(12 * 1024**3)})
        self.assertEqual(desired["kubeReserved"], {"cpu": "250m", "memory": str(512 * 1024**2)})
        self.assertEqual(desired["staticPodPath"], CONFIG["staticPodPath"])
        self.assertNotIn("systemReserved", CONFIG)
        # Public kind v0.31.0 config template supplies disk thresholds only; a
        # nonempty eviction map does not merge Kubernetes' other defaults.
        baseline = {
            **CONFIG,
            "evictionHard": {
                "nodefs.available": "0%",
                "nodefs.inodesFree": "0%",
                "imagefs.available": "0%",
            },
        }
        corrected = kubelet_configuration(baseline, MACHINE, BUDGET)
        self.assertEqual(corrected["evictionHard"]["memory.available"], "100Mi")
        self.assertEqual(corrected["evictionHard"]["nodefs.available"], "0%")
        self.assertEqual(kubelet_configuration(desired, MACHINE, BUDGET), desired)
        self.assertEqual(envelope(BUDGET), {"cpuMillicores": 1750, "memoryBytes": 3484 * 1024**2})
        configured = {
            **CONFIG,
            "evictionHard": {"memory.available": "100Mi", "nodefs.available": "0%"},
        }
        self.assertEqual(
            kubelet_configuration(configured, MACHINE, BUDGET)["evictionHard"],
            configured["evictionHard"],
        )

    def test_foreign_reservations_and_resource_policies_are_never_overwritten(self):
        for extra in [
            {"kubeReserved": {"cpu": "1"}},
            {"systemReserved": {"memory": "1Gi"}},
            {"reservedSystemCPUs": "0"},
            {"cpuManagerPolicy": "static"},
            {"memoryManagerPolicy": "Static"},
            {"cgroupsPerQOS": False},
            {"enforceNodeAllocatable": ["pods", "system-reserved"]},
            {"evictionHard": {"memory.available": "500Mi"}},
            {"evictionSoft": {"memory.available": "1Gi"}},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                kubelet_configuration({**CONFIG, **extra}, MACHINE, BUDGET)
        with self.assertRaises(ValueError):
            kubelet_configuration(CONFIG, {"cpu": "1", "memory": "16Gi"}, BUDGET)

    def test_pod_requests_include_init_maximum_and_overhead_and_reject_advanced_shapes(self):
        pod = {
            "spec": {
                "containers": [
                    {"resources": {"requests": {"cpu": "100m", "memory": "100Mi"}}},
                    {"resources": {"requests": {"cpu": "200m", "memory": "50Mi"}}},
                ],
                "initContainers": [{"resources": {"requests": {"cpu": "500m", "memory": "80Mi"}}}],
                "overhead": {"cpu": "10m", "memory": "1Mi"},
            }
        }
        self.assertEqual(pod_requests(pod), {"cpu": 510, "memory": 151 * 1024**2})
        for shape in [
            {"resources": {"requests": {"cpu": "1"}}},
            {"initContainers": [{"restartPolicy": "Always"}]},
        ]:
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                pod_requests({"spec": shape})
        with self.assertRaises(ValueError):
            quantity("1.00000000000000000000000000000000001")

    def test_health_rejects_the_actual_stage_a_host_sized_allocatable(self):
        verify_allocatable(STATUS, BUDGET)
        for extra in [
            {"cpu": "4", "memory": "16155204Ki"},
            {"cpu": "2"},
            {"memory": "4Gi"},
            {"cpu": "1500m"},
            {"pods": "0"},
        ]:
            status = copy.deepcopy(STATUS)
            status["allocatable"].update(extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                verify_allocatable(status, BUDGET)


class CapacityExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Target(
            "neutral",
            "local-engine",
            Path(self.temp.name).resolve(),
            "isolated-network-local",
            18443,
        )
        self.nodes: dict[str, Any] = {}
        self.containers = {}
        for role, char in [("control-plane", "a"), ("worker", "b")]:
            name = f"neutral-local-{role}"
            self.nodes[role] = {
                "metadata": {
                    "name": name,
                    "uid": char * 32,
                    "labels": {
                        LABEL: "neutral",
                        "k8s-infrastructure.io/role": role,
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                    },
                },
                "status": {
                    **copy.deepcopy(STATUS),
                    "nodeInfo": {"kubeletVersion": "v1.35.0"},
                    "conditions": [
                        {"type": k, "status": "False"}
                        for k in ("MemoryPressure", "DiskPressure", "PIDPressure")
                    ],
                },
            }
            self.containers[name] = {"id": char * 64}

    def boundaries(self, stack=None):
        if stack is None:
            stack = ExitStack()
            self.addCleanup(stack.close)
        selections: list[tuple[str, str, dict[str, Any]]] = [
            (
                "owner",
                "scripts.consumer_capacity.lifecycle.owned",
                {"return_value": {"phase": "ready"}},
            ),
            (
                "identity",
                "scripts.consumer_capacity.runtime.node_identity",
                {"return_value": self.containers},
            ),
            ("limits", "scripts.consumer_capacity.runtime.verify_nodes", {}),
            (
                "baseHealth",
                "scripts.consumer_capacity.lifecycle.health",
                {"return_value": {"baseReadiness": "observed"}},
            ),
            ("workloads", "scripts.consumer_capacity.preflight_workloads", {}),
            (
                "observe",
                "scripts.consumer_capacity.observations",
                {"side_effect": lambda _: copy.deepcopy(self.nodes)},
            ),
            (
                "read",
                "scripts.consumer_capacity.read_configuration",
                {"return_value": ("c" * 64, copy.deepcopy(CONFIG))},
            ),
            ("command", "scripts.consumer_capacity.runtime.command", {"return_value": ""}),
            (
                "wait",
                "scripts.consumer_capacity.wait_allocatable",
                {"return_value": {"observed": True}},
            ),
            (
                "incarnation",
                "scripts.consumer_capacity.runtime.incarnation",
                {"return_value": "original-incarnation"},
            ),
        ]
        return {
            name: stack.enter_context(patch(path, **options)) for name, path, options in selections
        }

    def test_private_config_reader_binds_the_exact_bytes_without_disclosure(self):
        content = (json.dumps(CONFIG) + "\n").encode()
        digest = hashlib.sha256(content).hexdigest()
        raw = digest + "  /var/lib/kubelet/config.yaml\n" + base64.b64encode(content).decode()
        with patch.object(runtime, "command", return_value=raw):
            self.assertEqual(capacity.read_configuration(self.target, "a" * 64), (digest, CONFIG))
        with (
            patch.object(runtime, "command", return_value=raw.replace(digest, "f" * 64)),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            capacity.read_configuration(self.target, "a" * 64)

    def test_preview_starts_no_process_and_reads_no_private_state(self):
        with (
            patch.object(runtime, "command") as command,
            patch.object(capacity.lifecycle, "owned") as owned,
        ):
            result = capacity.preview(self.target)
        self.assertEqual(result["execution"], "not-performed")
        self.assertEqual(result["podEnvelopes"]["worker"]["memoryBytes"], 3484 * 1024**2)
        command.assert_not_called()
        owned.assert_not_called()
        self.assertFalse(self.target.directory.exists())

    def test_both_configs_are_preflighted_before_write_then_exact_ids_restarted(self):
        mocks = self.boundaries()
        capacity.configure(self.target)
        calls = mocks["command"].call_args_list
        self.assertEqual(len(calls), 4)
        writes = [c for c in calls if "-i" in c.args[1]]
        self.assertEqual(len(writes), 2)
        for call in writes:
            self.assertIn("c" * 64, call.args[1])
            self.assertIn(capacity.WRITE, call.args[1])
            body = json.loads(call.kwargs["input_data"])
            self.assertEqual(body["staticPodPath"], CONFIG["staticPodPath"])
        self.assertEqual(
            [c.args[1][4] for c in calls if "restart" in c.args[1]], ["a" * 64, "b" * 64]
        )
        self.assertEqual(mocks["read"].call_count, 4)
        mocks["baseHealth"].assert_called_once_with(self.target)

    def test_deleting_identity_and_oversize_config_are_refused_before_writes(self):
        for reason in ["deleting", "oversize"]:
            with self.subTest(reason=reason), ExitStack() as stack:
                mocks = self.boundaries(stack)
                if reason == "deleting":
                    mocks["owner"].return_value = {"phase": "deleting"}
                else:
                    mocks["read"].return_value = ("c" * 64, {**CONFIG, "padding": "x" * 65536})
                with self.assertRaises(ValueError):
                    capacity.configure(self.target)
                mocks["command"].assert_not_called()

    def test_foreign_second_node_config_prevents_every_write(self):
        mocks = self.boundaries()
        mocks["read"].side_effect = [
            ("c" * 64, CONFIG),
            ("d" * 64, {**CONFIG, "kubeReserved": {"cpu": "1"}}),
        ]
        with self.assertRaises(ValueError):
            capacity.configure(self.target)
        mocks["command"].assert_not_called()

    def test_owner_uid_and_config_races_refuse_writes(self):
        for failure in ["owner", "uid", "config"]:
            with self.subTest(failure=failure), ExitStack() as stack:
                mocks = self.boundaries(stack)
                if failure == "owner":
                    mocks["owner"].side_effect = [
                        {"phase": "ready"},
                        ValueError("identity mismatch"),
                    ]
                elif failure == "uid":
                    changed = copy.deepcopy(self.nodes)
                    changed["control-plane"]["metadata"]["uid"] = "replacement"
                    mocks["observe"].side_effect = [self.nodes, changed]
                else:
                    mocks["read"].side_effect = [
                        ("c" * 64, CONFIG),
                        ("c" * 64, CONFIG),
                        ("d" * 64, CONFIG),
                    ]
                with self.assertRaises(ValueError):
                    capacity.configure(self.target)
                mocks["command"].assert_not_called()

    def test_unchanged_healthy_nodes_are_not_restarted(self):
        mocks = self.boundaries()
        mocks["read"].return_value = ("c" * 64, kubelet_configuration(CONFIG, MACHINE, BUDGET))
        capacity.configure(self.target)
        mocks["command"].assert_not_called()

    def test_written_config_with_incomplete_status_is_restarted_without_rewrite(self):
        mocks = self.boundaries()
        mocks["read"].return_value = ("c" * 64, kubelet_configuration(CONFIG, MACHINE, BUDGET))
        for n in self.nodes.values():
            n["status"]["allocatable"] = {**MACHINE, "pods": "110"}
        capacity.configure(self.target)
        calls = mocks["command"].call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("restart" in c.args[1] and "-i" not in c.args[1] for c in calls))

    def test_workload_preflight_refuses_apps_controllers_and_overcommitted_requests(self):
        system = {
            "metadata": {"namespace": "kube-system"},
            "status": {"phase": "Running"},
            "spec": {
                "nodeName": "neutral-local-worker",
                "containers": [{"resources": {"requests": {"cpu": "2", "memory": "1Gi"}}}],
            },
        }
        for controllers, pods in [
            ([{"metadata": {"namespace": "consumer"}}], []),
            ([], [{"metadata": {"namespace": "consumer"}}]),
            ([], [system]),
        ]:
            with (
                self.subTest(controllers=controllers, pods=pods),
                patch.object(
                    runtime,
                    "kubectl",
                    side_effect=[json.dumps({"items": controllers}), json.dumps({"items": pods})],
                ),
                self.assertRaises(ValueError),
            ):
                capacity.preflight_workloads(self.target)
        system["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "100m"
        with patch.object(
            runtime,
            "kubectl",
            side_effect=[json.dumps({"items": []}), json.dumps({"items": [system]}), ""],
        ):
            capacity.preflight_workloads(self.target)

    def test_operator_database_desired_state_refuses_even_without_pods(self):
        with (
            patch.object(
                runtime,
                "kubectl",
                side_effect=[
                    json.dumps({"items": []}),
                    json.dumps({"items": []}),
                    "customresourcedefinition.apiextensions.k8s.io/clusters.postgresql.cnpg.io",
                    json.dumps({"items": [{"metadata": {"name": "consumer-db"}}]}),
                ],
            ),
            self.assertRaisesRegex(ValueError, "database desired state"),
        ):
            capacity.preflight_workloads(self.target)

    def test_partial_restart_failure_retains_config_and_stops_before_second_node(self):
        mocks = self.boundaries()
        mocks["command"].side_effect = ["", ValueError("restart failed")]
        with self.assertRaises(ValueError):
            capacity.configure(self.target)
        self.assertEqual(mocks["command"].call_count, 2)
        mocks["wait"].assert_not_called()
        self.assertFalse(
            any("rm" in c.args[1] or "update" in c.args[1] for c in mocks["command"].call_args_list)
        )

    def test_allocatable_wait_is_bounded_and_never_hides_api_errors(self):
        for n in self.nodes.values():
            n["status"]["allocatable"] = {**MACHINE, "pods": "110"}
        with (
            patch.object(capacity, "observations", return_value=self.nodes),
            patch.object(capacity.time, "monotonic", side_effect=[0, 121]),
            self.assertRaises(ValueError),
        ):
            capacity.wait_allocatable(self.target)
        with (
            patch.object(capacity, "observations", side_effect=ValueError("API failed")),
            patch.object(capacity.time, "sleep") as sleep,
            self.assertRaisesRegex(ValueError, "API failed"),
        ):
            capacity.wait_allocatable(self.target)
        sleep.assert_not_called()

    def test_health_sensor_rejects_legacy_inflation_without_mutating(self):
        from tests.test_consumer_kind import ready

        status = ready(self.target)
        status["items"][0]["status"]["allocatable"].update(cpu="4", memory="16Gi")
        with self.assertRaisesRegex(ValueError, "capped envelope"):
            runtime.check_readiness(self.target, status)

    def test_atomic_write_script_guards_original_hash_and_retains_backup(self):
        self.assertIn('test "$(sha256sum "$p" | cut', capacity.WRITE)
        self.assertIn("set -C", capacity.WRITE)
        self.assertIn('cat "$p" > "$b"', capacity.WRITE)
        self.assertIn('mv -T -- "$n" "$p"', capacity.WRITE)
        self.assertNotIn("rm ", capacity.WRITE)

    def test_cli_failure_does_not_disclose_config_content(self):
        args = [
            "apply",
            "--execute",
            "--consumer",
            "neutral",
            "--profile",
            "isolated-network-local",
            "--docker-context",
            "local-engine",
            "--state-root",
            str(self.target.state_root),
            "--https-port",
            "18443",
        ]
        with (
            patch.object(capacity, "configure", side_effect=ValueError("private settings")),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(capacity.main(args), 1)
        self.assertNotIn("private settings", output.getvalue())


@unittest.skipUnless(
    sys.platform == "linux",
    "Owned kind node script runs on Linux; CI exercises real shell fixtures",
)
class CapacityShellTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        file = root / "config.yaml"
        original = json.dumps(CONFIG).encode()
        file.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        script = capacity.WRITE.replace("/var/lib/kubelet", str(root))
        return root, file, original, digest, script

    def execute(self, script, digest):
        return subprocess.run(
            ["sh", "-ceu", script, "capacity-config", digest],
            input=json.dumps(kubelet_configuration(CONFIG, MACHINE, BUDGET)).encode(),
            capture_output=True,
            timeout=5,
        )

    def test_atomic_replace_retains_original_bytes_and_private_permissions(self):
        root, file, original, digest, script = self.fixture()
        result = self.execute(script, digest)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((root / "config.before-allocatable.yaml").read_bytes(), original)
        self.assertEqual(
            json.loads(file.read_bytes()), kubelet_configuration(CONFIG, MACHINE, BUDGET)
        )
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertFalse((root / "config.allocatable-next").exists())

    def test_changed_original_or_existing_backup_refuses_without_overwrite(self):
        root, file, original, digest, script = self.fixture()
        self.assertNotEqual(self.execute(script, "d" * 64).returncode, 0)
        self.assertEqual(file.read_bytes(), original)
        backup = root / "config.before-allocatable.yaml"
        backup.write_text("retained backup")
        self.assertNotEqual(self.execute(script, digest).returncode, 0)
        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(backup.read_text(), "retained backup")

    def test_read_and_write_refuse_symlink_config_without_side_effects(self):
        for operation in (capacity.READ, capacity.WRITE):
            with self.subTest(operation=operation.splitlines()[1]):
                root, file, original, digest, _ = self.fixture()
                outside = root / "outside"
                outside.write_bytes(original)
                file.unlink()
                file.symlink_to(outside)
                script = operation.replace("/var/lib/kubelet", str(root))
                result = self.execute(script, digest)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(file.is_symlink())
                self.assertEqual(outside.read_bytes(), original)
                self.assertFalse((root / "config.before-allocatable.yaml").exists())
                self.assertFalse((root / "config.allocatable-next").exists())

    def test_read_and_write_refuse_symlink_parent_without_side_effects(self):
        for operation in (capacity.READ, capacity.WRITE):
            with self.subTest(operation=operation.splitlines()[1]):
                root, file, original, digest, _ = self.fixture()
                directory = root / "linked-kubelet"
                directory.symlink_to(root, target_is_directory=True)
                script = operation.replace("/var/lib/kubelet", str(directory))
                result = self.execute(script, digest)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(directory.is_symlink())
                self.assertEqual(file.read_bytes(), original)
                self.assertFalse((root / "config.before-allocatable.yaml").exists())
                self.assertFalse((root / "config.allocatable-next").exists())

    def test_backup_and_temporary_paths_are_never_adopted(self):
        for name in ("config.before-allocatable.yaml", "config.allocatable-next"):
            for kind in ("regular", "symlink", "dangling-symlink"):
                with self.subTest(path=name, kind=kind):
                    root, file, original, digest, script = self.fixture()
                    retained = root / name
                    outside = root / "outside"
                    if kind == "regular":
                        retained.write_bytes(b"partial prior write")
                    else:
                        if kind == "symlink":
                            outside.write_bytes(b"external bytes")
                        retained.symlink_to(outside)
                    self.assertNotEqual(self.execute(script, digest).returncode, 0)
                    self.assertEqual(file.read_bytes(), original)
                    if kind == "regular":
                        self.assertEqual(retained.read_bytes(), b"partial prior write")
                    else:
                        self.assertTrue(retained.is_symlink())
                        if kind == "symlink":
                            self.assertEqual(outside.read_bytes(), b"external bytes")
                        else:
                            self.assertFalse(outside.exists())
                    if name == "config.allocatable-next":
                        self.assertFalse((root / "config.before-allocatable.yaml").exists())


class FullInstallationProposalTests(unittest.TestCase):
    def test_proposal_is_not_selectable_and_leaves_per_node_peak_headroom(self):
        import yaml

        from scripts.consumer_profile import PROFILES, load_profile

        proposal = yaml.safe_load(
            (
                Path(__file__).resolve().parents[1] / "docs/capacity/fleetiq-full-installation.yaml"
            ).read_text(encoding="utf8")
        )
        self.assertEqual(proposal["status"], "proposed-not-selectable")
        self.assertNotIn(proposal["profile"], PROFILES)
        for role, budget in proposal["nodes"].items():
            e = envelope(budget)
            sizing = proposal["sizing"][role]
            peak_mi = (
                sizing["providerMainLimitMemoryMi"]
                + sum(sizing.get("applicationMainLimitsMi", {}).values())
                + sum(sizing["allowancesMi"].values())
            )
            self.assertGreater(e["memoryBytes"], peak_mi * 1024**2)
            self.assertGreater(e["cpuMillicores"], sizing["peakRequestCpuMillicores"])
        self.assertGreaterEqual(
            proposal["minimumEngine"]["cpus"], sum(n["cpus"] for n in proposal["nodes"].values())
        )
        self.assertGreaterEqual(
            proposal["minimumEngine"]["memoryBytes"],
            sum(n["memoryBytes"] for n in proposal["nodes"].values()) + 4 * 1024**3,
        )
        old = envelope(load_profile(name="isolated-network-local")["nodes"]["worker"])
        self.assertGreater(
            proposal["sizing"]["worker"]["providerMainLimitMemoryMi"] * 1024**2, old["memoryBytes"]
        )
