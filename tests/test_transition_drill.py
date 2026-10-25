"""Disposable drill boundaries delegate effects and explicit recovery to the real protocol."""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_profile_transition as fixtures

from scripts import consumer_runtime as native
from scripts import consumer_transition_drill as drill
from scripts import profile_transition_model as model
from scripts import profile_transition_runtime as transition
from scripts import profile_transition_state as store
from scripts.consumer_profile import TRANSITION_TEST_PROFILE, Target


class DrillFixture(fixtures.TransitionFixture):
    CONSUMER = "qual-drill"


class DrillTests(unittest.TestCase):
    def fixture(self, profile=model.SUCCESSOR):
        fixture = DrillFixture(methodName="runTest")
        fixture.SUCCESSOR_PROFILE = profile
        if profile == TRANSITION_TEST_PROFILE:
            fixture.CURRENT_MACHINE = {"cpu": "4", "memory": "16Gi"}
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_each_native_boundary_stops_then_recovers_with_both_successors(self):
        all_events = [("backup", role) for role in model.ROLES] + [
            (effect, role) for role in model.ROLES for effect in ("cap", "write", "restart")
        ]
        for profile in model.SUCCESSORS:
            for checkpoint in drill.CHECKPOINTS:
                with self.subTest(profile=profile, checkpoint=checkpoint):
                    fixture = self.fixture(profile)
                    try:
                        result = drill.interrupt(
                            fixture.target,
                            fixture.request,
                            profile,
                            checkpoint,
                            fixture.target.name,
                        )
                        self.assertEqual(result["execution"], "interrupted-as-requested")
                        self.assertIn("pending", result["liveRecoveryQualification"])
                        self.assertNotEqual(result["phase"], "complete")
                        if checkpoint == "owner-commit":
                            expected = all_events
                        else:
                            role, effect = checkpoint.rsplit("-", 1)
                            expected = all_events[: all_events.index((effect, role)) + 1]
                        self.assertEqual(fixture.events, expected)
                        with self.assertRaises(ValueError):
                            fixtures.owner.load(fixture.target)
                        before = list(fixture.events)
                        fixture.run_transition("recover")
                        self.assertEqual(fixtures.owner.load(fixture.successor)["profile"], profile)
                        if checkpoint.startswith("worker-") or checkpoint == "owner-commit":
                            self.assertEqual(
                                [event for event in fixture.events if event[1] == "control-plane"],
                                [event for event in before if event[1] == "control-plane"],
                            )
                        completed = list(fixture.events)
                        fixture.run_transition("recover")
                        self.assertEqual(fixture.events, completed)
                        self.assertEqual(
                            fixture.target.path("kind.yaml").read_bytes(), fixture.original_kind
                        )
                        self.assertEqual(
                            fixture.target.path("kubeconfig").read_bytes(), fixture.original_access
                        )
                    finally:
                        fixture.doCleanups()

    def test_runtime_callbacks_cannot_target_retained_consumers_or_recovery(self):
        fixture = self.fixture()
        ordinary = Target("fleetiq", "local-engine", fixture.root, model.PREDECESSOR, 18443)
        for target, action in (
            (ordinary, "apply"),
            (fixture.target, "recover"),
            (fixture.target, "inspect"),
        ):
            with (
                self.subTest(action=action, consumer=target.consumer),
                self.assertRaises(ValueError),
            ):
                transition.execute(target, fixture.request, action, after_effect=lambda *_: None)
        self.assertEqual(fixture.events, [])
        self.assertFalse(fixture.target.path("transition.json").exists())

    def test_cli_reports_only_a_deliberate_stop_and_keeps_recovery_pending(self):
        fixture = self.fixture()
        request = fixture.root / "request.json"
        request.write_bytes(model.encoded(fixture.request))
        args = [
            "interrupt",
            "--consumer",
            fixture.target.consumer,
            "--docker-context",
            "local-engine",
            "--state-root",
            str(fixture.root),
            "--https-port",
            "18443",
            "--checkpoint",
            "control-plane-cap",
            "--execute",
            "--confirm-quiesced",
            fixture.target.name,
            "--request",
            str(request),
        ]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(drill.main(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["execution"], "interrupted-as-requested")
        self.assertEqual(result["stages"]["control-plane"], "cap-intent")
        self.assertIn("pending", result["liveRecoveryQualification"])

    def test_invalid_scope_or_request_refuses_before_native_effects(self):
        fixture = self.fixture()
        ordinary = Target("fleetiq", "local-engine", fixture.root, model.PREDECESSOR, 18443)
        invalid = copy.deepcopy(fixture.request)
        invalid["nodes"]["worker"]["newMachineCapacity"]["pods"] = "110"
        cases = (
            (ordinary, fixture.request, model.SUCCESSOR, "control-plane-cap", ordinary.name),
            (fixture.target, fixture.request, model.SUCCESSOR, "arbitrary", fixture.target.name),
            (fixture.target, fixture.request, model.SUCCESSOR, "control-plane-cap", "other"),
            (fixture.target, invalid, model.SUCCESSOR, "control-plane-cap", fixture.target.name),
            (
                fixture.target,
                fixture.request,
                TRANSITION_TEST_PROFILE,
                "owner-commit",
                fixture.target.name,
            ),
        )
        with patch.object(transition, "execute") as execute:
            for args in cases:
                with self.subTest(args=args[2:]), self.assertRaises(ValueError):
                    drill.interrupt(*args)
            execute.assert_not_called()
        self.assertEqual(fixture.events, [])
        self.assertFalse(fixture.target.path("transition.json").exists())

    def test_existing_transition_history_is_not_reused_or_removed(self):
        fixture = self.fixture()
        lock = fixture.target.path("transition.lock")
        lock.write_bytes(b"retain")
        with patch.object(transition, "execute") as execute, self.assertRaises(ValueError):
            drill.interrupt(
                fixture.target,
                fixture.request,
                model.SUCCESSOR,
                "owner-commit",
                fixture.target.name,
            )
        execute.assert_not_called()
        self.assertEqual(lock.read_bytes(), b"retain")
        lock.unlink()  # Only a synthetic temporary fixture; never a live history operation.
        fixture.run_transition()
        original = fixture.target.path("transition.json").read_bytes()
        before = list(fixture.events)
        with self.assertRaises(ValueError):
            drill.interrupt(
                fixture.target,
                fixture.request,
                model.SUCCESSOR,
                "owner-commit",
                fixture.target.name,
            )
        self.assertEqual(fixture.events, before)
        self.assertEqual(fixture.target.path("transition.json").read_bytes(), original)

    def test_unrelated_native_failure_is_not_a_successful_drill_and_hook_does_not_leak(self):
        fixture = self.fixture()
        fixture.failure = "cap-control-plane"
        with self.assertRaisesRegex(ValueError, "injected"):
            drill.interrupt(
                fixture.target,
                fixture.request,
                model.SUCCESSOR,
                "control-plane-cap",
                fixture.target.name,
            )
        journal = store.load(fixture.target, fixture.request)
        self.assertEqual(journal["stages"]["control-plane"], "cap-intent")
        fixture.run_transition("recover")
        self.assertEqual(store.load(fixture.target, fixture.request)["phase"], "complete")
        with (
            patch.object(transition, "execute", return_value={}),
            self.assertRaisesRegex(ValueError, "not reached"),
        ):
            fresh = self.fixture()
            drill.interrupt(
                fresh.target, fresh.request, model.SUCCESSOR, "owner-commit", fresh.target.name
            )

    def test_preview_and_missing_execution_authority_cannot_read_private_state(self):
        fixture = self.fixture()
        common = [
            "--consumer",
            fixture.target.consumer,
            "--docker-context",
            "local-engine",
            "--state-root",
            str(fixture.root),
            "--https-port",
            "18443",
            "--checkpoint",
            "owner-commit",
        ]
        with patch.object(store, "read") as read, patch.object(native, "command") as command:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(drill.main(["interrupt", *common, "--request", "absent.json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")
            for args in (
                ["preview", *common, "--execute"],
                ["interrupt", *common, "--execute", "--request", "absent.json"],
                ["interrupt", *common, "--execute", "--confirm-quiesced", fixture.target.name],
            ):
                with redirect_stdout(io.StringIO()), self.subTest(args=args):
                    self.assertEqual(drill.main(args), 1)
            read.assert_not_called()
            command.assert_not_called()

    def test_cli_failure_diagnostics_withhold_private_request_and_native_details(self):
        fixture = self.fixture()
        path = fixture.root / "request.json"
        path.write_bytes(model.encoded(fixture.request))
        fixture.failure = "write-control-plane"
        args = [
            "interrupt",
            "--consumer",
            fixture.target.consumer,
            "--docker-context",
            "local-engine",
            "--state-root",
            str(fixture.root),
            "--https-port",
            "18443",
            "--checkpoint",
            "owner-commit",
            "--execute",
            "--confirm-quiesced",
            fixture.target.name,
            "--request",
            str(path),
        ]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(drill.main(args), 1)
        self.assertNotIn("private-config-never-print", output.getvalue())
        self.assertNotIn("injected", output.getvalue())
