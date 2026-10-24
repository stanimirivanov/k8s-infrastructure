"""Disposable memory-transition scope, explicit selection and shared protocol proofs."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_profile_transition as fixtures
import yaml

from scripts import consumer_runtime as native
from scripts import consumer_state as owner
from scripts import consumer_transition as cli
from scripts import profile_transition_model as model
from scripts import profile_transition_runtime as transition
from scripts import profile_transition_state as store
from scripts.consumer_profile import TRANSITION_TEST_PROFILE, Target, identity, load_profile
from scripts.node_capacity import envelope


class MinimalProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.predecessor = Target("qual-small", "local-engine", self.root, model.PREDECESSOR, 18445)
        self.target = model.paired(self.predecessor, successor_profile=TRANSITION_TEST_PROFILE)

    def test_exact_budget_and_four_cpu_engine_minimum_do_not_change_full_profile(self):
        profile = load_profile(name=TRANSITION_TEST_PROFILE)
        self.assertEqual(profile["minimumEngine"], {"cpus": 4, "memoryBytes": 14 * 1024**3})
        for budget in profile["nodes"].values():
            self.assertEqual(budget, {"cpus": 2, "memoryBytes": 5 * 1024**3})
            self.assertEqual(
                envelope(budget), {"cpuMillicores": 1750, "memoryBytes": 4508 * 1024**2}
            )
        self.assertEqual(
            load_profile(name=model.SUCCESSOR)["minimumEngine"],
            {"cpus": 8, "memoryBytes": 24 * 1024**3},
        )
        for cpu, memory, allowed in (
            (3, 14 * 1024**3, False),
            (4, 14 * 1024**3 - 1, False),
            (4, 14 * 1024**3, True),
        ):
            info = json.dumps(
                {"os": "linux", "architecture": "amd64", "cpus": cpu, "memory": memory}
            )
            with (
                self.subTest(cpu=cpu, memory=memory),
                patch.object(native, "command", side_effect=["kind v0.31.0", info]),
                patch.object(native, "engine", return_value="unix:///engine.sock"),
            ):
                if allowed:
                    native.prerequisites(self.target, creating=True)
                else:
                    with self.assertRaises(ValueError):
                        native.prerequisites(self.target, creating=True)

    def test_test_profile_rejects_normal_consumers_and_unreviewed_budget_changes(self):
        for consumer in ("fleetiq", "neutral", "qualification"):
            with self.subTest(consumer=consumer), self.assertRaises(ValueError):
                Target(consumer, "local-engine", self.root, TRANSITION_TEST_PROFILE, 18445)
            previous = Target(consumer, "local-engine", self.root, model.PREDECESSOR, 18445)
            with self.assertRaises(ValueError):
                model.request_successor(
                    previous, {"successorDigest": identity(self.target)["profileDigest"]}
                )
        value = load_profile(name=TRANSITION_TEST_PROFILE)
        value["nodes"]["worker"]["memoryBytes"] += 1024**3
        path = self.root / "altered.yaml"
        path.write_text(yaml.safe_dump(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_profile(path, name=TRANSITION_TEST_PROFILE)
        with self.assertRaises(ValueError):
            model.paired(self.predecessor, successor_profile="unreviewed-test")

    def test_preview_and_execute_require_explicit_successor_with_no_fallback(self):
        common = [
            "--consumer",
            self.predecessor.consumer,
            "--docker-context",
            "local-engine",
            "--state-root",
            str(self.root),
            "--https-port",
            "18445",
        ]
        for flag, cpus in (([], 8), (["--successor-profile", TRANSITION_TEST_PROFILE], 4)):
            with (
                patch.object(store, "read") as read,
                patch.object(native, "command") as command,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(["preview", *common, *flag]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["minimumEngine"]["cpus"], cpus)
            if flag:
                self.assertIn("memory-transition", result["qualificationScope"])
            read.assert_not_called()
            command.assert_not_called()
        request = self.root / "request.json"
        request.write_text(
            json.dumps({"successorDigest": identity(self.target)["profileDigest"]}),
            encoding="utf-8",
        )
        args = [
            "apply",
            *common,
            "--request",
            str(request),
            "--execute",
            "--confirm-quiesced",
            self.predecessor.name,
        ]
        with (
            patch.object(transition, "execute", return_value={}) as execute,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(args), 1)
            execute.assert_not_called()
            self.assertEqual(cli.main([*args, "--successor-profile", TRANSITION_TEST_PROFILE]), 0)
            execute.assert_called_once()
            request.write_text("[]", encoding="utf-8")
            self.assertEqual(cli.main([*args, "--successor-profile", TRANSITION_TEST_PROFILE]), 1)
            execute.assert_called_once()


class MinimalTransitionTests(fixtures.TransitionFixture):
    CONSUMER = "qual-small"
    SUCCESSOR_PROFILE = TRANSITION_TEST_PROFILE
    CURRENT_MACHINE = {"cpu": "4", "memory": "16Gi"}

    def test_shared_protocol_transitions_both_memories_and_preserves_completed_history(self):
        result = self.run_transition()
        self.assertIn("memory-transition", result["qualificationScope"])
        completed = store.load(self.target, self.request)
        self.assertEqual(completed["phase"], "complete")
        self.assertEqual(owner.load(self.successor)["profile"], TRANSITION_TEST_PROFILE)
        for role in model.ROLES:
            node = self.containers[f"{self.target.name}-{role}"]
            self.assertEqual(node["cpu"], 2 * 10**9)
            self.assertEqual(node["memory"], 5 * 1024**3)
            config = json.loads(self.configs[node["id"]])
            self.assertEqual(config["systemReserved"]["memory"], str(11 * 1024**3))
        events = list(self.events)
        self.run_transition("recover")
        self.assertEqual(self.events, events)
        full = copy.deepcopy(self.request)
        full["successorDigest"] = identity(model.paired(self.target))["profileDigest"]
        with self.assertRaises(ValueError):
            transition.execute(self.target, full, "recover")

    def test_interrupted_memory_write_recovers_only_with_the_original_test_request(self):
        self.failure = "write-control-plane"
        with self.assertRaises(ValueError):
            self.run_transition()
        with self.assertRaises(ValueError):
            owner.load(self.target)
        self.run_transition("recover")
        self.assertEqual(owner.load(self.successor)["profile"], TRANSITION_TEST_PROFILE)
        self.assertEqual(self.target.path("kind.yaml").read_bytes(), self.original_kind)
        self.assertEqual(self.target.path("kubeconfig").read_bytes(), self.original_access)
