"""Offline checks at the minimal networking resource and execution boundaries."""

import unittest

from scripts import network_resources


class MinimalNetworkTests(unittest.TestCase):
    def test_effective_resources_include_overlapping_sidecars_and_overhead(self):
        def container(name, cpu, memory, **fields):
            resources = {"cpu": cpu, "memory": memory}
            return {
                "name": name,
                "resources": {"requests": resources, "limits": resources},
                **fields,
            }

        spec = {
            "containers": [container("agent", "100m", "256Mi")],
            "initContainers": [
                container("first", "50m", "128Mi", restartPolicy="Always"),
                container("prepare", "200m", "512Mi"),
                container("second", "25m", "64Mi", restartPolicy="Always"),
                container("install", "100m", "256Mi"),
            ],
            "overhead": {"cpu": "5m", "memory": "32Mi"},
        }
        evidence = network_resources.pod_bounds(spec)
        self.assertEqual(evidence["effectiveRequests"], {"cpu": 255, "memory": 672 * 1024**2})
        self.assertEqual(evidence["effectiveLimits"], {"cpu": 255, "memory": 672 * 1024**2})
        self.assertEqual(evidence["steadyLimits"], {"cpu": 180, "memory": 480 * 1024**2})

    def test_unbounded_or_underrequested_initializers_refuse_accounting(self):
        app = network_resources.resources(100, 1000, 256, 512)
        for init in ({}, network_resources.resources(100, 1000, 128, 512)):
            with self.subTest(init=init), self.assertRaisesRegex(ValueError, "Initializer|finite"):
                network_resources.pod_bounds(
                    {
                        "containers": [{"name": "agent", "resources": app}],
                        "initContainers": [{"name": "setup", "resources": init}],
                    }
                )

    def test_foundation_requests_over_ledger_refuse_before_controller_creation(self):
        from scripts.minimal_network import check_foundation_bounds

        evidence = {
            "control-plane": {"cpu": 650, "memory": 100 * 1024**2},
            "worker": {"cpu": 200, "memory": 140 * 1024**2},
            "unassigned": {"cpu": 0, "memory": 0},
        }
        with self.assertRaisesRegex(ValueError, "850m.*800m"):
            check_foundation_bounds(evidence)

    def test_minimal_network_preview_never_loads_private_admission_or_runs_a_provider(self):
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from scripts.network import main

        with (
            tempfile.TemporaryDirectory() as state,
            redirect_stdout(io.StringIO()) as output,
            patch(
                "scripts.consumer_runtime.command", side_effect=AssertionError("provider accessed")
            ),
            patch(
                "scripts.consumer_state.read_bounded",
                side_effect=AssertionError("private state read"),
            ),
        ):
            code = main(
                [
                    "qualify",
                    "--profile",
                    "minimal-demo",
                    "--consumer",
                    "qual-neutral",
                    "--docker-context",
                    "local-engine",
                    "--state-root",
                    state,
                    "--https-port",
                    "18443",
                    "--request",
                    "/missing/private/request",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")

    def test_initialized_pod_with_oom_or_restart_cannot_qualify(self):
        from scripts.minimal_network import completed_containers

        start = "2026-11-22T12:00:00Z"
        end = "2026-11-22T12:00:02Z"
        image = "sha256:" + "a" * 64
        digest = "sha256:" + "b" * 64
        repo = "quay.io/cilium/cilium"
        reference = f"{repo}:v1.20.3@{digest}"
        staged = {reference: {"configDigest": image, "repoDigest": f"{repo}@{digest}"}}
        spec = {
            "containers": [
                {
                    "name": "agent",
                    "image": reference,
                    "resources": network_resources.resources(100, 1000, 256, 512),
                }
            ],
            "initContainers": [
                {
                    "name": "setup",
                    "image": reference,
                    "resources": network_resources.resources(100, 1000, 512, 512),
                }
            ],
        }
        init = {
            "name": "setup",
            "restartCount": 0,
            "imageID": image,
            "state": {"terminated": {"exitCode": 0, "startedAt": start, "finishedAt": end}},
        }
        pod = {
            "spec": spec,
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "initContainerStatuses": [init],
                "containerStatuses": [
                    {
                        "name": "agent",
                        "ready": True,
                        "restartCount": 0,
                        "imageID": f"docker-pullable://{repo}@{digest}",
                        "state": {"running": {"startedAt": end}},
                    }
                ],
            },
        }
        bounds = network_resources.pod_bounds(spec)
        self.assertEqual(completed_containers(pod, bounds, staged)["setup"]["finishedAt"], end)
        for updates in ({"exitCode": 137}, {"reason": "OOMKilled"}):
            termination = init["state"]["terminated"]
            prior = termination.copy()
            termination.update(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "Initializer"):
                completed_containers(pod, bounds, staged)
            termination.clear()
            termination.update(prior)
        for other in ("sha256:" + "c" * 64, f"docker-pullable://quay.io/other/agent@{digest}"):
            with self.subTest(image=other), self.assertRaisesRegex(ValueError, "staged image"):
                init["imageID"] = other
                completed_containers(pod, bounds, staged)
        init["imageID"] = f"containerd://{image}"
        self.assertEqual(completed_containers(pod, bounds, staged)["setup"]["finishedAt"], end)
        init["restartCount"] = 1
        with self.assertRaisesRegex(ValueError, "restarted"):
            completed_containers(pod, bounds, staged)

    def test_installed_fixture_refuses_admission_added_authority(self):
        from copy import deepcopy
        from pathlib import Path
        from unittest.mock import patch

        from scripts import minimal_network, network_fixtures
        from scripts.consumer_profile import MINIMAL_PROFILE, Target

        target = Target("qual-neutral", "local-engine", Path("/tmp"), MINIMAL_PROFILE, 18443)
        document = next(
            item for item in network_fixtures.workloads(target) if item["kind"] == "Pod"
        )
        pod = deepcopy(document)
        pod["metadata"]["uid"] = "pod-uid"
        spec = pod["spec"]
        spec.update(nodeName=f"{target.name}-worker", serviceAccountName="default")
        container = spec["containers"][0]
        digest = "sha256:" + "b" * 64
        staged = {spec["nodeName"]: {container["image"]: {"configDigest": digest}}}
        pod["status"] = {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": container["name"],
                    "ready": True,
                    "restartCount": 0,
                    "imageID": digest,
                    "state": {"running": {"startedAt": "2026-11-22T12:00:00Z"}},
                }
            ],
        }
        for changed in (
            {"hostPID": True},
            {"serviceAccountName": "unexpected"},
            {"automountServiceAccountToken": True},
            {"volumes": [{"name": "extra", "hostPath": {"path": "/"}}]},
        ):
            with (
                self.subTest(changed=changed),
                patch.object(minimal_network, "list_resources", return_value=[pod]),
            ):
                minimal_network.fixture_pods(target, [document], staged)
                with patch.dict(spec, changed), self.assertRaisesRegex(ValueError, "authority"):
                    minimal_network.fixture_pods(target, [document], staged)
        with (
            patch.object(minimal_network, "list_resources", return_value=[pod]),
            patch.dict(container["securityContext"], {"privileged": True}),
            self.assertRaisesRegex(ValueError, "authority"),
        ):
            minimal_network.fixture_pods(target, [document], staged)

    def test_asset_timeout_stops_qualification_before_provider_access(self):
        import subprocess
        from pathlib import Path
        from unittest.mock import patch

        from scripts import bootstrap_failure, minimal_bootstrap, minimal_network, network_assets
        from scripts.consumer_profile import MINIMAL_PROFILE, Target

        target = Target("qual-neutral", "local-engine", Path("/tmp"), MINIMAL_PROFILE, 18443)
        receipt = {"artifactDigest": minimal_bootstrap.source_digest()}

        def timed_out(command, *, timeout, **kwargs):
            self.assertGreater(timeout, 0)
            self.assertLess(timeout, 20)
            raise subprocess.TimeoutExpired(command, timeout)

        with (
            minimal_network.runtime.deadline(20),
            patch.object(network_assets, "urlopen", side_effect=AssertionError("unbounded fetch")),
            patch.object(network_assets.subprocess, "run", side_effect=timed_out) as child,
            patch.object(minimal_network.runtime, "command") as provider,
            patch.object(minimal_network.network, "install_apis") as mutation,
            self.assertRaises(bootstrap_failure.Failure) as refused,
        ):
            minimal_network.install(target, [], receipt, Path("/unused"), lambda: None)
        self.assertEqual(
            refused.exception.evidence,
            {
                "category": "timeout",
                "phase": "network-assets",
                "timedOut": True,
            },
        )
        child.assert_called_once()
        provider.assert_not_called()
        mutation.assert_not_called()

    def test_fixture_budget_refuses_overlapping_oversized_init(self):
        from pathlib import Path

        from scripts.consumer_profile import MINIMAL_PROFILE, Target

        target = Target("qual-neutral", "local-engine", Path("/tmp"), MINIMAL_PROFILE, 18443)
        bounds = network_resources.pod_bounds(
            {
                "containers": [
                    {"name": "agent", "resources": network_resources.resources(100, 1000, 256, 512)}
                ],
                "initContainers": [
                    {
                        "name": "setup",
                        "resources": network_resources.resources(100, 1000, 1025, 1025),
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "reviewed memory allowance"):
            network_resources.stage_fit(target, {"cilium": bounds}, next_controller="cilium")

    def test_each_initializer_requires_all_four_finite_resources(self):
        from copy import deepcopy

        app = {"name": "cilium-agent", "resources": network_resources.MAIN["cilium"]}
        initial: list[dict] = [
            {"name": name, "resources": network_resources.resources(100, 1000, memory, memory)}
            for name, memory in network_resources.INIT.items()
        ]
        spec: dict = {"containers": [app], "initContainers": initial}
        bounds = network_resources.pod_bounds(spec)
        self.assertEqual(list(bounds["containers"]), [app["name"], *network_resources.INIT])
        self.assertEqual(bounds["effectiveLimits"]["memory"], 1024 * 1024**2)
        for index, initializer in enumerate(initial):
            for kind in ("requests", "limits"):
                for key in ("cpu", "memory"):
                    changed = deepcopy(spec)
                    del changed["initContainers"][index]["resources"][kind][key]
                    with (
                        self.subTest(initializer=initializer["name"], kind=kind, key=key),
                        self.assertRaisesRegex(ValueError, "lacks finite"),
                    ):
                        network_resources.pod_bounds(changed)
