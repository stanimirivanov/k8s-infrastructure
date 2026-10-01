"""Regression tests for the repository's executable contributor policy."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.repository_policy import (
    ISSUE_HEADINGS,
    PR_HEADINGS,
    check_adrs,
    check_documentation,
    check_repository,
    check_templates,
)


class RepositoryPolicyTests(TestCase):
    def test_current_repository_accepts_commit_pinned_actions(self) -> None:
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
                "- Milestone: MNN — Outcome\n"
                + "\n".join(f"## {heading}" for heading in PR_HEADINGS),
                encoding="utf-8",
            )
            self.assertEqual(check_templates(root), [])
            issue.write_text("**Milestone:** MNN — Outcome\n## Scope\n", encoding="utf-8")
            self.assertTrue(any("repository identity" in error for error in check_templates(root)))
            self.assertTrue(any("section order" in error for error in check_templates(root)))

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
