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
    def test_repository_configuration_is_internally_consistent(self) -> None:
        self.assertEqual(gitops.validate_configuration(), "v3.5.2")

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

    def test_root_health_requires_sync_and_health(self) -> None:
        with patch("scripts.gitops.cluster.run") as run:
            gitops.wait_for_root(["kubectl"])

        conditions = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(
            conditions,
            [
                "--for=jsonpath={.status.sync.status}=Synced",
                "--for=jsonpath={.status.health.status}=Healthy",
            ],
        )


if __name__ == "__main__":
    unittest.main()
