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

    def test_physical_aliases_cannot_reduce_full_reservation(self):
        from tests.test_host_admission import observation, request

        value = request()
        images = value["disk"][1]
        images["limitBytes"] //= 2
        value["disk"].append({**images, "id": "images-alias"})
        observed = observation(value)
        observed["disk"]["images-alias"] = dict(observed["disk"][images["id"]])
        with self.assertRaisesRegex(ValueError, "alias"):
            bootstrap.verify_stores(value, observed, {})

    def test_network_pin_inventory_change_refuses_prior_artifact_before_provider_access(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from tests.test_host_admission import request

        target = Target("qual-demo", "default", Path("/tmp"), "minimal-demo", 18443)
        relative = Path("clusters/local-kind/isolated-network-local/releases.json")
        with TemporaryDirectory() as root, patch.object(bootstrap, "ROOT", Path(root)):
            inventory = Path(root) / relative
            inventory.parent.mkdir(parents=True)
            inventory.write_bytes((Path(__file__).resolve().parents[1] / relative).read_bytes())
            (Path(root) / "uv.lock").touch()
            value = request()
            value.update(target=target.name, artifactDigest=bootstrap.source_digest())
            inventory.write_bytes(inventory.read_bytes() + b"\n")
            with (
                patch.object(bootstrap.state, "load") as load,
                self.assertRaisesRegex(ValueError, "artifact"),
            ):
                bootstrap.network_bootstrap(target, value, "boot")
            load.assert_not_called()

    def test_reservation_needs_full_35gib_plus_filesystem_floor(self):
        from scripts import host_admission
        from tests.test_host_admission import observation, request

        value = request()
        observed = observation(value)
        for metric in observed["disk"].values():
            metric.update(availableBytes=39 * host_admission.GIB, residentBytes=host_admission.GIB)
        bootstrap.verify_stores(value, observed, {})
        decision = host_admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)
        self.assertFalse(decision["admitted"])

    def test_runtime_requires_positive_reservation_on_both_store_filesystems(self):
        from unittest.mock import Mock, patch

        from tests.test_host_admission import request

        first, second = Mock(), Mock()
        first.stat.return_value.st_dev = 1
        second.stat.return_value.st_dev = 2
        first.is_relative_to.return_value = False
        second.is_relative_to.return_value = False
        value = request()
        runtime = next(item for item in value["disk"] if item["category"] == "runtime")
        value["disk"].append(
            {**runtime, "id": "runtime-second", "path": "/second", "limitBytes": 0}
        )

        def reservation(path):
            result = Mock()
            result.stat.return_value.st_dev = 2 if path == "/second" else 1
            result.is_relative_to.return_value = False
            return result

        with patch.object(bootstrap.host, "ordinary_path", side_effect=reservation):
            with self.assertRaisesRegex(ValueError, "positive"):
                bootstrap.verify_stores(value, None, {"runtime": [first, second]})
            runtime["limitBytes"] -= 1
            value["disk"][-1]["limitBytes"] = 1
            bootstrap.verify_stores(value, None, {"runtime": [first, second]})

    def test_disk_reservation_cannot_understate_the_bootstrap_envelope(self):
        from tests.test_host_admission import request

        value = request()
        target = SimpleNamespace(name=value["target"])
        value["disk"][1]["limitBytes"] = 1
        with self.assertRaisesRegex(ValueError, "disk"):
            bootstrap.validate_request(target, value, value["artifactDigest"])


class BootstrapTests(unittest.TestCase):
    def test_cached_image_requires_digest_linux_amd64_metadata(self):
        import json
        from unittest.mock import patch

        from scripts.consumer_profile import load_profile

        target = Target("qual-demo", "default", Path("/tmp"), "minimal-demo")
        image = load_profile(name="minimal-demo")["nodeImage"]
        digest = "kindest/node@" + image.split("@", 1)[1]
        for architecture in ("amd64", "arm64"):
            with patch.object(
                bootstrap.runtime,
                "command",
                return_value=json.dumps(
                    {"digests": [digest], "os": "linux", "architecture": architecture}
                ),
            ):
                if architecture == "amd64":
                    bootstrap.cached_image(target)
                else:
                    with self.assertRaises(ValueError):
                        bootstrap.cached_image(target)

    def test_uncached_image_refuses_before_reservation(self):
        from unittest.mock import patch

        from tests.test_host_admission import request

        target = Target("qual-demo", "default", Path("/tmp"), "minimal-demo")
        value = request()
        value.update(target=target.name, artifactDigest=bootstrap.source_digest())
        with (
            patch.object(bootstrap.runtime, "prerequisites", return_value="unix:///socket"),
            patch.object(bootstrap.runtime, "tools", return_value={}),
            patch.object(bootstrap.runtime, "command", return_value="[]") as command,
            patch.object(bootstrap.state, "reserve") as reserve,
        ):
            with self.assertRaisesRegex(ValueError, "cached-node-image"):
                bootstrap.create(target, value, "host")
            reserve.assert_not_called()
            self.assertEqual(command.call_args.args[1][3:5], ["image", "inspect"])

    def test_preview_is_offline_and_unqualified(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with (
            TemporaryDirectory() as root,
            patch("subprocess.Popen", side_effect=AssertionError("must be offline")),
        ):
            target = Target("qual-demo", "default", Path(root), "minimal-demo", 18443)
            result = bootstrap.preview(target)
        self.assertFalse(result["installedAcceptance"])
        self.assertEqual(result["stage"], "pre-cilium")
        self.assertEqual(result["loopbackHttpsPort"], 18443)

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


class NetworkContinuationTests(unittest.TestCase):
    def test_continuation_credits_owned_cgroups_and_reserves_only_future_stage_disk(self):
        from contextlib import ExitStack
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import consumer_state
        from scripts.consumer_profile import identity
        from tests.test_host_admission import observation, request

        with TemporaryDirectory() as root, ExitStack() as stack:
            target = Target("qual-demo", "default", Path(root), "minimal-demo", 18443)
            owner = consumer_state.reserve(target, "unix:///run/docker.sock")
            value = request()
            value.update(target=target.name, artifactDigest=bootstrap.source_digest())
            observed = observation(value)
            owner.update(phase="provisioned", incarnation="cluster-uid")
            journals = [
                {
                    "target": target.name,
                    "nonce": owner["nonce"],
                    "name": f"{target.name}-{role}",
                    "role": role,
                    "id": letter * 64,
                    "phase": "started",
                }
                for role, letter in (("control-plane", "a"), ("worker", "b"))
            ]
            owner["nodeIds"] = {item["name"]: item["id"] for item in journals}
            consumer_state.save(target, owner)
            peaks = {
                item["name"]: {"cgroupIdentity": item["id"], "startedAt": "first-start"}
                for item in journals
            }
            paths = {name: Path(f"/sys/fs/cgroup/{name}") for name in peaks}
            bootstrap.write_receipt(
                target,
                {
                    **identity(target),
                    "nonce": owner["nonce"],
                    "stage": "pre-cilium",
                    "phase": "pre-cilium-allocatable",
                    "coldBootstrapQualified": True,
                    "artifactDigest": value["artifactDigest"],
                    "admission": bootstrap.admission.evaluate(
                        value, observed, host_id=observed["hostId"], now=1001
                    ),
                    "allocatable": {
                        "cluster": target.name,
                        "incarnation": owner["incarnation"],
                        "nodes": {
                            role: {
                                "schedulerEnvelope": {
                                    "cpuMillicores": 1750,
                                    "memoryBytes": memory * 1024**2,
                                }
                            }
                            for role, memory in (("control-plane", 3484), ("worker", 7580))
                        },
                    },
                    "kernelEvidence": peaks,
                    "nodes": journals,
                    "memoryAccountIdentities": {},
                },
            )

            available_gib = 22

            def observe(current, *, expected_host_id):
                result = observation(current)
                result["memAvailableBytes"] = 18 * bootstrap.admission.GIB
                for account in result["disk"].values():
                    account.update(
                        availableBytes=available_gib * bootstrap.admission.GIB,
                        residentBytes=bootstrap.admission.GIB,
                    )
                self.assertEqual(expected_host_id, observed["hostId"])
                node_accounts = [item for item in current["memory"] if item["kind"] == "workload"]
                self.assertEqual(
                    {item["cgroup"] for item in node_accounts}, set(map(str, paths.values()))
                )
                for item in journals:
                    result["memory"][f"bounded-{item['role']}"] = {
                        "residentBytes": bootstrap.admission.GIB,
                        "cgroupId": item["id"],
                    }
                return result

            for name, result in [
                ("host.host_id", observed["hostId"]),
                ("_node_metrics", (peaks, paths)),
                ("physical_engine", {}),
                ("tooling_metrics", []),
                ("time.time", 1001),
            ]:
                stack.enter_context(patch(f"scripts.minimal_bootstrap.{name}", return_value=result))
            stack.enter_context(
                patch("scripts.minimal_bootstrap.host.observe", side_effect=observe)
            )
            stack.enter_context(
                patch("scripts.bounded_docker.read_journals", return_value=journals)
            )
            stack.enter_context(patch("scripts.consumer_lifecycle.owned", return_value=owner))
            result, retained = bootstrap.network_bootstrap(target, value, observed["hostId"])
            self.assertEqual(retained, journals)
            self.assertEqual(
                result["networkAdmission"]["memoryRemainingBytes"], 5 * bootstrap.admission.GIB
            )
            self.assertEqual(
                result["networkAdmission"]["filesystems"],
                [
                    {
                        "filesystemId": "physical-store",
                        "incrementalBytes": 12 * bootstrap.admission.GIB,
                        "remainingBytes": 10 * bootstrap.admission.GIB,
                    }
                ],
            )
            available_gib = 21
            with self.assertRaisesRegex(ValueError, "Fresh network host admission refused"):
                bootstrap.network_bootstrap(target, value, observed["hostId"])

    def test_cold_bootstrap_receipt_must_bind_exact_source_and_request_before_provider_access(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import consumer_state
        from scripts.consumer_profile import identity
        from tests.test_host_admission import request

        with TemporaryDirectory() as root:
            target = Target("qual-demo", "default", Path(root), "minimal-demo", 18443)
            owner = consumer_state.reserve(target, "unix:///run/docker.sock")
            owner["phase"] = "provisioned"
            consumer_state.save(target, owner)
            value = request()
            value.update(target=target.name, artifactDigest=bootstrap.source_digest())
            bootstrap.write_receipt(
                target,
                {
                    **identity(target),
                    "nonce": owner["nonce"],
                    "stage": "pre-cilium",
                    "phase": "pre-cilium-allocatable",
                    "coldBootstrapQualified": True,
                    "artifactDigest": value["artifactDigest"],
                    "admission": {"admitted": True, "hostId": "boot", "requestDigest": "drift"},
                },
            )
            with (
                patch.object(bootstrap.runtime, "command") as command,
                self.assertRaisesRegex(ValueError, "bootstrap receipt"),
            ):
                bootstrap.network_bootstrap(target, value, "boot")
            command.assert_not_called()

    def test_network_operation_and_removal_cannot_overlap_for_the_same_target(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import consumer_state

        with TemporaryDirectory() as root:
            target = Target("qual-demo", "default", Path(root), "minimal-demo", 18443)
            consumer_state.reserve(target, "unix:///run/docker.sock")
            with (
                bootstrap.network_operation(target),
                patch.object(bootstrap.runtime, "command") as command,
                self.assertRaisesRegex(ValueError, "target lock"),
            ):
                bootstrap.down(target, target.name)
            command.assert_not_called()


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
                ("cached_image", None),
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
            with self.assertRaisesRegex(ValueError, "dependency-failure"):
                bootstrap.create(target, value, observed["hostId"])
            self.assertEqual(consumer_state.load(target)["phase"], "partial")
            receipt = json.loads(bootstrap.artifact_path(target, "bootstrap.json").read_text())
            self.assertFalse(receipt["coldBootstrapQualified"])
            self.assertEqual(receipt["phase"], "partial")
            self.assertEqual(receipt["failure"]["category"], "dependency-failure")
            self.assertEqual(receipt["failure"]["phase"], "kind-create")
            self.assertEqual(receipt["lastCompletedPhase"], "adapter-prepared")
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

        for foreign, stuck_sampler in ((False, False), (True, False), (False, True)):
            with (
                self.subTest(foreign=foreign, stuck_sampler=stuck_sampler),
                TemporaryDirectory() as root,
                ExitStack() as stack,
            ):
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
                    ("cached_image", None),
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
                if stuck_sampler:
                    sampler = stack.enter_context(
                        patch("scripts.minimal_bootstrap.threading.Thread")
                    )
                    sampler.return_value.is_alive.return_value = True
                    with self.assertRaisesRegex(ValueError, "timeout"):
                        bootstrap.qualify(target, value, observed["hostId"])
                    receipt = json.loads(
                        bootstrap.artifact_path(target, "bootstrap.json").read_text()
                    )
                    self.assertFalse(receipt["coldBootstrapQualified"])
                    self.assertEqual(receipt["failure"]["category"], "timeout")
                elif foreign:
                    with self.assertRaisesRegex(ValueError, "identity-mismatch"):
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


class FailureEvidenceTests(unittest.TestCase):
    def test_cli_distinguishes_safe_categories_and_never_prints_process_output(self):
        import io
        import json
        import subprocess
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from scripts.bootstrap_failure import Failure

        for error, category in (
            (
                Failure({"category": "missing-prerequisite", "phase": "cached-node-image"}),
                "missing-prerequisite",
            ),
            (
                Failure({"category": "identity-mismatch", "phase": "node-identity"}),
                "identity-mismatch",
            ),
            (
                subprocess.CalledProcessError(
                    17, ["private-credential"], stderr="private-credential"
                ),
                "dependency-failure",
            ),
            (
                subprocess.TimeoutExpired(["private-credential"], 2, stderr="private-credential"),
                "timeout",
            ),
        ):
            with (
                self.subTest(category=category),
                patch.object(bootstrap, "create", side_effect=error),
                patch.object(bootstrap.state, "read_bounded", return_value="{}"),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    bootstrap.main(
                        [
                            "create",
                            "--consumer",
                            "qual-demo",
                            "--docker-context",
                            "default",
                            "--state-root",
                            "/tmp",
                            "--request",
                            "/tmp/request",
                            "--host-id",
                            "host",
                            "--execute",
                        ]
                    ),
                    1,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result["failure"]["category"], category)
            self.assertNotIn("private-credential", output.getvalue())
            if category == "dependency-failure":
                self.assertEqual(result["failure"]["exitCode"], 17)
            if category == "timeout":
                self.assertTrue(result["failure"]["timedOut"])
