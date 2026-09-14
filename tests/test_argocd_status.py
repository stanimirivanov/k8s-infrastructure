from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts import gitops_status


class GitOpsStatusTests(unittest.TestCase):
    def test_application_health_requires_creation_sync_and_health(self) -> None:
        with patch("scripts.gitops_status.cluster.run") as run:
            gitops_status.wait_for_application(["kubectl"], "example")

        conditions = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(
            conditions,
            [
                "--for=create",
                "--for=jsonpath={.status.sync.status}=Synced",
                "--for=jsonpath={.status.health.status}=Healthy",
            ],
        )
        for call in run.call_args_list:
            self.assertIn("application/example", call.args[0])

    def test_application_accepts_state_reached_at_primary_timeout_boundary(self) -> None:
        status = {
            "status": {
                "sync": {"status": "Synced", "revision": "a" * 40},
                "health": {"status": "Healthy"},
            }
        }
        output = StringIO()
        with (
            patch(
                "scripts.gitops_status.cluster.run",
                side_effect=["", ValueError("wait expired"), json.dumps(status), ""],
            ) as run,
            redirect_stdout(output),
        ):
            gitops_status.wait_for_application(["kubectl"], "example")

        self.assertIn("as the primary wait expired; continuing", output.getvalue())
        self.assertFalse(
            any(
                f"--timeout={gitops_status.APPLICATION_GRACE_TIMEOUT}s" in call.args[0]
                for call in run.call_args_list
            )
        )

    def test_application_failure_prints_conditions_and_uses_one_grace_period(self) -> None:
        status = {
            "status": {
                "sync": {"status": "Unknown", "revision": "a" * 40},
                "health": {"status": "Healthy"},
                "conditions": [
                    {
                        "type": "ComparisonError",
                        "message": "repository server unavailable",
                    }
                ],
            }
        }
        output = StringIO()
        with (
            patch(
                "scripts.gitops_status.cluster.run",
                side_effect=[
                    "",
                    ValueError("primary wait expired"),
                    json.dumps(status),
                    ValueError("grace wait expired"),
                    json.dumps(status),
                ],
            ) as run,
            redirect_stdout(output),
            self.assertRaisesRegex(
                ValueError,
                "example to report sync=Synced failed after 180s plus 60s grace",
            ),
        ):
            gitops_status.wait_for_application(["kubectl"], "example")

        rendered = output.getvalue()
        self.assertIn("Condition ComparisonError: repository server unavailable", rendered)
        self.assertIn("Application example: sync=Unknown, health=Healthy", rendered)
        waits = [call.args[0] for call in run.call_args_list if "wait" in call.args[0]]
        self.assertEqual(
            [
                argument
                for command in waits
                for argument in command
                if argument.startswith("--timeout")
            ],
            ["--timeout=180s", "--timeout=180s", "--timeout=60s"],
        )

    def test_argocd_rollout_failure_identifies_workload_and_pod_state(self) -> None:
        workload = {
            "spec": {"replicas": 1},
            "status": {"updatedReplicas": 1, "readyReplicas": 0, "availableReplicas": 0},
        }
        pods = {
            "items": [
                {
                    "metadata": {"name": "argocd-repo-server-example"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {
                                "ready": False,
                                "restartCount": 4,
                                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            }
                        ],
                    },
                }
            ]
        }
        output = StringIO()
        with (
            patch(
                "scripts.gitops_status.cluster.run",
                side_effect=[
                    ValueError("rollout expired"),
                    json.dumps(workload),
                    json.dumps(pods),
                ],
            ),
            redirect_stdout(output),
            self.assertRaisesRegex(
                ValueError,
                "Argo CD deployment argocd-repo-server.*within 300s",
            ),
        ):
            gitops_status.wait_for_workload(["kubectl"], "deployment", "argocd-repo-server")

        rendered = output.getvalue()
        self.assertIn("Deployment argocd-repo-server: desired=1, updated=1, ready=0", rendered)
        self.assertIn("restarts=4, reason=CrashLoopBackOff", rendered)

    def test_condition_messages_are_bounded_and_redact_credentials(self) -> None:
        token = "github_pat_" + "x" * 30
        message = f"failed https://operator:{token}@github.com/repository: {token}"

        rendered = gitops_status.compact_message(message, limit=80)

        self.assertNotIn(token, rendered)
        self.assertNotIn("operator", rendered)
        self.assertLessEqual(len(rendered), 80)


if __name__ == "__main__":
    unittest.main()
