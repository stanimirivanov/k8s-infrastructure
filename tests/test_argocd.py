from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import gitops

TOKEN = "github-private-repository-read-token"


def stored_credentials() -> dict[str, object]:
    document = gitops.credential_document(TOKEN)
    return {
        "metadata": document["metadata"],
        "immutable": document["immutable"],
        "type": document["type"],
        "data": {
            key: base64.b64encode(value.encode()).decode("ascii")
            for key, value in document["stringData"].items()
        },
    }


class GitOpsTests(unittest.TestCase):
    def test_supported_actions_exclude_superseded_deploy(self) -> None:
        self.assertEqual(gitops.ACTIONS, ("validate", "bootstrap", "health"))
        with patch("scripts.gitops.validate_configuration") as validate:
            with self.assertRaisesRegex(ValueError, "Unknown executable"):
                gitops.execute("deploy")
        validate.assert_not_called()

    def test_repository_configuration_selects_an_explicit_release(self) -> None:
        self.assertEqual(gitops.validate_configuration(), "v3.5.2")

    def test_project_sources_are_limited_to_child_application_repositories(self) -> None:
        applications = [
            {"spec": {"source": {"repoURL": gitops.REPOSITORY_URL}}},
            {"spec": {"source": {"repoURL": gitops.DEPLOYMENT_REPOSITORY}}},
        ]
        project_spec = {"sourceRepos": [gitops.REPOSITORY_URL, gitops.DEPLOYMENT_REPOSITORY]}

        gitops.validate_project_sources(project_spec, applications)

        project_spec["sourceRepos"].append(
            "https://github.com/stanimirivanov/k8s-infrastructure.git"
        )
        with self.assertRaisesRegex(ValueError, "AppProject source repositories"):
            gitops.validate_project_sources(project_spec, applications)

    def test_control_plane_application_is_bound_to_the_deployment_lock(self) -> None:
        root = Path(__file__).resolve().parents[1]
        application = gitops.load_yaml(
            root / "gitops/environments/local/control-plane-application.yaml"
        )
        release = gitops.load_lock(root / "deployments/local-control-plane.json")

        gitops.validate_control_plane_application(application, release)
        application["spec"]["source"]["targetRevision"] = "main"
        with self.assertRaisesRegex(ValueError, "control-plane Application"):
            gitops.validate_control_plane_application(application, release)

    def test_postgres_application_preserves_the_existing_release(self) -> None:
        root = Path(__file__).resolve().parents[1]
        application = gitops.load_yaml(root / "gitops/environments/local/postgres-application.yaml")

        gitops.validate_postgres_application(application)
        application["spec"]["source"]["helm"]["releaseName"] = "postgres-gitops"
        with self.assertRaisesRegex(ValueError, "PostgreSQL Application"):
            gitops.validate_postgres_application(application)

    def test_postgres_application_is_active_after_rollback_rehearsal(self) -> None:
        root = Path(__file__).resolve().parents[1]
        kustomization = gitops.load_yaml(root / "gitops/environments/local/kustomization.yaml")

        self.assertIn("postgres-application.yaml", kustomization["resources"])

    def test_seaweedfs_application_preserves_the_existing_release(self) -> None:
        root = Path(__file__).resolve().parents[1]
        application = gitops.load_yaml(
            root / "gitops/environments/local/seaweedfs-application.yaml"
        )

        gitops.validate_seaweedfs_application(application)
        application["spec"]["source"]["helm"]["releaseName"] = "seaweedfs-gitops"
        with self.assertRaisesRegex(ValueError, "SeaweedFS Application"):
            gitops.validate_seaweedfs_application(application)

    def test_seaweedfs_application_requires_self_healing_without_pruning(self) -> None:
        root = Path(__file__).resolve().parents[1]
        application = gitops.load_yaml(
            root / "gitops/environments/local/seaweedfs-application.yaml"
        )

        gitops.validate_seaweedfs_application(application)
        application["spec"]["syncPolicy"]["automated"]["selfHeal"] = False
        with self.assertRaisesRegex(ValueError, "SeaweedFS Application"):
            gitops.validate_seaweedfs_application(application)

    def test_seaweedfs_application_is_active_after_rollback_rehearsal(self) -> None:
        root = Path(__file__).resolve().parents[1]
        kustomization = gitops.load_yaml(root / "gitops/environments/local/kustomization.yaml")

        self.assertIn("seaweedfs-application.yaml", kustomization["resources"])

    def test_workload_applications_self_heal_without_pruning(self) -> None:
        root = Path(__file__).resolve().parents[1]
        applications = {
            "root": gitops.load_yaml(root / "gitops/bootstrap/local-root-application.yaml"),
            "sample-sut": gitops.load_yaml(
                root / "gitops/environments/local/sample-sut-application.yaml"
            ),
            "postgres": gitops.load_yaml(
                root / "gitops/environments/local/postgres-application.yaml"
            ),
            "seaweedfs": gitops.load_yaml(
                root / "gitops/environments/local/seaweedfs-application.yaml"
            ),
            "control-plane": gitops.load_yaml(
                root / "gitops/environments/local/control-plane-application.yaml"
            ),
        }

        policies = {
            name: application["spec"]["syncPolicy"]["automated"]
            for name, application in applications.items()
        }
        self.assertIs(policies["root"]["selfHeal"], False)
        self.assertIs(policies["sample-sut"]["selfHeal"], True)
        self.assertIs(policies["postgres"]["selfHeal"], True)
        self.assertIs(policies["seaweedfs"]["selfHeal"], True)
        self.assertIs(policies["control-plane"]["selfHeal"], True)
        self.assertTrue(all(policy["prune"] is False for policy in policies.values()))

    def test_mutable_or_foreign_manifest_source_is_rejected(self) -> None:
        prefix = "https://raw.githubusercontent.com/argoproj/argo-cd/"
        sources = [
            f"{prefix}stable/manifests/install.yaml",
            f"{prefix}main/manifests/install.yaml",
            "https://raw.githubusercontent.com/someone/argo-cd/v3.5.2/manifests/install.yaml",
        ]
        for source in sources:
            with self.subTest(source=source):
                kustomization = {
                    "apiVersion": "kustomize.config.k8s.io/v1beta1",
                    "kind": "Kustomization",
                    "namespace": "argocd",
                    "resources": ["namespace.yaml", source],
                }
                with self.assertRaises(ValueError):
                    gitops.argocd_version(kustomization)

    def test_credentials_are_immutable_and_scoped_to_the_github_owner(self) -> None:
        document = gitops.credential_document(TOKEN)

        self.assertIs(document["immutable"], True)
        self.assertEqual(document["stringData"]["url"], gitops.REPOSITORY_PREFIX)
        self.assertEqual(
            document["metadata"]["labels"]["argocd.argoproj.io/secret-type"],
            "repo-creds",
        )
        gitops.validate_stored_credentials(stored_credentials())

    def test_new_credentials_use_environment_and_private_stdin(self) -> None:
        kube = ["kubectl", "--namespace", gitops.NAMESPACE]
        with (
            patch.dict(
                "scripts.gitops.os.environ",
                {gitops.TOKEN_ENVIRONMENT_VARIABLE: TOKEN},
                clear=True,
            ),
            patch("scripts.gitops.cluster.run", side_effect=["", ""]) as run,
        ):
            gitops.ensure_credentials(kube, create=True)

        create = run.call_args_list[1]
        self.assertNotIn(TOKEN, " ".join(create.args[0]))
        payload = json.loads(create.kwargs["input_data"])
        self.assertEqual(payload["stringData"]["password"], TOKEN)

    def test_existing_credentials_are_reused_without_reading_environment(self) -> None:
        kube = ["kubectl", "--namespace", gitops.NAMESPACE]
        environment = Mock(side_effect=AssertionError("environment read was unexpected"))
        with (
            patch(
                "scripts.gitops.cluster.run", return_value=json.dumps(stored_credentials())
            ) as run,
            patch("scripts.gitops.os.environ.get", environment),
        ):
            gitops.ensure_credentials(kube, create=True)

        run.assert_called_once()
        environment.assert_not_called()

    def test_foreign_namespace_is_rejected(self) -> None:
        namespace = {"metadata": {"labels": {"app.kubernetes.io/managed-by": "someone-else"}}}
        with patch("scripts.gitops.cluster.run", return_value=json.dumps(namespace)):
            with self.assertRaisesRegex(ValueError, "not owned"):
                gitops.verify_namespace(["kubectl"], create=True)

    def test_root_application_is_applied_through_stdin(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with patch("scripts.gitops.cluster.run", side_effect=["", ""]) as run:
            gitops.ensure_root(["kubectl"], root, apply=True)

        apply = run.call_args_list[1]
        self.assertEqual(apply.args[0][-3:], ["apply", "-f", "-"])
        self.assertIn(b"name: perfeng-local-root", apply.kwargs["input_data"])

    def test_bootstrap_stops_after_stateful_workloads_are_ready(self) -> None:
        kube = ["kubectl"]
        with (
            patch("scripts.gitops.validate_configuration"),
            patch("scripts.gitops.cluster.verify_local_docker"),
            patch("scripts.gitops.cluster.verify_context"),
            patch("scripts.gitops.kubectl", return_value=kube),
            patch("scripts.gitops.verify_namespace") as namespace,
            patch("scripts.gitops.cluster.run"),
            patch("scripts.gitops.gitops_status.wait_for_argocd"),
            patch("scripts.gitops.ensure_credentials") as credentials,
            patch("scripts.gitops.ensure_root") as root,
            patch("scripts.gitops.gitops_status.wait_for_application") as wait,
            patch("scripts.gitops.postgres.execute") as postgres_health,
            patch("scripts.gitops.object_store.execute") as object_store_health,
            patch("scripts.gitops.control_plane_health.verify") as control,
        ):
            gitops.execute("bootstrap")

        namespace.assert_called_once_with(kube, create=True)
        credentials.assert_called_once_with(kube, create=True)
        root.assert_called_once_with(kube, gitops.cluster.ROOT, apply=True)
        self.assertIs(wait.call_args_list[0].kwargs["require_health"], False)
        self.assertEqual(
            [call.args[1] for call in wait.call_args_list],
            [
                gitops.ROOT_APPLICATION,
                gitops.SAMPLE_SUT_APPLICATION,
                gitops.POSTGRES_APPLICATION,
                gitops.SEAWEEDFS_APPLICATION,
            ],
        )
        postgres_health.assert_called_once_with("health", gitops.cluster.ROOT)
        object_store_health.assert_not_called()
        control.assert_not_called()

    def test_health_observes_without_applying_resources(self) -> None:
        kube = ["kubectl"]
        with (
            patch("scripts.gitops.validate_configuration"),
            patch("scripts.gitops.cluster.verify_local_docker"),
            patch("scripts.gitops.cluster.verify_context"),
            patch("scripts.gitops.kubectl", return_value=kube),
            patch("scripts.gitops.verify_namespace") as namespace,
            patch("scripts.gitops.cluster.run") as run,
            patch("scripts.gitops.gitops_status.wait_for_argocd"),
            patch("scripts.gitops.ensure_credentials") as credentials,
            patch("scripts.gitops.ensure_root") as root,
            patch("scripts.gitops.gitops_status.wait_for_application") as wait,
            patch("scripts.gitops.postgres.execute") as postgres_health,
            patch("scripts.gitops.object_store.execute") as object_store_health,
            patch("scripts.gitops.control_plane_health.verify") as control,
        ):
            gitops.execute("health")

        namespace.assert_called_once_with(kube, create=False)
        credentials.assert_called_once_with(kube, create=False)
        root.assert_called_once_with(kube, gitops.cluster.ROOT, apply=False)
        self.assertTrue(
            all(call.kwargs.get("require_health", True) for call in wait.call_args_list)
        )
        self.assertEqual(run.call_count, 1)
        self.assertIn("rollout", run.call_args.args[0])
        postgres_health.assert_called_once_with("health", gitops.cluster.ROOT)
        object_store_health.assert_called_once()
        control.assert_called_once_with(gitops.cluster.ROOT)


if __name__ == "__main__":
    unittest.main()
