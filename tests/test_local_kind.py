import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.cluster import (
    CONTEXT,
    NAME,
    check_nodes,
    commands,
    execute,
    verify_context,
    verify_local_docker,
)


class ClusterTests(unittest.TestCase):
    def test_remote_docker_endpoints_rejected(self):
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2376"}),
            patch("scripts.cluster.run") as run,
        ):
            with self.assertRaises(ValueError):
                verify_local_docker()
            run.assert_not_called()
        with patch.dict(os.environ, {}, clear=True):
            with patch("scripts.cluster.run", return_value='"ssh://remote"'):
                with self.assertRaises(ValueError):
                    verify_local_docker()
            with patch("scripts.cluster.run", return_value='"unix:///var/run/docker.sock"'):
                verify_local_docker()

    def test_failed_creation_is_retained_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            def fake_run(command):
                if command == ["kind", "version"]:
                    return "kind v0.31.0"
                if command == ["kind", "get", "clusters"]:
                    return ""
                if command[:3] == ["kind", "create", "cluster"]:
                    raise ValueError("creation failed")
                return ""

            with (
                patch("scripts.cluster.verify_local_docker"),
                patch("scripts.cluster.run", side_effect=fake_run) as run,
            ):
                with self.assertRaises(ValueError):
                    execute("up", root)
                self.assertFalse(any("delete" in call.args[0] for call in run.call_args_list))
            self.assertTrue((root / ".local/perfeng-local.kubeconfig").exists())

    def test_confirmed_teardown_only_removes_dedicated_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            config = root / ".local/perfeng-local.kubeconfig"
            config.write_text("clusters: [{name: local}]")
            sentinel = root / "keep.txt"
            sentinel.write_text("keep")
            with (
                patch("scripts.cluster.verify_context") as verify,
                patch("scripts.cluster.verify_local_docker"),
                patch("scripts.cluster.run", side_effect=["kind v0.31.0", NAME, ""]) as run,
            ):
                with patch("builtins.print"):
                    execute("down", root, NAME)
                verify.assert_called_once_with(root)
                self.assertEqual(run.call_args.args[0], commands("down", root)[0])
            self.assertFalse(config.exists())
            self.assertEqual(sentinel.read_text(), "keep")

    def test_commands_always_pin_context_and_kubeconfig(self):
        for action in ["up", "deploy", "health", "down"]:
            for command in commands(action):
                self.assertIn("--kubeconfig", command)
                if command[0] == "kubectl":
                    self.assertEqual(command[command.index("--context") + 1], CONTEXT)
                if command[0] == "helm":
                    self.assertEqual(command[command.index("--kube-context") + 1], CONTEXT)
        self.assertFalse(any("delete" in command for command in commands("up")))

    def test_down_requires_confirmation_before_any_command(self):
        with patch("scripts.cluster.run") as run:
            with self.assertRaises(ValueError):
                execute("down")
            run.assert_not_called()

    def test_up_never_recreates_existing_cluster(self):
        with patch("scripts.cluster.run", side_effect=["kind v0.31.0", NAME]) as run:
            with self.assertRaises(ValueError):
                execute("up")
            self.assertEqual(run.call_count, 2)

    def test_wrong_kind_version_is_rejected(self):
        with patch("scripts.cluster.run", return_value="kind v0.30.0") as run:
            with self.assertRaises(ValueError):
                execute("up")
            self.assertEqual(run.call_count, 1)

    def test_foreign_kubeconfig_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            config = root / ".local/perfeng-local.kubeconfig"
            config.write_text("clusters: [{name: production}]")
            with patch("scripts.cluster.run", return_value="clusters: [{name: local}]"):
                with self.assertRaises(ValueError):
                    verify_context(root)

    def test_matching_kubeconfig_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            config = root / ".local/perfeng-local.kubeconfig"
            value = {"clusters": [{"name": CONTEXT}], "users": [], "contexts": []}
            config.write_text(yaml.safe_dump(value))
            with patch("scripts.cluster.run", return_value=yaml.safe_dump(value)):
                verify_context(root)

    def test_node_readiness_and_pressure_fail_closed(self):
        healthy = {
            "items": [
                {
                    "metadata": {"labels": {"workload": role}},
                    "status": {
                        "conditions": [
                            {"type": "Ready", "status": "True"},
                            *[
                                {"type": kind, "status": "False"}
                                for kind in ["MemoryPressure", "DiskPressure", "PIDPressure"]
                            ],
                        ]
                    },
                }
                for role in ["control-plane", "performance-generator", "sut"]
            ]
        }
        check_nodes(healthy)
        for status in ["True", "Unknown"]:
            data = json.loads(json.dumps(healthy))
            data["items"][1]["status"]["conditions"][1]["status"] = status
            with self.assertRaises(ValueError):
                check_nodes(data)
        for data in [
            {"items": []},
            {"items": healthy["items"][:2]},
            {"items": [{"metadata": {}, "status": {}} for _ in range(3)]},
        ]:
            with self.assertRaises(ValueError):
                check_nodes(data)

    def test_preview_has_no_execution(self):
        with patch("scripts.cluster.run") as run:
            for action in ["up", "deploy", "health", "down"]:
                self.assertTrue(commands(action))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
