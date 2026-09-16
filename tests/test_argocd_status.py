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

    def test_staged_application_wait_can_stop_after_sync(self) -> None:
        with patch("scripts.gitops_status.cluster.run") as run:
            gitops_status.wait_for_application(["kubectl"], "example", require_health=False)

        conditions = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(
            conditions,
            ["--for=create", "--for=jsonpath={.status.sync.status}=Synced"],
        )

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
        self.assertFalse(any("annotate" in call.args[0] for call in run.call_args_list))
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
                    "",
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
        refreshes = [call.args[0] for call in run.call_args_list if "annotate" in call.args[0]]
        self.assertEqual(
            refreshes,
            [
                [
                    "kubectl",
                    "--request-timeout=10s",
                    "annotate",
                    "application/example",
                    "argocd.argoproj.io/refresh=normal",
                    "--overwrite",
                ]
            ],
        )
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

    def test_refresh_allows_sync_to_converge_during_grace(self) -> None:
        output = StringIO()
        with (
            patch(
                "scripts.gitops_status.cluster.run",
                side_effect=[
                    ValueError("primary expired"),
                    json.dumps({"status": {}}),
                    "",
                    "",
                ],
            ) as run,
            redirect_stdout(output),
        ):
            gitops_status.wait_for_application_state(["kubectl"], "example", "sync", "Synced")

        self.assertEqual(run.call_args_list[2].args[0][2], "annotate")
        self.assertIn("--timeout=60s", run.call_args_list[3].args[0])
        self.assertIn("Requesting normal refresh", output.getvalue())

    def test_refresh_failure_does_not_replace_original_timeout(self) -> None:
        output = StringIO()
        with (
            patch(
                "scripts.gitops_status.cluster.run",
                side_effect=[
                    ValueError("primary expired"),
                    "{}",
                    ValueError("refresh forbidden"),
                    ValueError("grace expired"),
                    "{}",
                ],
            ),
            redirect_stdout(output),
            self.assertRaisesRegex(ValueError, "failed after 180s plus 60s grace") as raised,
        ):
            gitops_status.wait_for_application_state(["kubectl"], "example", "health", "Healthy")

        self.assertIn("refresh request failed", output.getvalue())
        self.assertEqual(str(raised.exception.__cause__), "grace expired")

    def test_application_accepts_state_reached_at_grace_timeout_boundary(self) -> None:
        status = {"status": {"sync": {"status": "Synced"}}}
        output = StringIO()
        with (
            patch(
                "scripts.gitops_status.cluster.run",
                side_effect=[
                    ValueError("primary expired"),
                    "{}",
                    "",
                    ValueError("grace expired"),
                    json.dumps(status),
                ],
            ),
            redirect_stdout(output),
        ):
            gitops_status.wait_for_application_state(["kubectl"], "example", "sync", "Synced")

        self.assertIn("as the grace wait expired; continuing", output.getvalue())

    def test_drift_diagnostics_identify_resources_without_reading_contents(self) -> None:
        status = {
            "status": {
                "resources": [
                    {
                        "group": "argoproj.io",
                        "kind": "Application",
                        "namespace": "argocd",
                        "name": "child",
                        "status": "OutOfSync",
                        "health": {"status": "Healthy"},
                    },
                    {
                        "kind": "Secret",
                        "name": "credentials",
                        "status": "OutOfSync",
                        "requiresPruning": True,
                        "data": {"token": "must-not-be-printed"},
                    },
                    {"kind": "Service", "name": "healthy", "status": "Synced"},
                    None,
                ]
            }
        }
        with patch("scripts.gitops_status.cluster.run") as run:
            rendered = "\n".join(gitops_status.application_diagnostics("example", status))

        run.assert_not_called()
        self.assertIn(
            "argoproj.io/Application argocd/child: sync=OutOfSync, health=Healthy", rendered
        )
        self.assertIn("core/Secret cluster-scoped/credentials", rendered)
        self.assertIn("requiresPruning=True", rendered)
        self.assertNotIn("must-not-be-printed", rendered)
        self.assertNotIn("healthy:", rendered)

    def test_drift_diagnostics_are_bounded_and_redacted(self) -> None:
        token = "github_pat_" + "x" * 30
        resources = [{"status": "OutOfSync", "name": token}] * 25
        rendered = gitops_status.out_of_sync_resources(resources)

        self.assertEqual(len(rendered), 21)
        self.assertIn("5 additional resources omitted", rendered[-1])
        self.assertNotIn(token, "\n".join(rendered))
        self.assertIn("[redacted]", "\n".join(rendered))

    def test_drift_diagnostics_distinguish_missing_status_from_no_reported_drift(self) -> None:
        self.assertEqual(
            gitops_status.out_of_sync_resources(None), ["Out-of-sync resources: status unavailable"]
        )
        self.assertIn("comparison may be stale", gitops_status.out_of_sync_resources([])[0])

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
