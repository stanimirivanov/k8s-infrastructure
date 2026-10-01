from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.check_docs import validate_links


class DocumentationLinkTests(TestCase):
    def test_accepts_existing_local_anchor_and_external_links(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            docs = root / "docs"
            docs.mkdir()
            (root / "README.md").write_text(
                "# Local\n[guide](docs/guide.md#usage) [site](https://example.com) "
                "[section](#local)\n",
                encoding="utf-8",
            )
            (docs / "guide.md").write_text("# Guide\n## Usage\n", encoding="utf-8")

            self.assertEqual(validate_links(root), [])

    def test_reports_missing_local_target(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("[missing](docs/missing.md)\n", encoding="utf-8")

            self.assertEqual(
                validate_links(root),
                ["README.md: missing link target 'docs/missing.md'"],
            )

    def test_rejects_link_escaping_repository(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("[outside](../outside.md)\n", encoding="utf-8")

            self.assertEqual(
                validate_links(root),
                ["README.md: link escapes repository '../outside.md'"],
            )

    def test_reference_link_checks_heading_and_ignores_fenced_examples(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "# Guide\n[details][target]\n\n[target]: <README.md#missing>\n"
                "```md\n[example](missing.md)\n```\n",
                encoding="utf-8",
            )

            self.assertEqual(
                validate_links(root),
                ["README.md: missing heading anchor 'README.md#missing'"],
            )

    def test_duplicate_heading_anchor_is_valid(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "# Guide\n## Repeat\n## Repeat\n[second](#repeat-1)\n",
                encoding="utf-8",
            )

            self.assertEqual(validate_links(root), [])
