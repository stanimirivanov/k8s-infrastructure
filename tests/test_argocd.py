from __future__ import annotations

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import argocd


class ArgoCDBootstrapTests(unittest.TestCase):
    def test_repository_configuration_selects_official_semantic_release(self) -> None:
        self.assertEqual(argocd.validate_configuration(), "v3.5.2")

    def test_floating_release_is_rejected(self) -> None:
        configuration = argocd.load_yaml(argocd.ROOT / argocd.BOOTSTRAP / "kustomization.yaml")
        configuration["resources"][1] = (
            "https://raw.githubusercontent.com/argoproj/argo-cd/main/manifests/install.yaml"
        )

        with self.assertRaisesRegex(ValueError, "semantic release tag"):
            argocd.argocd_version(configuration)

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

        self.assertEqual(len(commands), 8)
        self.assertEqual(
            commands[0][-5:],
            [
                "apply",
                "--server-side",
                "--force-conflicts",
                "-k",
                str(argocd.ROOT / argocd.BOOTSTRAP),
            ],
        )
        self.assertIn(str(state_root / ".local/perfeng-local.kubeconfig"), commands[0])
        self.assertEqual(
            commands[-1][-2:], ["statefulset/argocd-application-controller", "--timeout=300s"]
        )

    def test_health_preview_contains_only_controller_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            commands = argocd.commands("health", state_root=Path(temporary).resolve())

        self.assertEqual(len(commands), 7)
        self.assertTrue(all("rollout" in command for command in commands))
        self.assertTrue(all("apply" not in command for command in commands))

    def test_execute_refuses_before_process_invocation(self) -> None:
        with (
            patch("scripts.argocd.local_kind.run") as run,
            self.assertRaisesRegex(ValueError, "perfeng-environment"),
        ):
            argocd.execute("bootstrap")

        run.assert_not_called()

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

    def test_cli_execution_reports_dormant_owner(self) -> None:
        output = io.StringIO()
        with patch("scripts.argocd.local_kind.run") as run, redirect_stdout(output):
            result = argocd.main(["bootstrap", "--execute"])

        self.assertEqual(result, 1)
        self.assertIn("perfeng-environment", output.getvalue())
        run.assert_not_called()

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
