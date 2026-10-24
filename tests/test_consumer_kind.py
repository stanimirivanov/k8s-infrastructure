"""Consumer identity, refusal and recovery tests without a live Docker/cluster dependency."""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from scripts import consumer_lifecycle as lifecycle
from scripts import consumer_runtime as runtime
from scripts import consumer_state as state
from scripts.consumer_kind import main
from scripts.consumer_profile import LABEL, Target, configuration, identity, load_profile
from scripts.local_kind import run

UID = "b0d8f12d-42ad-4eea-9303-ffaba205b01b"
ENDPOINT = "unix:///var/run/docker.sock"


def nodes(target):
    return {
        f"{target.name}-{role}": {
            "id": char * 64,
            "name": f"/{target.name}-{role}",
            "cluster": target.name,
            "role": role,
            "image": load_profile(name=target.profile)["nodeImage"],
            "memory": budget["memoryBytes"],
            "cpu": budget["cpus"] * 10**9,
            "pids": 2048,
        }
        for (role, budget), char in zip(
            load_profile(name=target.profile)["nodes"].items(), "ab", strict=True
        )
    }


def ready(target):
    return {
        "items": [
            {
                "metadata": {
                    "name": name,
                    "labels": {
                        LABEL: target.consumer,
                        "k8s-infrastructure.io/role": name.removeprefix(f"{target.name}-"),
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                    },
                },
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        *[
                            {"type": name, "status": "False"}
                            for name in ("MemoryPressure", "DiskPressure", "PIDPressure")
                        ],
                    ],
                    "nodeInfo": {"kubeletVersion": "v1.35.0"},
                    "capacity": {"cpu": "4", "memory": "16Gi"},
                    "allocatable": {
                        "cpu": f"{load_profile(name=target.profile)['nodes'][name.removeprefix(f'{target.name}-')]['cpus'] * 1000 - 250}m",
                        "memory": str(
                            load_profile(name=target.profile)["nodes"][
                                name.removeprefix(f"{target.name}-")
                            ]["memoryBytes"]
                            - 612 * 1024**2
                        ),
                        "pods": "110",
                    },
                },
            }
            for name in nodes(target)
        ]
    }


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = Target("fleetiq", "local-engine", self.root)

    def mock_runtime(self, *, existing=True):
        stack = ExitStack()
        self.addCleanup(stack.close)

        def output(_target, args, **_kwargs):
            selected = next((arg[3:] for arg in args if arg.startswith("id=")), None)
            if "ps" in args and selected:
                return selected if existing else ""
            if "rev-parse" in args:
                return "c" * 40
            return ""

        def kubectl(_target, args):
            return json.dumps(ready(self.target)) if args[:2] == ["get", "nodes"] else ""

        mocks = {"capacity": stack.enter_context(patch("scripts.consumer_capacity.configure"))}
        selections: dict[str, dict[str, Any]] = {
            "prerequisites": {"return_value": ENDPOINT},
            "clusters": {"return_value": [self.target.name] if existing else []},
            "node_identity": {"return_value": nodes(self.target)},
            "inspect_node": {"side_effect": lambda target, reference, name: nodes(target)[name]},
            "verify_context": {},
            "incarnation": {"return_value": UID},
            "kubectl": {"side_effect": kubectl},
            "command": {"side_effect": output},
            "tools": {"return_value": {"kind": "v0.31.0", "kubectl": "v1.36.4", "docker": {}}},
        }
        for name, options in selections.items():
            mocks[name] = stack.enter_context(patch.object(runtime, name, **options))
        return mocks

    def reserve_ready(self):
        record = state.reserve(self.target, ENDPOINT)
        record.update(
            phase="ready",
            incarnation=UID,
            nodeIds={name: value["id"] for name, value in nodes(self.target).items()},
        )
        state.save(self.target, record)
        return record

    def test_creation_waits_for_ready_node_taints_to_converge(self):
        pending = ready(self.target)
        pending["items"][1]["spec"] = {
            "taints": [{"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule"}]
        }
        with (
            patch.object(
                runtime,
                "kubectl",
                side_effect=[json.dumps(pending), json.dumps(ready(self.target))],
            ),
            patch.object(runtime.time, "sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            observed = runtime.read_readiness(self.target, wait=True)
        self.assertEqual(len(observed), 2)
        sleep.assert_called_once()

    def test_creation_wait_is_bounded_and_health_does_not_wait(self):
        pending = ready(self.target)
        pending["items"][1]["spec"] = {
            "taints": [{"key": "unqualified-placement", "effect": "NoSchedule"}]
        }
        with (
            patch.object(runtime, "kubectl", return_value=json.dumps(pending)),
            patch.object(runtime.time, "monotonic", side_effect=[0, 0, 91]),
            patch.object(runtime.time, "sleep") as sleep,
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "blocking taint"),
        ):
            runtime.read_readiness(self.target, wait=True)
        sleep.assert_called_once()
        with (
            patch.object(runtime, "kubectl", return_value=json.dumps(pending)),
            patch.object(runtime.time, "sleep") as sleep,
            self.assertRaisesRegex(ValueError, "blocking taint"),
        ):
            runtime.read_readiness(self.target)
        sleep.assert_not_called()

    def test_creation_readiness_does_not_retry_command_or_parse_failures(self):
        for response in [ValueError("command failed"), "invalid JSON"]:
            options: dict[str, Any] = (
                {"side_effect": response}
                if isinstance(response, Exception)
                else {"return_value": response}
            )
            with (
                patch.object(runtime, "kubectl", **options) as command,
                patch.object(runtime.time, "sleep") as sleep,
                self.assertRaises(ValueError),
            ):
                runtime.read_readiness(self.target, wait=True)
            command.assert_called_once()
            sleep.assert_not_called()

    def test_profiles_are_distinct_and_preserve_reserved_perfeng_identity(self):
        other = Target("future-project", "local-engine", self.root)
        self.assertNotEqual(other.name, self.target.name)
        self.assertNotEqual(other.path("kubeconfig"), self.target.path("kubeconfig"))
        self.assertEqual(
            configuration(self.target)["networking"], {"apiServerAddress": "127.0.0.1"}
        )
        self.assertEqual(len(configuration(self.target)["nodes"]), 2)
        for consumer in ["perfeng", "perfeng-local", "FleetIQ", "../outside", "", "a" * 33]:
            with self.subTest(consumer=consumer), self.assertRaises(ValueError):
                Target(consumer, "local-engine", self.root)
        with self.assertRaises(ValueError):
            Target("fleetiq", "ssh://outside", self.root)

    def test_profile_changes_fail_closed(self):
        profile = load_profile()
        for key in ["unknown", "minimumEngine", "nodeImage", "version"]:
            changed = copy.deepcopy(profile)
            changed[key] = True if key == "version" else "unreviewed"
            file = self.root / "profile.yaml"
            file.write_text(yaml.safe_dump(changed), encoding="utf-8")
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_profile(file)

    def test_argo_profile_is_explicit_and_cannot_adopt_the_base_owner_record(self):
        larger = Target("fleetiq", "local-engine", self.root, "isolated-argo-local")
        base = load_profile()
        argo = load_profile(name=larger.profile)
        self.assertEqual(base["nodes"]["control-plane"], {"cpus": 1, "memoryBytes": 2 * 1024**3})
        self.assertEqual(argo["nodes"]["control-plane"], {"cpus": 2, "memoryBytes": 4 * 1024**3})
        self.assertEqual(base["nodes"]["worker"], argo["nodes"]["worker"])
        runtime.verify_nodes(nodes(larger), profile=larger.profile)
        with self.assertRaises(ValueError):
            runtime.verify_nodes(nodes(self.target), profile=larger.profile)
        with self.assertRaises(ValueError):
            runtime.verify_nodes(nodes(larger))
        self.assertNotEqual(
            identity(self.target)["profileDigest"], identity(larger)["profileDigest"]
        )
        state.reserve(self.target, ENDPOINT)
        with self.assertRaisesRegex(ValueError, "profile/context mismatch"):
            state.load(larger)
        with self.assertRaises(ValueError):
            Target("fleetiq", "local-engine", self.root, "arbitrary-unbounded")

    def test_all_previews_are_offline_and_do_not_read_or_create_private_state(self):
        for action in ("up", "ensure", "health", "handoff", "down"):
            with (
                patch("scripts.local_kind.run") as command,
                patch.object(state, "read_bounded") as private_read,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    main(
                        [
                            action,
                            "--profile",
                            "isolated-local",
                            "--consumer",
                            "fleetiq",
                            "--docker-context",
                            "local-engine",
                            "--state-root",
                            str(self.root),
                        ]
                    ),
                    0,
                )
                self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")
                command.assert_not_called()
                private_read.assert_not_called()
        self.assertFalse(self.target.directory.exists())

    def test_ownership_record_is_exclusive_and_binds_profile_context_and_configuration(self):
        record = state.reserve(self.target, ENDPOINT)
        self.assertEqual(state.load(self.target), record)
        with self.assertRaises(ValueError):
            state.reserve(self.target, ENDPOINT)
        other = Target("fleetiq", "different-engine", self.root)
        with self.assertRaises(ValueError):
            state.load(other)
        self.target.path("kind.yaml").write_text("name: foreign", encoding="utf-8")
        with self.assertRaises(ValueError):
            state.load(self.target)

    def test_redirected_private_directory_is_rejected(self):
        destination = self.root / "outside"
        destination.mkdir()
        try:
            self.target.directory.symlink_to(destination, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Windows junction creation requires no developer-mode symlink privilege.
            import subprocess

            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(self.target.directory), str(destination)],
                check=True,
                capture_output=True,
            )
        with self.assertRaises(ValueError):
            self.target.path("owner.json")
        if self.target.directory.is_junction():
            self.target.directory.rmdir()
        else:
            self.target.directory.unlink()

    def test_capacity_setup_runs_after_caps_and_failure_retains_partial_identity(self):
        mocks = self.mock_runtime(existing=False)

        def refuse(_target):
            updates = [c for c in mocks["command"].call_args_list if "update" in c.args[1]]
            self.assertEqual(len(updates), 2)
            self.assertEqual(state.load(self.target)["phase"], "provisioned")
            raise ValueError("capacity reconciliation failed")

        mocks["capacity"].side_effect = refuse
        with self.assertRaisesRegex(ValueError, "capacity reconciliation failed"):
            lifecycle.create(self.target)
        self.assertEqual(state.load(self.target)["phase"], "partial")
        self.assertEqual(state.load(self.target)["incarnation"], UID)

    def test_creation_records_identity_and_bounds_only_its_owned_node_ids(self):
        mocks = self.mock_runtime(existing=False)
        observed = lifecycle.create(self.target)
        record = state.load(self.target)
        self.assertEqual(record["phase"], "ready")
        mocks["capacity"].assert_called_once_with(self.target)
        self.assertEqual(observed["incarnation"], UID)
        self.assertEqual(observed["baseReadiness"], "observed")
        self.assertIn("network policy", observed["capabilitiesNotQualified"])
        updates = [
            call.args[1] for call in mocks["command"].call_args_list if "update" in call.args[1]
        ]
        self.assertEqual(len(updates), 2)
        self.assertEqual({args[-1] for args in updates}, {"a" * 64, "b" * 64})
        self.assertFalse(any("rm" in call.args[1] for call in mocks["command"].call_args_list))

    def test_existing_cluster_is_never_adopted(self):
        self.mock_runtime()
        with self.assertRaises(ValueError):
            lifecycle.create(self.target)
        self.assertFalse(self.target.directory.exists())

    def test_failed_creation_is_retained_and_never_auto_deleted(self):
        mocks = self.mock_runtime(existing=False)
        mocks["command"].side_effect = ValueError("safe dependency failure")
        with self.assertRaises(ValueError):
            lifecycle.create(self.target)
        self.assertEqual(state.load(self.target)["phase"], "partial")
        self.assertEqual(mocks["command"].call_count, 1)
        self.assertNotIn("delete", mocks["command"].call_args.args[1])

    def test_wrong_incarnation_nodes_endpoint_and_partial_state_block_access(self):
        self.reserve_ready()
        mocks = self.mock_runtime()
        for name, value in [
            ("incarnation", "changed"),
            ("node_identity", {}),
            ("prerequisites", "unix:///different"),
        ]:
            with patch.object(runtime, name, return_value=value), self.assertRaises(ValueError):
                lifecycle.owned(self.target)
        record = state.load(self.target)
        record["phase"] = "partial"
        state.save(self.target, record)
        mocks["command"].reset_mock()
        with self.assertRaises(ValueError):
            lifecycle.health(self.target)
        mocks["command"].assert_not_called()

    def test_ensure_reuses_only_a_complete_healthy_identity(self):
        self.reserve_ready()
        self.mock_runtime()
        with patch.object(lifecycle, "health", return_value={"owned": True}) as health:
            self.assertEqual(lifecycle.ensure(self.target), {"owned": True})
            health.assert_called_once_with(self.target)

    def test_ensure_refuses_missing_or_orphaned_identity(self):
        mocks = self.mock_runtime()
        with self.assertRaises(ValueError):
            lifecycle.ensure(self.target)
        state.reserve(self.target, ENDPOINT)
        mocks["clusters"].return_value = []
        with self.assertRaises(ValueError):
            lifecycle.ensure(self.target)

    def test_removal_requires_confirmation_before_any_command(self):
        with patch.object(runtime, "command") as command, self.assertRaises(ValueError):
            lifecycle.down(self.target, "perfeng-local")
        command.assert_not_called()

    def test_removal_preserves_other_consumer_state_and_proves_absence(self):
        self.reserve_ready()
        other = Target("another-project", "local-engine", self.root)
        sentinel = state.reserve(other, ENDPOINT)
        mocks = self.mock_runtime()
        mocks["clusters"].side_effect = [[self.target.name], []]
        lifecycle.down(self.target, self.target.name)
        self.assertFalse(self.target.path("owner.json").exists())
        self.assertEqual(state.load(other), sentinel)
        deleted = [call.args[1] for call in mocks["command"].call_args_list if "rm" in call.args[1]]
        self.assertEqual(len(deleted), 2)
        self.assertEqual({args[-1] for args in deleted}, {"a" * 64, "b" * 64})

    def test_interrupted_removal_finishes_without_repeating_destructive_command(self):
        record = self.reserve_ready()
        record["phase"] = "deleting"
        state.save(self.target, record)
        mocks = self.mock_runtime(existing=False)
        lifecycle.down(self.target, self.target.name)
        self.assertFalse(self.target.path("owner.json").exists())
        self.assertFalse(any("rm" in call.args[1] for call in mocks["command"].call_args_list))

    def test_failed_removal_preserves_state_for_retry(self):
        self.reserve_ready()
        mocks = self.mock_runtime()
        mocks["command"].side_effect = ValueError("safe dependency failure")
        with self.assertRaises(ValueError):
            lifecycle.down(self.target, self.target.name)
        self.assertEqual(state.load(self.target)["phase"], "deleting")

    def test_readiness_rejects_foreign_versions_labels_pressure_and_missing_capacity(self):
        runtime.check_readiness(self.target, ready(self.target))
        for change in [
            lambda node: node["metadata"]["labels"].update({LABEL: "foreign"}),
            lambda node: node["status"]["conditions"].pop(),
            lambda node: node["status"].update(allocatable={}),
            lambda node: node["status"]["nodeInfo"].update(kubeletVersion="v1.36.0"),
        ]:
            data = ready(self.target)
            change(data["items"][0])
            with self.assertRaises(ValueError):
                runtime.check_readiness(self.target, data)

    def test_runtime_rejects_remote_engine_and_small_capacity(self):
        with (
            patch.object(runtime, "command", return_value='"ssh://remote"'),
            self.assertRaises(ValueError),
        ):
            runtime.engine(self.target)
        with (
            patch.object(
                runtime,
                "command",
                side_effect=[
                    "kind v0.31.0",
                    json.dumps(ENDPOINT),
                    '{"os":"linux","architecture":"x86_64","cpus":2,"memory":1}',
                ],
            ),
            self.assertRaises(ValueError),
        ):
            runtime.prerequisites(self.target, creating=True)

    def test_explicit_context_discards_ambient_docker_and_kubernetes_redirects(self):
        with (
            patch.dict(
                os.environ,
                {
                    "DOCKER_HOST": "tcp://remote:2376",
                    "DOCKER_CONTEXT": "foreign",
                    "KUBECONFIG": "private",
                },
            ),
            patch("scripts.local_kind.subprocess.Popen") as popen,
            redirect_stdout(io.StringIO()),
        ):
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.return_value = (b"ok", b"")
            self.assertEqual(run(["kind", "version"], docker_context="local-engine"), "ok")
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["DOCKER_CONTEXT"], "local-engine")
            self.assertNotIn("DOCKER_HOST", environment)
            self.assertNotIn("KUBECONFIG", environment)

    def test_semantic_profile_identity_is_stable(self):
        self.assertEqual(
            identity(self.target), identity(Target("fleetiq", "local-engine", self.root))
        )

    def test_replacement_cluster_is_retained_and_never_deleted_by_name(self):
        self.reserve_ready()
        mocks = self.mock_runtime()
        with self.assertRaisesRegex(ValueError, "replacement"):
            lifecycle.down(self.target, self.target.name)
        removed = [
            call.args[1][-1] for call in mocks["command"].call_args_list if "rm" in call.args[1]
        ]
        self.assertEqual(set(removed), {"a" * 64, "b" * 64})
        self.assertEqual(state.load(self.target)["phase"], "deleting")

    def test_partial_deletion_resumes_only_a_remaining_verified_id_without_api(self):
        record = self.reserve_ready()
        record["phase"] = "deleting"
        state.save(self.target, record)
        mocks = self.mock_runtime(existing=False)

        def output(_target, args, **_kwargs):
            return "b" * 64 if "ps" in args and f"id={'b' * 64}" in args else ""

        mocks["command"].side_effect = output
        lifecycle.down(self.target, self.target.name)
        removed = [
            call.args[1][-1] for call in mocks["command"].call_args_list if "rm" in call.args[1]
        ]
        self.assertEqual(removed, ["b" * 64])
        mocks["incarnation"].assert_not_called()

    def test_archive_handoff_does_not_guess_a_parent_repository(self):
        with (
            patch.object(lifecycle, "ROOT", self.root),
            patch.object(runtime, "command") as command,
        ):
            self.assertEqual(
                lifecycle.source_identity(self.target)["sourceBinding"], "caller-required"
            )
            command.assert_not_called()

    def test_container_boundary_rejects_foreign_and_replaced_node_identity(self):
        valid = list(nodes(self.target).values())
        names = "\n".join(nodes(self.target))
        with patch.object(
            runtime, "command", side_effect=[names, *[json.dumps(node) for node in valid]]
        ):
            self.assertEqual(runtime.node_identity(self.target), nodes(self.target))
        for field, value in [
            ("id", "invalid"),
            ("cluster", "perfeng-local"),
            ("image", "kindest/node:latest"),
            ("role", "foreign"),
        ]:
            changed = copy.deepcopy(valid)
            changed[0][field] = value
            with (
                patch.object(runtime, "command", side_effect=[names, json.dumps(changed[0])]),
                self.assertRaises(ValueError),
            ):
                runtime.node_identity(self.target)

    def test_container_limits_and_worker_taints_are_enforced(self):
        for field in ("memory", "cpu", "pids"):
            changed = nodes(self.target)
            changed[f"{self.target.name}-worker"][field] = 0
            with self.assertRaises(ValueError):
                runtime.verify_nodes(changed)
        data = ready(self.target)
        data["items"][1]["spec"] = {"taints": [{"key": "foreign", "effect": "NoSchedule"}]}
        with self.assertRaises(ValueError):
            runtime.check_readiness(self.target, data)


if __name__ == "__main__":
    unittest.main()
