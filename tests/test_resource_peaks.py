"""Offline verification of the bounded resource peak collector's public boundaries."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from scripts import resource_peaks


class KernelCountersTests(unittest.TestCase):
    def test_memory_peak_uses_read_only_kernel_lifetime_counter_and_cpu_is_cumulative(self):
        with tempfile.TemporaryDirectory() as directory:
            cgroup = Path(directory)
            (cgroup / "memory.current").write_text("20\n")
            (cgroup / "memory.peak").write_text("80\n")
            (cgroup / "cpu.stat").write_text(
                "usage_usec 231\nuser_usec 123\nsystem_usec 108\n"
                "nr_periods 8\nnr_throttled 2\nthrottled_usec 18\n"
            )
            evidence = resource_peaks.read_cgroup(cgroup)
            self.assertEqual(evidence["memoryCurrentBytes"], 20)
            self.assertEqual(evidence["lifetimeMemoryPeakBytes"], 80)
            self.assertEqual(
                evidence["cpu"],
                {"usageUsec": 231, "periods": 8, "throttledPeriods": 2, "throttledUsec": 18},
            )
            self.assertEqual((cgroup / "memory.peak").read_text(), "80\n")


class PeakSeriesTests(unittest.TestCase):
    @staticmethod
    def sample(at, current, peak, cpu, *, identity="first"):
        return {
            "elapsedNs": at,
            "cgroupIdentity": identity,
            "memoryCurrentBytes": current,
            "lifetimeMemoryPeakBytes": peak,
            "cpu": dict(zip(resource_peaks.CPU_FIELDS.values(), cpu, strict=True)),
        }

    def test_lifetime_peak_and_interval_cpu_are_separate_from_the_sampled_memory_maximum(self):
        samples = [
            self.sample(1_000_000_000, 20, 100, (18, 7, 1, 4)),
            self.sample(2_000_000_000, 40, 100, (38, 9, 2, 8)),
            self.sample(3_000_000_000, 10, 110, (68, 12, 2, 10)),
        ]
        evidence = resource_peaks.summarize_cgroup(samples, max_gap_ns=2_000_000_000)
        self.assertEqual(evidence["state"], "observed")
        self.assertEqual(evidence["sampledMemoryMaxBytes"], 40)
        self.assertEqual(evidence["lifetimeMemoryPeakBytes"], 110)
        self.assertEqual(
            evidence["intervalCpuDelta"],
            {"usageUsec": 50, "periods": 5, "throttledPeriods": 1, "throttledUsec": 6},
        )
        self.assertEqual(evidence["cpuIntervalElapsedNs"], [1_000_000_000, 3_000_000_000])

    def test_missing_stale_and_replaced_observations_refuse_an_interval_delta(self):
        first = self.sample(1, 20, 100, (18, 7, 1, 4))
        fresh = self.sample(2, 30, 110, (38, 9, 2, 8))
        for samples in (
            [first, None, fresh],
            [first, self.sample(10, 40, 100, (38, 9, 2, 8))],
            [first, self.sample(2, 40, 100, (38, 9, 2, 8), identity="replacement")],
            [first, self.sample(2, 40, 100, (12, 9, 2, 8))],
        ):
            with self.subTest(samples=samples):
                evidence = resource_peaks.summarize_cgroup(samples, max_gap_ns=5)
                self.assertEqual(evidence["state"], "incomplete")
                self.assertIsNone(evidence["intervalCpuDelta"])
                self.assertTrue(evidence["reasons"])
        missing = resource_peaks.summarize_cgroup([None, None], max_gap_ns=5)
        self.assertEqual(missing["state"], "missing")
        self.assertIsNone(missing["sampledMemoryMaxBytes"])
        self.assertIsNone(missing["lifetimeMemoryPeakBytes"])


class CollectorBudgetTests(unittest.TestCase):
    def test_observation_refuses_unbounded_or_outside_collector_and_records_admitted_cost(self):
        from unittest.mock import patch

        from scripts import host_observation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tooling = root / "tools"
            tooling.mkdir()
            outsider = root / "outside"
            outsider.mkdir()
            for name, value in {
                "cpu.max": "100000 100000\n",
                "cpu.stat": "usage_usec 45\nnr_periods 7\nnr_throttled 0\nthrottled_usec 0\n",
                "memory.max": "100\n",
                "memory.current": "30\n",
                "memory.peak": "80\n",
                "memory.events": "oom 0\noom_kill 0\n",
                "memory.stat": "anon 20\n",
            }.items():
                (tooling / name).write_text(value)
            request = {
                "memory": [
                    {
                        "id": "tooling",
                        "kind": "tooling",
                        "cgroup": str(tooling),
                        "limitBytes": 100,
                        "cpuMillicores": 1000,
                    }
                ]
            }
            with (
                patch.object(host_observation, "CGROUP_ROOT", root),
                patch.object(host_observation, "collector_cgroup", return_value=tooling),
            ):
                charge = resource_peaks.observe_tooling(request)
                self.assertEqual(charge["tooling"]["lifetimeMemoryPeakBytes"], 80)
                self.assertEqual(charge["tooling"]["cpu"]["usageUsec"], 45)
                (tooling / "cpu.max").write_text("200000 100000\n")
                with self.assertRaisesRegex(ValueError, "Tooling CPU"):
                    resource_peaks.observe_tooling(request)
                (tooling / "cpu.max").write_text("100000 100000\n")
                with (
                    patch.object(host_observation, "collector_cgroup", return_value=outsider),
                    self.assertRaisesRegex(ValueError, "not inside"),
                ):
                    resource_peaks.observe_tooling(request)


class PodInventoryTests(unittest.TestCase):
    def test_inventory_projects_exact_attempts_and_distinguishes_a_missing_init_status(self):
        from scripts import peak_observation

        uid = "88776655-4433-2211-ffff-001122334455"
        node = "qual-demo-local-worker"
        identity = "1" * 64
        payload = {
            "items": [
                {
                    "metadata": {"uid": uid, "name": "app", "namespace": "demo"},
                    "spec": {
                        "nodeName": node,
                        "initContainers": [{"name": "setup", "restartPolicy": "Always"}],
                        "containers": [
                            {"name": "server", "env": [{"name": "TEST", "value": "private-value"}]}
                        ],
                    },
                    "status": {
                        "containerStatuses": [
                            {
                                "name": "server",
                                "containerID": f"containerd://{identity}",
                                "state": {"running": {"startedAt": "2026-11-22T00:00:00Z"}},
                                "restartCount": 0,
                            }
                        ],
                    },
                }
            ]
        }
        rows = peak_observation.pod_attempts(payload, {node}, window_start_ns=0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["containerId"], identity)
        self.assertEqual(rows[0]["podUid"], uid)
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[1]["kind"], "init")
        self.assertTrue(rows[1]["restartableInit"])
        self.assertEqual(rows[1]["state"], "missing-status")
        self.assertIsNone(rows[1]["containerId"])
        self.assertNotIn("private-value", str(rows))


class OfflineCliTests(unittest.TestCase):
    def test_preview_does_not_open_receipt_or_call_any_provider(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with (
                redirect_stdout(output),
                patch("subprocess.Popen", side_effect=AssertionError("Live process")),
            ):
                code = resource_peaks.main(
                    [
                        "collect",
                        "--consumer",
                        "qual-peaks",
                        "--docker-context",
                        "local",
                        "--state-root",
                        str(root),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")
            self.assertEqual(list(root.iterdir()), [])


class InitCoverageTests(unittest.TestCase):
    def test_expected_waiting_and_finished_frames_leave_confirmed_init_window_observable(self):
        uid = "88776655-4433-2211-ffff-001122334455"
        node = "qual-demo-local-worker"
        identities = {"app": "a" * 64, "init": "b" * 64}
        frames: list[dict] = []
        for at in (0, 3, 6, 9):
            rows = []
            for kind in ("app", "init"):
                seconds = at * 1_000_000_000
                active = kind == "app" or at in {3, 6}
                metric = (
                    {
                        **PeakSeriesTests.sample(seconds, 40 + at, 100, (18 + at, 7, 1, 4)),
                        "populated": True,
                    }
                    if active
                    else None
                )
                rows.append(
                    {
                        "node": node,
                        "podUid": uid,
                        "podName": "demo",
                        "namespace": "demo",
                        "containerName": kind,
                        "kind": kind,
                        "restartableInit": False,
                        "state": "running" if active else "waiting" if at == 0 else "terminated",
                        "containerId": None if kind == "init" and at == 0 else identities[kind],
                        "startedAtNs": 0 if kind == "app" else 2_000_000_000 if at else None,
                        "finishedAtNs": 8_000_000_000 if kind == "init" and at == 9 else None,
                        "restartCount": 0,
                        "counters": metric,
                    }
                )
            node_counters = {node: rows[0]["counters"]}
            frames.append(
                {
                    "startedElapsedNs": seconds,
                    "finishedElapsedNs": seconds + 100,
                    "nodes": node_counters,
                    "containers": rows,
                    "tooling": {},
                    "host": {"memAvailableBytes": 10},
                    "filesystems": {},
                    "initOverlap": resource_peaks.init_overlap(rows, node_counters),
                }
            )
        result = resource_peaks.summarize(frames, 3, window_start_ns=0)
        self.assertEqual(result["state"], "observed")
        init = result["containers"][identities["init"]]
        self.assertEqual(init["cpuIntervalElapsedNs"], [3_000_000_000, 6_000_000_000])
        self.assertEqual(init["intervalCpuDelta"]["usageUsec"], 3)
        self.assertEqual(result["initOverlap"]["state"], "co-observed")
        self.assertEqual(result["initOverlap"]["sampledConcurrentNodeMemoryMaxBytes"], 46)
        frames[2]["containers"][1]["counters"] = None
        incomplete = resource_peaks.summarize(frames, 3, window_start_ns=0)
        self.assertEqual(incomplete["state"], "incomplete")
        self.assertIsNone(incomplete["containers"][identities["init"]]["intervalCpuDelta"])


class NativeProcessBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "native Linux descriptor boundary")
    def test_inventory_uses_only_raw_get_and_reads_private_kubeconfig_from_descriptor(self):
        import os
        from unittest.mock import patch

        from scripts import peak_observation
        from scripts.consumer_profile import MINIMAL_PROFILE, Target

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kubectl = root / "kubectl"
            kubectl.write_text(
                "#!/bin/sh\n"
                'test "$#" = 8 && test "$1" = --kubeconfig && '
                'test "$(cat "$2")" = private-kubeconfig && '
                'test "$3" = --context && test "$4" = kind-qual-peaks-local && '
                'test "$5" = --request-timeout=3s && test "$6" = get && '
                'test "$7" = --raw && test "$8" = "/api/v1/pods?limit=256" && '
                'test -z "$KUBECONFIG" || exit 2\n'
                "echo private-diagnostic >&2\n"
                "echo '{\"items\":[]}'\n"
            )
            kubectl.chmod(0o700)
            target = Target("qual-peaks", "local", root, MINIMAL_PROFILE)
            with patch.dict(
                os.environ,
                {"PATH": f"{root}:{os.environ['PATH']}", "KUBECONFIG": "ambient-credential"},
            ):
                self.assertEqual(
                    peak_observation.inventory(
                        target, "private-kubeconfig", {f"{target.name}-worker"}, 0, timeout=3
                    ),
                    [],
                )
                with self.assertRaisesRegex(ValueError, "Unsupported"):
                    peak_observation.read_api(
                        target, "private-kubeconfig", "/api/v1/secrets", timeout=3
                    )


class PrivateReceiptTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "native Linux operational receipt")
    def test_interrupted_preflight_retains_private_receipt_and_refuses_to_replace_it(self):
        import io
        import json
        import stat
        from contextlib import redirect_stdout
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / ".local"
            private.mkdir()
            request = root / "request.json"
            request.write_text('{"private":"diagnostic-canary"}')
            receipt = private / "run.jsonl"
            args = [
                "collect",
                "--consumer",
                "qual-peaks",
                "--docker-context",
                "local",
                "--state-root",
                str(root),
                "--request",
                str(request),
                "--host-id",
                f"sha256:{'1' * 64}",
                "--receipt",
                str(receipt),
                "--confirm-target",
                "qual-peaks-local",
                "--execute",
            ]
            output = io.StringIO()
            with (
                redirect_stdout(output),
                patch.object(resource_peaks, "bind", side_effect=KeyboardInterrupt),
                patch("subprocess.Popen", side_effect=AssertionError("Live process")),
            ):
                self.assertEqual(resource_peaks.main(args), 1)
                saved = receipt.read_text()
                self.assertEqual(resource_peaks.main(args), 1)
            self.assertEqual(receipt.read_text(), saved)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            records = [json.loads(line) for line in saved.splitlines()]
            self.assertEqual([item["record"] for item in records], ["start", "result"])
            self.assertEqual(
                records[-1]["failure"], {"category": "cancelled", "phase": "preflight"}
            )
            self.assertEqual(records[-1]["state"], "incomplete")
            self.assertNotIn("diagnostic-canary", saved + output.getvalue())
