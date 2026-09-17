from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import call, patch

from scripts import argocd_status


class ArgoCDStatusTests(unittest.TestCase):
    def test_rollout_commands_cover_the_reviewed_non_ha_controllers(self) -> None:
        commands = argocd_status.rollout_commands(["kubectl", "-n", "argocd"])

        self.assertEqual(len(commands), 7)
        self.assertEqual(
            commands[0][-2:],
            ["deployment/argocd-applicationset-controller", "--timeout=300s"],
        )
        self.assertEqual(
            commands[-1][-2:],
            ["statefulset/argocd-application-controller", "--timeout=300s"],
        )

    def test_diagnostic_json_never_replaces_the_original_failure(self) -> None:
        with patch("scripts.argocd_status.local_kind.run", side_effect=ValueError("failed")):
            self.assertIsNone(argocd_status.diagnostic_json(["kubectl"]))

        with patch("scripts.argocd_status.local_kind.run", return_value="not-json"):
            self.assertIsNone(argocd_status.diagnostic_json(["kubectl"]))

    def test_pod_summary_reports_readiness_restarts_and_waiting_reason(self) -> None:
        pod = {
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

        rendered = argocd_status.pod_summary(pod)

        self.assertIn("phase=Running", rendered)
        self.assertIn("ready=0/1", rendered)
        self.assertIn("restarts=4", rendered)
        self.assertIn("reason=CrashLoopBackOff", rendered)

    def test_malformed_nested_diagnostics_do_not_mask_the_rollout_failure(self) -> None:
        with (
            patch(
                "scripts.argocd_status.local_kind.run",
                side_effect=[
                    OSError("kubectl unavailable"),
                    json.dumps({"spec": [], "status": "unknown"}),
                    json.dumps({"items": [{"metadata": [], "status": None}]}),
                ],
            ),
            self.assertRaisesRegex(ValueError, "did not complete"),
        ):
            argocd_status.wait_for_workload(
                ["kubectl", "-n", "argocd"], "deployment", "argocd-server"
            )

    def test_rollout_failure_identifies_controller_and_pod_state(self) -> None:
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
        output = io.StringIO()
        with (
            patch(
                "scripts.argocd_status.local_kind.run",
                side_effect=[ValueError("rollout expired"), json.dumps(workload), json.dumps(pods)],
            ),
            redirect_stdout(output),
            self.assertRaisesRegex(
                ValueError,
                "Argo CD deployment argocd-repo-server.*within 300s",
            ),
        ):
            argocd_status.wait_for_workload(
                ["kubectl", "-n", "argocd"], "deployment", "argocd-repo-server"
            )

        rendered = output.getvalue()
        self.assertIn("Deployment argocd-repo-server: desired=1, updated=1, ready=0", rendered)
        self.assertIn("restarts=4, reason=CrashLoopBackOff", rendered)

    def test_wait_for_argocd_uses_each_reviewed_controller_once(self) -> None:
        with patch("scripts.argocd_status.wait_for_workload") as wait:
            argocd_status.wait_for_argocd(["kubectl"])

        expected = [call(["kubectl"], "deployment", name) for name in argocd_status.DEPLOYMENTS]
        expected.extend(
            call(["kubectl"], "statefulset", name) for name in argocd_status.STATEFULSETS
        )
        self.assertEqual(wait.call_args_list, expected)

    def test_diagnostics_are_bounded_and_redact_credentials(self) -> None:
        token = "github_pat_" + "x" * 30
        message = f"failed https://operator:{token}@github.com/repository: {token}"

        rendered = argocd_status.compact_message(message, limit=80)

        self.assertNotIn(token, rendered)
        self.assertNotIn("operator", rendered)
        self.assertLessEqual(len(rendered), 80)

    def test_module_excludes_consumer_application_state(self) -> None:
        for name in (
            "application_status",
            "application_diagnostics",
            "wait_for_application",
            "request_application_refresh",
        ):
            self.assertFalse(hasattr(argocd_status, name))


if __name__ == "__main__":
    unittest.main()
