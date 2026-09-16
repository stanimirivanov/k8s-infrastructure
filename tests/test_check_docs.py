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
                "[guide](docs/guide.md#usage) [site](https://example.com) [section](#local)\n",
                encoding="utf-8",
            )
            (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")

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
