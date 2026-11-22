"""Consumer-facing incremental admission, independent of host/provider access."""

import unittest

from scripts import host_admission as admission

GIB = 1024**3


def request():
    return {
        "version": 1,
        "target": "qualification-target",
        "artifactDigest": "sha256:" + "a" * 64,
        "memory": [
            {"id": "nodes", "kind": "workload", "limitBytes": 12 * GIB},
            {"id": "browser", "kind": "browser", "limitBytes": GIB},
            {"id": "tools", "kind": "tooling", "limitBytes": 2 * GIB, "cpuMillicores": 1000},
        ],
        "disk": [
            {"id": name, "category": name, "limitBytes": size * GIB, "path": "/" + name}
            for name, size in (("retained", 11), ("images", 12), ("runtime", 4), ("preparation", 8))
        ],
    }


def observation(value):
    return {
        "version": 1,
        "requestDigest": admission.digest(value),
        "hostId": "sha256:" + "b" * 64,
        "target": value["target"],
        "observedAt": 1000,
        "memAvailableBytes": 19 * GIB,
        "memory": {item["id"]: {"residentBytes": 0} for item in value["memory"]},
        "disk": {
            item["id"]: {
                "physicalId": item["id"],
                "filesystemId": "physical-store",
                "availableBytes": 45 * GIB,
                "residentBytes": 0,
            }
            for item in value["disk"]
        },
    }


class HostAdmissionTests(unittest.TestCase):
    def test_cold_start_preserves_exact_headroom_on_shared_physical_store(self):
        value = request()
        observed = observation(value)
        result = admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)
        self.assertEqual(result["memoryRemainingBytes"], 4 * GIB)
        self.assertEqual(
            result["filesystems"],
            [
                {
                    "filesystemId": "physical-store",
                    "incrementalBytes": 35 * GIB,
                    "remainingBytes": 10 * GIB,
                }
            ],
        )
        self.assertTrue(result["admitted"])

    def test_resident_usage_is_not_subtracted_twice_and_aliases_are_not_copies(self):
        value = request()
        value["disk"].append({**value["disk"][0], "id": "retained-alias"})
        observed = observation(value)
        observed["memAvailableBytes"] = 7 * GIB
        observed["memory"]["nodes"]["residentBytes"] = 12 * GIB
        for metric in observed["disk"].values():
            metric["availableBytes"] = 34 * GIB
        observed["disk"]["retained"]["residentBytes"] = 11 * GIB
        observed["disk"]["retained-alias"] = dict(observed["disk"]["retained"])
        result = admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["filesystems"][0]["incrementalBytes"], 24 * GIB)
        observed["disk"]["retained-alias"]["physicalId"] = "second-node-copy"
        with self.assertRaisesRegex(ValueError, "category"):
            admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)

    def test_deficits_missing_metrics_stale_and_changed_identity_fail_closed(self):
        import copy

        value = request()
        observed = observation(value)
        for field, reason in (("memAvailableBytes", "insufficient-memory"),):
            bad = copy.deepcopy(observed)
            bad[field] -= 1
            result = admission.evaluate(value, bad, host_id=observed["hostId"], now=1001)
            self.assertFalse(result["admitted"])
            self.assertIn(reason, result["reasons"])
        bad = copy.deepcopy(observed)
        for metric in bad["disk"].values():
            metric["availableBytes"] -= 1
        self.assertIn(
            "insufficient-disk",
            admission.evaluate(value, bad, host_id=observed["hostId"], now=1001)["reasons"],
        )
        for key, replacement in (
            ("observedAt", 939),
            ("observedAt", 1002),
            ("target", "replaced"),
            ("hostId", "replaced"),
            ("requestDigest", "replaced"),
            ("memAvailableBytes", None),
            ("memory", {}),
            ("disk", {}),
        ):
            with self.subTest(key=key, replacement=replacement), self.assertRaises(ValueError):
                admission.evaluate(
                    value, {**observed, key: replacement}, host_id=observed["hostId"], now=1001
                )

    def test_missing_categories_and_unbounded_tooling_fail_closed(self):
        for change in (
            lambda value: value["disk"].pop(),
            lambda value: value["memory"][2].update(cpuMillicores=1001),
            lambda value: value["memory"].pop(1),
        ):
            value = request()
            change(value)
            with self.assertRaises(ValueError):
                admission.validate(value)

    def test_observation_rejects_unlisted_accounts(self):
        value = request()
        observed = observation(value)
        observed["memory"]["unknown"] = {"residentBytes": 1}
        with self.assertRaisesRegex(ValueError, "account inventory"):
            admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)

    def test_offline_preview_never_observes_host_and_new_profile_stays_disabled(self):
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_admission_cli as cli
        from scripts.consumer_profile import load_profile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(request()))
            with (
                patch("scripts.host_observation.observe") as observe,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(["preview", "--request", str(path)]), 0)
            observe.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())["liveAdmission"])
        with self.assertRaises(ValueError):
            load_profile(name="minimal-demo")

    def test_live_observation_is_read_only_bounded_and_binds_physical_identity(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_observation as live

        value = request()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cgroup = root / "tools"
            cgroup.mkdir()
            for name, data in (
                ("memory.max", str(2 * GIB)),
                ("memory.current", str(GIB)),
                ("memory.stat", "anon 1048576\nfile 10000000\n"),
                ("cpu.max", "100000 100000"),
            ):
                (cgroup / name).write_text(data)
            value["memory"][2]["cgroup"] = str(cgroup)
            for item in value["disk"]:
                path = root / item["id"]
                path.mkdir()
                item["path"] = str(path)
            with (
                patch.object(live, "host_id", return_value="host"),
                patch.object(live, "collector_cgroup", return_value=cgroup),
                patch.object(live, "CGROUP_ROOT", root),
                patch.object(live, "mem_available", return_value=19 * GIB),
                patch.object(live, "mount_points", return_value=[]),
                patch.object(live, "physical_filesystem"),
            ):
                observed = live.observe(value, expected_host_id="host")
            self.assertEqual(observed["memory"]["tools"]["residentBytes"], 1048576)
            self.assertEqual(len({item["filesystemId"] for item in observed["disk"].values()}), 1)
            self.assertNotIn(directory, str(observed))
            self.assertEqual(observed["requestDigest"], admission.digest(value))

    def test_unknown_disk_backing_is_not_treated_as_physical_host_storage(self):
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_observation as live

        for kind in ("overlay", "xfs"):
            with (
                self.subTest(kind=kind),
                patch.object(live, "mount_records", return_value=[(Path("/"), kind)]),
                self.assertRaisesRegex(ValueError, "local ext4"),
            ):
                live.physical_filesystem(Path("/tmp"))

    def test_live_disk_credit_counts_hardlinked_bytes_once_across_accounts(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_observation as live

        value = request()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in value["disk"]:
                path = root / item["id"]
                path.mkdir()
                item["path"] = str(path)
            source = root / "retained" / "shared"
            source.write_bytes(b"x" * 1048576)
            os.link(source, root / "images" / "shared")
            expected = source.stat().st_blocks * 512 + sum(
                Path(item["path"]).stat().st_blocks * 512 for item in value["disk"]
            )
            with (
                patch.object(live, "host_id", return_value="host"),
                patch.object(live, "memory_observation", return_value={}),
                patch.object(live, "mem_available", return_value=19 * GIB),
                patch.object(live, "mount_points", return_value=[]),
                patch.object(live, "physical_filesystem"),
            ):
                observed = live.observe(value, expected_host_id="host")
            self.assertEqual(
                sum(item["residentBytes"] for item in observed["disk"].values()), expected
            )

    def test_independent_physical_stores_each_keep_headroom(self):
        value = request()
        observed = observation(value)
        for name, metric in observed["disk"].items():
            metric["filesystemId"] = name
            metric["availableBytes"] = (
                next(item["limitBytes"] for item in value["disk"] if item["id"] == name) + 10 * GIB
            )
        self.assertTrue(
            admission.evaluate(value, observed, host_id=observed["hostId"], now=1060)["admitted"]
        )
        observed["disk"]["images"]["availableBytes"] -= 1
        self.assertFalse(
            admission.evaluate(value, observed, host_id=observed["hostId"], now=1060)["admitted"]
        )

    def test_cli_timeout_and_missing_execute_refuse_without_private_output(self):
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_admission_cli as cli

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-request.json"
            path.write_text(json.dumps(request()))
            with (
                patch(
                    "scripts.host_observation.observe", side_effect=TimeoutError("private-path")
                ) as observe,
                redirect_stdout(io.StringIO()) as output,
            ):
                args = ["observe", "--request", str(path), "--host-id", "host"]
                self.assertEqual(cli.main(args), 1)
                observe.assert_not_called()
                self.assertEqual(cli.main([*args, "--execute"]), 1)
            self.assertNotIn("private-path", output.getvalue())
            self.assertNotIn(directory, output.getvalue())

    def test_cli_failure_categories_are_distinct_and_sanitized(self):
        import io
        import json
        import subprocess
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_admission_cli as cli

        cases = (
            (TimeoutError("private-path"), "observation-timeout"),
            (subprocess.TimeoutExpired("private-command", 5), "observation-timeout"),
            (FileNotFoundError("private-path"), "missing-prerequisite"),
            (PermissionError("private-path"), "permission-denied"),
            (subprocess.CalledProcessError(1, "private-command"), "measurement-failed"),
            (OSError("private-path"), "host-evidence-unavailable"),
            (AttributeError("private-path"), "internal-error"),
            (KeyError("private-path"), "internal-error"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(request()))
            for error, reason in cases:
                with (
                    self.subTest(reason=reason, error=type(error)),
                    patch("scripts.host_observation.observe", side_effect=error),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    status = cli.main(
                        ["observe", "--request", str(path), "--host-id", "host", "--execute"]
                    )
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output.getvalue())["reason"], reason)
                self.assertNotIn("private-", output.getvalue())

    def test_malformed_request_and_observation_shapes_have_explicit_refusals(self):
        import copy

        for value in (None, [], {"version": 1, "target": []}):
            with self.subTest(value=value), self.assertRaises(admission.Refusal) as error:
                admission.validate(value)
            self.assertEqual(error.exception.code, "invalid-request")
        for field, replacement in (
            ("memory", [None]),
            ("disk", [1]),
            ("target", 1),
            ("artifactDigest", []),
        ):
            value = request()
            value[field] = replacement
            with self.subTest(field=field), self.assertRaises(admission.Refusal):
                admission.validate(value)
        value = request()
        for field, replacement in (
            ("memory", []),
            ("disk", None),
            ("memory", {"nodes": None, "browser": {}, "tools": {}}),
        ):
            observed = copy.deepcopy(observation(value))
            observed[field] = replacement
            with self.subTest(field=field), self.assertRaises(admission.Refusal) as error:
                admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)
            self.assertEqual(error.exception.code, "invalid-observation")

    def test_policy_refusals_distinguish_identity_staleness_and_storage(self):
        from pathlib import Path
        from unittest.mock import patch

        from scripts import host_observation as live

        value = request()
        observed = observation(value)
        for identity, now, code in (
            ("changed", 1001, "identity-mismatch"),
            (observed["hostId"], 1061, "stale-observation"),
        ):
            with self.assertRaises(admission.Refusal) as error:
                admission.evaluate(value, observed, host_id=identity, now=now)
            self.assertEqual(error.exception.code, code)
        with (
            patch.object(live, "mount_records", return_value=[(Path("/"), "xfs")]),
            self.assertRaises(admission.Refusal) as error,
        ):
            live.physical_filesystem(Path("/data"))
        self.assertEqual(error.exception.code, "unsupported-storage")

    def test_cli_malformed_json_and_missing_execute_are_invalid_request(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        from scripts import host_admission_cli as cli

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text("{private-invalid-json")
            for args in (["preview", "--request", str(path)], ["host-id"]):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(args), 1)
                self.assertIn('"reason": "invalid-request"', output.getvalue())
                self.assertNotIn("private-", output.getvalue())

    def test_tooling_memory_bounds_and_observed_resident_overages_refuse(self):
        value = request()
        value["memory"][2]["limitBytes"] += 1
        with self.assertRaisesRegex(ValueError, "tooling"):
            admission.validate(value)
        for field, account in (("memory", "nodes"), ("disk", "retained")):
            value = request()
            observed = observation(value)
            observed[field][account]["residentBytes"] = 100 * GIB
            with self.assertRaisesRegex(ValueError, "exceeds"):
                admission.evaluate(value, observed, host_id=observed["hostId"], now=1001)
