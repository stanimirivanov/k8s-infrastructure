"""Check local engineering policy that CI and contributors must agree on."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from .check_docs import markdown_files, validate_links, visible_lines

ROOT = Path(__file__).resolve().parents[1]
ISSUE_HEADINGS = (
    "Goal",
    "Scope",
    "Design decisions",
    "Security and operations",
    "Acceptance criteria",
    "Verification",
    "Out of scope",
)
PR_HEADINGS = (
    "Linked work",
    "Outcome and scope",
    "Design, state and operations",
    "Verification",
    "Limitations and review",
)
ADR_NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
ADR_HEADING = re.compile(r"^# ADR[- ](\d{4}):\s+\S")
METADATA = re.compile(r"^- ([A-Za-z][A-Za-z ]+):\s*(.*)$", re.MULTILINE)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def check_documentation(root: Path) -> list[str]:
    """Check links and required summaries in selectively read documents."""
    errors = validate_links(root)
    for path in markdown_files(root):
        relative = _relative(root, path)
        if relative.startswith(".github/"):
            continue
        visible = "\n".join(visible_lines(path.read_text(encoding="utf-8")))
        words = len(re.findall(r"\b\w+\b", visible))
        sections = len(re.findall(r"^##\s+", visible, re.MULTILINE))
        selective = relative.startswith("docs/") and any(
            part in relative.lower()
            for part in (
                "architecture",
                "migration",
                "security",
                "harness",
                "local-kind",
                "local-argocd",
            )
        )
        if words >= 800 or sections > 5 or selective:
            first_section = re.search(r"^##\s+(.+?)\s*$", visible, re.MULTILINE)
            if first_section is None or first_section.group(1) != "TL;DR":
                errors.append(f"{relative}: first section of a long guide must be ## TL;DR")
    return errors


def check_templates(root: Path) -> list[str]:
    """Keep issue and PR prompts aligned with CONTRIBUTING's canonical form."""
    errors: list[str] = []
    for name, expected in (
        (".github/ISSUE_TEMPLATE/work-item.md", ISSUE_HEADINGS),
        (".github/PULL_REQUEST_TEMPLATE.md", PR_HEADINGS),
    ):
        path = root / name
        if not path.is_file():
            errors.append(f"{name}: required template is missing")
            continue
        text = path.read_text(encoding="utf-8")
        actual = tuple(re.findall(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
        if actual != expected:
            errors.append(f"{name}: section order differs from CONTRIBUTING.md")
        if "Milestone:" not in text:
            errors.append(f"{name}: milestone prompt is missing")
        if (
            name.startswith(".github/ISSUE_TEMPLATE/")
            and "**Repository:** k8s-infrastructure" not in text
        ):
            errors.append(f"{name}: repository identity is missing")
        if name == ".github/PULL_REQUEST_TEMPLATE.md":
            if not text.startswith("Closes #<issue-number>\n"):
                errors.append(f"{name}: first line must be Closes #<issue-number>")
            if "[#<issue-number>]" not in text:
                errors.append(f"{name}: issue-prefixed PR title prompt is missing")
    return errors


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def check_pr_event(event: Mapping[str, object]) -> list[str]:
    """Validate human PR metadata without using the GitHub API."""
    request = _mapping(event.get("pull_request"))
    if not request:
        return ["pull_request: event payload is missing pull_request"]
    author = _mapping(request.get("user")).get("login")
    if author == "dependabot[bot]":
        return []

    title = request.get("title")
    body = request.get("body")
    branch = _mapping(request.get("head")).get("ref")
    title_match = (
        re.fullmatch(r"\[#([1-9][0-9]*)\] +\S.*", title) if isinstance(title, str) else None
    )
    first_line = body.splitlines()[0] if isinstance(body, str) and body else ""
    body_match = re.fullmatch(r"Closes #([1-9][0-9]*)", first_line)
    branch_match = (
        re.fullmatch(r"(?:feature|fix)/issue-([1-9][0-9]*)", branch)
        if isinstance(branch, str)
        else None
    )

    errors: list[str] = []
    if title_match is None:
        errors.append("pull_request.title: start with [#N] and a concise summary")
    if body_match is None:
        errors.append("pull_request.body: first line must be Closes #N")
    if branch_match is None:
        errors.append("pull_request.head.ref: use feature/issue-N or fix/issue-N")
    numbers = [
        match.group(1) for match in (title_match, body_match, branch_match) if match is not None
    ]
    if len(set(numbers)) > 1:
        errors.append("pull_request: title, body, and branch must reference one issue")
    return errors


def check_adrs(root: Path) -> list[str]:
    """Check ADR numbering, metadata, and index links without rewriting history."""
    directory = root / "docs/decisions"
    index = directory / "README.md"
    if not index.is_file():
        return ["docs/decisions/README.md: ADR index is missing"]
    indexed = index.read_text(encoding="utf-8")
    errors: list[str] = []
    numbers: list[int] = []
    for path in sorted(directory.glob("*.md")):
        if path == index:
            continue
        relative = _relative(root, path)
        match = ADR_NAME.fullmatch(path.name)
        if match is None:
            errors.append(f"{relative}: ADR filename must start with four digits")
            continue
        number = int(match.group(1))
        numbers.append(number)
        text = path.read_text(encoding="utf-8")
        heading = ADR_HEADING.match(text)
        if heading is None or int(heading.group(1)) != number:
            errors.append(f"{relative}: ADR heading must match its filename number")
        metadata = dict(METADATA.findall(text))
        for required in ("Status", "Date"):
            if not metadata.get(required):
                errors.append(f"{relative}: {required} metadata is missing")
        milestone = metadata.get("Milestone")
        if milestone and not re.fullmatch(r"M\d{2} — .+", milestone):
            errors.append(f"{relative}: milestone must use MNN — Outcome")
        if f"({path.name})" not in indexed:
            errors.append(f"docs/decisions/README.md: {path.name} is not indexed")
    if numbers and numbers != list(range(1, max(numbers) + 1)):
        errors.append("docs/decisions/README.md: ADR numbers must be consecutive from 0001")
    return errors


def check_repository(root: Path) -> list[str]:
    """Return deterministic, actionable diagnostics for repository policy drift."""
    root = root.resolve()
    errors: list[str] = []
    for required in (
        "uv.lock",
        "docs/README.md",
        "docs/development/harness.md",
        "docs/development/dependency-policy.md",
    ):
        if not (root / required).is_file():
            errors.append(f"{required}: required repository guidance or lock is missing")
    errors.extend(check_documentation(root))
    errors.extend(check_templates(root))
    errors.extend(check_adrs(root))
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pr-event",
        action="store_true",
        help="also validate the pull request payload at GITHUB_EVENT_PATH",
    )
    arguments = parser.parse_args(argv)
    errors = check_repository(ROOT)
    if arguments.pr_event:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not event_path:
            errors.append("GITHUB_EVENT_PATH: missing pull-request event path")
        else:
            try:
                event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                errors.append(f"GITHUB_EVENT_PATH: cannot read pull-request event: {error}")
            else:
                if isinstance(event, dict):
                    errors.extend(check_pr_event(event))
                else:
                    errors.append("GITHUB_EVENT_PATH: event must be a JSON object")
    for error in errors:
        print(error)
    if errors:
        print(f"repository-policy: found {len(errors)} violation(s)")
        return 1
    print("repository-policy: repository policy checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
