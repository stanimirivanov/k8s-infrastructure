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
                                "lastState": {
                                    "terminated": {
                                        "exitCode": 137,
                                        "reason": "ContainerStatusUnknown",
                                        "startedAt": None,
                                        "finishedAt": None,
                                    }
                                },
                            }
                        ],
                    },
                }
            ]
        }
        rows = peak_observation.pod_attempts(payload, {node}, window_start_ns=0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["containerId"], identity)
        self.assertEqual(rows[0]["podUid"], uid)
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[2]["kind"], "init")
        self.assertTrue(rows[2]["restartableInit"])
        self.assertEqual(rows[2]["state"], "missing-status")
        self.assertIsNone(rows[2]["containerId"])
        self.assertEqual(rows[1]["state"], "terminated")
        self.assertIsNone(rows[1]["startedAtNs"])
        self.assertIsNone(rows[1]["finishedAtNs"])
        evidence = resource_peaks.summarize(
            [
                {
                    "startedElapsedNs": 0,
                    "finishedElapsedNs": 100,
                    "nodes": {},
                    "containers": [{**row, "counters": None} for row in rows],
                    "tooling": {},
                    "host": {"memAvailableBytes": 10},
                    "filesystems": {},
                    "initOverlap": [],
                }
            ],
            3,
        )
        self.assertIn("container-attempt-chronology-unavailable", evidence["reasons"])
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

    def test_preview_supports_full_normal_window_with_a_fixed_sample_cap(self):
        import io
        import json
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as directory:
            base = [
                "collect",
                "--consumer",
                "qual-peaks",
                "--docker-context",
                "local",
                "--state-root",
                directory,
            ]
            for duration, interval, expected in ((1800, 30, 0), (1800, 25, 1), (1830, 30, 1)):
                with self.subTest(duration=duration, interval=interval):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = resource_peaks.main(
                            [
                                *base,
                                "--duration-seconds",
                                str(duration),
                                "--interval-seconds",
                                str(interval),
                            ]
                        )
                    self.assertEqual(code, expected)
                    if code == 0:
                        self.assertEqual(json.loads(output.getvalue())["durationSeconds"], 1800)


class InitCoverageTests(unittest.TestCase):
    def test_terminated_init_without_native_endpoint_keeps_only_the_observed_prefix(self):
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
        self.assertEqual(result["state"], "incomplete")
        init = result["containers"][identities["init"]]
        self.assertIn("termination-native-coverage-missing", init["reasons"])
        self.assertIsNone(init["intervalCpuDelta"])
        self.assertEqual(init["lastObservedLifetimeMemoryPeakBytes"], 100)
        self.assertEqual(init["observedCpuIntervalElapsedNs"], [3_000_000_000, 6_000_000_000])
        self.assertEqual(init["observedCpuDelta"]["usageUsec"], 3)
        self.assertEqual(result["initOverlap"]["state"], "incomplete")
        self.assertEqual(result["initOverlap"]["sampledConcurrentNodeMemoryMaxBytes"], 46)
        self.assertEqual(result["pendingContainerCount"], 0)
        frames[3]["containers"][1]["counters"] = {
            **PeakSeriesTests.sample(9_000_000_000, 0, 110, (27, 7, 1, 4)),
            "populated": False,
        }
        frames[3]["unattributedRuntimeScopes"] = [
            {"node": node, "scopeId": "c" * 64, "counters": frames[3]["nodes"][node]},
            {"node": node, "scopeId": "d" * 64, "counters": None},
        ]
        complete = resource_peaks.summarize(frames, 3)
        self.assertEqual(complete["state"], "observed")
        self.assertEqual(
            complete["runtimeAttribution"],
            {
                "state": "incomplete",
                "reasons": ["runtime-scope-attribution-unavailable"],
                "unattributedScopeCount": 2,
                "unattributedScopeObservationsWithCounters": 1,
                "unattributedScopeObservationsWithoutCounters": 1,
            },
        )
        self.assertEqual(len(complete["containers"]), 2)
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

    @unittest.skipUnless(sys.platform == "linux", "native Linux descriptor boundary")
    def test_failed_api_get_preserves_public_exit_status_without_private_diagnostics(self):
        import os
        from unittest.mock import patch

        from scripts import bootstrap_failure, peak_observation
        from scripts.consumer_profile import MINIMAL_PROFILE, Target
        from scripts.local_kind import CommandFailure

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kubectl = root / "kubectl"
            kubectl.write_text("#!/bin/sh\necho private-stderr >&2\necho private-stdout\nexit 23\n")
            kubectl.chmod(0o700)
            target = Target("qual-peaks", "local", root, MINIMAL_PROFILE)
            with (
                patch.dict(os.environ, {"PATH": f"{root}:{os.environ['PATH']}"}),
                self.assertRaises(CommandFailure) as caught,
            ):
                peak_observation.read_api(
                    target, "private-kubeconfig", "/api/v1/namespaces/kube-system", timeout=3
                )
            self.assertEqual(
                bootstrap_failure.evidence(caught.exception, "sampling"),
                {"category": "dependency-failure", "phase": "sampling", "exitCode": 23},
            )
            self.assertNotIn("private-", str(caught.exception))


class CollectionTests(unittest.TestCase):
    def collect_with_mocked_boundaries(self, *, duration=1800, interval=30, outcome="normal"):
        from unittest.mock import patch

        from scripts.consumer_profile import MINIMAL_PROFILE, Target

        second = 1_000_000_000
        elapsed = 1
        node = "qual-peaks-local-worker"

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += round(seconds * second)

        def frame(_target, _request, _measurement, clock, _start, _interval):
            at = clock()
            metric = PeakSeriesTests.sample(at, 18, 31, (at + 18, 3, 0, 0))
            return {
                "startedElapsedNs": at,
                "finishedElapsedNs": at + 100,
                "nodes": {node: metric},
                "containers": [
                    {
                        "node": node,
                        "podUid": "88776655-4433-2211-ffff-001122334455",
                        "podName": "demo",
                        "namespace": "demo",
                        "containerName": "app",
                        "kind": "app",
                        "restartableInit": False,
                        "state": "running",
                        "containerId": "a" * 64,
                        "startedAtNs": 0,
                        "finishedAtNs": None,
                        "restartCount": 0,
                        "counters": metric,
                    }
                ],
                "tooling": {"tools": metric},
                "host": {"memAvailableBytes": 5 * 1024**3},
                "filesystems": {},
                "initOverlap": [],
            }

        initial = {
            **PeakSeriesTests.sample(1, 18, 31, (18, 3, 0, 0)),
            "cpuLimitMillicores": 1000,
            "memoryLimitBytes": 2 * 1024**3,
        }
        measurement = resource_peaks.Measurement(
            {},
            "private",
            {},
            {},
            {},
            {},
            {},
            {"tools": initial},
            {"artifactDigest": "before", "bootstrapReceiptDigest": "receipt"},
        )
        final = (
            resource_peaks.Measurement(
                {},
                "private",
                {},
                {},
                {},
                {},
                {},
                {"tools": initial},
                {"artifactDigest": "after", "bootstrapReceiptDigest": "receipt"},
            )
            if outcome == "identity-drift"
            else measurement
        )
        after_charge = {
            **initial,
            **PeakSeriesTests.sample(
                0, 18, 31, (0 if outcome == "regressed-charge" else 48, 3, 0, 0)
            ),
        }
        records: list[dict] = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(resource_peaks, "bind", side_effect=[measurement, final]),
            patch.object(resource_peaks, "sample", side_effect=frame),
            patch.object(resource_peaks, "observe_tooling", return_value={"tools": after_charge}),
            patch.object(resource_peaks.time, "monotonic_ns", side_effect=lambda: elapsed),
            patch.object(resource_peaks.time, "sleep", side_effect=sleep),
        ):
            target = Target("qual-peaks", "local", Path(directory), MINIMAL_PROFILE)
            result = resource_peaks.collect(
                target, {}, "host", duration=duration, interval=interval, write=records.append
            )
        return result, records

    def test_collect_runs_all_61_scheduled_samples_and_charges_full_normal_window(self):
        result, records = self.collect_with_mocked_boundaries()
        self.assertEqual(result["state"], "observed")
        self.assertEqual(result["sampleCount"], 61)
        self.assertEqual(records[0]["requestedDurationSeconds"], 1800)
        self.assertEqual(records[-1]["startedElapsedNs"], 1_800_000_000_000)
        self.assertEqual(
            result["collectorToolingCharge"]["tools"]["intervalCpuDelta"]["usageUsec"], 30
        )

    def test_collect_retains_evidence_but_refuses_drift_and_incomplete_tooling_charge(self):
        drift, _ = self.collect_with_mocked_boundaries(
            duration=10, interval=5, outcome="identity-drift"
        )
        self.assertEqual(drift["state"], "incomplete")
        self.assertEqual(drift["sampleCount"], 3)
        self.assertEqual(
            drift["failure"], {"phase": "final-identity", "category": "identity-mismatch"}
        )

        charge, _ = self.collect_with_mocked_boundaries(
            duration=10, interval=5, outcome="regressed-charge"
        )
        self.assertEqual(charge["state"], "incomplete")
        self.assertIn("collector-tooling-charge-incomplete", charge["reasons"])
        self.assertEqual(
            charge["collectorToolingCharge"]["tools"]["reasons"], ["kernel-counter-regressed"]
        )


class PrivateReceiptTests(unittest.TestCase):
    @staticmethod
    def invocation(root):
        import json

        from scripts.host_admission import GIB, digest

        private = root / ".local"
        private.mkdir()
        request = root / "request.json"
        receipt = private / "run.jsonl"
        host_id = f"sha256:{'1' * 64}"
        request.write_text(
            json.dumps(
                {
                    "private": "diagnostic-canary",
                    "disk": [
                        {
                            "id": "prep",
                            "category": "preparation",
                            "path": str(root),
                            "limitBytes": 8 * GIB,
                        }
                    ],
                }
            )
        )
        cold = {
            "memoryAccountIdentities": {},
            "admission": {"filesystems": [{"filesystemId": digest([host_id, root.stat().st_dev])}]},
        }
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
            host_id,
            "--receipt",
            str(receipt),
            "--confirm-target",
            "qual-peaks-local",
            "--execute",
        ]
        return args, receipt, cold

    @unittest.skipUnless(sys.platform == "linux", "native Linux operational receipt")
    def test_interrupted_preflight_retains_private_receipt_and_refuses_to_replace_it(self):
        import io
        import json
        import stat
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, receipt, cold = self.invocation(root)
            output = io.StringIO()
            from scripts import host_observation

            with (
                redirect_stdout(output),
                patch.object(
                    resource_peaks, "bootstrap_identity", return_value=({}, cold, "digest")
                ),
                patch.object(resource_peaks, "tooling_endpoint", return_value={}),
                patch.object(host_observation, "mount_records", return_value=[(root, "ext4")]),
                patch.object(host_observation, "allocated_disk_bytes", return_value={root: 0}),
                patch(
                    "os.fstatvfs", return_value=SimpleNamespace(f_bavail=4_000_000, f_frsize=4096)
                ),
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

    @unittest.skipUnless(sys.platform == "linux", "native Linux operational receipt")
    def test_receipt_setup_refuses_unaccounted_or_exhausted_storage_before_opening(self):
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch

        from scripts import host_observation
        from scripts.host_admission import GIB

        for kind, resident in (("overlay", 0), ("ext4", 8 * GIB)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args, receipt, cold = self.invocation(root)
                output = io.StringIO()
                with (
                    redirect_stdout(output),
                    patch.object(
                        resource_peaks, "bootstrap_identity", return_value=({}, cold, "digest")
                    ),
                    patch.object(resource_peaks, "tooling_endpoint", return_value={}),
                    patch.object(host_observation, "mount_records", return_value=[(root, kind)]),
                    patch.object(
                        host_observation, "allocated_disk_bytes", return_value={root: resident}
                    ),
                    patch(
                        "os.fstatvfs",
                        return_value=SimpleNamespace(f_bavail=4_000_000, f_frsize=4096),
                    ),
                    patch.object(
                        resource_peaks,
                        "collect",
                        side_effect=AssertionError("Uncharged collection"),
                    ),
                ):
                    self.assertEqual(resource_peaks.main(args), 1)
                self.assertFalse(receipt.exists())
                self.assertEqual(json.loads(output.getvalue())["failure"]["phase"], "receipt-setup")

    @unittest.skipUnless(sys.platform == "linux", "native Linux operational receipt")
    def test_request_and_final_sync_deadlines_keep_the_durable_receipt_prefix(self):
        import io
        import json
        import signal
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch

        from scripts import host_observation

        for stage in ("receipt-setup", "receipt-finalization", "success"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args, receipt, cold = self.invocation(root)
                output = io.StringIO()
                real_read = resource_peaks.state.read_bounded
                sync_count = 0

                def read_request(path, stage=stage, real_read=real_read):
                    if stage == "receipt-setup":
                        signal.raise_signal(signal.SIGALRM)
                    return real_read(path)

                def sync(_descriptor, stage=stage):
                    nonlocal sync_count
                    sync_count += 1
                    if stage == "receipt-finalization" and sync_count == 3:
                        signal.raise_signal(signal.SIGALRM)

                result = {
                    "state": "observed",
                    "sampleCount": 2,
                    "reasons": [],
                    "runtimeAttribution": {"state": "incomplete"},
                }
                with (
                    redirect_stdout(output),
                    patch.object(resource_peaks.state, "read_bounded", side_effect=read_request),
                    patch.object(
                        resource_peaks, "bootstrap_identity", return_value=({}, cold, "digest")
                    ),
                    patch.object(resource_peaks, "tooling_endpoint", return_value={}),
                    patch.object(host_observation, "mount_records", return_value=[(root, "ext4")]),
                    patch.object(host_observation, "allocated_disk_bytes", return_value={root: 0}),
                    patch(
                        "os.fstatvfs",
                        return_value=SimpleNamespace(f_bavail=4_000_000, f_frsize=4096),
                    ),
                    patch("os.fsync", side_effect=sync),
                    patch.object(resource_peaks, "collect", return_value=result),
                ):
                    self.assertEqual(resource_peaks.main(args), 0 if stage == "success" else 1)
                public = json.loads(output.getvalue())
                if stage == "success":
                    self.assertEqual(public["runtimeAttributionState"], "incomplete")
                    self.assertFalse(public["installedAcceptance"])
                else:
                    self.assertEqual(
                        public["failure"], {"category": "timeout", "phase": stage, "timedOut": True}
                    )
                if stage == "receipt-setup":
                    self.assertFalse(receipt.exists())
                else:
                    records = [json.loads(line) for line in receipt.read_text().splitlines()]
                    self.assertEqual(
                        [record["record"] for record in records],
                        ["start", "result"] if stage == "success" else ["start"],
                    )
