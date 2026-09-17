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

from scripts.local_kind import (
    ACTIONS,
    CONTEXT,
    NAME,
    NODE_IMAGE,
    PROFILE,
    check_nodes,
    commands,
    ensure,
    execute,
    load_profile,
    main,
    normalize_state_root,
    run,
    verify_context,
    verify_local_docker,
)


class ClusterTests(unittest.TestCase):
    def test_state_root_normalization_returns_the_canonical_ordinary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            self.assertEqual(normalize_state_root(root), root.resolve(strict=True))

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
        with patch("scripts.local_kind.subprocess.Popen") as popen, redirect_stdout(io.StringIO()):
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
        with patch("scripts.local_kind.subprocess.Popen") as popen, redirect_stdout(captured):
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

    def test_command_timeout_terminates_the_process(self):
        with (
            patch("scripts.local_kind.subprocess.Popen") as popen,
            patch("scripts.local_kind.time.monotonic", side_effect=[0.0, 601.0]),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "timed out after 600s"),
        ):
            process = popen.return_value.__enter__.return_value
            process.communicate.return_value = (b"", b"")
            run(["kubectl"])

        process.kill.assert_called_once_with()
        process.communicate.assert_called_once_with()

    def test_invalid_stdout_fails_without_false_completion(self):
        captured = io.StringIO()
        with redirect_stdout(captured), self.assertRaisesRegex(ValueError, "invalid UTF-8"):
            run([sys.executable, "-c", "import os; os.write(1, b'\\x8f')"])
        self.assertNotIn("Completed", captured.getvalue())

    def test_remote_docker_endpoints_rejected(self):
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2376"}),
            patch("scripts.local_kind.run") as run,
        ):
            with self.assertRaises(ValueError):
                verify_local_docker()
            run.assert_not_called()
        with patch.dict(os.environ, {}, clear=True):
            with patch("scripts.local_kind.run", return_value='"ssh://remote"'):
                with self.assertRaises(ValueError):
                    verify_local_docker()
            with patch("scripts.local_kind.run", return_value='"unix:///var/run/docker.sock"'):
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
                patch("scripts.local_kind.verify_local_docker"),
                patch("scripts.local_kind.run", side_effect=fake_run) as run,
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
                patch("scripts.local_kind.verify_context") as verify,
                patch("scripts.local_kind.verify_local_docker"),
                patch("scripts.local_kind.run", side_effect=["kind v0.31.0", NAME, ""]) as run,
            ):
                with patch("builtins.print"):
                    execute("down", root, NAME)
                verify.assert_called_once_with(root)
                self.assertEqual(run.call_args.args[0], commands("down", root)[0])
            self.assertFalse(config.exists())
            self.assertEqual(sentinel.read_text(), "keep")

    def test_commands_always_pin_context_and_kubeconfig(self):
        for action in ["up", "health", "down"]:
            for command in commands(action):
                self.assertIn("--kubeconfig", command)
                if command[0] == "kubectl":
                    self.assertEqual(command[command.index("--context") + 1], CONTEXT)
                self.assertNotEqual(command[0], "helm")
        self.assertFalse(any("delete" in command for command in commands("up")))

    def test_down_requires_confirmation_before_any_command(self):
        with patch("scripts.local_kind.run") as run:
            with self.assertRaises(ValueError):
                execute("down")
            run.assert_not_called()

    def test_unknown_execution_action_fails_before_any_command(self):
        with patch("scripts.local_kind.run") as run:
            with self.assertRaisesRegex(ValueError, "Unknown executable"):
                execute("ensure")
            run.assert_not_called()

    def test_up_never_recreates_existing_cluster(self):
        with patch("scripts.local_kind.run", side_effect=["kind v0.31.0", NAME]) as run:
            with self.assertRaises(ValueError):
                execute("up")
            self.assertEqual(run.call_count, 2)

    def test_ensure_reuses_only_a_healthy_owned_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            (root / ".local/perfeng-local.kubeconfig").write_text("owned")
            with (
                patch("scripts.local_kind.run", return_value=NAME),
                patch("scripts.local_kind.execute") as execute,
            ):
                ensure(root)
            execute.assert_called_once_with("health", root)

    def test_ensure_creates_only_when_cluster_and_kubeconfig_are_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch("scripts.local_kind.run", return_value=""),
                patch("scripts.local_kind.execute") as execute,
            ):
                ensure(root)
            execute.assert_called_once_with("up", root)

    def test_ensure_rejects_incomplete_local_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            config = root / ".local/perfeng-local.kubeconfig"
            config.write_text("orphaned")
            with patch("scripts.local_kind.run", return_value=""):
                with self.assertRaisesRegex(ValueError, "without the named local cluster"):
                    ensure(root)
            config.unlink()
            with patch("scripts.local_kind.run", return_value=NAME):
                with self.assertRaisesRegex(ValueError, "without its dedicated kubeconfig"):
                    ensure(root)

    def test_wrong_kind_version_is_rejected(self):
        with patch("scripts.local_kind.run", return_value="kind v0.30.0") as run:
            with self.assertRaises(ValueError):
                execute("up")
            self.assertEqual(run.call_count, 1)

    def test_foreign_kubeconfig_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            config = root / ".local/perfeng-local.kubeconfig"
            config.write_text("clusters: [{name: production}]")
            with patch("scripts.local_kind.run", return_value="clusters: [{name: local}]"):
                with self.assertRaises(ValueError):
                    verify_context(root)

    def test_matching_kubeconfig_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".local").mkdir()
            config = root / ".local/perfeng-local.kubeconfig"
            value = {"clusters": [{"name": CONTEXT}], "users": [], "contexts": []}
            config.write_text(yaml.safe_dump(value))
            with patch("scripts.local_kind.run", return_value=yaml.safe_dump(value)):
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
        with patch("scripts.local_kind.run") as run:
            for action in ["up", "health", "down"]:
                self.assertTrue(commands(action))
            run.assert_not_called()

    def test_profile_preserves_reviewed_cluster_identity(self):
        profile = load_profile()

        self.assertEqual(PROFILE.parts[-3:], ("local-kind", "perfeng-local", "cluster-config.yaml"))
        self.assertEqual(profile["name"], NAME)
        self.assertEqual({node["image"] for node in profile["nodes"]}, {NODE_IMAGE})
        self.assertEqual(ACTIONS, ("up", "ensure", "health", "down"))

    def test_changed_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster-config.yaml"
            profile = load_profile()
            profile["networking"]["apiServerAddress"] = "0.0.0.0"
            path.write_text(yaml.safe_dump(profile), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "compatibility identity"):
                load_profile(path)

    def test_live_cli_dispatches_the_selected_action_and_state_root(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.local_kind.execute") as execute,
        ):
            self.assertEqual(main(["health", "--execute", "--state-root", directory]), 0)

        execute.assert_called_once_with("health", Path(directory).resolve(), None)

    def test_live_ensure_cli_uses_the_idempotent_entry_point(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.local_kind.ensure") as ensure,
            patch("scripts.local_kind.execute") as execute,
        ):
            self.assertEqual(main(["ensure", "--execute", "--state-root", directory]), 0)

        ensure.assert_called_once_with(Path(directory).resolve())
        execute.assert_not_called()

    def test_preview_cli_is_offline(self):
        captured = io.StringIO()
        with patch("scripts.local_kind.run") as run, redirect_stdout(captured):
            self.assertEqual(main(["up"]), 0)

        run.assert_not_called()
        preview = captured.getvalue().splitlines()
        self.assertIn(str(PROFILE), json.loads(preview[0]))
        self.assertIn("Add --execute", captured.getvalue())

    def test_preview_accepts_explicit_consumer_state_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            captured = io.StringIO()
            with patch("scripts.local_kind.run") as run, redirect_stdout(captured):
                self.assertEqual(main(["health", "--state-root", str(root)]), 0)

        run.assert_not_called()
        command = json.loads(captured.getvalue().splitlines()[0])
        self.assertIn(str(root / ".local/perfeng-local.kubeconfig"), command)


if __name__ == "__main__":
    unittest.main()
