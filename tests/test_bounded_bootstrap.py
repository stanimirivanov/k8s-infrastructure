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
                self.assertEqual(args[-1], self.image)
                for flag, value in (
                    ("--cpus", "2"),
                    ("--memory", "4294967296"),
                    ("--memory-swap", "4294967296"),
                    ("--pids-limit", "2048"),
                ):
                    self.assertEqual(args[args.index(flag) + 1], value)
                return self.node["id"] + "\n"
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

                def docker(args):
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
