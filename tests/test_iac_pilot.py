"""Offline checks through the #74 laptop rehearsal command."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import iac_pilot
from scripts.iac_pilot_state import DOCKER_ENDPOINT, Target


class PilotPreviewTests(unittest.TestCase):
    def test_preview_has_no_native_side_effects(self):
        with (
            patch("scripts.local_kind.run") as run,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(iac_pilot.main(["apply-create"]), 0)
        self.assertIn("laptop", output.getvalue())
        run.assert_not_called()


class PilotLifecycleTests(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        self.targets = {
            "pilot": Target(home / ".local/state/perfeng/kind-74"),
            "retained-434": Target(home / ".local/state/perfeng/kind-434", "perfeng-retained-434"),
        }
        self.target = self.targets["pilot"]
        self.clusters = {}
        self.argo = False
        self.plan_action = None
        self.fail_mutation = None
        self.change_source_after_staging = False
        self.apply_payloads = []
        self.replacement_uid = None
        self.commands = []
        self.output = ""
        self.daemon = "laptop-daemon"
        self.enterContext(patch("scripts.iac_pilot.Path.home", return_value=home))
        self.enterContext(
            patch("scripts.iac_pilot.Runtime.initialize", return_value="pinned-module")
        )
        self.enterContext(patch("scripts.iac_pilot.Runtime.preflight"))
        self.enterContext(patch("scripts.local_kind.run", side_effect=self.native))

    def invoke(self, action, *, digest=None, success=True, profile="pilot", expected_uid=None):
        self.target = self.targets[profile]
        args = [action, "--execute"]
        if profile != "pilot":
            args += ["--profile", profile]
        if digest:
            args += ["--plan-sha256", digest]
        if expected_uid:
            args += ["--expected-cluster-uid", expected_uid]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = iac_pilot.main(args)
        self.output = output.getvalue()
        self.assertEqual(status, 0 if success else 1, self.output)

    def read_record(self, name):
        return json.loads((self.target.directory / f"{name}.json").read_bytes())

    def plan_digest(self, action):
        return self.read_record(f"{action}-plan")["plan"]

    def fake_state(self, target, present=True):
        import json

        resource = {
            "mode": "managed",
            "type": "kind_cluster",
            "name": "pilot",
            "provider": 'provider["registry.terraform.io/elioseverojunior/kind"]',
            "instances": [{"attributes": {"name": target.name}}],
        }
        target.state.write_text(json.dumps({"resources": [resource] if present else []}))
        target.state.chmod(0o600)

    def native(self, command, **kwargs):
        import json
        from pathlib import Path
        from uuid import UUID

        import yaml

        self.commands.append((command, kwargs))
        verb = command[:2]
        if verb == ["docker", "context"]:
            return json.dumps(DOCKER_ENDPOINT)
        if verb == ["docker", "info"]:
            return json.dumps({"ID": self.daemon})
        if verb == ["docker", "ps"]:
            return "\n".join(
                f"{identifier} {name} {cluster}"
                for cluster in self.clusters
                for name, identifier in self.docker_nodes(cluster).items()
            )
        if command[0] == "tofu":
            action = command[2]
            if action == "plan":
                path = Path(next(arg[5:] for arg in command if arg.startswith("-out=")))
                path.write_bytes(b"reviewed saved plan")
                return ""
            if action == "show":
                if "-json" not in command:
                    return "Private plan for review"
                if self.change_source_after_staging:
                    (self.target.directory / "create.tfplan").write_bytes(b"different source")
                return json.dumps(self.plan_json())
            if action == "apply":
                self.apply_payloads.append(Path(command[-1]).read_bytes())
                if self.fail_mutation == "apply":
                    raise ValueError("Simulated provider interruption")
                self.fake_state(self.target, present=self.plan_action != "delete")
                if self.plan_action == "delete":
                    self.clusters.pop(self.target.name)
                else:
                    self.clusters[self.target.name] = self.cluster_uid
                return ""
        if command[0] == "kind":
            if command[1:3] == ["get", "kubeconfig"]:
                if self.target.name not in self.clusters:
                    raise ValueError("Simulated absent cluster")
                context = self.target.context
                return yaml.safe_dump(
                    {
                        "current-context": context,
                        "contexts": [
                            {"name": context, "context": {"cluster": context, "user": context}}
                        ],
                        "clusters": [
                            {
                                "name": context,
                                "cluster": {
                                    "server": "https://127.0.0.1:44321",
                                    "certificate-authority-data": "certificate",
                                },
                            }
                        ],
                        "users": [
                            {
                                "name": context,
                                "user": {
                                    "client-certificate-data": "client",
                                    "client-key-data": "private",
                                },
                            }
                        ],
                    }
                )
        if command[0] == "kubectl":
            if "kustomize" in command:
                return "approved rendered Argo CD manifest\n"
            if "kube-system" in command and "namespace" in command:
                return self.replacement_uid or self.clusters[self.target.name]
            if "apply" in command:
                self.assertNotIn("--force-conflicts", command)
                self.assertIn("--field-manager=k8s-infrastructure", command)
                self.assertEqual(kwargs["input_data"], b"approved rendered Argo CD manifest\n")
                self.argo = True
                return ""
            if "get" in command and "namespace" in command:
                return (
                    json.dumps(
                        {
                            "apiVersion": "v1",
                            "kind": "Namespace",
                            "metadata": {
                                "name": "argocd",
                                "uid": str(UUID(int=180)),
                                "labels": {
                                    "app.kubernetes.io/managed-by": "k8s-infrastructure",
                                    "app.kubernetes.io/name": "argocd",
                                    "app.kubernetes.io/part-of": "kubernetes-substrate",
                                },
                            },
                        }
                    )
                    if self.argo
                    else ""
                )
            if "get" in command and "nodes" in command:
                items = []
                for n, (name, role) in enumerate(
                    zip(
                        self.docker_nodes(self.target.name),
                        ("control-plane", "performance-generator", "sut"),
                        strict=True,
                    )
                ):
                    labels = {
                        "workload": role,
                        "perfeng.io/node-type": "generator" if n == 1 else role,
                    }
                    conditions = [
                        {"type": key, "status": "False"}
                        for key in ("MemoryPressure", "DiskPressure", "PIDPressure")
                    ]
                    conditions.append({"type": "Ready", "status": "True"})
                    items.append(
                        {
                            "metadata": {
                                "name": name,
                                "uid": str(UUID(int=n + 30)),
                                "labels": labels,
                            },
                            "status": {"conditions": conditions},
                        }
                    )
                return json.dumps({"items": items})
            if "storageclass" in command:
                return json.dumps({"provisioner": "rancher.io/local-path"})
            if "rollout" in command:
                return ""
            if "deployment/argocd-server" in command and "get" in command:
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": item.split("/")[1],
                                    "uid": str(UUID(int=200 + n)),
                                    "generation": 1,
                                }
                            }
                            for n, item in enumerate(command)
                            if item.startswith(("deployment/", "statefulset/"))
                        ]
                    }
                )
        self.fail(f"Unexpected native command in offline test: {command[:3]}")

    @property
    def cluster_uid(self):
        return "15dd2573-d648-4e27-af00-5e302f662b43"

    def docker_nodes(self, name):
        return {
            f"{name}-{suffix}": character * 64
            for suffix, character in (
                ("control-plane", "a"),
                ("worker", "b"),
                ("worker2", "c"),
            )
        }

    def plan_json(self):
        return {
            "format_version": "1.2",
            "terraform_version": "1.13.10",
            "errored": False,
            "variables": {"cluster_name": {"value": self.target.name}},
            "resource_changes": [
                {
                    "mode": "managed",
                    "address": "kind_cluster.pilot",
                    "provider_name": "registry.terraform.io/elioseverojunior/kind",
                    "change": {
                        "actions": self.plan_action
                        if isinstance(self.plan_action, list)
                        else [self.plan_action],
                        "before": {"name": self.target.name},
                        "after": {"name": self.target.name},
                    },
                }
            ],
        }

    def create(self):
        self.plan_action = "create"
        self.invoke("plan-create")
        self.invoke("apply-create", digest=self.plan_digest("create"))

    def test_retained_plan_is_separate_and_leaves_the_running_pilot_intact(self):
        self.create()
        pilot = self.targets["pilot"]
        pilot_state = pilot.state.read_bytes()
        self.invoke("plan-create", profile="retained-434")
        retained = self.targets["retained-434"]
        self.assertTrue(
            any(
                cmd[:3] == ["tofu", f"-chdir={retained.directory / 'opentofu'}", "plan"]
                and f"-var=cluster_name={retained.name}" in cmd
                for cmd, _ in self.commands
            )
        )
        self.assertEqual(self.read_record("create-plan")["statePath"], str(retained.state))
        self.assertFalse(retained.state.exists())
        self.assertEqual(pilot.state.read_bytes(), pilot_state)
        self.assertEqual(set(self.clusters), {pilot.name})

    def test_retained_profile_refuses_unbound_bootstrap_and_destroy(self):
        self.invoke("bootstrap", profile="retained-434", success=False)
        self.invoke("plan-destroy", profile="retained-434", success=False)
        self.assertEqual(self.commands, [])

    def test_retained_bootstrap_requires_the_recorded_uid_and_repeats(self):
        self.plan_action = "create"
        self.invoke("plan-create", profile="retained-434")
        self.invoke("apply-create", digest=self.plan_digest("create"), profile="retained-434")
        self.invoke(
            "bootstrap",
            profile="retained-434",
            expected_uid="7c719d16-9125-4fe3-a93a-23ad9c25df06",
            success=False,
        )
        self.assertIn("differs from the approved bootstrap UID", self.output)
        self.assertFalse(any("kustomize" in command for command, _ in self.commands))
        self.invoke("bootstrap", profile="retained-434", expected_uid=self.cluster_uid)
        first = self.read_record("bootstrap")
        self.invoke("bootstrap", profile="retained-434", expected_uid=self.cluster_uid)
        self.assertEqual(first, self.read_record("bootstrap"))

    def test_lifecycle_applies_staged_bytes_and_bootstrap_repeats(self):
        self.plan_action = "create"
        self.invoke("plan-create")
        self.assertTrue((self.target.directory / "create-plan.txt").exists())
        self.change_source_after_staging = True
        self.invoke("apply-create", digest=self.plan_digest("create"))
        self.assertEqual(self.apply_payloads, [b"reviewed saved plan"])
        self.assertEqual(
            (self.target.directory / "create.tfplan").read_bytes(), b"different source"
        )
        self.assertEqual(self.target.kubeconfig.stat().st_mode & 0o777, 0o600)
        self.change_source_after_staging = False
        self.plan_action = "no-op"
        self.invoke("plan-repeat")
        self.invoke("bootstrap")
        self.invoke("bootstrap")
        self.assertEqual(sum("apply" in cmd for cmd, _ in self.commands if cmd[0] == "kubectl"), 2)
        self.assertTrue(
            all(
                call["isolated_environment"]["DOCKER_HOST"] == DOCKER_ENDPOINT
                for _, call in self.commands
            )
        )

    def test_apply_requires_the_reviewed_digest(self):
        self.plan_action = "create"
        self.invoke("plan-create")
        self.invoke("apply-create", success=False)
        self.invoke("apply-create", digest="0" * 64, success=False)
        self.assertEqual(self.apply_payloads, [])

    def test_apply_refuses_replacement_even_if_digest_matches(self):
        self.plan_action = "create"
        self.invoke("plan-create")
        self.plan_action = ["delete", "create"]
        self.invoke("apply-create", digest=self.plan_digest("create"), success=False)
        self.assertIn("unapproved action", self.output)
        self.assertEqual(self.apply_payloads, [])

    def test_wrong_uid_cannot_bootstrap_or_destroy(self):
        self.create()
        self.plan_action = "delete"
        self.invoke("plan-destroy")
        self.replacement_uid = "7c719d16-9125-4fe3-a93a-23ad9c25df06"
        self.invoke("bootstrap", success=False)
        self.invoke("apply-destroy", digest=self.plan_digest("destroy"), success=False)
        self.assertIn("UID", self.output)
        self.assertEqual(self.apply_payloads, [b"reviewed saved plan"])

    def test_refuses_stale_state_and_requires_fresh_destroy_plan(self):
        self.create()
        self.plan_action = "delete"
        self.invoke("plan-destroy")
        with self.target.state.open("a") as state:
            state.write(" ")
        self.invoke("apply-destroy", digest=self.plan_digest("destroy"), success=False)
        self.assertIn("stale", self.output)
        self.invoke("plan-destroy")
        self.invoke("apply-destroy", digest=self.plan_digest("destroy"))
        self.assertEqual(self.clusters, {})
        self.assertFalse(self.target.kubeconfig.exists())

    def test_refuses_plan_after_docker_daemon_changes(self):
        self.plan_action = "create"
        self.invoke("plan-create")
        self.daemon = "different-daemon"
        self.invoke("apply-create", digest=self.plan_digest("create"), success=False)
        self.assertIn("stale", self.output)
        self.assertEqual(self.apply_payloads, [])

    def test_interruption_blocks_writes_until_state_is_reconciled(self):
        self.plan_action = "create"
        self.invoke("plan-create")
        self.fail_mutation = "apply"
        self.invoke("apply-create", digest=self.plan_digest("create"), success=False)
        self.invoke("plan-create", success=False)
        self.assertIn("interrupted", self.output)
        self.invoke("observe")
        self.assertIn('"unfinished":"create"', self.output)
        self.invoke("recover")
        self.fail_mutation = None
        self.create()

    def test_recover_accepts_metadata_only_state_after_a_rolled_back_create(self):
        self.plan_action = "create"
        self.invoke("plan-create", profile="retained-434")
        self.fail_mutation = "apply"
        self.invoke(
            "apply-create", digest=self.plan_digest("create"), profile="retained-434", success=False
        )
        state = self.target.state
        state.write_text('{"version":4,"serial":1,"resources":[],"outputs":{"unexpected":{}}}')
        state.chmod(0o600)
        self.invoke("recover", profile="retained-434", success=False)
        self.assertTrue((self.target.directory / "pending.json").exists())
        empty_state = b'{"version":4,"serial":1,"resources":[],"outputs":{}}'
        state.write_bytes(empty_state)
        self.invoke("recover", profile="retained-434")
        self.assertEqual(state.read_bytes(), empty_state)
        self.assertFalse((self.target.directory / "pending.json").exists())
        self.invoke("plan-create", profile="retained-434")

    def test_recover_completed_retained_create_requires_matching_provider_identity(self):
        self.plan_action = "create"
        self.invoke("plan-create", profile="retained-434")
        self.assertEqual(self.target.context, "kind-perfeng-retained-434")
        with patch(
            "scripts.iac_pilot.Runtime.publish_config", side_effect=ValueError("Interrupted")
        ):
            self.invoke(
                "apply-create",
                digest=self.plan_digest("create"),
                profile="retained-434",
                success=False,
            )
        saved = json.loads(self.target.state.read_bytes())
        attributes = saved["resources"][0]["instances"][0]["attributes"]
        attributes["kubeconfig"] = self.native(
            ["kind", "get", "kubeconfig", "--name", self.target.name]
        )
        attributes["endpoint"] = "https://127.0.0.1:44444"
        self.target.state.write_text(json.dumps(saved))
        self.invoke("recover", profile="retained-434", success=False)
        self.assertTrue((self.target.directory / "pending.json").exists())
        self.assertFalse(self.target.kubeconfig.exists())

        attributes["endpoint"] = "https://127.0.0.1:44321"
        self.target.state.write_text(json.dumps(saved))
        self.invoke("recover", profile="retained-434")
        self.assertEqual(self.read_record("identity")["uid"], self.cluster_uid)
        self.assertFalse((self.target.directory / "pending.json").exists())
        self.assertEqual(len(self.apply_payloads), 1)

    def test_same_target_lock_refuses_a_second_command_before_docker_access(self):
        with self.target.locked():
            self.invoke("plan-create", success=False)
        self.assertIn("holds the #74 target lock", self.output)
        self.assertEqual(self.commands, [])
