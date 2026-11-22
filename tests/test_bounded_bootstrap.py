"""Public bounded profile selection and Docker creation protocol contracts."""

import tempfile
import unittest
from pathlib import Path

from scripts.consumer_profile import Target, configuration, load_profile
from scripts.node_capacity import envelope


class ProfileTests(unittest.TestCase):
    def test_node_observation_reads_docker_scope_above_systemd_init(self):
        from unittest.mock import patch

        from scripts import minimal_bootstrap as bootstrap

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = Target("qual-cold", "default", root, "minimal-demo")
            node_id = "a" * 64
            node = f"{target.name}-control-plane"
            memory = 4 * 1024**3
            scope = root / "system.slice" / f"docker-{node_id}.scope"
            (scope / "init.scope").mkdir(parents=True)
            for name, data in {
                "cpu.max": "200000 100000\n",
                "memory.max": str(memory),
                "memory.swap.max": "0",
                "pids.max": "2048",
                "memory.peak": "65536",
                "memory.events": "oom 0\noom_kill 0\n",
                "cpu.stat": "usage_usec 1234\n",
            }.items():
                (scope / name).write_text(data, encoding="utf-8")
            read_text = Path.read_text
            journal = {
                "id": node_id,
                "name": node,
                "role": "control-plane",
                "startRequestedAt": 1_000_000_000,
                "startCompletedAt": 1_000_000_000,
            }

            def read(path: Path, *args, **kwargs) -> str:
                if path == Path("/proc/123/cgroup"):
                    return f"0::/{scope.relative_to(root)}/init.scope\n"
                return read_text(path, *args, **kwargs)

            with (
                patch.object(bootstrap.host, "CGROUP_ROOT", root),
                patch.object(Path, "read_text", read),
                patch.object(bootstrap.runtime, "inspect_node", return_value={"swap": memory}),
                patch.object(bootstrap.runtime, "verify_nodes"),
                patch.object(
                    bootstrap.runtime,
                    "command",
                    return_value='{"running":true,"restarts":0,"pid":123,"startedAt":"1970-01-01T00:00:01Z"}',
                ),
            ):
                evidence = bootstrap.node_metrics(target, [journal])[node]
            self.assertEqual(evidence["memoryPeakBytes"], 65536)
            self.assertEqual(evidence["cpuStat"], {"usage_usec": 1234})
            self.assertEqual(
                evidence["cgroupIdentity"],
                bootstrap.admission.digest([scope.stat().st_dev, scope.stat().st_ino]),
            )

    def test_minimal_https_listener_is_opt_in_loopback_and_bound_to_owner(self):
        import yaml

        from scripts.consumer_profile import identity

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = Target("qual-cold", "default", root, "minimal-demo", 18443)
            nodes = configuration(target)["nodes"]
            self.assertNotIn("extraPortMappings", nodes[0])
            self.assertEqual(
                nodes[1]["extraPortMappings"],
                [
                    {
                        "containerPort": 8443,
                        "hostPort": 18443,
                        "listenAddress": "127.0.0.1",
                        "protocol": "TCP",
                    }
                ],
            )
            self.assertEqual(identity(target)["httpsPort"], "18443")
            self.assertEqual(
                yaml.safe_load(nodes[0]["kubeadmConfigPatches"][0]),
                {
                    "apiVersion": "kubeadm.k8s.io/v1beta3",
                    "kind": "InitConfiguration",
                    "patches": {"directory": "/kind/minimal-demo-patches"},
                },
            )
            self.assertNotIn("kubeadmConfigPatches", nodes[1])
            self.assertNotIn(
                "httpsPort", identity(Target("qual-cold", "default", root, "minimal-demo"))
            )
            with self.assertRaises(ValueError):
                Target("qual-cold", "default", root, "minimal-demo", 443)

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
    def test_network_foundation_is_bounded_before_kind_can_run_kubeadm(self):
        import json

        import yaml

        from scripts import bounded_docker as adapter
        from scripts.database_assets import selection

        target = Target("qual-cold", "default", self.root, "minimal-demo", 18443)
        storage = {
            "kind": "Deployment",
            "metadata": {"name": "local-path-provisioner", "namespace": "local-path-storage"},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [{"name": "local-path-provisioner", "image": "untouched"}]
                    }
                },
            },
        }
        helper_image = selection()["images"]["kind-helper"]
        config = {
            "kind": "ConfigMap",
            "metadata": {"name": "local-path-config", "namespace": "local-path-storage"},
            "data": {
                "helperPod.yaml": yaml.safe_dump(
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {"name": "helper-pod"},
                        "spec": {
                            "containers": [
                                {"name": "helper-pod", "image": helper_image.split("@")[0]}
                            ]
                        },
                    }
                )
            },
        }
        calls = []

        def docker(command):
            calls.append(command)
            if command[0] == "inspect":
                return json.dumps(
                    {
                        **self.node,
                        "ports": {"6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "34567"}]},
                    }
                )
            if command[:1] == ["exec"] and command[2] == "cat":
                return yaml.safe_dump_all([storage, config])
            return self.node["id"]

        adapter.intercept(self.args, adapter.policy(target, "owner-nonce", self.root), docker)
        self.assertEqual([call[0] for call in calls[:3]], ["create", "inspect", "start"])
        phase_commands = [call for call in calls[3:] if call[0] == "exec"]
        for call in phase_commands:
            self.assertEqual(call[1], self.node["id"])
        patches = {call[-2].split("/")[-1]: call[-1] for call in phase_commands if len(call) > 4}
        static_names = ("etcd", "kube-apiserver", "kube-controller-manager", "kube-scheduler")
        for name in static_names:
            container = json.loads(patches[f"{name}+strategic.json"])["spec"]["containers"][0]
            self.assertEqual(container["name"], name)
            self.assertEqual(set(container["resources"]["limits"]), {"cpu", "memory"})
        dns = json.loads(patches["corednsdeployment+strategic.json"])["spec"]["template"]["spec"]
        self.assertEqual(dns["nodeSelector"]["k8s-infrastructure.io/role"], "worker")
        self.assertEqual(dns["containers"][0]["resources"]["requests"]["cpu"], "25m")
        result, rendered_config = yaml.safe_load_all(patches["default-storage.yaml"])
        provisioner = result["spec"]["template"]["spec"]
        self.assertEqual(provisioner["containers"][0]["image"], "untouched")
        self.assertEqual(provisioner["containers"][0]["resources"]["limits"]["memory"], "64Mi")
        helper = yaml.safe_load(rendered_config["data"]["helperPod.yaml"])["spec"]["containers"][0]
        self.assertEqual(helper["image"], helper_image)
        self.assertEqual(helper["imagePullPolicy"], "Never")
        self.assertEqual(
            helper["resources"],
            {
                "requests": {"cpu": "25m", "memory": "64Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi"},
            },
        )

    def test_worker_gateway_publish_is_exact_and_inspected_before_start(self):
        import json

        from scripts import bounded_docker as adapter

        target = Target("qual-cold", "default", self.root, "minimal-demo", 18443)
        name = f"{target.name}-worker"
        selection = adapter.policy(target, "owner-nonce", self.root)
        publish = "--publish=127.0.0.1:18443:8443/tcp"
        args = [
            part.replace(self.name, name).replace("role=control-plane", "role=worker")
            for part in self.args[:-4]
        ] + [publish, self.image]
        for mapping in ("--publish=0.0.0.0:18443:8443/tcp", "--publish=127.0.0.1:18444:8443/tcp"):
            with self.assertRaises(ValueError):
                adapter.node_arguments([*args[:-2], mapping, self.image], selection)

        actions = []

        def docker(command):
            actions.append(command[0])
            if command[0] == "create":
                self.assertIn(publish, command)
                self.assertEqual(command[command.index("--memory") + 1], "8589934592")
                return self.node["id"]
            return json.dumps(
                {
                    **self.node,
                    "name": "/" + name,
                    "role": "worker",
                    "memory": 8589934592,
                    "swap": 8589934592,
                    "ports": {"8443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18443"}]},
                }
            )

        with self.assertRaisesRegex(ValueError, "listener"):
            adapter.intercept(args, selection, docker)
        self.assertEqual(actions, ["create", "inspect"])

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
