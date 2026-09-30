"""Validate local Markdown paths and heading anchors without network access."""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

LINK = re.compile(r"(?<!!)\[[^\]]+\]\((<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
IGNORED_DIRECTORIES = {
    ".git",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
}
EXTERNAL_SCHEMES = {"http", "https", "mailto"}


def markdown_files(root: Path) -> list[Path]:
    """Return repository Markdown files while excluding local/generated trees."""
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
    )


def visible_lines(text: str) -> list[str]:
    """Blank fenced code while preserving line positions for diagnostics."""
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = line.lstrip()[:3]
        if marker in {"```", "~~~"}:
            fence = None if fence == marker else marker
            lines.append("")
        else:
            lines.append("" if fence else line)
    return lines


def heading_anchors(text: str) -> set[str]:
    """Return GitHub-style anchors, including suffixes for duplicate headings."""
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for line in visible_lines(text):
        match = HEADING.match(line)
        if not match:
            continue
        name = re.sub(r"<[^>]+>", "", match.group(1)).strip().lower()
        name = re.sub(r"[^\w\- ]", "", name)
        name = re.sub(r"[\s-]+", "-", name).strip("-")
        count = counts.get(name, 0)
        counts[name] = count + 1
        anchors.add(name if count == 0 else f"{name}-{count}")
    return anchors


def _targets(text: str) -> list[str]:
    visible = "\n".join(visible_lines(text))
    matches = [*LINK.finditer(visible), *REFERENCE.finditer(visible)]
    return [match.group(1).strip("<>") for match in sorted(matches, key=lambda item: item.start())]


def validate_links(root: Path) -> list[str]:
    """Return diagnostics for broken, escaping, or unsupported Markdown links."""
    repository = root.resolve()
    errors: list[str] = []
    for document in markdown_files(repository):
        for target in _targets(document.read_text(encoding="utf-8")):
            parsed = urlsplit(target)
            if parsed.scheme.lower() in EXTERNAL_SCHEMES:
                continue
            relative = document.relative_to(repository)
            if (
                parsed.scheme
                or target.startswith(("/", "\\"))
                or PureWindowsPath(target).is_absolute()
            ):
                errors.append(f"{relative}: unsupported link {target!r}")
                continue
            path = unquote(parsed.path)
            resolved = (document.parent / path).resolve() if path else document
            try:
                resolved.relative_to(repository)
            except ValueError:
                errors.append(f"{relative}: link escapes repository {target!r}")
                continue
            if not resolved.is_file():
                errors.append(f"{relative}: missing link target {target!r}")
                continue
            fragment = unquote(parsed.fragment)
            if fragment and fragment not in heading_anchors(resolved.read_text(encoding="utf-8")):
                errors.append(f"{relative}: missing heading anchor {target!r}")
    return errors


def main() -> int:
    """Validate documentation links for the repository containing this module."""
    errors = validate_links(Path(__file__).resolve().parents[1])
    for error in errors:
        print(error)
    if errors:
        return 1
    print("Validated local Markdown links and anchors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
