from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import argocd


class ArgoCDBootstrapTests(unittest.TestCase):
    @staticmethod
    def namespace(owner: str = argocd.OWNER) -> str:
        labels = {"app.kubernetes.io/managed-by": owner}
        if owner == argocd.OWNER:
            labels.update(
                {
                    "app.kubernetes.io/name": argocd.NAMESPACE,
                    "app.kubernetes.io/part-of": "kubernetes-substrate",
                }
            )
        elif owner == argocd.LEGACY_OWNER:
            labels["perfeng.io/purpose"] = "gitops-control-plane"
        return json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": argocd.NAMESPACE, "labels": labels},
            }
        )

    def test_repository_configuration_selects_official_semantic_release(self) -> None:
        self.assertEqual(argocd.validate_configuration(), "v3.5.2")

    def test_reviewed_redis_digest_and_pull_policy_patch_are_accepted(self) -> None:
        configuration = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "kustomization.yaml")
        configuration["images"] = [
            {
                "name": "public.ecr.aws/docker/library/redis",
                "newName": "docker.io/library/redis",
                "digest": "sha256:08ad0b1d280850169a790dba1393ff7a90aef951fc19632cf4d3ce4f78e679ba",
            }
        ]
        configuration["patches"] = [{"path": "redis-pull-policy.yaml"}]
        self.assertEqual(argocd.argocd_version(configuration), "v3.5.2")

    def test_floating_release_is_rejected(self) -> None:
        configuration = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "kustomization.yaml")
        configuration["resources"][1] = (
            "https://raw.githubusercontent.com/argoproj/argo-cd/main/manifests/install.yaml"
        )

        with self.assertRaisesRegex(ValueError, "semantic release tag"):
            argocd.argocd_version(configuration)

    def test_redis_selection_rejects_unreviewed_images_patches_and_release(self) -> None:
        original = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "kustomization.yaml")
        for field, value in (
            ("name", "other/redis"),
            ("newName", "example.test/redis"),
            ("digest", "sha256:" + "0" * 64),
            ("digest", "latest"),
            ("newTag", "latest"),
        ):
            with self.subTest(field=field, value=value):
                configuration = copy.deepcopy(original)
                configuration["images"][0][field] = value
                with self.assertRaises(ValueError):
                    argocd.argocd_version(configuration)
        for key, value in (
            ("images", []),
            ("images", original["images"] * 2),
            ("patches", []),
            ("patches", [{"path": "../redirect.yaml"}]),
            ("patches", original["patches"] * 2),
            ("labels", []),
            (
                "resources",
                [
                    "namespace.yaml",
                    "https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml",
                ],
            ),
        ):
            with self.subTest(key=key, value=value):
                configuration = copy.deepcopy(original)
                configuration[key] = value
                with self.assertRaises(ValueError):
                    argocd.argocd_version(configuration)

    def test_pull_policy_patch_cannot_change_target_or_other_resource_fields(self) -> None:
        original = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "redis-pull-policy.yaml")
        for change in (
            "kind",
            "name",
            "namespace",
            "container",
            "always",
            "never",
            "image",
            "sidecar",
            "replicas",
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bootstrap = root / argocd.BOOTSTRAP
                bootstrap.mkdir(parents=True)
                for filename in ("kustomization.yaml", "namespace.yaml"):
                    (bootstrap / filename).write_text(
                        (argocd.ROOT / argocd.BOOTSTRAP / filename).read_text()
                    )
                document = copy.deepcopy(original)
                container = document["spec"]["template"]["spec"]["containers"][0]
                if change == "kind":
                    document["kind"] = "StatefulSet"
                elif change == "name":
                    document["metadata"][change] = "other"
                elif change == "namespace":
                    document["metadata"][change] = argocd.NAMESPACE
                elif change == "container":
                    container["name"] = "secret-init"
                elif change in {"always", "never"}:
                    container["imagePullPolicy"] = change.title()
                elif change == "image":
                    container["image"] = "example.test/redis:latest"
                elif change == "sidecar":
                    document["spec"]["template"]["spec"]["containers"].append({"name": "other"})
                else:
                    document["spec"]["replicas"] = 2
                (bootstrap / "redis-pull-policy.yaml").write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, "Redis pull-policy patch"):
                    argocd.validate_configuration(root)

    def test_non_official_manifest_host_is_rejected(self) -> None:
        configuration = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "kustomization.yaml")
        configuration["resources"][1] = (
            "https://example.test/argoproj/argo-cd/v3.5.2/manifests/install.yaml"
        )

        with self.assertRaisesRegex(ValueError, "official versioned install manifest"):
            argocd.argocd_version(configuration)

    def test_unexpected_kustomization_field_is_rejected(self) -> None:
        configuration = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "kustomization.yaml")
        configuration["images"] = []

        with self.assertRaisesRegex(ValueError, "Invalid Argo CD Kustomize bootstrap"):
            argocd.argocd_version(configuration)

    def test_namespace_identity_is_exact(self) -> None:
        namespace = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "namespace.yaml")
        changed = copy.deepcopy(namespace)
        changed["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "another-owner"

        argocd.validate_namespace(namespace)
        with self.assertRaisesRegex(ValueError, "namespace identity"):
            argocd.validate_namespace(changed)

    def test_bootstrap_preview_contains_apply_then_bounded_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary).resolve()
            commands = argocd.commands("bootstrap", state_root=state_root)

        self.assertEqual(len(commands), 10)
        self.assertEqual(
            commands[0][-6:], ["get", "namespace", "argocd", "--ignore-not-found", "-o", "json"]
        )
        self.assertEqual(
            commands[1][-5:],
            [
                "apply",
                "--server-side",
                "--force-conflicts",
                "-k",
                str(argocd.ROOT / argocd.BOOTSTRAP),
            ],
        )
        self.assertIn(str(state_root / ".local/perfeng-local.kubeconfig"), commands[1])
        self.assertEqual(commands[2], commands[0])
        self.assertEqual(
            commands[-1][-2:], ["statefulset/argocd-application-controller", "--timeout=300s"]
        )

    def test_health_preview_contains_only_controller_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            commands = argocd.commands("health", state_root=Path(temporary).resolve())

        self.assertEqual(len(commands), 8)
        self.assertEqual(
            commands[0][-6:], ["get", "namespace", "argocd", "--ignore-not-found", "-o", "json"]
        )
        self.assertTrue(all("rollout" in command for command in commands[1:]))
        self.assertTrue(all("apply" not in command for command in commands))

    def test_bootstrap_migrates_only_the_reviewed_legacy_namespace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.argocd.local_kind.execute") as cluster_health,
            patch(
                "scripts.argocd.local_kind.run",
                side_effect=[self.namespace(argocd.LEGACY_OWNER), "", self.namespace()],
            ) as run,
            patch("scripts.argocd.argocd_status.wait_for_argocd") as wait,
        ):
            argocd.execute("bootstrap", state_root=Path(temporary))

        cluster_health.assert_called_once_with("health", Path(temporary).resolve())
        self.assertEqual(run.call_count, 3)
        self.assertIn("apply", run.call_args_list[1].args[0])
        wait.assert_called_once()

    def test_health_is_read_only_and_requires_infrastructure_ownership(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.argocd.local_kind.execute") as cluster_health,
            patch("scripts.argocd.local_kind.run", return_value=self.namespace()) as run,
            patch("scripts.argocd.argocd_status.wait_for_argocd") as wait,
        ):
            argocd.execute("health", state_root=Path(temporary))

        cluster_health.assert_called_once()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("apply", run.call_args.args[0])
        wait.assert_called_once()

    def test_bootstrap_rejects_foreign_namespace_before_apply(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.argocd.local_kind.execute"),
            patch("scripts.argocd.local_kind.run", return_value=self.namespace("other")) as run,
            patch("scripts.argocd.argocd_status.wait_for_argocd") as wait,
            self.assertRaisesRegex(ValueError, "unexpected owner"),
        ):
            argocd.execute("bootstrap", state_root=Path(temporary))

        run.assert_called_once()
        wait.assert_not_called()

    def test_namespace_validation_rejects_incomplete_new_and_legacy_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "namespace is missing"):
            argocd.validate_live_namespace("")

        incomplete = json.loads(self.namespace())
        del incomplete["metadata"]["labels"]["app.kubernetes.io/part-of"]
        with self.assertRaisesRegex(ValueError, "incomplete infrastructure ownership"):
            argocd.validate_live_namespace(json.dumps(incomplete))

        legacy = json.loads(self.namespace(argocd.LEGACY_OWNER))
        del legacy["metadata"]["labels"]["perfeng.io/purpose"]
        with self.assertRaisesRegex(ValueError, "invalid legacy ownership"):
            argocd.validate_live_namespace(json.dumps(legacy), allow_legacy=True)

    def test_cli_preview_is_offline(self) -> None:
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.argocd.local_kind.run") as run,
            redirect_stdout(output),
        ):
            result = argocd.main(["health", "--state-root", temporary])

        self.assertEqual(result, 0)
        self.assertIn("Preview only", output.getvalue())
        run.assert_not_called()

    def test_cli_execution_dispatches_without_using_preview_commands(self) -> None:
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.argocd.execute") as execute,
            patch("scripts.argocd.commands") as commands,
            redirect_stdout(output),
        ):
            result = argocd.main(["bootstrap", "--execute", "--state-root", temporary])

        self.assertEqual(result, 0)
        execute.assert_called_once_with("bootstrap", state_root=Path(temporary))
        commands.assert_not_called()

    def test_production_module_has_no_consumer_gitops_behavior(self) -> None:
        source = (argocd.ROOT / "scripts/argocd.py").read_text(encoding="utf-8")
        for excluded in (
            "ROOT_APPLICATION",
            "AppProject",
            "ensure_credentials",
            "PERFENG_REPOSITORIES",
            "control_plane_health",
            "object_store",
            "postgres",
        ):
            self.assertNotIn(excluded, source)


if __name__ == "__main__":
    unittest.main()
