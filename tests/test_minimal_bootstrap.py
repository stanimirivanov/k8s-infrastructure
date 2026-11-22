"""Minimal bootstrap public policy and orchestration contracts."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import minimal_bootstrap as bootstrap
from scripts.consumer_profile import Target


class AdmissionTests(unittest.TestCase):
    def test_rejects_tiny_workload_even_with_all_categories(self):
        request = {
            "version": 1,
            "target": "qual-demo-local",
            "artifactDigest": "sha256:" + "a" * 64,
            "memory": [
                {"id": "nodes", "kind": "workload", "limitBytes": 1},
                {"id": "browser", "kind": "browser", "limitBytes": 1},
                {"id": "tools", "kind": "tooling", "limitBytes": 1, "cpuMillicores": 1},
            ],
            "disk": [
                {"id": k, "category": k, "path": "/tmp", "limitBytes": 1}
                for k in ("retained", "images", "runtime", "preparation")
            ],
        }
        with self.assertRaisesRegex(ValueError, "12GiB"):
            bootstrap.validate_request(
                SimpleNamespace(name="qual-demo-local"), request, request["artifactDigest"]
            )

    def test_disk_reservation_cannot_understate_the_bootstrap_envelope(self):
        from tests.test_host_admission import request

        value = request()
        target = SimpleNamespace(name=value["target"])
        value["disk"][1]["limitBytes"] = 1
        with self.assertRaisesRegex(ValueError, "disk"):
            bootstrap.validate_request(target, value, value["artifactDigest"])


class BootstrapTests(unittest.TestCase):
    def test_preview_is_offline_and_unqualified(self):
        from unittest.mock import patch

        target = SimpleNamespace(
            name="qual-demo-local", profile="minimal-demo", consumer="qual-demo"
        )
        with patch("subprocess.Popen", side_effect=AssertionError("must be offline")):
            result = bootstrap.preview(target)
        self.assertFalse(result["installedAcceptance"])
        self.assertEqual(result["stage"], "pre-cilium")

    def test_environment_discards_ambient_provider_overrides(self):
        from unittest.mock import patch

        with patch.dict(
            "os.environ",
            {
                "DOCKER_HOST": "ssh://foreign",
                "KIND_EXPERIMENTAL_PROVIDER": "podman",
                "KUBECONFIG": "foreign",
            },
        ):
            result = bootstrap.environment("local", {"PATH": "/private/bin"})
        self.assertNotIn("DOCKER_HOST", result)
        self.assertNotIn("KUBECONFIG", result)
        self.assertEqual(result["DOCKER_CONTEXT"], "local")
        self.assertEqual(result["KIND_EXPERIMENTAL_PROVIDER"], "docker")

    def test_unsupported_vm_and_containerd_storage_are_refused(self):
        for info in (
            {"Driver": "overlayfs"},
            {"Driver": "overlay2", "OperatingSystem": "Docker Desktop"},
        ):
            with self.subTest(info=info), self.assertRaises(ValueError):
                bootstrap.validate_engine(info)

    def test_qualification_requires_disposable_target(self):
        target = SimpleNamespace(name="demo-local", profile="minimal-demo", consumer="demo")
        with self.assertRaisesRegex(ValueError, "qual-"):
            bootstrap.qualify(target, {}, "host")

    def test_kernel_peak_oom_refusal(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory)
            for name, value in {
                "memory.max": "4294967296",
                "memory.swap.max": "0",
                "cpu.max": "200000 100000",
                "pids.max": "2048",
                "memory.peak": "1048576",
                "memory.events": "oom 1\noom_kill 0\n",
                "cpu.stat": "nr_throttled 3\nthrottled_usec 10\n",
            }.items():
                (path / name).write_text(value)
            with self.assertRaisesRegex(ValueError, "OOM"):
                bootstrap.kernel_metrics(path, 4294967296)
            (path / "memory.events").write_text("oom 0\noom_kill 0\n")
            result = bootstrap.kernel_metrics(path, 4294967296)
            self.assertEqual(result["memoryPeakBytes"], 1048576)
            self.assertEqual(result["cpuStat"]["nr_throttled"], 3)

    def test_create_rejects_wrong_artifact_before_provider_access(self):
        from unittest.mock import patch

        target = Target("qual-demo", "default", Path("/tmp"), "minimal-demo")
        with patch(
            "scripts.consumer_runtime.prerequisites", side_effect=AssertionError("provider touched")
        ):
            with self.assertRaises(ValueError):
                bootstrap.create(target, {}, "host")


class CreationTests(unittest.TestCase):
    def test_kind_failure_preserves_owner_and_partial_receipt_without_cleanup(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import consumer_state
        from scripts.consumer_profile import Target
        from tests.test_host_admission import observation, request

        with TemporaryDirectory() as root, ExitStack() as stack:
            target = Target("qual-demo", "default", Path(root), "minimal-demo")
            value = request()
            value.update(target=target.name, artifactDigest=bootstrap.source_digest())
            observed = observation(value)
            for name, result in [
                ("runtime.prerequisites", "unix:///var/run/docker.sock"),
                ("runtime.tools", {}),
                ("runtime.command", "[]"),
                ("physical_engine", {}),
                ("runtime.clusters", []),
                ("host.observe", observed),
                ("verify_stores", None),
                ("host.host_id", observed["hostId"]),
                ("host.mem_available", 20 * 1024**3),
                ("time.time", 1001),
            ]:
                stack.enter_context(patch("scripts.minimal_bootstrap." + name, return_value=result))
            stack.enter_context(patch("scripts.bounded_docker.prepare", return_value={}))
            runner = stack.enter_context(
                patch("scripts.minimal_bootstrap.run_kind", side_effect=ValueError("kind failed"))
            )
            with self.assertRaisesRegex(ValueError, "kind failed"):
                bootstrap.create(target, value, observed["hostId"])
            self.assertEqual(consumer_state.load(target)["phase"], "partial")
            receipt = json.loads(bootstrap.artifact_path(target, "bootstrap.json").read_text())
            self.assertFalse(receipt["coldBootstrapQualified"])
            self.assertEqual(receipt["phase"], "partial")
            self.assertEqual(runner.call_count, 1)

    def test_success_is_pre_cilium_only_and_foreign_creation_stays_partial(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import consumer_state
        from scripts.consumer_profile import Target
        from tests.test_host_admission import observation, request

        for foreign in (False, True):
            with self.subTest(foreign=foreign), TemporaryDirectory() as root, ExitStack() as stack:
                target = Target("qual-demo", "default", Path(root), "minimal-demo")
                value = request()
                value.update(target=target.name, artifactDigest=bootstrap.source_digest())
                observed = observation(value)
                journals = []

                def run_kind(*args, target=target, foreign=foreign, journals=journals):
                    owner = consumer_state.load(target)
                    directory = bootstrap.artifact_path(target, "bounded-docker")
                    directory.mkdir()
                    for role, letter in (("control-plane", "a"), ("worker", "b")):
                        item = {
                            "target": target.name,
                            "nonce": "foreign" if foreign else owner["nonce"],
                            "name": f"{target.name}-{role}",
                            "role": role,
                            "id": letter * 64,
                            "phase": "started",
                        }
                        journals.append(item)
                        (directory / f"{item['name']}.json").write_text(json.dumps(item))

                nodes = {
                    f"{target.name}-{role}": {"id": letter * 64}
                    for role, letter in (("control-plane", "a"), ("worker", "b"))
                }
                metrics = {
                    name: {"cgroupIdentity": "immutable", "startedAt": "first-start"}
                    for name in nodes
                }
                for name, result in [
                    ("runtime.prerequisites", "unix:///var/run/docker.sock"),
                    ("runtime.tools", {}),
                    ("runtime.command", "[]"),
                    ("physical_engine", {}),
                    ("runtime.clusters", []),
                    ("host.observe", observed),
                    ("verify_stores", None),
                    ("host.host_id", observed["hostId"]),
                    ("host.mem_available", 20 * 1024**3),
                    ("time.time", 1001),
                    ("runtime.verify_context", None),
                    ("runtime.node_identity", nodes),
                    ("runtime.incarnation", "cluster-uid"),
                    ("node_metrics", metrics),
                    ("tooling_metrics", []),
                    ("host.memory_observation", observed["memory"]),
                    (
                        "configure_capacity",
                        {
                            "cluster": target.name,
                            "incarnation": "cluster-uid",
                            "nodes": {
                                "control-plane": {
                                    "schedulerEnvelope": {
                                        "cpuMillicores": 1750,
                                        "memoryBytes": 3484 * 1024**2,
                                    }
                                },
                                "worker": {
                                    "schedulerEnvelope": {
                                        "cpuMillicores": 1750,
                                        "memoryBytes": 7580 * 1024**2,
                                    }
                                },
                            },
                        },
                    ),
                ]:
                    stack.enter_context(
                        patch("scripts.minimal_bootstrap." + name, return_value=result)
                    )
                stack.enter_context(patch("scripts.bounded_docker.prepare", return_value={}))
                stack.enter_context(
                    patch("scripts.minimal_bootstrap.run_kind", side_effect=run_kind)
                )
                if foreign:
                    with self.assertRaisesRegex(ValueError, "foreign"):
                        bootstrap.qualify(target, value, observed["hostId"])
                    self.assertEqual(consumer_state.load(target)["phase"], "partial")
                else:
                    result = bootstrap.qualify(target, value, observed["hostId"])
                    self.assertTrue(result["coldBootstrapQualified"])
                    self.assertFalse(result["installedAcceptance"])
                    self.assertEqual(result["phase"], "pre-cilium-allocatable")
                    self.assertEqual(consumer_state.load(target)["phase"], "provisioned")

    def test_partial_cleanup_requires_exact_confirmation_before_access(self):
        from unittest.mock import patch

        target = Target("qual-demo", "default", Path("/tmp"), "minimal-demo")
        with patch("scripts.consumer_state.load", side_effect=AssertionError("state accessed")):
            with self.assertRaisesRegex(ValueError, "confirm-delete"):
                bootstrap.down(target, "other-target")

    def test_cleanup_uses_journal_ids_without_api_and_refuses_unrecorded_container(self):
        import json
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import consumer_state

        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected), TemporaryDirectory() as root:
                target = Target("qual-demo", "default", Path(root), "minimal-demo")
                owner = consumer_state.reserve(target, "unix:///var/run/docker.sock")
                directory = bootstrap.artifact_path(target, "bounded-docker")
                directory.mkdir()
                name, identifier = f"{target.name}-worker", "a" * 64
                journal = {
                    "name": name,
                    "id": identifier,
                    "target": target.name,
                    "nonce": owner["nonce"],
                }
                (directory / f"{name}.json").write_text(json.dumps(journal))
                bootstrap.write_receipt(target, {"nonce": owner["nonce"], "phase": "partial"})
                responses = [("b" * 64 if unexpected else identifier), "", ""]
                with (
                    patch(
                        "scripts.minimal_bootstrap.runtime.prerequisites",
                        return_value=owner["endpoint"],
                    ),
                    patch(
                        "scripts.minimal_bootstrap.runtime.inspect_node",
                        return_value={"id": identifier},
                    ),
                    patch(
                        "scripts.minimal_bootstrap.runtime.command", side_effect=responses
                    ) as command,
                ):
                    if unexpected:
                        with self.assertRaisesRegex(ValueError, "Unrecorded"):
                            bootstrap.down(target, target.name)
                        self.assertEqual(command.call_count, 1)
                    else:
                        receipt = bootstrap.down(target, target.name)
                        self.assertEqual(receipt["phase"], "removed")
                        self.assertEqual(command.call_args_list[1].args[1][-1], identifier)
                        self.assertTrue(target.path("owner.json").exists())
