"""Public bounded profile selection and Docker creation protocol contracts."""

import tempfile
import unittest
from pathlib import Path

from scripts.consumer_profile import Target, configuration, load_profile
from scripts.node_capacity import envelope


class ProfileTests(unittest.TestCase):
    def test_minimal_demo_has_exact_distinct_budgets_without_cni_installation(self):
        profile = load_profile(name="minimal-demo")
        self.assertEqual(profile["nodes"]["control-plane"], {"cpus": 2, "memoryBytes": 4294967296})
        self.assertEqual(profile["nodes"]["worker"], {"cpus": 2, "memoryBytes": 8589934592})
        self.assertEqual(
            envelope(profile["nodes"]["control-plane"]),
            {"cpuMillicores": 1750, "memoryBytes": 3653238784},
        )
        self.assertEqual(
            envelope(profile["nodes"]["worker"]), {"cpuMillicores": 1750, "memoryBytes": 7948206080}
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-cold", "default", Path(directory), "minimal-demo")
            config = configuration(target)
        self.assertTrue(config["networking"]["disableDefaultCNI"])
        self.assertEqual(config["networking"]["kubeProxyMode"], "none")
        self.assertNotIn("extraPortMappings", config["nodes"][1])
        self.assertEqual(load_profile()["nodes"]["control-plane"]["cpus"], 1)

    def test_generic_lifecycle_refuses_minimal_demo_before_provider_access(self):
        from unittest.mock import patch

        from scripts import consumer_lifecycle as lifecycle

        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-cold", "default", Path(directory), "minimal-demo")
            with patch.object(lifecycle.runtime, "command") as command:
                for operation in (
                    lifecycle.create,
                    lifecycle.ensure,
                    lifecycle.health,
                    lifecycle.observe,
                ):
                    with (
                        self.subTest(operation=operation.__name__),
                        self.assertRaisesRegex(ValueError, "minimal_bootstrap"),
                    ):
                        operation(target)
                with self.assertRaisesRegex(ValueError, "minimal_bootstrap"):
                    lifecycle.down(target, target.name)
                command.assert_not_called()


class DockerProtocolTests(unittest.TestCase):
    def test_adapter_failure_retains_safe_journal(self):
        import json
        from unittest.mock import patch

        from scripts import bounded_docker

        selection = bounded_docker.policy(self.target, "nonce", self.root)
        policy = self.root / "policy.json"
        policy.write_text(json.dumps(selection))
        with (
            patch.dict(
                "os.environ",
                {"BOUNDED_DOCKER_POLICY": str(policy), "BOUNDED_DOCKER_REAL": "/usr/bin/docker"},
            ),
            patch("sys.argv", ["shim", "pull", self.image]),
        ):
            self.assertEqual(bounded_docker.main(), 1)
        errors = list(self.root.glob("failure-*.json"))
        self.assertEqual(len(errors), 1)
        error = json.loads(errors[0].read_text())
        self.assertEqual(error["failure"]["category"], "unsupported-capability")
        self.assertEqual(error["failure"]["phase"], "adapter-protocol")

    def test_adapter_refuses_even_the_pinned_image_pull(self):
        from scripts import bounded_docker

        with self.assertRaises(ValueError):
            bounded_docker.forward(["pull", "exact-image"], {"image": "exact-image"})

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = Target("qual-cold", "default", self.root, "minimal-demo")
        self.name = self.target.name + "-control-plane"
        self.image = load_profile(name="minimal-demo")["nodeImage"]
        self.args = [
            "run",
            "--name",
            self.name,
            "--hostname",
            self.name,
            "--label",
            "io.x-k8s.kind.role=control-plane",
            "--privileged",
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "apparmor=unconfined",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "--volume",
            "/var",
            "--volume",
            "/lib/modules:/lib/modules:ro",
            "-e",
            "KIND_EXPERIMENTAL_CONTAINERD_SNAPSHOTTER",
            "--detach",
            "--tty",
            "--label",
            "io.x-k8s.kind.cluster=" + self.target.name,
            "--net",
            "kind",
            "--restart=on-failure:1",
            "--init=false",
            "--cgroupns=private",
            "--publish=127.0.0.1:34567:6443/tcp",
            "-e",
            "KUBECONFIG=/etc/kubernetes/admin.conf",
            self.image,
        ]
        self.node = {
            "id": "a" * 64,
            "name": "/" + self.name,
            "cluster": self.target.name,
            "role": "control-plane",
            "image": self.image,
            "memory": 4294967296,
            "swap": 4294967296,
            "cpu": 2000000000,
            "pids": 2048,
            "running": False,
            "startedAt": "0001-01-01T00:00:00Z",
            "restartCount": 0,
        }

    def test_first_process_start_follows_caps_inspection_and_durable_identity(self):
        import json

        from scripts import bounded_docker as adapter

        events = []
        policy = adapter.policy(self.target, "owner-nonce", self.root)

        def docker(args):
            events.append(args)
            if args[0] == "create":
                self.assertIn("--pull=never", args)
                self.assertEqual(args[-1], self.image)
                for flag, value in (
                    ("--cpus", "2"),
                    ("--memory", "4294967296"),
                    ("--memory-swap", "4294967296"),
                    ("--pids-limit", "2048"),
                ):
                    self.assertEqual(args[args.index(flag) + 1], value)
                return str(self.node["id"]) + "\n"
            if args[0] == "inspect":
                return json.dumps(self.node)
            self.assertEqual(args, ["start", self.node["id"]])
            receipt = adapter.read_journals(self.root)[0]
            self.assertEqual(receipt["id"], self.node["id"])
            self.assertEqual(receipt["phase"], "prestart-verified")
            return self.node["id"]

        self.assertEqual(adapter.intercept(self.args, policy, docker).strip(), self.node["id"])
        self.assertEqual([entry[0] for entry in events], ["create", "inspect", "start"])
        self.assertEqual(adapter.read_journals(self.root)[0]["phase"], "started")

    def test_mismatched_caps_or_started_container_are_never_started(self):
        import json

        from scripts import bounded_docker as adapter

        for changed in (
            {"memory": 0},
            {"swap": -1},
            {"cpu": 0},
            {"running": True},
            {"restartCount": 1},
            {"startedAt": "2026-11-22T00:00:00Z"},
        ):
            with tempfile.TemporaryDirectory() as directory, self.subTest(changed=changed):
                policy = adapter.policy(self.target, "owner-nonce", Path(directory))
                calls = []

                def docker(args, calls=calls, changed=changed):
                    calls.append(args[0])
                    return (
                        self.node["id"]
                        if args[0] == "create"
                        else json.dumps({**self.node, **changed})
                    )

                with self.assertRaises(ValueError):
                    adapter.intercept(self.args, policy, docker)
                self.assertEqual(calls, ["create", "inspect"])
                self.assertEqual(adapter.read_journals(Path(directory))[0]["id"], self.node["id"])

    def test_partial_failure_retains_created_identity_and_refuses_retry(self):
        import json

        from scripts import bounded_docker as adapter

        policy = adapter.policy(self.target, "owner-nonce", self.root)
        calls = []

        def docker(args):
            calls.append(args[0])
            if args[0] == "create":
                return self.node["id"]
            if args[0] == "inspect":
                return json.dumps(self.node)
            raise ValueError("simulated start interruption")

        with self.assertRaises(ValueError):
            adapter.intercept(self.args, policy, docker)
        self.assertEqual(adapter.read_journals(self.root)[0]["phase"], "prestart-verified")
        with self.assertRaises(FileExistsError):
            adapter.intercept(self.args, policy, docker)
        self.assertEqual(calls, ["create", "inspect", "start"])

    def test_protocol_variants_and_unowned_mutations_fail_closed(self):
        from scripts import bounded_docker as adapter

        policy = adapter.policy(self.target, "owner-nonce", self.root)
        for args in (
            ["create", self.image],
            ["container", "run", self.image],
            ["update", self.name],
            ["start", self.name],
            ["rm", self.name],
            ["exec", "foreign", "true"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                adapter.forward(args, policy)
        for args in (
            [*self.args[:-1], "--memory=0", self.image],
            [*self.args, "sh"],
            [*self.args[:2], "foreign", *self.args[3:]],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                adapter.intercept(args, policy, lambda _: self.fail("refusal must precede Docker"))

    def test_private_executable_preserves_docker_streams_and_context_without_global_install(self):
        import json
        import os
        import subprocess
        import sys
        from unittest.mock import patch

        from scripts import bounded_docker as adapter

        docker = self.root / "real-docker"
        calls = self.root / "calls.jsonl"
        docker.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"with open({str(calls)!r}, 'a') as stream: stream.write(json.dumps([sys.argv[1:], os.environ.get('DOCKER_CONTEXT'), os.environ.get('DOCKER_HOST')]) + '\\n')\n"
            f"node = {self.node!r}\n"
            "print(json.dumps(node) if sys.argv[1] == 'inspect' else node['id'])\n"
        )
        docker.chmod(0o700)
        directory = self.root / "adapter"
        with patch.object(adapter.shutil, "which", return_value=str(docker)):
            overlay = adapter.prepare(self.target, "owner-nonce", directory)
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        environment = {
            **os.environ,
            **overlay,
            "DOCKER_HOST": "tcp://foreign:2375",
            "DOCKER_CONTEXT": "foreign",
        }
        result = subprocess.run(
            [str(directory / "docker"), *self.args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.node["id"])
        events = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual([entry[0][0] for entry in events], ["create", "inspect", "start"])
        self.assertTrue(all(event[1:] == ["default", None] for event in events))
        result = subprocess.run(
            [
                str(directory / "docker"),
                "exec",
                "--privileged",
                "-e",
                "TEST=value",
                self.name,
                "true",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(calls.read_text().splitlines()[-1])[0][4], self.node["id"])
        with (
            patch.object(adapter.shutil, "which", return_value=str(docker)),
            self.assertRaises(FileExistsError),
        ):
            adapter.prepare(self.target, "owner-nonce", directory)

    def test_worker_creation_uses_eight_gib_without_a_published_listener(self):
        import json

        from scripts import bounded_docker as adapter

        worker = self.target.name + "-worker"
        args = [
            part.replace(self.name, worker).replace("role=control-plane", "role=worker")
            for part in self.args
        ]
        args.remove("--publish=127.0.0.1:34567:6443/tcp")
        args = args[:-3] + [self.image]
        node = {
            **self.node,
            "name": "/" + worker,
            "role": "worker",
            "memory": 8589934592,
            "swap": 8589934592,
        }

        def docker(command):
            if command[0] == "create":
                self.assertEqual(command[command.index("--memory") + 1], "8589934592")
                self.assertFalse(any(part.startswith("--publish") for part in command))
            return json.dumps(node) if command[0] == "inspect" else self.node["id"]

        adapter.intercept(args, adapter.policy(self.target, "owner-nonce", self.root), docker)
        self.assertEqual(adapter.read_journals(self.root)[0]["caps"]["memoryBytes"], 8589934592)

    def test_pinned_kind_network_lookup_is_read_only_and_exact(self):
        from scripts import bounded_docker as adapter

        selection = adapter.policy(self.target, "owner-nonce", self.root)
        self.assertEqual(adapter.forward(["-v"], selection), ["-v"])
        lookup = ["network", "ls", "--filter=name=^kind$", "--format={{.ID}}"]
        self.assertEqual(adapter.forward(lookup, selection), lookup)
        for args in (["network", "create", "kind"], ["network", "rm", "kind"], ["network", "ls"]):
            with self.assertRaises(ValueError):
                adapter.forward(args, selection)


class DeadlineTests(unittest.TestCase):
    def test_scoped_provider_deadline_bounds_commands_and_refuses_after_expiry(self):
        from unittest.mock import patch

        from scripts import consumer_runtime as runtime

        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-cold", "default", Path(directory), "minimal-demo")
            with (
                patch.object(runtime.time, "monotonic", return_value=100) as clock,
                patch.object(runtime, "run", return_value="observed") as run,
            ):
                with runtime.deadline(900):
                    self.assertEqual(runtime.remaining_timeout(600), 600)
                    clock.return_value = 990
                    runtime.command(target, ["docker", "info"])
                    self.assertEqual(run.call_args.kwargs["timeout"], 10)
                    with runtime.deadline(900):
                        self.assertEqual(runtime.remaining_timeout(600), 10)
                    clock.return_value = 1000
                    with self.assertRaises(ValueError):
                        runtime.command(target, ["docker", "info"])
                    self.assertEqual(run.call_count, 1)
                runtime.command(target, ["docker", "info"])
                self.assertNotIn("timeout", run.call_args.kwargs)
