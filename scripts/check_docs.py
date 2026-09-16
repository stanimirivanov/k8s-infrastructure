"""Validate that local Markdown links resolve inside the repository."""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_SCHEMES = {"http", "https", "mailto"}
IGNORED_DIRECTORIES = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv"}


def markdown_files(root: Path) -> list[Path]:
    """Return repository Markdown files while excluding generated/local trees."""
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
    )


def validate_links(root: Path) -> list[str]:
    """Return diagnostics for missing, escaping, or unsupported local links."""
    repository = root.resolve()
    errors: list[str] = []

    for document in markdown_files(repository):
        text = document.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            raw_target = match.group(1).strip().strip("<>")
            target = raw_target.split(maxsplit=1)[0]
            if not target or target.startswith("#"):
                continue

            parsed = urlsplit(target)
            if parsed.scheme.lower() in EXTERNAL_SCHEMES:
                continue
            if parsed.scheme or target.startswith("/") or PureWindowsPath(target).is_absolute():
                errors.append(
                    f"{document.relative_to(repository)}: unsupported link {raw_target!r}"
                )
                continue

            relative_target = unquote(parsed.path)
            if not relative_target:
                continue

            resolved = (document.parent / relative_target).resolve()
            try:
                resolved.relative_to(repository)
            except ValueError:
                errors.append(
                    f"{document.relative_to(repository)}: link escapes repository {raw_target!r}"
                )
                continue

            if not resolved.exists():
                errors.append(
                    f"{document.relative_to(repository)}: missing link target {raw_target!r}"
                )

    return errors


def main() -> int:
    """Validate documentation links for the repository containing this module."""
    root = Path(__file__).resolve().parents[1]
    errors = validate_links(root)
    if errors:
        for error in errors:
            print(error)
        return 1

    print("Validated local Markdown links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
