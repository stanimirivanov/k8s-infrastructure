"""Regression tests for the repository's executable contributor policy."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.repository_policy import (
    ISSUE_HEADINGS,
    PR_HEADINGS,
    check_adrs,
    check_documentation,
    check_pr_event,
    check_repository,
    check_templates,
    main,
)


class RepositoryPolicyTests(TestCase):
    def test_current_repository_conforms(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(check_repository(root), [])

    def test_templates_reject_missing_repository_and_section_drift(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            issue = root / ".github/ISSUE_TEMPLATE/work-item.md"
            issue.parent.mkdir(parents=True)
            issue.write_text(
                "**Repository:** k8s-infrastructure\n**Milestone:** MNN — Outcome\n"
                + "\n".join(f"## {heading}" for heading in ISSUE_HEADINGS),
                encoding="utf-8",
            )
            pull_request = root / ".github/PULL_REQUEST_TEMPLATE.md"
            pull_request.write_text(
                "Closes #<issue-number>\n\n"
                "<!-- PR title: [#<issue-number>] Concise outcome -->\n\n"
                "- Milestone: MNN — Outcome\n"
                + "\n".join(f"## {heading}" for heading in PR_HEADINGS),
                encoding="utf-8",
            )
            self.assertEqual(check_templates(root), [])
            issue.write_text("**Milestone:** MNN — Outcome\n## Scope\n", encoding="utf-8")
            self.assertTrue(any("repository identity" in error for error in check_templates(root)))
            self.assertTrue(any("section order" in error for error in check_templates(root)))
            pull_request.write_text(
                pull_request.read_text(encoding="utf-8").replace("Closes #<issue-number>", "Issue"),
                encoding="utf-8",
            )
            self.assertTrue(any("first line" in error for error in check_templates(root)))

    def test_pr_event_requires_matching_issue_numbers(self) -> None:
        request = {
            "title": "[#18] Enforce issue-linked pull requests",
            "body": "Closes #18\n\nVerification: make verify",
            "head": {"ref": "feature/issue-18"},
            "user": {"login": "contributor"},
        }
        self.assertEqual(check_pr_event({"pull_request": request}), [])
        for field, value in (
            ("title", "[#19] Wrong issue"),
            ("body", "Closes #19\n\nWrong issue"),
            ("head", {"ref": "fix/issue-19"}),
        ):
            with self.subTest(field=field):
                changed = {**request, field: value}
                self.assertTrue(
                    any("one issue" in error for error in check_pr_event({"pull_request": changed}))
                )
        missing = {**request, "body": "Summary without closing reference"}
        self.assertTrue(
            any("first line" in error for error in check_pr_event({"pull_request": missing}))
        )

    def test_dependabot_pr_is_exempt_from_human_issue_format(self) -> None:
        event = {
            "pull_request": {
                "title": "deps(actions): update checkout",
                "body": None,
                "head": {"ref": "dependabot/github_actions/actions/checkout-7"},
                "user": {"login": "dependabot[bot]"},
            }
        }
        self.assertEqual(check_pr_event(event), [])

    def test_cli_validates_event_payload_from_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "event.json"
            path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "title": "[#18] Valid",
                            "body": "Closes #18\n\nDetails",
                            "head": {"ref": "feature/issue-19"},
                            "user": {"login": "contributor"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("scripts.repository_policy.check_repository", return_value=[]),
                patch.dict("os.environ", {"GITHUB_EVENT_PATH": str(path)}),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--pr-event"]), 1)
                self.assertIn("one issue", output.getvalue())

    def test_adrs_reject_missing_index_and_number_mismatch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            decisions = root / "docs/decisions"
            decisions.mkdir(parents=True)
            (decisions / "README.md").write_text("# Decisions\n", encoding="utf-8")
            (decisions / "0001-choice.md").write_text(
                "# ADR-0002: Choice\n- Status: Accepted\n- Date: 2026-09-30\n",
                encoding="utf-8",
            )
            errors = check_adrs(root)
            self.assertTrue(any("heading" in error for error in errors))
            self.assertTrue(any("indexed" in error for error in errors))

    def test_selectively_read_guide_requires_tldr(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            guide = root / "docs/architecture/boundary.md"
            guide.parent.mkdir(parents=True)
            guide.write_text("# Boundary\nA short but important contract.\n", encoding="utf-8")
            self.assertTrue(any("TL;DR" in error for error in check_documentation(root)))
            guide.write_text(
                "# Boundary\n## Details\nText\n## TL;DR\nLate summary\n",
                encoding="utf-8",
            )
            self.assertTrue(any("first section" in error for error in check_documentation(root)))
