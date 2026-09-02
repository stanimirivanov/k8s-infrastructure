import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.cluster import (
    CONTEXT,
    NAME,
    check_nodes,
    commands,
    execute,
    run,
    verify_context,
    verify_local_docker,
)


class ClusterTests(unittest.TestCase):
    def test_private_stdin_is_delivered_without_printing(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            output = run(
                [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
                input_data=b"private-password",
            )
        self.assertEqual(output.strip(), "16")
        self.assertNotIn("private-password", captured.getvalue())

    def test_private_stdin_is_not_resubmitted_after_timeout(self):
        with patch("scripts.cluster.subprocess.Popen") as popen, redirect_stdout(io.StringIO()):
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.side_effect = [subprocess.TimeoutExpired("kubectl", 10), (b"", b"")]
            run(["kubectl", "create", "-f", "-"], input_data=b"private-password")
            self.assertEqual(
                process.communicate.call_args_list[0].kwargs["input"], b"private-password"
            )
            self.assertIsNone(process.communicate.call_args_list[1].kwargs["input"])
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)

    def test_utf8_output_and_non_locale_stderr(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            output = run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os; os.write(1, b'caf\\xc3\\xa9'); "
                        "os.write(2, b'private-progress-\\xe2\\x8f\\xb3\\x8f')"
                    ),
                ]
            )
        self.assertEqual(output, "caf\u00e9")
        self.assertIn("Starting", captured.getvalue())
        self.assertIn("Completed", captured.getvalue())
        self.assertNotIn("private-progress", captured.getvalue())

    def test_waiting_progress_preserves_private_output(self):
        captured = io.StringIO()
        with patch("scripts.cluster.subprocess.Popen") as popen, redirect_stdout(captured):
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.side_effect = [
                subprocess.TimeoutExpired("kind", 10),
                (b"private-kubeconfig", b"private-stderr"),
            ]
            self.assertEqual(run(["kind", "get", "kubeconfig"]), "private-kubeconfig")
        self.assertIn("Still running kind command", captured.getvalue())
        self.assertNotIn("private-", captured.getvalue())
        self.assertEqual(process.communicate.call_count, 2)
        self.assertNotIn("text", popen.call_args.kwargs)

    def test_failure_withholds_output_and_completion(self):
        captured = io.StringIO()
        with redirect_stdout(captured), self.assertRaisesRegex(ValueError, "exit 7"):
            run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os; os.write(1, b'private-output'); "
                        "os.write(2, b'private-error'); raise SystemExit(7)"
                    ),
                ]
            )
        self.assertNotIn("private-", captured.getvalue())
        self.assertNotIn("Completed", captured.getvalue())

    def test_invalid_stdout_fails_without_false_completion(self):
        captured = io.StringIO()
        with redirect_stdout(captured), self.assertRaisesRegex(ValueError, "invalid UTF-8"):
            run([sys.executable, "-c", "import os; os.write(1, b'\\x8f')"])
        self.assertNotIn("Completed", captured.getvalue())

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
