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
