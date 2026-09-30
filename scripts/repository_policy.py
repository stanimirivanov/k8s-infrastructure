"""Check local engineering policy that CI and contributors must agree on."""

from __future__ import annotations

import re
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
ACTION = re.compile(r"^\s*(?:-\s*)?uses:\s*(.*?)\s*$")
BLOCK_SCALAR = re.compile(r"^\s*(?:-\s*)?[^:#]+:\s*[|>][+-]?\d*\s*(?:#.*)?$")
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


def action_references(text: str) -> list[tuple[int, str]]:
    """Read YAML uses lines, ignoring examples inside block scalar bodies."""
    references: list[tuple[int, str]] = []
    block_indent: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if block_indent is not None:
            if not stripped or indent > block_indent:
                continue
            block_indent = None
        if BLOCK_SCALAR.match(line):
            block_indent = indent
            continue
        match = ACTION.match(line)
        if match is None:
            continue
        raw = match.group(1).strip()
        if raw.startswith(("'", '"')):
            quote = raw[0]
            end = raw.find(quote, 1)
            reference = raw[1:end] if end > 0 else raw
        else:
            reference = raw.split("#", 1)[0].strip()
        references.append((number, reference))
    return references


def check_actions(root: Path) -> list[str]:
    """Require semantic-major remote action references in workflows and composites."""
    paths = [*root.glob(".github/workflows/*.yml"), *root.glob(".github/workflows/*.yaml")]
    paths += [
        *root.glob(".github/actions/**/action.yml"),
        *root.glob(".github/actions/**/action.yaml"),
    ]
    errors: list[str] = []
    for path in sorted(paths):
        for line, reference in action_references(path.read_text(encoding="utf-8")):
            if reference.startswith(("./", "docker://")):
                continue
            if re.fullmatch(r"[^\s@]+@v\d+", reference):
                continue
            errors.append(
                f"{_relative(root, path)}:{line}: use a semantic-major action reference "
                f"(owner/action@vN): {reference!r}"
            )
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
    errors.extend(check_actions(root))
    return sorted(set(errors))


def main() -> int:
    errors = check_repository(ROOT)
    for error in errors:
        print(error)
    if errors:
        print(f"repository-policy: found {len(errors)} violation(s)")
        return 1
    print("repository-policy: repository policy checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
